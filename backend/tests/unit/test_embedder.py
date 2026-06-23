"""Tests for app/ingestion/embedder.py.

All tests use mock_bedrock — no real Bedrock spend.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.ingestion.chunker import CodeChunk
from app.ingestion.embedder import COHERE_EMBED_MAX_CHARS, embed_chunks, embed_query
from app.review.bedrock_client import EMBED_BATCH_LIMIT, BedrockClient


def _chunk(content: str = "def x(): pass", line: int = 0) -> CodeChunk:
    return CodeChunk(
        content=content,
        context_prefix=f"file.py > x",
        file_path="file.py",
        language="python",
        chunk_type="function",
        name="x",
        start_line=line,
        end_line=line + 1,
    )


def _single_embed_response(dim: int = 1024) -> dict:
    """Cohere flat-list response with `count` 1024-dim vectors of zeros."""
    return {"embeddings": [[0.0] * dim]}


def _make_embed_mock(vectors_per_call: int) -> MagicMock:
    """A boto3-client mock that returns N vectors per invoke_model call."""
    payload = {"embeddings": [[0.0] * 1024] * vectors_per_call}
    stream = MagicMock()
    stream.read.return_value = json.dumps(payload).encode()
    client = MagicMock(name="bedrock-runtime")
    client.invoke_model.return_value = {"body": stream}
    return client


# ── empty input ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_embed_chunks_empty_returns_empty(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    assert await embed_chunks([], client=bc) == []
    mock_bedrock.invoke_model.assert_not_called()


# ── single batch (≤ 96) ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_embed_chunks_small_batch_single_call():
    chunks = [_chunk(line=i) for i in range(5)]
    client = _make_embed_mock(vectors_per_call=5)
    bc = BedrockClient(client=client)

    embeddings = await embed_chunks(chunks, client=bc)

    assert len(embeddings) == 5
    assert all(len(v) == 1024 for v in embeddings)
    # One Bedrock call for ≤96 chunks
    assert client.invoke_model.call_count == 1


# ── batching across the 96-text cap ────────────────────────────────────
@pytest.mark.asyncio
async def test_embed_chunks_splits_at_batch_limit():
    chunks = [_chunk(line=i) for i in range(EMBED_BATCH_LIMIT + 5)]

    # Build a mock that returns the right batch size on each call.
    # First call → 96 vectors, second call → 5 vectors.
    call_count = {"n": 0}

    def _invoke(modelId: str = "", **_kw):
        call_count["n"] += 1
        n = EMBED_BATCH_LIMIT if call_count["n"] == 1 else 5
        payload = {"embeddings": [[float(call_count["n"])] * 1024] * n}
        stream = MagicMock()
        stream.read.return_value = json.dumps(payload).encode()
        return {"body": stream}

    client = MagicMock(name="bedrock-runtime")
    client.invoke_model.side_effect = _invoke
    bc = BedrockClient(client=client)

    embeddings = await embed_chunks(chunks, client=bc)

    assert len(embeddings) == EMBED_BATCH_LIMIT + 5
    # Two batches because 101 > 96
    assert client.invoke_model.call_count == 2
    # First batch's vectors carry the "1.0" marker, second's the "2.0" — order preserved
    assert embeddings[0][0] == 1.0
    assert embeddings[EMBED_BATCH_LIMIT - 1][0] == 1.0
    assert embeddings[EMBED_BATCH_LIMIT][0] == 2.0
    assert embeddings[-1][0] == 2.0


# ── input_type for indexing ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_embed_chunks_uses_search_document_input_type():
    chunks = [_chunk()]
    client = _make_embed_mock(vectors_per_call=1)
    bc = BedrockClient(client=client)

    await embed_chunks(chunks, client=bc)

    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    assert body["input_type"] == "search_document"


# ── text the embedder actually sends ───────────────────────────────────
@pytest.mark.asyncio
async def test_embed_chunks_prefixes_context_in_embed_text():
    """Embed text = `{context_prefix}\\n\\n{content}` — matches old voyage path."""
    chunks = [_chunk(content="def greet(): pass", line=0)]
    client = _make_embed_mock(vectors_per_call=1)
    bc = BedrockClient(client=client)

    await embed_chunks(chunks, client=bc)

    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    assert body["texts"] == ["file.py > x\n\ndef greet(): pass"]


# ── alignment guard ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_embed_chunks_raises_on_misaligned_response():
    """Defensive check — if Bedrock ever returns fewer vectors than texts."""
    chunks = [_chunk(line=i) for i in range(3)]
    # Mock returns 2 vectors for 3 inputs
    client = _make_embed_mock(vectors_per_call=2)
    bc = BedrockClient(client=client)

    with pytest.raises(RuntimeError, match="2 vectors for 3 chunks"):
        await embed_chunks(chunks, client=bc)


# ── per-text truncation (Cohere v3 max 2048 chars) ─────────────────────
@pytest.mark.asyncio
async def test_embed_chunks_truncates_oversized_text_to_max_chars():
    """Cohere Embed v3 rejects texts >2048 chars with ValidationException.

    The embedder truncates at the tail so the context_prefix + signature
    + first body lines (the highest-signal bits for retrieval) survive.
    """
    # Build a chunk whose total embed text comfortably exceeds 2048 chars.
    long_body = "x = 1\n" * 1000  # ~6000 chars, well over the cap
    chunks = [
        CodeChunk(
            content=long_body,
            context_prefix="file.py > big_function",
            file_path="file.py",
            language="python",
            chunk_type="function",
            name="big_function",
            start_line=0,
            end_line=999,
        )
    ]
    client = _make_embed_mock(vectors_per_call=1)
    bc = BedrockClient(client=client)

    await embed_chunks(chunks, client=bc)

    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    sent_text = body["texts"][0]
    assert len(sent_text) == COHERE_EMBED_MAX_CHARS, (
        f"expected truncation to exactly {COHERE_EMBED_MAX_CHARS} chars, "
        f"got {len(sent_text)}"
    )
    # The prefix MUST survive truncation (it lives at the head)
    assert sent_text.startswith("file.py > big_function")


@pytest.mark.asyncio
async def test_embed_chunks_leaves_short_text_untouched():
    """Texts ≤2048 chars must NOT be truncated."""
    chunks = [
        CodeChunk(
            content="def small(): return 1",
            context_prefix="file.py > small",
            file_path="file.py",
            language="python",
            chunk_type="function",
            name="small",
            start_line=0,
            end_line=1,
        )
    ]
    client = _make_embed_mock(vectors_per_call=1)
    bc = BedrockClient(client=client)

    await embed_chunks(chunks, client=bc)

    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    sent_text = body["texts"][0]
    assert sent_text == "file.py > small\n\ndef small(): return 1"
    assert len(sent_text) < COHERE_EMBED_MAX_CHARS


# ── embed_query ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_embed_query_uses_search_query_input_type():
    client = _make_embed_mock(vectors_per_call=1)
    bc = BedrockClient(client=client)

    vector = await embed_query("how does the polling agent work?", client=bc)

    assert len(vector) == 1024
    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    assert body["input_type"] == "search_query"
    assert body["texts"] == ["how does the polling agent work?"]


@pytest.mark.asyncio
async def test_embed_query_truncates_oversized_input():
    """Regression: a real diff's joined `added_lines` easily exceeds 2048
    chars. Cohere returns ValidationException above the cap, so embed_query
    must truncate at the boundary just like embed_chunks does.

    The bug that surfaced this: context_builder.build_context joined every
    added line across hunks into a single embed_query call (~6000 chars
    for a normal feature commit), which Bedrock rejected outright —
    poisoning every poll's review_push_task.
    """
    oversized = "added line " * 500  # ~6000 chars, well over 2048
    assert len(oversized) > COHERE_EMBED_MAX_CHARS

    client = _make_embed_mock(vectors_per_call=1)
    bc = BedrockClient(client=client)

    vector = await embed_query(oversized, client=bc)

    assert len(vector) == 1024
    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    sent_text = body["texts"][0]
    assert len(sent_text) == COHERE_EMBED_MAX_CHARS
    # Head survives (highest-signal bit for ANN)
    assert sent_text.startswith("added line ")


@pytest.mark.asyncio
async def test_embed_query_leaves_short_input_untouched():
    """Below the cap → passed through verbatim."""
    short = "diff context: refactor handler"
    client = _make_embed_mock(vectors_per_call=1)
    bc = BedrockClient(client=client)

    await embed_query(short, client=bc)

    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    assert body["texts"] == [short]
