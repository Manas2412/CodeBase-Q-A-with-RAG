"""Tests for app/review/bedrock_client.py.

No real Bedrock calls — every test uses a mock client (via the
`mock_bedrock` fixture from conftest or a hand-rolled MagicMock for the
backoff tests).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.review.bedrock_client import (
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
        [{"role": "user", "content": [{"text": "ping"}]}]
    )
    assert isinstance(resp, ChatResponse)
    assert resp.text == "mocked review output"
    assert resp.input_tokens == 100
    assert resp.output_tokens == 50
    assert resp.total_tokens == 150
    assert resp.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_chat_passes_through_to_converse_with_correct_inference_config(
    mock_bedrock,
):
    bc = BedrockClient(
        client=mock_bedrock,
        llm_model="us.anthropic.claude-opus-4-6-v1",
    )
    msgs = [{"role": "user", "content": [{"text": "hi"}]}]
    await bc.chat(msgs, max_tokens=8192, temperature=0.3)

    mock_bedrock.converse.assert_called_once()
    call_kwargs = mock_bedrock.converse.call_args.kwargs
    assert call_kwargs["modelId"] == "us.anthropic.claude-opus-4-6-v1"
    assert call_kwargs["messages"] == msgs
    assert call_kwargs["inferenceConfig"] == {
        "maxTokens": 8192,
        "temperature": 0.3,
    }
    # No system param when caller didn't pass one
    assert "system" not in call_kwargs


@pytest.mark.asyncio
async def test_chat_defaults_to_max_output_tokens(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    await bc.chat([{"role": "user", "content": [{"text": "x"}]}])
    assert (
        mock_bedrock.converse.call_args.kwargs["inferenceConfig"]["maxTokens"]
        == MAX_OUTPUT_TOKENS
    )


@pytest.mark.asyncio
async def test_chat_wraps_string_system_prompt_into_converse_shape(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    await bc.chat(
        [{"role": "user", "content": [{"text": "hi"}]}],
        system="You are a senior tech lead.",
    )
    assert mock_bedrock.converse.call_args.kwargs["system"] == [
        {"text": "You are a senior tech lead."}
    ]


@pytest.mark.asyncio
async def test_chat_passes_through_list_system_prompt_untouched(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    system = [{"text": "block 1"}, {"text": "block 2"}]
    await bc.chat(
        [{"role": "user", "content": [{"text": "hi"}]}],
        system=system,
    )
    assert mock_bedrock.converse.call_args.kwargs["system"] == system


@pytest.mark.asyncio
async def test_chat_concatenates_multiple_text_blocks(mock_bedrock):
    mock_bedrock.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "first "}, {"text": "second"}],
            }
        },
        "usage": {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30},
        "stopReason": "end_turn",
    }
    bc = BedrockClient(client=mock_bedrock)
    resp = await bc.chat([{"role": "user", "content": [{"text": "x"}]}])
    assert resp.text == "first second"


@pytest.mark.asyncio
async def test_chat_raises_bedrock_error_on_malformed_response(mock_bedrock):
    mock_bedrock.converse.return_value = {"unexpected": "shape"}
    bc = BedrockClient(client=mock_bedrock)
    with pytest.raises(BedrockError, match="Malformed Converse"):
        await bc.chat([{"role": "user", "content": [{"text": "x"}]}])


# ── embed() ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_embed_returns_vectors_from_invoke_model(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    vectors = await bc.embed(["hello"])
    assert isinstance(vectors, list)
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024


@pytest.mark.asyncio
async def test_embed_with_dict_response_shape(mock_bedrock):
    """When embedding_types=['float'] is honoured, the response nests under .float."""
    embedding = [0.1] * 1024
    mock_bedrock.invoke_model.return_value = {
        "body": MagicMock(
            read=MagicMock(
                return_value=json.dumps({"embeddings": {"float": [embedding]}}).encode()
            )
        )
    }
    bc = BedrockClient(client=mock_bedrock)
    vectors = await bc.embed(["doc"])
    assert vectors == [embedding]


@pytest.mark.asyncio
async def test_embed_empty_returns_empty_without_api_call(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
    assert await bc.embed([]) == []
    mock_bedrock.invoke_model.assert_not_called()


@pytest.mark.asyncio
async def test_embed_passes_input_type_to_payload(mock_bedrock):
    bc = BedrockClient(client=mock_bedrock)
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
        error_response={"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        operation_name="Converse",
    )


def _validation_error() -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": "ValidationException", "Message": "bad input"}},
        operation_name="Converse",
    )


@pytest.mark.asyncio
async def test_with_backoff_retries_then_succeeds(monkeypatch):
    """Two throttles then a success — we get the success value back."""
    # Make sleeps instant in tests
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


async def _instant_sleep(_seconds):
    return None
