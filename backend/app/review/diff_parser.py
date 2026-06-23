"""Diff parsing for the review pipeline.

Two public helpers, both async, both operating on the project's persistent
clone managed by app/storage/clone_manager:

  • diff_between(project_id, before, after) -> list[DiffHunk]
        Runs `git diff <before>..<after> --unified=N` and parses via
        `unidiff` into structured DiffHunk objects. Each hunk carries
        file/line range info, the added/removed lines, and the raw
        unified-diff text (the latter is what we feed to Claude).

  • commits_between(project_id, before, after) -> list[CommitInfo]
        Runs `git log` with a tab-separated custom format and returns
        one CommitInfo per commit in the range. Maps directly to the
        `commits` table for review-row attribution.

Used by:
  • the context_builder (Day 4) to know which files + line ranges to
    pull related chunks for
  • the reviewer (Day 5) to feed Claude the actual diff text
  • the polling agent (Week 3) to populate `commits` rows when a new
    push is detected
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from typing import Sequence

from unidiff import PatchSet

from app.storage import clone_manager
from app.storage.clone_manager import CloneError, get_clone_path


#: How many lines of context surround each hunk in the diff we send to
#: Claude. 10 gives the model enough surrounding code to ground a
#: review without blowing the prompt budget.
DIFF_CONTEXT_LINES: int = 10


@dataclass(frozen=True)
class DiffHunk:
    """One contiguous changed region in a single file.

    Line ranges are 1-indexed (matching `git diff` output and most
    editor conventions). The `raw` field is the original unified-diff
    text including the `@@ ... @@` header — that's what we hand to
    Claude so the model has both structure and the leading context.
    """

    file_path: str            # new path; same as old when modified
    old_file_path: str | None  # None if added; differs from file_path when renamed
    change_type: str          # 'added' | 'modified' | 'deleted' | 'renamed'

    new_start: int
    new_count: int
    old_start: int
    old_count: int

    added_lines: tuple[str, ...]
    removed_lines: tuple[str, ...]

    raw: str

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count - 1, 0)

    @property
    def old_end(self) -> int:
        return self.old_start + max(self.old_count - 1, 0)


@dataclass(frozen=True)
class CommitInfo:
    """One commit in the (before, after] range.

    Field names align with the `commits` table so the polling agent can
    write rows directly: project_id is supplied by the caller, sha through
    subject come from here.
    """

    sha: str
    parent_sha: str | None
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    committed_at: datetime.datetime
    subject: str


# ── Internal helper ──────────────────────────────────────────────────────
async def _run_git(*args: str, cwd) -> str:
    """Run `git -C <cwd> <args>...` and return stdout. Raises CloneError on non-zero."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise CloneError(
            f"git {' '.join(args)} failed: {stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode(errors="replace")


def _strip_prefix(path: str, prefix: str) -> str:
    """Strip an `a/` or `b/` prefix that git uses in diff headers."""
    return path[len(prefix):] if path.startswith(prefix) else path


def _change_type(patched_file) -> str:
    """Classify a unidiff PatchedFile as added/modified/deleted/renamed."""
    if patched_file.is_added_file:
        return "added"
    if patched_file.is_removed_file:
        return "deleted"
    if patched_file.is_rename:
        return "renamed"
    return "modified"


# ── Public API ───────────────────────────────────────────────────────────
async def diff_between(
    project_id: str,
    before: str,
    after: str,
    *,
    context_lines: int = DIFF_CONTEXT_LINES,
) -> list[DiffHunk]:
    """Return the parsed hunks for `before..after`.

    Empty list when the SHAs are identical or no files changed.
    Passes `-M` so renames are detected as renames (not delete+add).
    """
    clone_path = get_clone_path(project_id)

    diff_text = await _run_git(
        "diff",
        f"--unified={context_lines}",
        "-M",
        "--no-color",
        f"{before}..{after}",
        cwd=clone_path,
    )
    if not diff_text.strip():
        return []

    patch = PatchSet(diff_text)
    hunks: list[DiffHunk] = []

    for patched_file in patch:
        change = _change_type(patched_file)

        # unidiff exposes the raw `a/foo` / `b/foo` headers as
        # source_file / target_file. .path returns the cleaned new path
        # (target with `b/` stripped, falling back to source for deletes).
        new_path = patched_file.path
        old_path = _strip_prefix(patched_file.source_file or "", "a/")
        if change == "added":
            old_file_path: str | None = None
        elif change == "deleted":
            old_file_path = old_path or new_path
        elif change == "renamed":
            old_file_path = old_path
        else:
            old_file_path = old_path or new_path

        emitted_any_hunk = False
        for h in patched_file:
            added = tuple(
                line.value.rstrip("\n") for line in h if line.is_added
            )
            removed = tuple(
                line.value.rstrip("\n") for line in h if line.is_removed
            )
            hunks.append(
                DiffHunk(
                    file_path=new_path,
                    old_file_path=old_file_path,
                    change_type=change,
                    new_start=h.target_start,
                    new_count=h.target_length,
                    old_start=h.source_start,
                    old_count=h.source_length,
                    added_lines=added,
                    removed_lines=removed,
                    raw=str(h),
                )
            )
            emitted_any_hunk = True

        # Pure rename (100% similarity) → git emits 'rename from X / rename to Y'
        # headers but ZERO @@ hunks. The PatchedFile iteration above sees nothing
        # and we'd otherwise lose the file entirely. Emit a marker hunk so the
        # reviewer still has a record of the change (and can comment on the
        # new name if it's inconsistent with conventions).
        if not emitted_any_hunk and change == "renamed":
            hunks.append(
                DiffHunk(
                    file_path=new_path,
                    old_file_path=old_file_path,
                    change_type=change,
                    new_start=0,
                    new_count=0,
                    old_start=0,
                    old_count=0,
                    added_lines=(),
                    removed_lines=(),
                    # str(patched_file) preserves the rename-from/rename-to headers
                    # so the LLM sees the metadata change.
                    raw=str(patched_file),
                )
            )

    return hunks


async def commits_between(
    project_id: str,
    before: str,
    after: str,
) -> list[CommitInfo]:
    """Return every commit in (before..after], newest first.

    For merge commits, parent_sha is the FIRST parent (mainline) so the
    polling agent can show "this commit landed on dev from feature X"
    without dragging in the whole feature branch's history.
    """
    clone_path = get_clone_path(project_id)

    # %H = full SHA, %P = parent SHAs (space-separated), %an/%ae = author,
    # %cn/%ce = committer, %ct = committer Unix timestamp, %s = subject.
    # \x09 = literal TAB, used as a field separator that won't appear
    # in any of the fields above (subjects can't contain tabs).
    fmt = "%H%x09%P%x09%an%x09%ae%x09%cn%x09%ce%x09%ct%x09%s"

    raw = await _run_git(
        "log",
        f"--format={fmt}",
        f"{before}..{after}",
        cwd=clone_path,
    )

    commits: list[CommitInfo] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 8:
            # Defensive: malformed line, skip rather than crash on weird input
            continue
        sha, parents, an, ae, cn, ce, ts, subject = parts[:8]
        first_parent = parents.split(" ", 1)[0] if parents else ""
        commits.append(
            CommitInfo(
                sha=sha,
                parent_sha=first_parent or None,
                author_name=an,
                author_email=ae,
                committer_name=cn,
                committer_email=ce,
                committed_at=datetime.datetime.fromtimestamp(
                    int(ts), tz=datetime.timezone.utc
                ),
                subject=subject,
            )
        )
    return commits


# Re-export for callers
__all__ = [
    "CommitInfo",
    "DIFF_CONTEXT_LINES",
    "DiffHunk",
    "CloneError",
    "commits_between",
    "diff_between",
]
