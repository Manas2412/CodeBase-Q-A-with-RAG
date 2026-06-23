"""Context builder for the review pipeline.

Given a set of `DiffHunk`s, returns the most relevant code chunks from
pgvector so the reviewer LLM can ground its findings in surrounding code.

Strategy
========

1. **Same-file chunks** — for every file touched by the diff, pull every
   existing chunk in that file from pgvector. This is the highest-signal
   context: the reviewer sees the rest of the function/class bodies in
   the changed files.

2. **Semantic neighbours** — embed the added lines of the diff via Cohere
   Embed v3 (query mode), then ANN-search pgvector for the top-K most
   similar chunks across OTHER files. Surfaces callers, tests, related
   utilities — the things the LLM should consider but that aren't in the
   files being reviewed.

3. **Token budget** — pack chunks greedily into a token cap (default 10K
   input tokens). Priority: same-file first (sorted by start_line), then
   semantic neighbours by descending similarity. Chunks that don't fit get
   skipped — we keep filling smaller ones rather than truncating in the
   middle of a function.

The output is a `ContextChunk` list ready for the prompt builder (Day 5).
Caller is responsible for the prompt assembly; this module just retrieves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import asyncpg
from pgvector.asyncpg import register_vector

from app.review.bedrock_client import BedrockClient
from app.review.diff_parser import DiffHunk

# NOTE: `embed_query` is imported lazily inside build_context() to break
# a circular import. Chain: any importer of `app.review` triggers this
# module's load, which used to do `from app.ingestion.embedder import
# embed_query` — but embedder.py imports from `app.review.bedrock_client`,
# which itself touches `app.review/__init__.py`. The lazy import keeps
# the module-level graph acyclic; the runtime call still works the same.


#: Default per-review context budget. ~10K input tokens is well within
#: Opus' 200K window but tight enough to keep cost + attention focused.
#: Adjustable via the `token_budget` kwarg per call.
DEFAULT_CONTEXT_BUDGET_TOKENS: int = 10_000

#: Rough chars→tokens approximation. Closer to ~4 for English, ~3.5 for
#: code. Conservative side keeps us under budget.
CHARS_PER_TOKEN: int = 4

#: How many semantic neighbours to ANN-fetch before budget-trimming. We
#: over-fetch so the budget step has options when chunks vary in size.
DEFAULT_SEMANTIC_TOP_K: int = 30


@dataclass(frozen=True)
class ContextChunk:
    """One retrieved chunk with relevance metadata.

    Fields mirror what the prompt builder needs to format an LLM-ready
    context block: file/line provenance plus the body itself.
    """

    file_path: str
    name: str | None
    chunk_type: str
    start_line: int
    end_line: int
    content: str
    relevance_reason: str   # 'same_file' | 'similar'
    similarity: float       # 1.0 for same-file (always relevant); else cosine
    token_estimate: int


def _estimate_tokens(text: str) -> int:
    return max(len(text) // CHARS_PER_TOKEN, 1)


async def build_context(
    conn: asyncpg.Connection,
    project_id: str,
    hunks: Sequence[DiffHunk],
    *,
    token_budget: int = DEFAULT_CONTEXT_BUDGET_TOKENS,
    semantic_top_k: int = DEFAULT_SEMANTIC_TOP_K,
    client: BedrockClient | None = None,
) -> list[ContextChunk]:
    """Retrieve relevant chunks from pgvector for `hunks`.

    Parameters
    ----------
    conn
        Live asyncpg connection. Caller is responsible for opening +
        closing it.
    project_id
        UUID of the project whose chunks we're searching.
    hunks
        The diff to find context for. Empty input → empty output.
    token_budget
        Max combined token estimate of returned chunks. Same-file
        chunks have priority; semantic neighbours fill the rest.
    semantic_top_k
        How many ANN candidates to fetch from pgvector before
        budget trimming. Default 30 gives the budget step room.
    client
        Optional BedrockClient for embed_query. Defaults to the
        module-level singleton (real Bedrock).

    Returns
    -------
    list[ContextChunk]
        Ordered: same-file first (by file_path, start_line), then
        semantic by descending similarity. Total token_estimate sum
        ≤ token_budget.
    """
    if not hunks:
        return []

    # Register pgvector codec — idempotent if already done by the caller's
    # pool init. Swallow exceptions so test mocks (which don't have the
    # codec machinery) don't break.
    try:
        await register_vector(conn)
    except Exception:
        pass

    changed_files = sorted({h.file_path for h in hunks})

    # ── 1. Same-file chunks ────────────────────────────────────────────
    same_rows = await conn.fetch(
        """
        SELECT file_path, name, chunk_type, start_line, end_line, content
          FROM chunks
         WHERE project_id = $1::uuid
           AND file_path = ANY($2::text[])
         ORDER BY file_path, start_line
        """,
        project_id,
        changed_files,
    )
    same_file_chunks = [
        ContextChunk(
            file_path=r["file_path"],
            name=r["name"],
            chunk_type=r["chunk_type"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            content=r["content"],
            relevance_reason="same_file",
            similarity=1.0,
            token_estimate=_estimate_tokens(r["content"]),
        )
        for r in same_rows
    ]

    # ── 2. Semantic neighbours (other files only) ──────────────────────
    # Build the query embedding from the added lines of every hunk.
    # If there are no added lines (deletions only), skip the ANN search.
    added_text = "\n".join(
        line for h in hunks for line in h.added_lines if line.strip()
    )

    semantic_chunks: list[ContextChunk] = []
    if added_text:
        # Lazy import — see module-level comment for the circular-import rationale.
        from app.ingestion.embedder import embed_query

        query_vec = await embed_query(added_text, client=client)
        semantic_rows = await conn.fetch(
            """
            SELECT file_path, name, chunk_type, start_line, end_line, content,
                   1 - (embedding <=> $1::vector) AS similarity
              FROM chunks
             WHERE project_id = $2::uuid
               AND NOT (file_path = ANY($3::text[]))
               AND embedding IS NOT NULL
             ORDER BY embedding <=> $1::vector
             LIMIT $4
            """,
            query_vec,
            project_id,
            changed_files,
            semantic_top_k,
        )
        semantic_chunks = [
            ContextChunk(
                file_path=r["file_path"],
                name=r["name"],
                chunk_type=r["chunk_type"],
                start_line=r["start_line"],
                end_line=r["end_line"],
                content=r["content"],
                relevance_reason="similar",
                similarity=float(r["similarity"]),
                token_estimate=_estimate_tokens(r["content"]),
            )
            for r in semantic_rows
        ]

    # ── 3. Greedy budget pack ──────────────────────────────────────────
    # Same-file always has priority. Within each group, the SQL ORDER BY
    # already sorted: same-file by (file_path, start_line), semantic by
    # ANN distance (most similar first).
    ordered = same_file_chunks + semantic_chunks

    out: list[ContextChunk] = []
    spent = 0
    for c in ordered:
        # Skip-but-keep-trying: a 500-tok chunk shouldn't lock out a later
        # 100-tok chunk that would still fit. Lets us pack densely.
        if spent + c.token_estimate > token_budget:
            continue
        out.append(c)
        spent += c.token_estimate

    return out


__all__ = [
    "CHARS_PER_TOKEN",
    "ContextChunk",
    "DEFAULT_CONTEXT_BUDGET_TOKENS",
    "DEFAULT_SEMANTIC_TOP_K",
    "build_context",
]
