"""Tests for app/review/context_builder.py.

We don't need a live Postgres — the SQL is shallow enough that a tiny
FakeConn (returns canned row lists) covers every branch. embed_query is
exercised against the conftest `mock_bedrock` so no real Bedrock calls.

Integration coverage against the actual indexed pgvector data happens at
the worker-level smoke (Week 2 Day 5 ships it).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.bedrock_client import BedrockClient
from app.review.context_builder import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextChunk,
    build_context,
)
from app.review.diff_parser import DiffHunk


# ── Tiny fake asyncpg.Connection ─────────────────────────────────────────
class _FakeRecord(dict):
    """Quacks like asyncpg.Record for the [...] item access we use."""


class _FakeConn:
    """Returns canned row lists from a FIFO queue, captures each call."""

    def __init__(self, *responses: list[dict]):
        self._responses = list(responses)
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args) -> list[_FakeRecord]:
        self.fetch_calls.append((query, args))
        if not self._responses:
            return []
        rows = self._responses.pop(0)
        return [_FakeRecord(r) for r in rows]


def _hunk(file_path: str, added: tuple[str, ...] = ("x = 1",)) -> DiffHunk:
    return DiffHunk(
        file_path=file_path,
        old_file_path=file_path,
        change_type="modified",
        new_start=1,
        new_count=len(added),
        old_start=1,
        old_count=1,
        added_lines=added,
        removed_lines=(),
        raw="@@ ... @@\n" + "\n".join("+" + line for line in added),
    )


def _row(
    *,
    file_path: str = "x.py",
    name: str = "fn",
    chunk_type: str = "function",
    start_line: int = 0,
    end_line: int = 5,
    content: str = "def fn():\n    return 1\n",
    similarity: float | None = None,
) -> dict[str, Any]:
    r: dict[str, Any] = {
        "file_path": file_path,
        "name": name,
        "chunk_type": chunk_type,
        "start_line": start_line,
        "end_line": end_line,
        "content": content,
    }
    if similarity is not None:
        r["similarity"] = similarity
    return r


# ── empty input ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_build_context_empty_hunks_returns_empty(mock_bedrock):
    conn = _FakeConn()
    bc = BedrockClient(client=mock_bedrock)
    assert await build_context(conn, "test-pid", [], client=bc) == []
    assert conn.fetch_calls == []  # no SQL run for empty input


# ── same-file chunks come first ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_build_context_returns_same_file_chunks_first(mock_bedrock):
    same_file_rows = [
        _row(file_path="changed.py", name="foo", start_line=0),
        _row(file_path="changed.py", name="bar", start_line=20),
    ]
    semantic_rows = [
        _row(file_path="other.py", name="caller", similarity=0.85),
    ]
    conn = _FakeConn(same_file_rows, semantic_rows)
    bc = BedrockClient(client=mock_bedrock)

    result = await build_context(
        conn, "test-pid", [_hunk("changed.py")], client=bc
    )

    assert [c.relevance_reason for c in result] == [
        "same_file",
        "same_file",
        "similar",
    ]
    assert result[0].file_path == "changed.py"
    assert result[-1].file_path == "other.py"


# ── budget enforcement ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_build_context_enforces_token_budget(mock_bedrock):
    """A tight budget should cut chunks that exceed it; smaller later ones still fit."""
    # Each row: content length 400 chars → ~100 tokens.
    big = "x" * 400      # ~100 tokens
    huge = "x" * 2000    # ~500 tokens
    tiny = "x" * 40      # ~10 tokens

    same_file_rows = [
        _row(file_path="f.py", name="big", content=big, start_line=0),
        _row(file_path="f.py", name="huge", content=huge, start_line=10),
        _row(file_path="f.py", name="tiny", content=tiny, start_line=20),
    ]
    conn = _FakeConn(same_file_rows, [])  # no semantic for simplicity
    bc = BedrockClient(client=mock_bedrock)

    # Budget = 150 tokens: 'big' fits (100), 'huge' doesn't (500), 'tiny' fits (10)
    result = await build_context(
        conn, "test-pid", [_hunk("f.py")], token_budget=150, client=bc
    )

    names = [c.name for c in result]
    assert "big" in names
    assert "tiny" in names
    assert "huge" not in names


# ── no semantic search when there are no added lines ───────────────────
@pytest.mark.asyncio
async def test_build_context_skips_semantic_search_when_no_added_lines(mock_bedrock):
    """A deletion-only diff has empty added_lines → skip the ANN call."""
    same_file_rows = [_row(file_path="f.py")]
    conn = _FakeConn(same_file_rows)  # only one response queued — semantic step won't fetch
    bc = BedrockClient(client=mock_bedrock)

    deletion_hunk = DiffHunk(
        file_path="f.py",
        old_file_path="f.py",
        change_type="deleted",
        new_start=0, new_count=0, old_start=1, old_count=2,
        added_lines=(),
        removed_lines=("removed", "lines"),
        raw="@@ -1,2 +0,0 @@",
    )
    result = await build_context(conn, "test-pid", [deletion_hunk], client=bc)

    # Only ONE fetch call (same-file SQL) — no ANN search
    assert len(conn.fetch_calls) == 1
    assert all(c.relevance_reason == "same_file" for c in result)
    # And we didn't call Bedrock embed either
    mock_bedrock.invoke_model.assert_not_called()


# ── SQL parameters ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_build_context_passes_changed_files_to_sql(mock_bedrock):
    """The same-file query receives the deduped list of touched files."""
    conn = _FakeConn([], [])
    bc = BedrockClient(client=mock_bedrock)

    await build_context(
        conn,
        "abc123",
        [
            _hunk("a.py"),
            _hunk("b.py"),
            _hunk("a.py"),  # dup — must be deduped
        ],
        client=bc,
    )

    # 1st call: same-file fetch. args = (project_id, changed_files[])
    _query, args = conn.fetch_calls[0]
    project_id, changed_files = args
    assert project_id == "abc123"
    assert sorted(changed_files) == ["a.py", "b.py"]


@pytest.mark.asyncio
async def test_build_context_semantic_query_excludes_changed_files(mock_bedrock):
    """The ANN query must NOT include chunks from files in the diff."""
    conn = _FakeConn([], [])  # no rows; we only care about the call args
    bc = BedrockClient(client=mock_bedrock)

    await build_context(
        conn,
        "abc123",
        [_hunk("changed.py", added=("def f(): pass",))],
        client=bc,
    )

    # 2nd call: semantic fetch. args = (query_vec, project_id, changed_files, top_k)
    assert len(conn.fetch_calls) == 2
    _query, args = conn.fetch_calls[1]
    query_vec, project_id, changed_files, top_k = args
    assert project_id == "abc123"
    assert changed_files == ["changed.py"]
    assert top_k > 0
    assert len(query_vec) == 1024  # cohere v3 dim


# ── relevance scores ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_build_context_same_file_chunks_have_similarity_one(mock_bedrock):
    """same_file chunks get similarity=1.0 (always relevant)."""
    same_file_rows = [_row(file_path="f.py")]
    conn = _FakeConn(same_file_rows, [])
    bc = BedrockClient(client=mock_bedrock)

    result = await build_context(conn, "pid", [_hunk("f.py")], client=bc)
    assert result[0].similarity == 1.0


@pytest.mark.asyncio
async def test_build_context_semantic_chunks_carry_similarity_from_db(mock_bedrock):
    """semantic chunks pass through the cosine similarity computed by pgvector."""
    semantic_rows = [
        _row(file_path="other.py", similarity=0.91),
        _row(file_path="other2.py", similarity=0.73),
    ]
    conn = _FakeConn([], semantic_rows)
    bc = BedrockClient(client=mock_bedrock)

    result = await build_context(conn, "pid", [_hunk("changed.py")], client=bc)
    similar = [c for c in result if c.relevance_reason == "similar"]
    assert similar[0].similarity == pytest.approx(0.91)
    assert similar[1].similarity == pytest.approx(0.73)


# ── default budget ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_build_context_uses_default_budget_when_unset(mock_bedrock):
    """No token_budget arg → default cap, which still admits typical chunks."""
    same_file_rows = [_row(file_path="f.py", content="x" * 100)]
    conn = _FakeConn(same_file_rows, [])
    bc = BedrockClient(client=mock_bedrock)

    result = await build_context(conn, "pid", [_hunk("f.py")], client=bc)
    # 100-char chunk is well under the 10k-token default
    assert len(result) == 1
    assert DEFAULT_CONTEXT_BUDGET_TOKENS == 10_000
