"""Async wrapper around AWS Bedrock for the review pipeline.

Two operations the pipeline uses:
  • chat()  → Claude Opus 4.6 via the Converse API for review generation
  • embed() → Cohere Embed Multilingual v3 via InvokeModel for indexing
              and query embeddings

Design notes:
  • max_tokens is a module-level constant (MAX_OUTPUT_TOKENS = 16384),
    NOT an env var. Bedrock charges per actual output, so a high ceiling
    is free — it just prevents review truncation on large diffs.
    Plan v3.3 §4 covers the rationale.
  • boto3 is synchronous; we marshal calls onto a thread via
    asyncio.to_thread() so async callers can await them.
  • Retries: exponential backoff with jitter on the known-transient
    Bedrock error codes (ThrottlingException, ServiceUnavailableException,
    InternalServerException).
  • Embeddings: Cohere's Bedrock API caps at 96 texts per call. We enforce
    the cap and let the caller batch.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import boto3
from botocore.exceptions import ClientError


# ── Constants ────────────────────────────────────────────────────────────
#: Output cap for every Converse call. See plan §4.1, §4.3.
MAX_OUTPUT_TOKENS: int = 16384

#: Cohere Embed Multilingual v3 hard limit per InvokeModel call.
EMBED_BATCH_LIMIT: int = 96

DEFAULT_LLM_MODEL = "us.anthropic.claude-opus-4-6-v1"
DEFAULT_EMBED_MODEL = "cohere.embed-multilingual-v3"
DEFAULT_REGION = "us-east-1"

# Bedrock error codes we treat as transient and retry.
_RETRYABLE_ERRORS: set[str] = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelStreamErrorException",
    "ModelTimeoutException",
}


class BedrockError(Exception):
    """Anything the wrapper can't recover from — auth, validation, exhausted retries."""


@dataclass(frozen=True)
class ChatResponse:
    """Structured Converse reply with token usage attached."""

    text: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    stop_reason: str


# ── Backoff helper ───────────────────────────────────────────────────────
T = TypeVar("T")


async def _with_backoff(
    func: Callable[[], T],
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
) -> T:
    """Run a sync boto3 call in a worker thread with exponential backoff.

    Sleeps `base_delay * 2**attempt * (0.5 + random)` between retries — the
    jitter prevents thundering-herd retries when many workers hit the same
    Bedrock rate limit at once.

    Non-retryable errors raise BedrockError immediately. Retryable errors
    that still fail after `max_attempts` also raise BedrockError, carrying
    the last underlying ClientError as `__cause__`.
    """
    last_error: ClientError | None = None
    for attempt in range(max_attempts):
        try:
            return await asyncio.to_thread(func)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code not in _RETRYABLE_ERRORS:
                raise BedrockError(f"Bedrock call failed ({code}): {e}") from e
            last_error = e
            if attempt == max_attempts - 1:
                break
            delay = base_delay * (2**attempt) * (0.5 + random.random())
            await asyncio.sleep(delay)
    raise BedrockError(
        f"Bedrock call failed after {max_attempts} attempts (last: "
        f"{last_error.response.get('Error', {}).get('Code') if last_error else '?'})"
    ) from last_error


# ── Client ───────────────────────────────────────────────────────────────
class BedrockClient:
    """Thin async wrapper around boto3's bedrock-runtime client.

    Reads model IDs and region from env (BEDROCK_LLM_MODEL,
    BEDROCK_EMBED_MODEL, AWS_REGION) unless explicitly overridden.

    For tests, inject a pre-configured boto3-shaped client (e.g. the
    `mock_bedrock` fixture from conftest) via the `client=` kwarg.
    """

    def __init__(
        self,
        *,
        llm_model: str | None = None,
        embed_model: str | None = None,
        region: str | None = None,
        client: Any | None = None,
    ):
        self.llm_model = llm_model or os.getenv("BEDROCK_LLM_MODEL", DEFAULT_LLM_MODEL)
        self.embed_model = embed_model or os.getenv(
            "BEDROCK_EMBED_MODEL", DEFAULT_EMBED_MODEL
        )
        self.region = region or os.getenv("AWS_REGION", DEFAULT_REGION)
        # Lazy-construct so unit tests that inject a mock don't need real AWS creds.
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    # ── Chat (Converse) ─────────────────────────────────────────────────
    async def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int = MAX_OUTPUT_TOKENS,
        temperature: float = 0.0,
        system: str | list[dict] | None = None,
    ) -> ChatResponse:
        """Call Bedrock Converse and return text + token usage.

        Parameters
        ----------
        messages : list[dict]
            Converse-format messages. Shape:
                [{"role": "user", "content": [{"text": "..."}]}, ...]
        max_tokens : int
            Output cap. Defaults to MAX_OUTPUT_TOKENS (16384).
        temperature : float
            0.0 for deterministic review output (recommended).
        system : str | list[dict] | None
            Optional system prompt. Plain string is wrapped to the
            Converse-required [{"text": ...}] shape.
        """
        kwargs: dict[str, Any] = {
            "modelId": self.llm_model,
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system is not None:
            if isinstance(system, str):
                kwargs["system"] = [{"text": system}]
            else:
                kwargs["system"] = system

        response = await _with_backoff(lambda: self.client.converse(**kwargs))

        try:
            content = response["output"]["message"]["content"]
            # Concatenate all text blocks — Converse can return tool-use or
            # multiple text blocks; we just want the readable answer.
            text = "".join(block.get("text", "") for block in content)
        except (KeyError, TypeError, IndexError) as e:
            raise BedrockError(f"Malformed Converse response: {response!r}") from e

        usage = response.get("usage", {}) or {}
        return ChatResponse(
            text=text,
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            total_tokens=int(usage.get("totalTokens", 0)),
            stop_reason=str(response.get("stopReason", "")),
        )

    # ── Embed (InvokeModel: Cohere Embed v3) ────────────────────────────
    async def embed(
        self,
        texts: list[str],
        *,
        input_type: str = "search_document",
    ) -> list[list[float]]:
        """Embed up to 96 texts in one call. Returns 1024-dim vectors.

        Cohere Embed v3 input_type:
          • 'search_document' — for chunks at indexing time
          • 'search_query'    — for queries at retrieval time
        Using the wrong one degrades retrieval noticeably.
        """
        if not texts:
            return []
        if len(texts) > EMBED_BATCH_LIMIT:
            raise BedrockError(
                f"Cohere Embed v3 accepts up to {EMBED_BATCH_LIMIT} texts per call "
                f"(got {len(texts)}). Batch upstream."
            )

        body = json.dumps(
            {
                "texts": texts,
                "input_type": input_type,
                "embedding_types": ["float"],
            }
        )

        response = await _with_backoff(
            lambda: self.client.invoke_model(modelId=self.embed_model, body=body)
        )

        try:
            payload = json.loads(response["body"].read())
        except (KeyError, AttributeError, json.JSONDecodeError) as e:
            raise BedrockError(f"Malformed InvokeModel response: {response!r}") from e

        # Cohere's response shape depends on whether embedding_types was set.
        # With embedding_types=["float"]:  {"embeddings": {"float": [[...]]}}
        # Without it:                       {"embeddings": [[...]]}
        embeddings = payload.get("embeddings")
        if isinstance(embeddings, dict):
            embeddings = embeddings.get("float", [])
        if not isinstance(embeddings, list):
            raise BedrockError(f"Unexpected embeddings shape: {payload!r}")
        return embeddings


# ── Module-level lazy singleton ──────────────────────────────────────────
_default_client: BedrockClient | None = None


def get_bedrock_client() -> BedrockClient:
    """Return a process-wide BedrockClient. Constructs lazily on first call.

    Tests should instantiate `BedrockClient(client=mock_bedrock)` directly
    instead of using this helper.
    """
    global _default_client
    if _default_client is None:
        _default_client = BedrockClient()
    return _default_client


def reset_default_client() -> None:
    """Test hook — force re-construction of the singleton on next get."""
    global _default_client
    _default_client = None
