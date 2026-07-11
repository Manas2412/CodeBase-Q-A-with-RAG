"""Tests for the Day 4 read endpoints in app.main.

Covers:
  GET /projects/{id}/reviews
  GET /reviews/{id}
  GET /reviews/{id}/findings
  GET /projects/{id}/commits

We don't spin up a real Postgres for these — instead `app.main.db_pool` is
replaced with a `FakePool` that captures every SQL query and returns
canned rows. This pins the contract (response shape, filter behaviour,
ordering, 404s) without paying for migrations + fixtures on every run.
The real-DB integration test for these endpoints lives in tests/integration/.

A small but important pattern:

    FakeRecord supports both `r["col"]` (asyncpg's __getitem__) and
    `r.col` (attribute access — handy when the production code uses
    either form across the codebase).
"""

from __future__ import annotations

import datetime
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app


# ── Fake DB plumbing ──────────────────────────────────────────────────────
class FakeRecord(dict):
    """A dict that ALSO supports attribute access — mirrors asyncpg.Record."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e


class FakeConn:
    """Records every SQL call and returns a configurable stack of results.

    Usage:
        conn = FakeConn()
        conn.queue_fetchval(1)              # SELECT 1 FROM projects ...
        conn.queue_fetch([row1, row2])      # main list query
        conn.queue_fetchval(2)              # COUNT(*)

    Calls dequeue in FIFO order. If the queue is empty when a method is
    invoked, the test fails loudly — easier than chasing silent Nones.
    """

    def __init__(self) -> None:
        self.fetchval_queue: list = []
        self.fetch_queue: list[list[FakeRecord]] = []
        self.fetchrow_queue: list[FakeRecord | None] = []
        self.executed: list[tuple[str, tuple]] = []
        self.queries: list[tuple[str, str, tuple]] = []

    # ── queue API for tests ──
    def queue_fetchval(self, value: Any) -> None:
        self.fetchval_queue.append(value)

    def queue_fetch(self, rows: list[FakeRecord]) -> None:
        self.fetch_queue.append(rows)

    def queue_fetchrow(self, row: FakeRecord | None) -> None:
        self.fetchrow_queue.append(row)

    # ── asyncpg surface ──
    async def fetchval(self, query: str, *args) -> Any:
        self.queries.append(("fetchval", query, args))
        if not self.fetchval_queue:
            raise AssertionError(
                f"FakeConn.fetchval called with no queued result. Query was:\n{query}"
            )
        return self.fetchval_queue.pop(0)

    async def fetch(self, query: str, *args) -> list[FakeRecord]:
        self.queries.append(("fetch", query, args))
        if not self.fetch_queue:
            raise AssertionError(
                f"FakeConn.fetch called with no queued result. Query was:\n{query}"
            )
        return self.fetch_queue.pop(0)

    async def fetchrow(self, query: str, *args) -> FakeRecord | None:
        self.queries.append(("fetchrow", query, args))
        if not self.fetchrow_queue:
            raise AssertionError(
                f"FakeConn.fetchrow called with no queued result. Query was:\n{query}"
            )
        return self.fetchrow_queue.pop(0)

    async def execute(self, query: str, *args) -> None:
        self.executed.append((query, args))


class FakePool:
    """Stands in for asyncpg.Pool. acquire() returns the same FakeConn."""

    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _cm():
            yield self.conn

        return _cm()


@pytest.fixture
def fake_conn() -> FakeConn:
    return FakeConn()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_conn: FakeConn) -> TestClient:
    """TestClient wired to a FakePool that uses our FakeConn.

    Also logs in via the shared-password auth flow so the test's requests
    carry a valid session cookie. Without this, every protected endpoint
    would return 401 before the FakePool got a chance to answer.
    """
    from app import main as main_module

    monkeypatch.setattr(main_module, "db_pool", FakePool(fake_conn))
    # The lifespan hook tries to acquire a real pool — skip it for these
    # unit tests by using TestClient as a plain client. Override the
    # lifespan with a no-op asynccontextmanager.
    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    monkeypatch.setattr(main_module, "lifespan", _noop_lifespan)

    # Configure the auth backend with a known password + secret and log in
    # so the TestClient's cookie jar has a valid session for every request
    # the test then makes.
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-pw")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-prod")

    tc = TestClient(app)
    login_resp = tc.post("/auth/login", json={"password": "test-pw"})
    assert login_resp.status_code == 200, (
        "Auth fixture login failed — check DASHBOARD_PASSWORD/SESSION_SECRET wiring"
    )
    return tc


# ── Row factories ─────────────────────────────────────────────────────────
def _project_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-1111-1111-111111111111")


def _review_id() -> uuid.UUID:
    return uuid.UUID("22222222-2222-2222-2222-222222222222")


def _review_row(
    *,
    review_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    branch: str = "main",
    status: str = "done",
    finding_count: int = 0,
    severity_counts: dict | None = None,
    summary: str | None = None,
    created_at: datetime.datetime | None = None,
) -> FakeRecord:
    """Shape matches the _REVIEW_SUMMARY_COLUMNS SELECT plus optional summary."""
    return FakeRecord(
        id=review_id or _review_id(),
        project_id=project_id or _project_id(),
        branch=branch,
        before_sha="aaaa1111",
        after_sha="bbbb2222",
        status=status,
        severity_counts=severity_counts or {"critical": 0, "major": 0, "minor": 0, "info": 0},
        token_usage={"input": 100, "output": 50, "total": 150},
        checklist_version=1,
        batch_mode="batch",
        created_at=created_at or datetime.datetime(2026, 6, 23, 12, 0, tzinfo=datetime.timezone.utc),
        completed_at=datetime.datetime(2026, 6, 23, 12, 0, 30, tzinfo=datetime.timezone.utc),
        finding_count=finding_count,
        summary=summary,
    )


def _commit_row(sha: str = "c0ffee", branch: str = "main") -> FakeRecord:
    return FakeRecord(
        sha=sha,
        parent_sha="dead1234",
        branch=branch,
        author_name="Alice",
        author_email="alice@example.invalid",
        committer_name="Alice",
        committer_email="alice@example.invalid",
        committed_at=datetime.datetime(2026, 6, 23, 11, 0, tzinfo=datetime.timezone.utc),
        subject="hello world",
        source="poll",
    )


def _finding_row(
    severity: str = "major",
    file_path: str = "foo.py",
    *,
    code_snippet: str | None = "+        cursor.execute(f\"SELECT * FROM t WHERE x = {x}\")",
    suggested_code: str | None = "        cursor.execute(\"SELECT * FROM t WHERE x = %s\", (x,))",
) -> FakeRecord:
    return FakeRecord(
        id=uuid.uuid4(),
        review_id=_review_id(),
        commit_id=None,
        severity=severity,
        category="security",
        file_path=file_path,
        start_line=10,
        end_line=20,
        message="potential SQL injection",
        suggestion="parameterise the query",
        rule_id="SEC-001",
        code_snippet=code_snippet,
        suggested_code=suggested_code,
    )


# ── GET /projects/{id}/reviews ────────────────────────────────────────────
class TestListProjectReviews:
    def test_returns_paginated_list(self, client: TestClient, fake_conn: FakeConn) -> None:
        fake_conn.queue_fetchval(1)  # project exists
        fake_conn.queue_fetch([
            _review_row(branch="main", finding_count=3),
            _review_row(branch="dev", finding_count=0),
        ])
        fake_conn.queue_fetchval(2)  # total

        r = client.get(f"/projects/{_project_id()}/reviews")

        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert len(body["reviews"]) == 2
        first = body["reviews"][0]
        assert first["branch"] == "main"
        assert first["finding_count"] == 3
        assert first["severity_counts"] == {"critical": 0, "major": 0, "minor": 0, "info": 0}
        # Never leak the verbose summary text into the list response
        assert "summary" not in first

    def test_404_when_project_missing(self, client: TestClient, fake_conn: FakeConn) -> None:
        fake_conn.queue_fetchval(None)  # project does not exist

        r = client.get(f"/projects/{_project_id()}/reviews")

        assert r.status_code == 404
        assert r.json()["detail"] == "Project not found"

    def test_branch_filter_passes_through_to_sql(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([])
        fake_conn.queue_fetchval(0)

        r = client.get(f"/projects/{_project_id()}/reviews?branch=dev&status=done")

        assert r.status_code == 200
        # Find the main SELECT query (skipping the existence check)
        main_select = next(
            q for kind, q, _ in fake_conn.queries
            if kind == "fetch" and "FROM reviews" in q
        )
        assert "r.branch = $2" in main_select
        assert "r.status = $3" in main_select

    def test_400_on_non_uuid_project_id(self, client: TestClient) -> None:
        r = client.get("/projects/not-a-uuid/reviews")
        assert r.status_code == 400
        assert r.json()["detail"] == "project_id must be a UUID"

    def test_respects_limit_and_offset(self, client: TestClient, fake_conn: FakeConn) -> None:
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([])
        fake_conn.queue_fetchval(0)

        r = client.get(f"/projects/{_project_id()}/reviews?limit=10&offset=20")

        assert r.status_code == 200
        body = r.json()
        assert body["limit"] == 10
        assert body["offset"] == 20

    def test_invalid_limit_clamped_by_pydantic(self, client: TestClient) -> None:
        # limit must be in [1, 100]
        r = client.get(f"/projects/{_project_id()}/reviews?limit=500")
        assert r.status_code == 422


# ── GET /reviews/{id} ─────────────────────────────────────────────────────
class TestGetReview:
    """Day-5: commits queried via the FK, not a time-window."""

    def test_returns_review_with_summary_and_commits(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchrow(_review_row(summary="Looks fine.", finding_count=1))
        fake_conn.queue_fetch([_commit_row("c1"), _commit_row("c2")])

        r = client.get(f"/reviews/{_review_id()}")

        assert r.status_code == 200
        body = r.json()
        assert body["id"] == str(_review_id())
        assert body["summary"] == "Looks fine."
        assert body["finding_count"] == 1
        assert len(body["commits"]) == 2
        assert body["commits"][0]["sha"] == "c1"

    def test_commit_query_uses_review_id_fk(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        """The commit query must filter by review_id directly — no time-window
        guessing, no LAG over prior reviews. One query, hard attribution.

        We assert on the WHERE clause specifically (committed_at appears
        in the SELECT and ORDER BY columns — that's expected).
        """
        fake_conn.queue_fetchrow(_review_row())
        fake_conn.queue_fetch([_commit_row()])

        r = client.get(f"/reviews/{_review_id()}")

        assert r.status_code == 200
        commit_fetches = [
            q for kind, q, _ in fake_conn.queries
            if kind == "fetch" and "FROM commits" in q
        ]
        assert len(commit_fetches) == 1
        # Hard attribution: WHERE review_id = $1, no time bounds.
        where_clause = commit_fetches[0].split("WHERE", 1)[1].split("ORDER BY")[0]
        assert "review_id = $1" in where_clause
        # And the old time-window approach should be GONE
        assert "committed_at <=" not in where_clause
        assert "committed_at >" not in where_clause

    def test_commits_empty_for_pre_day5_reviews(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        """Reviews from before the FK migration have NULL review_id on
        their commits → the join returns empty. That's correct semantics."""
        fake_conn.queue_fetchrow(_review_row(summary="old review"))
        fake_conn.queue_fetch([])

        r = client.get(f"/reviews/{_review_id()}")

        assert r.status_code == 200
        assert r.json()["commits"] == []

    def test_404_on_unknown_review(self, client: TestClient, fake_conn: FakeConn) -> None:
        fake_conn.queue_fetchrow(None)

        r = client.get(f"/reviews/{_review_id()}")

        assert r.status_code == 404
        assert r.json()["detail"] == "Review not found"

    def test_400_on_bad_uuid(self, client: TestClient) -> None:
        r = client.get("/reviews/not-a-uuid")
        assert r.status_code == 400
        assert r.json()["detail"] == "review_id must be a UUID"


# ── GET /reviews/{id}/findings ────────────────────────────────────────────
class TestListReviewFindings:
    def test_returns_findings_sorted_critical_first(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(1)  # review exists
        fake_conn.queue_fetch([
            _finding_row(severity="critical", file_path="auth.py"),
            _finding_row(severity="major", file_path="db.py"),
            _finding_row(severity="minor", file_path="utils.py"),
        ])
        fake_conn.queue_fetchval(3)

        r = client.get(f"/reviews/{_review_id()}/findings")

        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        # Order preserved as we returned it (production code does the SORT in SQL)
        assert [f["severity"] for f in body["findings"]] == ["critical", "major", "minor"]
        assert body["findings"][0]["category"] == "security"
        assert body["findings"][0]["suggestion"] == "parameterise the query"

    def test_severity_multi_select_passes_through(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([])
        fake_conn.queue_fetchval(0)

        r = client.get(
            f"/reviews/{_review_id()}/findings?severity=critical&severity=major"
        )

        assert r.status_code == 200
        main = next(
            q for kind, q, _ in fake_conn.queries
            if kind == "fetch" and "review_findings" in q
        )
        assert "f.severity = ANY($2::text[])" in main

    def test_file_path_substring_filter(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([])
        fake_conn.queue_fetchval(0)

        r = client.get(f"/reviews/{_review_id()}/findings?file_path=auth")

        assert r.status_code == 200
        # The ILIKE param is wrapped in % and passed positionally
        args = next(
            args for kind, q, args in fake_conn.queries
            if kind == "fetch" and "review_findings" in q
        )
        assert "%auth%" in args

    def test_404_on_unknown_review(self, client: TestClient, fake_conn: FakeConn) -> None:
        fake_conn.queue_fetchval(None)
        r = client.get(f"/reviews/{_review_id()}/findings")
        assert r.status_code == 404
        assert r.json()["detail"] == "Review not found"

    def test_invalid_limit_returns_422(self, client: TestClient) -> None:
        r = client.get(f"/reviews/{_review_id()}/findings?limit=1000")
        assert r.status_code == 422  # max 500

    def test_code_snippet_and_suggested_code_surface(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        """Day-5 added two TEXT NULL columns. The API must expose both — null-safe."""
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([
            _finding_row(
                severity="critical",
                code_snippet="+    eval(user_input)",
                suggested_code="    # Don't eval untrusted input.",
            ),
            _finding_row(
                severity="minor",
                code_snippet=None,         # extractor couldn't map the line
                suggested_code=None,       # LLM didn't propose code-as-fix
            ),
        ])
        fake_conn.queue_fetchval(2)

        r = client.get(f"/reviews/{_review_id()}/findings")
        body = r.json()
        assert body["findings"][0]["code_snippet"] == "+    eval(user_input)"
        assert body["findings"][0]["suggested_code"] == "    # Don't eval untrusted input."
        assert body["findings"][1]["code_snippet"] is None
        assert body["findings"][1]["suggested_code"] is None
        # SELECT clause must request the new columns
        main = next(
            q for kind, q, _ in fake_conn.queries
            if kind == "fetch" and "FROM review_findings" in q
        )
        assert "f.code_snippet" in main
        assert "f.suggested_code" in main


# ── GET /projects/{id}/commits ────────────────────────────────────────────
class TestListProjectCommits:
    def test_returns_commits_newest_first(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([
            _commit_row("c1"),
            _commit_row("c2"),
            _commit_row("c3"),
        ])
        fake_conn.queue_fetchval(3)

        r = client.get(f"/projects/{_project_id()}/commits")

        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        shas = [c["sha"] for c in body["commits"]]
        assert shas == ["c1", "c2", "c3"]
        assert body["commits"][0]["author_email"] == "alice@example.invalid"
        assert body["commits"][0]["source"] == "poll"

    def test_branch_and_author_filters_compose(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([])
        fake_conn.queue_fetchval(0)

        r = client.get(
            f"/projects/{_project_id()}/commits"
            "?branch=main&author_email=alice@example.invalid"
        )

        assert r.status_code == 200
        main = next(
            q for kind, q, _ in fake_conn.queries
            if kind == "fetch" and "FROM commits" in q
        )
        assert "c.branch = $2" in main
        assert "c.author_email = $3" in main

    def test_404_on_unknown_project(self, client: TestClient, fake_conn: FakeConn) -> None:
        fake_conn.queue_fetchval(None)
        r = client.get(f"/projects/{_project_id()}/commits")
        assert r.status_code == 404
        assert r.json()["detail"] == "Project not found"


# ── BranchEvent fixtures + endpoint tests (Day 5B) ────────────────────────
def _branch_event_row(
    *,
    event_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    branch: str = "main",
    event_type: str = "force_push",
    detail: dict | None = None,
    resolved: bool = False,
    created_at: datetime.datetime | None = None,
) -> FakeRecord:
    return FakeRecord(
        id=event_id or uuid.uuid4(),
        project_id=project_id or _project_id(),
        branch=branch,
        event_type=event_type,
        detail=detail or {"previous_sha": "aaa111", "new_sha": "bbb222"},
        resolved=resolved,
        created_at=created_at
        or datetime.datetime(2026, 6, 23, 9, 0, tzinfo=datetime.timezone.utc),
    )


class TestListBranchEvents:
    def test_returns_events_and_unresolved_total(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(1)  # project exists
        fake_conn.queue_fetch([
            _branch_event_row(branch="dev", resolved=False),
            _branch_event_row(branch="main", resolved=True),
        ])
        fake_conn.queue_fetchval(2)  # filtered total
        fake_conn.queue_fetchval(3)  # unresolved_total (project-wide, not filtered)

        r = client.get(f"/projects/{_project_id()}/branch-events")

        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert body["unresolved_total"] == 3
        assert len(body["events"]) == 2
        first = body["events"][0]
        assert first["event_type"] == "force_push"
        assert first["detail"]["new_sha"] == "bbb222"

    def test_resolved_filter_passes_through(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([])
        fake_conn.queue_fetchval(0)
        fake_conn.queue_fetchval(0)

        r = client.get(
            f"/projects/{_project_id()}/branch-events?resolved=false"
        )

        assert r.status_code == 200
        main = next(
            q for kind, q, _ in fake_conn.queries
            if kind == "fetch" and "FROM branch_events" in q
        )
        assert "e.resolved = $2" in main

    def test_combined_branch_and_event_type_filters(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([])
        fake_conn.queue_fetchval(0)
        fake_conn.queue_fetchval(0)

        r = client.get(
            f"/projects/{_project_id()}/branch-events"
            "?branch=dev&event_type=force_push"
        )

        assert r.status_code == 200
        main = next(
            q for kind, q, _ in fake_conn.queries
            if kind == "fetch" and "FROM branch_events" in q
        )
        assert "e.branch = $2" in main
        assert "e.event_type = $3" in main

    def test_unresolved_total_is_unfiltered_by_user_choice(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        """Even when the user is viewing `resolved=true`, unresolved_total
        reflects the project-wide unresolved count — the dashboard's red-dot
        number must not vary by view."""
        fake_conn.queue_fetchval(1)
        fake_conn.queue_fetch([_branch_event_row(resolved=True)])
        fake_conn.queue_fetchval(1)   # filtered total
        fake_conn.queue_fetchval(7)   # unresolved_total

        r = client.get(
            f"/projects/{_project_id()}/branch-events?resolved=true"
        )

        body = r.json()
        assert body["total"] == 1
        assert body["unresolved_total"] == 7

    def test_404_on_unknown_project(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchval(None)
        r = client.get(f"/projects/{_project_id()}/branch-events")
        assert r.status_code == 404
        assert r.json()["detail"] == "Project not found"


class TestResolveBranchEvent:
    def test_marks_event_resolved_and_returns_row(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        event_id = uuid.uuid4()
        fake_conn.queue_fetchrow(_branch_event_row(event_id=event_id, resolved=True))

        r = client.post(f"/branch-events/{event_id}/resolve")

        assert r.status_code == 200
        body = r.json()
        assert body["id"] == str(event_id)
        assert body["resolved"] is True
        # The query should be a single UPDATE … RETURNING (no SELECT first)
        update_calls = [
            q for kind, q, _ in fake_conn.queries
            if kind == "fetchrow" and "UPDATE branch_events" in q
        ]
        assert len(update_calls) == 1
        assert "RETURNING" in update_calls[0]

    def test_idempotent_when_already_resolved(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        """Calling resolve on an already-resolved event must return 200
        with `resolved=true`, not 4xx — the UI may double-click."""
        event_id = uuid.uuid4()
        fake_conn.queue_fetchrow(_branch_event_row(event_id=event_id, resolved=True))

        r = client.post(f"/branch-events/{event_id}/resolve")

        assert r.status_code == 200
        assert r.json()["resolved"] is True

    def test_404_on_unknown_event(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchrow(None)  # UPDATE ... RETURNING returned no rows
        r = client.post(f"/branch-events/{uuid.uuid4()}/resolve")
        assert r.status_code == 404
        assert r.json()["detail"] == "Branch event not found"

    def test_400_on_bad_uuid(self, client: TestClient) -> None:
        r = client.post("/branch-events/not-a-uuid/resolve")
        assert r.status_code == 400
        assert r.json()["detail"] == "event_id must be a UUID"
