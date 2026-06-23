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
    """TestClient wired to a FakePool that uses our FakeConn."""
    from app import main as main_module

    monkeypatch.setattr(main_module, "db_pool", FakePool(fake_conn))
    # The lifespan hook tries to acquire a real pool — skip it for these
    # unit tests by using TestClient as a plain client. Override the
    # lifespan with a no-op asynccontextmanager.
    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    monkeypatch.setattr(main_module, "lifespan", _noop_lifespan)
    # TestClient runs lifespan by default; pass raise_server_exceptions=True
    # so a server-side bug surfaces as a test failure not a 500.
    return TestClient(app)


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


def _finding_row(severity: str = "major", file_path: str = "foo.py") -> FakeRecord:
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
    def test_returns_review_with_summary_and_commits(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        fake_conn.queue_fetchrow(_review_row(summary="Looks fine.", finding_count=1))
        fake_conn.queue_fetchval(None)  # no previous review (first ever)
        fake_conn.queue_fetch([_commit_row("c1"), _commit_row("c2")])

        r = client.get(f"/reviews/{_review_id()}")

        assert r.status_code == 200
        body = r.json()
        assert body["id"] == str(_review_id())
        assert body["summary"] == "Looks fine."
        assert body["finding_count"] == 1
        assert len(body["commits"]) == 2
        assert body["commits"][0]["sha"] == "c1"

    def test_uses_previous_review_window_when_one_exists(
        self, client: TestClient, fake_conn: FakeConn
    ) -> None:
        """When a previous review exists, commits are scoped to (prev, this]."""
        fake_conn.queue_fetchrow(_review_row())
        prev_time = datetime.datetime(2026, 6, 22, 12, 0, tzinfo=datetime.timezone.utc)
        fake_conn.queue_fetchval(prev_time)  # previous review exists
        fake_conn.queue_fetch([_commit_row()])

        r = client.get(f"/reviews/{_review_id()}")

        assert r.status_code == 200
        # The 3rd query (commit fetch) should be the "previous review window"
        # path — params include both boundaries.
        commit_fetches = [
            q for kind, q, args in fake_conn.queries
            if kind == "fetch" and "FROM commits" in q
        ]
        assert len(commit_fetches) == 1
        # Bounded by `>` and `<=`
        assert "committed_at >  $3" in commit_fetches[0]
        assert "committed_at <= $4" in commit_fetches[0]

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
