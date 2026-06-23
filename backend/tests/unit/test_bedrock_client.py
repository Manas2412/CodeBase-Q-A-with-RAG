"""Tests for app/review/bedrock_client.py.

All tests use the `mock_bedrock` fixture (or a hand-rolled MagicMock for
the backoff tests). Zero real Bedrock calls — zero AWS spend.

The wrapper uses `invoke_model` for both chat (Anthropic Messages API
body) and embed (Cohere body). The mock_bedrock fixture dispatches on
the model id prefix so the same client serves both call sites.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.review.bedrock_client import (
    ANTHROPIC_BEDROCK_VERSION,
    EMBED_BATCH_LIMIT,
    MAX_OUTPUT_TOKENS,
    BedrockClient,
    BedrockError,
    ChatResponse,
    _with_backoff,
)


# ── chat() ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_chat_returns_shaped_chat_response(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    resp = await bc.chat(
        [{"role": "user", "content": [{"type": "text", "text": "ping"}]}]
    )
    assert isinstance(resp, ChatResponse)
    assert resp.text == "mocked review output"
    assert resp.input_tokens == 100
    assert resp.output_tokens == 50
    assert resp.total_tokens == 150
    assert resp.stop_reason == "end_turn"
    assert resp.cache_read_tokens == 0
    assert resp.cache_creation_tokens == 0


@pytest.mark.asyncio
async def test_chat_invokes_model_with_anthropic_body(mock_bedrock):
    bc = BedrockClient(
        client=mock_bedrock,
        llm_model="us.anthropic.claude-opus-4-6-v1",
    )
    await bc.chat(
        [{"role": "user", "content": "hi"}],
        max_tokens=8192,
        temperature=0.3,
    )

    mock_bedrock.invoke_model.assert_called_once()
    call_kwargs = mock_bedrock.invoke_model.call_args.kwargs
    assert call_kwargs["modelId"] == "us.anthropic.claude-opus-4-6-v1"
    assert call_kwargs["contentType"] == "application/json"
    assert call_kwargs["accept"] == "application/json"

    body = json.loads(call_kwargs["body"])
    assert body["anthropic_version"] == ANTHROPIC_BEDROCK_VERSION
    assert body["max_tokens"] == 8192
    assert body["temperature"] == 0.3
    assert body["messages"][0]["role"] == "user"
    # string content gets promoted to typed block list
    assert body["messages"][0]["content"] == [{"type": "text", "text": "hi"}]
    # no system field when caller didn't provide one
    assert "system" not in body


@pytest.mark.asyncio
async def test_chat_defaults_to_max_output_tokens(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    await bc.chat([{"role": "user", "content": "x"}])
    body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])
    assert body["max_tokens"] == MAX_OUTPUT_TOKENS


@pytest.mark.asyncio
async def test_chat_includes_system_prompt_as_top_level_field(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    await bc.chat(
        [{"role": "user", "content": "hi"}],
        system="You are a senior tech lead.",
    )
    body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])
    assert body["system"] == "You are a senior tech lead."


@pytest.mark.asyncio
async def test_chat_cache_prefix_marks_first_block_ephemeral(mock_bedrock):
    """cache_prefix → first content block carries cache_control marker."""
    bc = BedrockClient(client=mock_bedrock)
    await bc.chat(
        [{"role": "user", "content": "the actual question"}],
        cache_prefix="STATIC CHECKLIST RULES — same for every call",
    )
    body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])
    blocks = body["messages"][0]["content"]
    assert len(blocks) == 2, "expected prefix + original content blocks"
    assert blocks[0]["text"] == "STATIC CHECKLIST RULES — same for every call"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "the actual question"
    # Second block must NOT carry cache_control — only the prefix does
    assert "cache_control" not in blocks[1]


@pytest.mark.asyncio
async def test_chat_reports_cache_token_usage_when_present(mock_bedrock):
    """When Anthropic reports cache hits, ChatResponse carries them."""
    cache_payload = {
        "id": "msg_test_002",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "cached"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 20,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 1500,
        },
    }
    stream = MagicMock()
    stream.read.return_value = json.dumps(cache_payload).encode()
    mock_bedrock.invoke_model.side_effect = None  # disable default routing
    mock_bedrock.invoke_model.return_value = {"body": stream}

    bc = BedrockClient(client=mock_bedrock)
    resp = await bc.chat([{"role": "user", "content": "x"}])

    assert resp.cache_read_tokens == 1500
    assert resp.cache_creation_tokens == 0


@pytest.mark.asyncio
async def test_chat_concatenates_multiple_text_blocks(mock_bedrock):
    multi_payload = {
        "id": "msg_test_003",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "first "},
            {"type": "text", "text": "second"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    stream = MagicMock()
    stream.read.return_value = json.dumps(multi_payload).encode()
    mock_bedrock.invoke_model.side_effect = None
    mock_bedrock.invoke_model.return_value = {"body": stream}

    bc = BedrockClient(client=mock_bedrock)
    resp = await bc.chat([{"role": "user", "content": "x"}])
    assert resp.text == "first second"


@pytest.mark.asyncio
async def test_chat_raises_bedrock_error_when_body_is_not_json(mock_bedrock):
    """Non-JSON body (e.g. truncated upstream) surfaces as BedrockError, not raw JSONDecodeError."""
    stream = MagicMock()
    stream.read.return_value = b"<html>not json</html>"
    mock_bedrock.invoke_model.side_effect = None
    mock_bedrock.invoke_model.return_value = {"body": stream}

    bc = BedrockClient(client=mock_bedrock)
    with pytest.raises(BedrockError, match="Malformed"):
        await bc.chat([{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_chat_promotes_string_content_to_typed_block(mock_bedrock):
    """Short-hand `content: "..."` becomes [{type: text, text: "..."}]."""
    bc = BedrockClient(client=mock_bedrock)
    await bc.chat([{"role": "user", "content": "shorthand"}])
    body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])
    assert body["messages"][0]["content"] == [
        {"type": "text", "text": "shorthand"}
    ]


# ── embed() ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_embed_returns_vectors_from_invoke_model(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    vectors = await bc.embed(["hello"])
    assert isinstance(vectors, list)
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024


@pytest.mark.asyncio
async def test_embed_handles_dict_response_shape(mock_bedrock):
    """When embedding_types=['float'] is honoured, response nests under .float."""
    nested_payload = {"embeddings": {"float": [[0.1] * 1024]}}
    stream = MagicMock()
    stream.read.return_value = json.dumps(nested_payload).encode()
    mock_bedrock.invoke_model.side_effect = None
    mock_bedrock.invoke_model.return_value = {"body": stream}

    bc = BedrockClient(client=mock_bedrock)
    vectors = await bc.embed(["doc"])
    assert vectors == [[0.1] * 1024]


@pytest.mark.asyncio
async def test_embed_empty_returns_empty_without_api_call(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    assert await bc.embed([]) == []
    mock_bedrock.invoke_model.assert_not_called()


@pytest.mark.asyncio
async def test_embed_passes_input_type_to_payload(mock_bedrock):
    bc = BedrockClient(
        client=mock_bedrock,
        embed_model="cohere.embed-multilingual-v3",
    )
    await bc.embed(["q"], input_type="search_query")
    body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])
    assert body["input_type"] == "search_query"
    assert body["texts"] == ["q"]
    assert body["embedding_types"] == ["float"]


@pytest.mark.asyncio
async def test_embed_raises_over_batch_limit(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    with pytest.raises(BedrockError, match=f"up to {EMBED_BATCH_LIMIT}"):
        await bc.embed(["x"] * (EMBED_BATCH_LIMIT + 1))
    mock_bedrock.invoke_model.assert_not_called()


# ── Backoff ─────────────────────────────────────────────────────────────
def _throttling_error() -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": "ThrottlingException", "Message": "slow"}},
        operation_name="InvokeModel",
    )


def _validation_error() -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": "ValidationException", "Message": "bad"}},
        operation_name="InvokeModel",
    )


async def _instant_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_with_backoff_retries_then_succeeds(monkeypatch):
    """Two throttles then a success — we get the success value back."""
    import app.review.bedrock_client as bc_mod
    monkeypatch.setattr(bc_mod.asyncio, "sleep", _instant_sleep)

    attempts = {"n": 0}

    def func():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _throttling_error()
        return "ok"

    result = await _with_backoff(func, max_attempts=5, base_delay=0.0)
    assert result == "ok"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_with_backoff_raises_after_max_attempts(monkeypatch):
    import app.review.bedrock_client as bc_mod
    monkeypatch.setattr(bc_mod.asyncio, "sleep", _instant_sleep)

    attempts = {"n": 0}

    def func():
        attempts["n"] += 1
        raise _throttling_error()

    with pytest.raises(BedrockError, match="ThrottlingException"):
        await _with_backoff(func, max_attempts=3, base_delay=0.0)
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_with_backoff_does_not_retry_non_retryable(monkeypatch):
    """ValidationException is a caller bug, not a transient issue. Bail immediately."""
    import app.review.bedrock_client as bc_mod
    monkeypatch.setattr(bc_mod.asyncio, "sleep", _instant_sleep)

    attempts = {"n": 0}

    def func():
        attempts["n"] += 1
        raise _validation_error()

    with pytest.raises(BedrockError, match="ValidationException"):
        await _with_backoff(func, max_attempts=5, base_delay=0.0)
    assert attempts["n"] == 1
