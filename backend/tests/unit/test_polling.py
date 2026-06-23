"""Tests for the polling fan-out (Week 3 Day 1).

The Beat-scheduled task `poll_all_projects_task` is just an asyncio.run
wrapper; the real logic lives in `_poll_all_projects()`. We test that
async helper directly so we don't need Celery + Redis at test time.

Strategy:
  • Mock asyncpg.connect() so we never touch a real Postgres
  • Mock check_project_for_changes_task.delay() so we don't queue anything
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.scheduling import BEAT_SCHEDULE, DEFAULT_POLL_INTERVAL_SECONDS, POLL_TASK_NAME


# ── Beat schedule sanity ────────────────────────────────────────────────
def test_beat_schedule_targets_poll_task():
    assert "poll-all-projects" in BEAT_SCHEDULE
    entry = BEAT_SCHEDULE["poll-all-projects"]
    assert entry["task"] == POLL_TASK_NAME
    assert entry["schedule"] == DEFAULT_POLL_INTERVAL_SECONDS


def test_beat_schedule_has_expiry_to_prevent_backlog():
    """If Beat enqueues a poll and no worker picks it up before the next tick,
    the stale message should expire rather than pile up a backlog."""
    entry = BEAT_SCHEDULE["poll-all-projects"]
    expires = entry["options"]["expires"]
    # Expiry should be tighter than the schedule so stale ticks get dropped
    assert expires < entry["schedule"]
    assert expires >= 30  # never collapse below 30s


def test_default_poll_interval_is_5_minutes():
    """Plan v3.3 §4.2 locks the cadence."""
    assert DEFAULT_POLL_INTERVAL_SECONDS == 300


# ── _poll_all_projects fan-out logic ─────────────────────────────────────
class _FakeConn:
    """Quacks like asyncpg.Connection.fetch() + close()."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.closed = False
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        return [dict(r) for r in self._rows]

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_poll_dispatches_one_check_per_project(monkeypatch):
    """Each row returned by the SELECT becomes one .delay() call."""
    rows = [{"id": "p-1"}, {"id": "p-2"}, {"id": "p-3"}]
    fake_conn = _FakeConn(rows)

    # Patch _open_conn to return our fake (no real Postgres needed)
    import app.workers.tasks as t
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=fake_conn))

    # Patch the check task's .delay so we don't enqueue anything for real
    delay_mock = MagicMock(name="check.delay")
    monkeypatch.setattr(
        t.check_project_for_changes_task, "delay", delay_mock
    )

    count = await t._poll_all_projects()

    assert count == 3
    assert delay_mock.call_count == 3
    assert [c.args[0] for c in delay_mock.call_args_list] == ["p-1", "p-2", "p-3"]
    assert fake_conn.closed is True


@pytest.mark.asyncio
async def test_poll_no_projects_dispatches_nothing(monkeypatch):
    fake_conn = _FakeConn([])

    import app.workers.tasks as t
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=fake_conn))
    delay_mock = MagicMock(name="check.delay")
    monkeypatch.setattr(t.check_project_for_changes_task, "delay", delay_mock)

    count = await t._poll_all_projects()

    assert count == 0
    delay_mock.assert_not_called()


@pytest.mark.asyncio
async def test_poll_query_filters_to_ready_poll_mode_with_branches(monkeypatch):
    """The SQL must filter:
      • status = 'ready'        (don't poll projects that are mid-indexing)
      • trigger_mode in poll/both  (skip pure webhook projects)
      • jsonb_array_length(branches_to_review) > 0  (no branches = nothing to poll)
    """
    fake_conn = _FakeConn([])

    import app.workers.tasks as t
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=fake_conn))
    monkeypatch.setattr(
        t.check_project_for_changes_task,
        "delay",
        MagicMock(),
    )

    await t._poll_all_projects()

    assert len(fake_conn.fetch_calls) == 1
    query, _args = fake_conn.fetch_calls[0]
    assert "status = 'ready'" in query
    assert "trigger_mode" in query
    assert "'poll'" in query and "'both'" in query
    assert "jsonb_array_length(branches_to_review)" in query


@pytest.mark.asyncio
async def test_poll_closes_connection_even_on_query_failure(monkeypatch):
    """A query exception must not leak the asyncpg connection."""

    class _ExplodingConn(_FakeConn):
        async def fetch(self, *args, **kw):
            raise RuntimeError("postgres exploded")

    fake_conn = _ExplodingConn([])

    import app.workers.tasks as t
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=fake_conn))

    with pytest.raises(RuntimeError, match="postgres exploded"):
        await t._poll_all_projects()

    assert fake_conn.closed is True


# ── Per-project check logic (Week 3 Day 2) ───────────────────────────────
class _CheckConn:
    """Richer FakeConn — supports fetchrow, fetchval, execute, fetch.

    Configured per test by setting attributes on the instance.
    """

    def __init__(
        self,
        project_row: dict | None = None,
        in_flight_review: bool = False,
    ):
        self.project_row = project_row
        self.in_flight_review = in_flight_review
        self.closed = False
        self.executes: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        return dict(self.project_row) if self.project_row else None

    async def fetchval(self, query: str, *args):
        self.fetchval_calls.append((query, args))
        return 1 if self.in_flight_review else None

    async def execute(self, query: str, *args):
        self.executes.append((query, args))

    async def close(self):
        self.closed = True


def _patch_git(monkeypatch, *, branch_heads: dict, ancestor_map: dict | None = None):
    """Stub out the git helpers in app.workers.tasks. branch_heads maps
    branch name → fake SHA. ancestor_map maps (old_sha, new_sha) → bool."""
    import app.workers.tasks as t

    async def _fake_fetch(_pid):
        return None

    async def _fake_branch_head(_pid, branch):
        if branch not in branch_heads:
            raise t.CloneError(f"branch {branch} not found")
        return branch_heads[branch]

    async def _fake_is_ancestor(_pid, old, new):
        if ancestor_map is None:
            return True
        return ancestor_map.get((old, new), True)

    monkeypatch.setattr(t, "fetch", _fake_fetch)
    monkeypatch.setattr(t, "branch_head", _fake_branch_head)
    monkeypatch.setattr(t, "is_ancestor", _fake_is_ancestor)


@pytest.mark.asyncio
async def test_check_baselines_first_seen_branch_no_review(monkeypatch):
    """First time we see a branch → record SHA, don't enqueue a review."""
    import app.workers.tasks as t

    conn = _CheckConn(
        project_row={
            "repo_url": "https://github.com/octocat/Hello-World",
            "branches_to_review": ["main"],
            "last_reviewed_sha": {},  # empty — never polled before
        }
    )
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))
    _patch_git(monkeypatch, branch_heads={"main": "abc111"})

    delay_mock = MagicMock(name="review.delay")
    monkeypatch.setattr(t.review_push_task, "delay", delay_mock)

    await t._check_project_for_changes("p-1")

    # No review enqueued
    delay_mock.assert_not_called()
    # The UPDATE persists the baseline
    update_args = conn.executes[-1][1]
    new_shas = update_args[0]
    assert new_shas == {"main": "abc111"}


@pytest.mark.asyncio
async def test_check_skips_unchanged_branch(monkeypatch):
    import app.workers.tasks as t

    conn = _CheckConn(
        project_row={
            "repo_url": "https://github.com/x/y",
            "branches_to_review": ["main"],
            "last_reviewed_sha": {"main": "abc111"},
        }
    )
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))
    _patch_git(monkeypatch, branch_heads={"main": "abc111"})  # unchanged

    delay_mock = MagicMock(name="review.delay")
    monkeypatch.setattr(t.review_push_task, "delay", delay_mock)

    await t._check_project_for_changes("p-1")

    delay_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_enqueues_review_on_forward_move(monkeypatch):
    """SHA changed + new is descendant of old → enqueue review."""
    import app.workers.tasks as t

    conn = _CheckConn(
        project_row={
            "repo_url": "https://github.com/x/y",
            "branches_to_review": ["main"],
            "last_reviewed_sha": {"main": "abc111"},
        }
    )
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))
    _patch_git(
        monkeypatch,
        branch_heads={"main": "def222"},
        ancestor_map={("abc111", "def222"): True},
    )

    delay_mock = MagicMock(name="review.delay")
    monkeypatch.setattr(t.review_push_task, "delay", delay_mock)

    await t._check_project_for_changes("p-1")

    delay_mock.assert_called_once_with("p-1", "main", "abc111", "def222")
    # last_reviewed_sha NOT updated yet — review_push_task does that
    update_args = conn.executes[-1][1]
    new_shas = update_args[0]
    assert new_shas == {"main": "abc111"}  # unchanged in projects.last_reviewed_sha


@pytest.mark.asyncio
async def test_check_records_branch_event_on_force_push(monkeypatch):
    """Non-ancestor SHA change → record force_push event, no review."""
    import app.workers.tasks as t

    conn = _CheckConn(
        project_row={
            "repo_url": "https://github.com/x/y",
            "branches_to_review": ["dev"],
            "last_reviewed_sha": {"dev": "abc111"},
        }
    )
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))
    _patch_git(
        monkeypatch,
        branch_heads={"dev": "rewrite222"},
        ancestor_map={("abc111", "rewrite222"): False},
    )

    delay_mock = MagicMock(name="review.delay")
    monkeypatch.setattr(t.review_push_task, "delay", delay_mock)

    await t._check_project_for_changes("p-1")

    # No review enqueued for force-pushes — humans decide
    delay_mock.assert_not_called()

    # branch_event INSERT happened
    insert_calls = [
        e for e in conn.executes if "INSERT INTO branch_events" in e[0]
    ]
    assert len(insert_calls) == 1
    args = insert_calls[0][1]
    # args = (event_id, project_id, branch, event_type, detail)
    assert args[2] == "dev"
    assert args[3] == "force_push"
    assert args[4] == {"previous_sha": "abc111", "new_sha": "rewrite222"}

    # last_reviewed_sha gets bumped to the new tip so we don't re-fire
    update_args = conn.executes[-1][1]
    new_shas = update_args[0]
    assert new_shas["dev"] == "rewrite222"


@pytest.mark.asyncio
async def test_check_skips_double_dispatch_when_review_in_flight(monkeypatch):
    """If a pending/running review for the same before..after already exists,
    don't queue another one — protects against duplicate work if a slow
    review hasn't completed before the next poll tick."""
    import app.workers.tasks as t

    conn = _CheckConn(
        project_row={
            "repo_url": "https://github.com/x/y",
            "branches_to_review": ["main"],
            "last_reviewed_sha": {"main": "abc111"},
        },
        in_flight_review=True,  # fetchval('SELECT 1 FROM reviews ...') returns 1
    )
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))
    _patch_git(
        monkeypatch,
        branch_heads={"main": "def222"},
        ancestor_map={("abc111", "def222"): True},
    )

    delay_mock = MagicMock(name="review.delay")
    monkeypatch.setattr(t.review_push_task, "delay", delay_mock)

    await t._check_project_for_changes("p-1")

    delay_mock.assert_not_called()  # in-flight guard worked


@pytest.mark.asyncio
async def test_check_missing_project_row_returns_quietly(monkeypatch):
    """Project deleted between dispatch and check — log + skip, no crash."""
    import app.workers.tasks as t

    conn = _CheckConn(project_row=None)
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))

    # Should NOT raise, even with no git stubs
    await t._check_project_for_changes("ghost-pid")

    # No updates (nothing to update)
    assert all("UPDATE projects" not in q for q, _ in conn.executes)
    assert conn.closed


@pytest.mark.asyncio
async def test_check_continues_when_one_branch_is_deleted(monkeypatch):
    """If one branch is gone from the clone (deleted upstream), still process
    the others — don't let one missing branch kill the whole poll."""
    import app.workers.tasks as t

    conn = _CheckConn(
        project_row={
            "repo_url": "https://github.com/x/y",
            "branches_to_review": ["main", "deleted-branch"],
            "last_reviewed_sha": {"main": "abc111", "deleted-branch": "old222"},
        }
    )
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))
    _patch_git(
        monkeypatch,
        branch_heads={"main": "def333"},  # only main exists
        ancestor_map={("abc111", "def333"): True},
    )

    delay_mock = MagicMock(name="review.delay")
    monkeypatch.setattr(t.review_push_task, "delay", delay_mock)

    await t._check_project_for_changes("p-1")

    # main still got reviewed
    delay_mock.assert_called_once()
    assert delay_mock.call_args.args == ("p-1", "main", "abc111", "def333")


# ── _review_push (Week 3 Day 3, Day 5-refactored) ───────────────────────
class _ReviewConn:
    """FakeConn for _review_push tests.

    Day 5 makes _review_push call fetchval TWICE in the normal path
    (idempotency-check, then atomic-claim-INSERT-RETURNING). The default
    setup queues `[done_id_or_None, claim_id_or_None]` in that order.

    Tests that don't care about claim_id can leave it at its default
    (a fresh UUID — happy path "we successfully claimed the row").
    """

    def __init__(
        self,
        *,
        existing_done_id=None,
        claim_id: object = "DEFAULT",
    ):
        # First fetchval = idempotency check ('done' row exists?)
        # Second fetchval = atomic claim INSERT RETURNING
        if claim_id == "DEFAULT":
            claim_id = uuid.UUID("99999999-9999-9999-9999-999999999999")
        self._fetchval_queue: list = [existing_done_id, claim_id]
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list]] = []
        self.closed = False

    async def fetchval(self, query: str, *args):
        self.fetchval_calls.append((query, args))
        if not self._fetchval_queue:
            # Default to None for any extra fetchval — keeps tests forgiving
            # if the production code adds another query down the line.
            return None
        return self._fetchval_queue.pop(0)

    async def execute(self, query: str, *args):
        self.executes.append((query, args))

    async def executemany(self, query: str, records: list):
        self.executemany_calls.append((query, records))

    async def close(self):
        self.closed = True


def _commit_info(sha: str, parent: str | None = None) -> object:
    """Minimal stand-in for CommitInfo with the fields _upsert_commits uses."""
    import datetime

    return type(
        "FakeCommitInfo",
        (),
        {
            "sha": sha,
            "parent_sha": parent,
            "author_name": "Alice",
            "author_email": "alice@example.invalid",
            "committer_name": "Alice",
            "committer_email": "alice@example.invalid",
            "committed_at": datetime.datetime(
                2026, 6, 23, 12, 0, tzinfo=datetime.timezone.utc
            ),
            "subject": f"commit {sha[:8]}",
        },
    )()


def _patch_review_helpers(monkeypatch, *, commits=None, review_result=None):
    """Stub out the heavy helpers in app.workers.tasks."""
    import app.workers.tasks as t

    async def _fake_commits_between(_pid, _b, _a):
        return commits or []

    async def _fake_run_review(**_kwargs):
        # Default: zero findings, fake review_id
        import uuid as _uuid

        return review_result or _FakeReviewResult(
            review_id=_uuid.uuid4(),
            findings=[],
            summary="ok",
            severity_counts={"info": 0, "minor": 0, "major": 0, "critical": 0},
            token_usage={"input": 100, "output": 50, "total": 150},
        )

    monkeypatch.setattr(t, "commits_between", _fake_commits_between)
    monkeypatch.setattr(t, "run_review_for_push", _fake_run_review)

    # register_vector is a no-op in tests
    async def _noop(_):
        return None

    monkeypatch.setattr(t, "register_vector", _noop)


class _FakeReviewResult:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.mark.asyncio
async def test_review_push_claims_then_persists_then_bumps_sha(monkeypatch):
    """Happy path (Day-5):
      1. Idempotency check → no existing done review
      2. Atomic claim INSERT → returns a claim_id
      3. UPDATE status='running'
      4. Commits inserted with the claim_id as review_id (FK)
      5. run_review_for_push called with review_id=claim_id (UPDATEs the row)
      6. last_reviewed_sha bumped
    """
    import app.workers.tasks as t

    claim_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    conn = _ReviewConn(existing_done_id=None, claim_id=claim_id)
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))
    _patch_review_helpers(
        monkeypatch,
        commits=[_commit_info("c1", "c0"), _commit_info("c2", "c1")],
    )

    await t._review_push("p-1", "main", "before-sha", "after-sha")

    # commits inserted via executemany — last record element is review_id (the FK)
    assert len(conn.executemany_calls) == 1
    insert_query, records = conn.executemany_calls[0]
    assert "INSERT INTO commits" in insert_query
    assert "review_id" in insert_query  # FK column is in the INSERT
    assert len(records) == 2
    # Each record: (id, project_id, branch, sha, parent, ..., source, review_id)
    assert records[0][2] == "main"     # branch
    assert records[0][3] == "c1"       # sha
    assert records[0][-1] == claim_id  # FK pointing at the claimed review

    # UPDATE reviews SET status='running' should have happened
    running_updates = [
        e for e in conn.executes
        if "UPDATE reviews" in e[0] and "'running'" in e[0]
    ]
    assert len(running_updates) == 1

    # last_reviewed_sha bumped (UPDATE projects)
    project_updates = [e for e in conn.executes if "UPDATE projects" in e[0]]
    assert len(project_updates) == 1
    sha_arg, project_id_arg = project_updates[-1][1]
    assert sha_arg == {"main": "after-sha"}
    assert project_id_arg == "p-1"

    assert conn.closed


@pytest.mark.asyncio
async def test_review_push_idempotent_when_done_review_exists(monkeypatch):
    """Retry of a partially-succeeded task: existing done review → skip LLM."""
    import app.workers.tasks as t

    existing_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    # First fetchval returns the existing done id → idempotent path
    conn = _ReviewConn(existing_done_id=existing_id, claim_id=None)
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))

    # The idempotent path should NEVER hit run_review_for_push or commits_between
    async def _shouldnt_be_called(*_a, **_kw):
        raise AssertionError("LLM path should be skipped on idempotent retry")

    monkeypatch.setattr(t, "commits_between", _shouldnt_be_called)
    monkeypatch.setattr(t, "run_review_for_push", _shouldnt_be_called)

    async def _noop(_):
        return None

    monkeypatch.setattr(t, "register_vector", _noop)

    await t._review_push("p-1", "main", "before-sha", "after-sha")

    # No commit inserts, no atomic claim attempted (second fetchval shouldn't fire)
    assert conn.executemany_calls == []
    # Only the SHA bump UPDATE happened
    project_updates = [e for e in conn.executes if "UPDATE projects" in e[0]]
    assert len(project_updates) == 1


@pytest.mark.asyncio
async def test_review_push_no_commits_doesnt_executemany(monkeypatch):
    """Empty commits range — claim still happens, review still runs, but
    no executemany on commits because there are no commit rows to write."""
    import app.workers.tasks as t

    conn = _ReviewConn(existing_done_id=None)
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))
    _patch_review_helpers(monkeypatch, commits=[])

    await t._review_push("p-1", "main", "before-sha", "after-sha")

    # No commit INSERT (commits=[])
    assert conn.executemany_calls == []
    # SHA still bumped
    project_updates = [e for e in conn.executes if "UPDATE projects" in e[0]]
    assert len(project_updates) == 1


@pytest.mark.asyncio
async def test_review_push_marks_error_when_review_raises(monkeypatch):
    """If run_review_for_push raises:
      • last_reviewed_sha must NOT update (Celery retry needs to re-fire)
      • The claimed review row must be UPDATEd to status='error' so the
        partial unique index unblocks the retry's fresh claim.
    """
    import app.workers.tasks as t

    claim_id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    conn = _ReviewConn(existing_done_id=None, claim_id=claim_id)
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))

    async def _exploding_review(**_kw):
        raise RuntimeError("bedrock down")

    async def _fake_commits(_pid, _b, _a):
        return []

    async def _noop(_):
        return None

    monkeypatch.setattr(t, "commits_between", _fake_commits)
    monkeypatch.setattr(t, "run_review_for_push", _exploding_review)
    monkeypatch.setattr(t, "register_vector", _noop)

    with pytest.raises(RuntimeError, match="bedrock down"):
        await t._review_push("p-1", "main", "before-sha", "after-sha")

    # No project SHA bump
    project_updates = [e for e in conn.executes if "UPDATE projects" in e[0]]
    assert project_updates == []

    # The claimed review row WAS marked as 'error' in the except block
    error_updates = [
        e for e in conn.executes
        if "UPDATE reviews" in e[0] and "'error'" in e[0]
    ]
    assert len(error_updates) == 1
    assert error_updates[0][1] == (claim_id,)

    assert conn.closed  # connection still closed on the way out


@pytest.mark.asyncio
async def test_review_push_bails_silently_when_claim_lost_to_concurrent_worker(monkeypatch):
    """ON CONFLICT DO NOTHING returns NULL when the partial unique index
    blocks the INSERT. That means a concurrent worker is already on this
    diff — bail silently without burning Bedrock tokens."""
    import app.workers.tasks as t

    # Idempotency: no done review. Claim: NULL (another worker has it).
    conn = _ReviewConn(existing_done_id=None, claim_id=None)
    monkeypatch.setattr(t, "_open_conn", AsyncMock(return_value=conn))

    async def _shouldnt_be_called(*_a, **_kw):
        raise AssertionError("Lost-claim path must not call commits/LLM helpers")

    monkeypatch.setattr(t, "commits_between", _shouldnt_be_called)
    monkeypatch.setattr(t, "run_review_for_push", _shouldnt_be_called)

    async def _noop(_):
        return None

    monkeypatch.setattr(t, "register_vector", _noop)

    # Must NOT raise — silent bail is the contract
    await t._review_push("p-1", "main", "before-sha", "after-sha")

    # No commit inserts, no sha bump
    assert conn.executemany_calls == []
    project_updates = [e for e in conn.executes if "UPDATE projects" in e[0]]
    assert project_updates == []
    # No running-status update either (we never claimed it)
    running_updates = [
        e for e in conn.executes
        if "UPDATE reviews" in e[0] and "'running'" in e[0]
    ]
    assert running_updates == []
