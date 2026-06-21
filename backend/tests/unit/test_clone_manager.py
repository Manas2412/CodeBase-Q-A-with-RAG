"""Tests for app/storage/clone_manager.py.

All tests run against a synthetic git repo on disk (via file://) — no
network, no real OpenForge/GitHub, no cleanup state leaks (each test
gets a fresh tmp_path-backed CODEREVIEW_REPOS_DIR).
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from app.storage import clone_manager


def _file_url(path: Path) -> str:
    return f"file://{path}"


# ── repos_base_dir + get_clone_path ─────────────────────────────────────
def test_repos_base_dir_respects_env(temp_repos_dir: Path):
    assert clone_manager.repos_base_dir() == temp_repos_dir


def test_get_clone_path_uses_project_id_as_subdir(temp_repos_dir: Path):
    pid = uuid.uuid4()
    path = clone_manager.get_clone_path(pid)
    assert path == temp_repos_dir / str(pid)


# ── ensure_cloned ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ensure_cloned_creates_bare_clone(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    pid = uuid.uuid4()
    path = await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))

    assert path == clone_manager.get_clone_path(pid)
    # Bare clones have HEAD + refs/ at the top level, no working tree
    assert (path / "HEAD").exists()
    assert (path / "refs" / "heads").is_dir()
    # No working tree files should appear
    assert not (path / "README.md").exists()


@pytest.mark.asyncio
async def test_ensure_cloned_is_idempotent(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    pid = uuid.uuid4()
    p1 = await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))
    p2 = await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))
    assert p1 == p2


@pytest.mark.asyncio
async def test_ensure_cloned_with_branch_kwarg_is_accepted(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    """`branch=` kwarg is accepted (forward-compat).

    Today `--mirror` tracks all branches regardless — we still verify the
    parameter doesn't raise so a future single-branch implementation can
    flip the behaviour without API churn.
    """
    pid = uuid.uuid4()
    path = await clone_manager.ensure_cloned(
        pid, _file_url(multi_branch_repo), branch="main"
    )
    assert (path / "HEAD").exists()
    # With --mirror, every branch is fetched
    out = subprocess.run(
        ["git", "branch", "--list"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "main" in out
    assert "dev" in out
    assert "uat" in out


@pytest.mark.asyncio
async def test_ensure_cloned_configures_fetch_refspec(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    """`--mirror` sets `remote.origin.fetch = +refs/*:refs/*`.

    Without that refspec, subsequent `git fetch` silently no-ops on bare
    clones — the bug we hit in our first pass at the clone manager.
    """
    pid = uuid.uuid4()
    path = await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))
    refspec = subprocess.run(
        ["git", "config", "--get", "remote.origin.fetch"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert refspec == "+refs/*:refs/*"


@pytest.mark.asyncio
async def test_ensure_cloned_raises_on_bad_url(temp_repos_dir: Path):
    pid = uuid.uuid4()
    with pytest.raises(clone_manager.CloneError):
        await clone_manager.ensure_cloned(pid, "file:///nope-not-here")


# ── fetch ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fetch_picks_up_new_commits(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))

    initial_head = await clone_manager.branch_head(pid, "main")

    # Make a new commit in the source repo on main
    new_file = multi_branch_repo / "new-feature.txt"
    new_file.write_text("post-clone change\n")
    subprocess.run(["git", "add", "."], cwd=multi_branch_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "post-clone commit"],
        cwd=multi_branch_repo,
        check=True,
    )

    await clone_manager.fetch(pid)
    new_head = await clone_manager.branch_head(pid, "main")
    assert new_head != initial_head


@pytest.mark.asyncio
async def test_fetch_raises_when_clone_missing(temp_repos_dir: Path):
    pid = uuid.uuid4()
    with pytest.raises(clone_manager.CloneError, match="No clone"):
        await clone_manager.fetch(pid)


# ── branch_head ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_branch_head_returns_full_sha(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))

    sha = await clone_manager.branch_head(pid, "main")
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


@pytest.mark.asyncio
async def test_branch_head_each_branch_distinct(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    """main, dev, uat each have their own tip SHA in the fixture."""
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))

    shas = {
        b: await clone_manager.branch_head(pid, b)
        for b in ("main", "dev", "uat")
    }
    assert len(set(shas.values())) == 3, f"expected 3 distinct SHAs, got {shas}"


# ── is_ancestor (force-push detection) ─────────────────────────────────
@pytest.mark.asyncio
async def test_is_ancestor_true_for_linear_history(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))

    main_head = await clone_manager.branch_head(pid, "main")
    # main is the ancestor of dev (dev branched off main and added commits)
    dev_head = await clone_manager.branch_head(pid, "dev")

    assert await clone_manager.is_ancestor(pid, main_head, dev_head) is True


@pytest.mark.asyncio
async def test_is_ancestor_false_for_force_push_shape(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    """Same SHA-graph shape a force-push produces: previous tip has
    commits that aren't reachable from the new tip.

    In our fixture: dev_head has commits beyond main_head (dev branched
    off main and added one). So dev_head is NOT an ancestor of main_head.
    This is the assertion the polling agent uses to detect force-pushes —
    when the prior reviewed tip can't be reached from the current tip,
    something rewrote history.
    """
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))

    main_head = await clone_manager.branch_head(pid, "main")
    dev_head = await clone_manager.branch_head(pid, "dev")

    assert await clone_manager.is_ancestor(pid, dev_head, main_head) is False


# ── cleanup ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cleanup_removes_clone(
    temp_repos_dir: Path, multi_branch_repo: Path
):
    pid = uuid.uuid4()
    await clone_manager.ensure_cloned(pid, _file_url(multi_branch_repo))
    assert clone_manager.get_clone_path(pid).exists()

    clone_manager.cleanup(pid)
    assert not clone_manager.get_clone_path(pid).exists()


def test_cleanup_is_safe_when_clone_missing(temp_repos_dir: Path):
    """Should silently no-op, not raise."""
    pid = uuid.uuid4()
    clone_manager.cleanup(pid)  # no clone exists; just returns
