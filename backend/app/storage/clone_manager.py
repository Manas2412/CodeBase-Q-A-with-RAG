"""Persistent clone manager for the review agent.

Each project gets a **bare** clone under CODEREVIEW_REPOS_DIR/<project_id>/.
Bare = no working tree, just `.git`-equivalent storage. Smaller on disk
and enough for everything Variant A polling needs:
    git fetch              → incremental object download
    git rev-parse           → branch HEAD lookups
    git log <a>..<b>        → author/committer attribution
    git diff <a>..<b>       → diff parsing for the reviewer
    git merge-base          → force-push detection
    git merge-tree          → pre-merge conflict prediction (Phase 1.5)

Defaults:
    CODEREVIEW_REPOS_DIR = $HOME/.codereview/repos     (native dev)
    CODEREVIEW_REPOS_DIR = /var/lib/codereview/repos   (container)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path


class CloneError(Exception):
    """git clone / fetch / rev-parse failed."""


# ── Where clones live ──────────────────────────────────────────────────
def repos_base_dir() -> Path:
    """The directory under which per-project bare clones are stored.

    Honours CODEREVIEW_REPOS_DIR env var. Defaults to ~/.codereview/repos
    so native dev runs don't need root.
    """
    return Path(
        os.getenv(
            "CODEREVIEW_REPOS_DIR",
            str(Path.home() / ".codereview" / "repos"),
        )
    )


def get_clone_path(project_id: str | uuid.UUID) -> Path:
    """Return the on-disk path for a given project's clone."""
    return repos_base_dir() / str(project_id)


# ── git operations ─────────────────────────────────────────────────────
async def _run_git(*args: str, cwd: Path | None = None) -> str:
    """Run a git subcommand and return stdout; raise CloneError on failure.

    Sensitive tokens may appear in args (e.g. clone URLs); we don't echo
    failures back through this helper without redacting upstream.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise CloneError(stderr.decode(errors="replace").strip() or "git failed")
    return stdout.decode(errors="replace")


async def ensure_cloned(
    project_id: str | uuid.UUID,
    clone_url: str,
    *,
    branch: str | None = None,
) -> Path:
    """Initial clone if missing; no-op if already present.

    Uses `git clone --mirror` so the resulting clone:
      • is bare (no working tree)
      • has `remote.origin.fetch = +refs/*:refs/*` configured, which means
        subsequent `git fetch` calls actually update local refs.

    NOTE: `--bare` alone does NOT set up a fetch refspec — incremental
    fetches would silently no-op. `--mirror` is the right primitive for a
    polling agent that needs to track every ref on the source.

    Parameters
    ----------
    project_id : the row's UUID. The clone dir is named after this.
    clone_url  : the auth-injected URL from the provider.
    branch     : currently no-op (--mirror tracks all refs). Kept for forward
                 compat — if a future single-branch mode is added it'll use
                 --bare + manual refspec setup.

    Returns
    -------
    Path of the on-disk clone.
    """
    path = get_clone_path(project_id)
    if (path / "HEAD").exists():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    await _run_git("clone", "--mirror", clone_url, str(path))
    return path


async def fetch(project_id: str | uuid.UUID) -> None:
    """Pull down any new objects from origin. Cheap incremental update."""
    path = get_clone_path(project_id)
    if not (path / "HEAD").exists():
        raise CloneError(f"No clone at {path}; call ensure_cloned() first.")
    await _run_git("fetch", "--all", "--prune", "--quiet", cwd=path)


async def branch_head(project_id: str | uuid.UUID, branch: str) -> str:
    """Return the SHA at the tip of `branch` (uses the origin/ remote ref).

    Always reads from the remote-tracking ref so we see the latest fetched
    state, not the local branch ref which may lag in bare clones.
    """
    path = get_clone_path(project_id)
    out = await _run_git("rev-parse", f"refs/heads/{branch}", cwd=path)
    return out.strip()


async def is_ancestor(
    project_id: str | uuid.UUID, old_sha: str, new_sha: str
) -> bool:
    """True if `old_sha` is reachable from `new_sha`. False means force-push.

    Wraps `git merge-base --is-ancestor`, which exits 0 (ancestor), 1
    (not ancestor), or non-zero-non-1 (an actual error).
    """
    path = get_clone_path(project_id)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(path),
        "merge-base",
        "--is-ancestor",
        old_sha,
        new_sha,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr = (await proc.communicate())[1]
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise CloneError(
        f"git merge-base --is-ancestor failed: {stderr.decode(errors='replace')}"
    )


def cleanup(project_id: str | uuid.UUID) -> None:
    """Remove the on-disk clone. Used when a project is deleted."""
    path = get_clone_path(project_id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
