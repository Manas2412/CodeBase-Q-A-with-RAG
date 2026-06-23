"""Embedding facade for the ingestion pipeline.

Wraps BedrockClient.embed() with the batching the ingestion side needs:
  • Splits the chunk list into ≤96-text batches (Cohere Embed v3 cap)
  • Builds the text-to-embed for each chunk as
      f"{context_prefix}\n\n{content}"
    so retrieval matches against the chunk's full signature, not just its
    raw body.
  • Truncates each text to Cohere's 2048-character per-text limit. For
    long functions the truncated tail is mostly implementation details;
    the signature, docstring, and first few lines (the highest-signal
    parts for semantic retrieval) survive.
  • Returns a parallel list of 1024-dim embeddings in the same order as
    the input chunks. Callers (indexer) zip them together with the chunks
    when upserting into pgvector.

Failures bubble up as BedrockError; the worker catches them and marks the
project status='error'.
"""

from __future__ import annotations

import logging
from typing import Iterable

from app.ingestion.chunker import CodeChunk
from app.review.bedrock_client import (
    EMBED_BATCH_LIMIT,
    BedrockClient,
    get_bedrock_client,
)


log = logging.getLogger(__name__)


#: Cohere Embed Multilingual v3 hard limit on each text in the input array
#: (a character count, not tokens). Anything longer makes Bedrock return a
#: ValidationException listing which texts overflowed.
COHERE_EMBED_MAX_CHARS: int = 2048


def _chunk_text(chunk: CodeChunk) -> str:
    """The actual string we embed.

    Prefixing with the context_prefix (file_path > class > function) means
    cosine similarity picks up structural hints (file/class/function names)
    alongside the raw code body. Voyage's voyage-code-2 also benefited from
    this layout in the old code path; Cohere Embed v3 responds similarly.

    Truncates to Cohere's 2048-character per-text limit. Truncation
    happens at the tail so the prefix + signature + start of body always
    survive (the highest-signal bits for retrieval).
    """
    text = f"{chunk.context_prefix}\n\n{chunk.content}"
    if len(text) > COHERE_EMBED_MAX_CHARS:
        log.info(
            "embedder: truncating %s (%d → %d chars) at %s:%d",
            chunk.name or chunk.chunk_type,
            len(text),
            COHERE_EMBED_MAX_CHARS,
            chunk.file_path,
            chunk.start_line,
        )
        text = text[:COHERE_EMBED_MAX_CHARS]
    return text


def _batches(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def embed_chunks(
    chunks: list[CodeChunk],
    *,
    client: BedrockClient | None = None,
) -> list[list[float]]:
    """Embed every chunk in `chunks`, preserving order.

    Returns a list of 1024-dim float vectors aligned with the input.

    Empty input → empty list, no API call.

    Parameters
    ----------
    chunks : list[CodeChunk]
        From `app/ingestion/chunker.chunk_file()`.
    client : BedrockClient | None
        Inject a pre-configured client (useful in tests). Defaults to the
        module-level singleton (real Bedrock).
    """
    if not chunks:
        return []

    bc = client or get_bedrock_client()
    embeddings: list[list[float]] = []

    for batch in _batches([_chunk_text(c) for c in chunks], EMBED_BATCH_LIMIT):
        result = await bc.embed(batch, input_type="search_document")
        embeddings.extend(result)

    if len(embeddings) != len(chunks):
        # Shouldn't happen — Bedrock returns one vector per text — but if
        # it ever does we want a hard fail at index time, not a silent
        # mis-aligned write.
        raise RuntimeError(
            f"Embedder returned {len(embeddings)} vectors for {len(chunks)} chunks"
        )

    return embeddings


async def embed_query(
    text: str,
    *,
    client: BedrockClient | None = None,
) -> list[float]:
    """Embed a single query string with input_type='search_query'.

    Used at retrieval time. The different input_type tells Cohere this is
    a query, not a document — and the resulting embedding lives in the
    query side of the search space.
    """
    bc = client or get_bedrock_client()
    result = await bc.embed([text], input_type="search_query")
    return result[0]
