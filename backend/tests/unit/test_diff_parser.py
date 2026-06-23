"""Tests for app/review/diff_parser.py.

All tests run against real synthetic git repos cloned into a tmp-path
CODEREVIEW_REPOS_DIR — no network, no real OpenForge/GitHub. The
`diff_repo` fixture provides a repo with every change type (added,
modified, deleted, renamed) so we can assert each cleanly.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

import pytest

from app.review import (
    CommitInfo,
    DiffHunk,
    commits_between,
    diff_between,
)
from app.storage import clone_manager


def _file_url(p: Path) -> str:
    return f"file://{p}"


# ── diff_between ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_diff_between_empty_when_same_sha(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """No diff between a SHA and itself → empty list."""
    repo, before, _after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    hunks = await diff_between(str(pid), before, before)
    assert hunks == []


@pytest.mark.asyncio
async def test_diff_between_finds_every_change_type(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """The fixture's two commits include add, modify, delete, rename."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    hunks = await diff_between(str(pid), before, after)

    by_path: dict[str, list[DiffHunk]] = {}
    for h in hunks:
        by_path.setdefault(h.file_path, []).append(h)

    # Every changed file is represented
    assert "added.py" in by_path
    assert "to_modify.py" in by_path
    assert "to_delete.py" in by_path
    assert "renamed.py" in by_path

    # change_type detection
    assert all(h.change_type == "added" for h in by_path["added.py"])
    assert all(h.change_type == "modified" for h in by_path["to_modify.py"])
    assert all(h.change_type == "deleted" for h in by_path["to_delete.py"])
    assert all(h.change_type == "renamed" for h in by_path["renamed.py"])


@pytest.mark.asyncio
async def test_diff_between_added_file_carries_added_lines(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """An added file's hunk has added_lines populated, removed_lines empty."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    hunks = await diff_between(str(pid), before, after)
    added = next(h for h in hunks if h.file_path == "added.py")

    assert added.old_file_path is None
    assert added.added_lines == ("def added():", "    return 'new'")
    assert added.removed_lines == ()


@pytest.mark.asyncio
async def test_diff_between_deleted_file_carries_removed_lines(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """A deleted file's hunk has removed_lines populated, added_lines empty."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    hunks = await diff_between(str(pid), before, after)
    deleted = next(h for h in hunks if h.file_path == "to_delete.py")

    assert deleted.added_lines == ()
    assert deleted.removed_lines == (
        "# soon-to-be-removed module",
        "DOOMED = True",
    )


@pytest.mark.asyncio
async def test_diff_between_modified_file_has_both_added_and_removed(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """A modified file mixes additions + removals in the same hunk."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    hunks = await diff_between(str(pid), before, after)
    modified = next(h for h in hunks if h.file_path == "to_modify.py")

    # added_lines and removed_lines are both non-empty (both old and new bodies)
    assert any("new_name" in line for line in modified.added_lines)
    assert any("old_name" in line for line in modified.removed_lines)


@pytest.mark.asyncio
async def test_diff_between_renamed_file_tracks_old_path(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """A rename keeps the previous path accessible via old_file_path."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    hunks = await diff_between(str(pid), before, after)
    renamed = next(h for h in hunks if h.file_path == "renamed.py")

    assert renamed.change_type == "renamed"
    assert renamed.old_file_path == "to_rename.py"


@pytest.mark.asyncio
async def test_diff_between_pure_rename_emits_marker_hunk(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """A 100%-similarity rename produces zero @@ hunks from git, but
    diff_parser still emits one marker DiffHunk so the file isn't lost."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    hunks = await diff_between(str(pid), before, after)
    rename_hunks = [h for h in hunks if h.file_path == "renamed.py"]

    assert len(rename_hunks) == 1, "expected exactly one marker hunk for pure rename"
    marker = rename_hunks[0]
    assert marker.change_type == "renamed"
    assert marker.old_file_path == "to_rename.py"
    assert marker.added_lines == ()
    assert marker.removed_lines == ()
    assert marker.new_count == 0
    assert marker.old_count == 0
    # The raw text should carry the rename-from/rename-to metadata
    assert "rename from to_rename.py" in marker.raw
    assert "rename to renamed.py" in marker.raw


@pytest.mark.asyncio
async def test_diff_between_hunk_line_ranges_are_one_indexed(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """Line ranges should match git's 1-indexed `@@ -a,b +c,d @@` semantics."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    hunks = await diff_between(str(pid), before, after)
    # `added.py` is a brand new file with 2 lines → new_start=1, new_count=2
    added = next(h for h in hunks if h.file_path == "added.py")
    assert added.new_start == 1
    assert added.new_count == 2
    assert added.new_end == 2


@pytest.mark.asyncio
async def test_diff_between_raw_contains_diff_markers(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """raw text preserves +/- markers so it's directly feedable to Claude."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    hunks = await diff_between(str(pid), before, after)
    added = next(h for h in hunks if h.file_path == "added.py")

    assert "+def added():" in added.raw
    assert "+    return 'new'" in added.raw


@pytest.mark.asyncio
async def test_diff_between_raises_on_unknown_sha(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """Unknown SHAs surface as CloneError, not a silent empty result."""
    repo, before, _after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    with pytest.raises(clone_manager.CloneError, match="git diff"):
        await diff_between(str(pid), before, "0000000000000000000000000000000000000000")


# ── commits_between ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_commits_between_returns_one_commit(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """`before..after` covers exactly one commit in the fixture."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    commits = await commits_between(str(pid), before, after)

    assert len(commits) == 1
    c = commits[0]
    assert isinstance(c, CommitInfo)
    assert c.sha == after
    assert c.parent_sha == before
    assert c.author_name == "Alice Reviewer"
    assert c.author_email == "alice@example.invalid"
    assert c.committer_name == "Alice Reviewer"
    assert c.subject == "diverse changes"


@pytest.mark.asyncio
async def test_commits_between_empty_for_same_sha(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    repo, before, _after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    assert await commits_between(str(pid), before, before) == []


@pytest.mark.asyncio
async def test_commits_between_carries_utc_timestamp(
    temp_repos_dir: Path, diff_repo: tuple[Path, str, str]
):
    """committed_at should be a timezone-aware UTC datetime, not naive."""
    repo, before, after = diff_repo
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(repo))

    commits = await commits_between(str(pid), before, after)
    assert commits[0].committed_at.tzinfo is datetime.timezone.utc
