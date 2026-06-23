"""Async wrapper around AWS Bedrock for the review pipeline.

Two operations the pipeline uses:
  • chat()  → Claude Opus 4.6 via `invoke_model` + Anthropic Messages API
  • embed() → Cohere Embed Multilingual v3 via `invoke_model`

Why `invoke_model` + Anthropic format instead of the newer Converse API:
  • Wider compatibility — older API, fewer rollout gaps across regions.
  • First-class support for Bedrock prompt caching via
    `cache_control: {"type": "ephemeral"}` blocks — gives a 90% discount
    on the cached input tokens, which is significant for the review
    pipeline where the checklist prefix repeats across every call.
  • Matches the pattern already proven in our other production Bedrock
    project (PQ-Panel) — same call shape, same error envelope.

Other design notes:
  • max_tokens is a module-level constant (MAX_OUTPUT_TOKENS = 16384),
    NOT an env var. Bedrock bills on actual output, so a high ceiling is
    free — it just prevents review truncation on big diffs. Plan v3.3 §4.
  • boto3 is synchronous; we marshal calls onto a thread via
    asyncio.to_thread() so async callers can await them.
  • Retries: exponential backoff with jitter on known-transient Bedrock
    error codes (Throttling, ServiceUnavailable, InternalServer, etc.).
  • Embed batch cap: Cohere's Bedrock API rejects >96 texts per call.
    We enforce the cap and let the caller batch upstream.
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
#: Default output cap for every chat() call. Plan §4.1, §4.3.
MAX_OUTPUT_TOKENS: int = 16384

#: Cohere Embed Multilingual v3 hard limit per InvokeModel call.
EMBED_BATCH_LIMIT: int = 96

DEFAULT_LLM_MODEL = "us.anthropic.claude-opus-4-6-v1"
DEFAULT_EMBED_MODEL = "cohere.embed-multilingual-v3"
DEFAULT_REGION = "us-east-1"

#: Required `anthropic_version` field on Bedrock-hosted Claude calls.
ANTHROPIC_BEDROCK_VERSION = "bedrock-2023-05-31"

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
    """Structured Anthropic Messages reply with token usage attached.

    `cache_read_tokens` + `cache_creation_tokens` are populated when
    prompt caching is in play (`cache_prefix` was passed to chat()).
    They're 0 otherwise.
    """

    text: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    stop_reason: str
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


# ── Backoff helper ───────────────────────────────────────────────────────
T = TypeVar("T")


async def _with_backoff(
    func: Callable[[], T],
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
) -> T:
    """Run a sync boto3 call in a worker thread with exponential backoff.

    Non-retryable codes raise BedrockError immediately. Retryable codes
    that still fail after `max_attempts` also raise BedrockError, with
    the last underlying ClientError attached as `__cause__`.
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
        """Lazy-construct the boto3 bedrock-runtime client.

        Credentials are read from env (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
        if present and passed explicitly to boto3 — mirrors the pattern used in
        our sibling PQ-Bot production service. If the env vars aren't set,
        boto3 falls through to its standard credential chain (IAM role on
        EC2/ECS, ~/.aws/credentials, etc.), so this works in containers too.
        """
        if self._client is None:
            client_kwargs: dict[str, Any] = {"region_name": self.region}
            ak = os.getenv("AWS_ACCESS_KEY_ID")
            sk = os.getenv("AWS_SECRET_ACCESS_KEY")
            if ak and sk:
                client_kwargs["aws_access_key_id"] = ak
                client_kwargs["aws_secret_access_key"] = sk
            self._client = boto3.client("bedrock-runtime", **client_kwargs)
        return self._client

    # ── Chat (invoke_model + Anthropic Messages API) ─────────────────────
    async def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int = MAX_OUTPUT_TOKENS,
        temperature: float = 0.0,
        system: str | None = None,
        cache_prefix: str | None = None,
    ) -> ChatResponse:
        """Call Bedrock with Anthropic Messages API; return text + token usage.

        Parameters
        ----------
        messages : list[dict]
            Anthropic-format messages:
                [{"role": "user", "content": [{"type": "text", "text": "..."}]}]
            OR the short-hand form (we promote it):
                [{"role": "user", "content": "..."}]
        max_tokens : int
            Output cap. Defaults to MAX_OUTPUT_TOKENS (16384).
        temperature : float
            0.0 for deterministic review output (recommended).
        system : str | None
            Optional system prompt. Inserted as top-level `system` field.
        cache_prefix : str | None
            If provided, the prefix is sent as a separate content block
            with `cache_control: {"type": "ephemeral"}` so Bedrock caches
            its tokens. Subsequent calls within the 5-minute TTL hit the
            cache at 10% of input price (90% discount). Use for the
            checklist + system block — anything that's identical across
            many calls within the same review/poll cycle.
        """
        # Build messages — promote simple strings into the typed-block shape
        # so the rest of the body assembly is uniform.
        normalised: list[dict] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                normalised.append(
                    {"role": msg["role"], "content": [{"type": "text", "text": content}]}
                )
            else:
                normalised.append(msg)

        # If cache_prefix is set, splice it as the first content block of
        # the first user message with cache_control marker. (Anthropic
        # supports cache markers anywhere in content; we put it first so
        # the cached prefix is at the start of the input.)
        if cache_prefix and normalised:
            first = normalised[0]
            existing = first.get("content", [])
            first["content"] = [
                {
                    "type": "text",
                    "text": cache_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                *existing,
            ]

        body: dict[str, Any] = {
            "anthropic_version": ANTHROPIC_BEDROCK_VERSION,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": normalised,
        }
        if system is not None:
            body["system"] = system

        body_json = json.dumps(body)

        response = await _with_backoff(
            lambda: self.client.invoke_model(
                modelId=self.llm_model,
                body=body_json,
                contentType="application/json",
                accept="application/json",
            )
        )

        try:
            payload = json.loads(response["body"].read())
        except (KeyError, AttributeError, json.JSONDecodeError) as e:
            raise BedrockError(f"Malformed invoke_model response: {response!r}") from e

        # Anthropic response shape:
        #   {"id": ..., "type": "message", "role": "assistant",
        #    "content": [{"type": "text", "text": "..."}],
        #    "stop_reason": "end_turn",
        #    "usage": {"input_tokens": N, "output_tokens": N,
        #              "cache_creation_input_tokens": N,  # if cache write
        #              "cache_read_input_tokens": N}}     # if cache hit
        content_blocks = payload.get("content") or []
        text = "".join(
            b.get("text", "") for b in content_blocks if b.get("type") == "text"
        )
        if not isinstance(text, str):
            raise BedrockError(f"Couldn't extract text from response: {payload!r}")

        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cache_read = int(usage.get("cache_read_input_tokens", 0))
        cache_creation = int(usage.get("cache_creation_input_tokens", 0))

        return ChatResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            stop_reason=str(payload.get("stop_reason", "")),
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
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
