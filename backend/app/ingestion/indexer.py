"""pgvector writes for the ingestion pipeline.

Schema reminder (migration 002_review_agent):
    chunks.project_id      → FK to projects.id
    chunks.commit_sha      → set on every insert; lets pruning be commit-scoped
    chunks.embedding       → vector(1024); Cohere Embed Multilingual v3
    UNIQUE (project_id, file_path, start_line) — ON CONFLICT target
"""

from __future__ import annotations

import uuid
from typing import Sequence

import asyncpg
from pgvector.asyncpg import register_vector

from app.ingestion.chunker import CodeChunk


async def upsert_chunks(
    conn: asyncpg.Connection,
    project_id: str | uuid.UUID,
    chunks: Sequence[CodeChunk],
    embeddings: Sequence[Sequence[float]],
    *,
    commit_sha: str | None = None,
) -> int:
    """Batch upsert chunks + embeddings into pgvector. Returns the row count.

    Uses ON CONFLICT (project_id, file_path, start_line) so re-indexing the
    same chunk overwrites in place — embeddings + body + range all update,
    while the row's id stays stable (lets future review_findings.commit_id
    references survive re-index).

    `commit_sha` is stamped on every inserted row, so future pruning can
    target "chunks not from this commit" when a file gets edited.
    """
    if not chunks:
        return 0
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"upsert_chunks: got {len(chunks)} chunks but {len(embeddings)} embeddings"
        )

    await register_vector(conn)

    pid = str(project_id)
    records = [
        (
            str(uuid.uuid4()),
            pid,
            commit_sha,
            c.file_path,
            c.language,
            c.chunk_type,
            c.name,
            c.start_line,
            c.end_line,
            c.content,
            c.context_prefix,
            list(emb),
        )
        for c, emb in zip(chunks, embeddings)
    ]

    await conn.executemany(
        """
        INSERT INTO chunks (
            id, project_id, commit_sha,
            file_path, language, chunk_type, name,
            start_line, end_line, content, context_prefix, embedding
        )
        VALUES (
            $1::uuid, $2::uuid, $3,
            $4, $5, $6, $7,
            $8, $9, $10, $11, $12::vector
        )
        ON CONFLICT (project_id, file_path, start_line)
        DO UPDATE SET
            commit_sha     = EXCLUDED.commit_sha,
            content        = EXCLUDED.content,
            context_prefix = EXCLUDED.context_prefix,
            embedding      = EXCLUDED.embedding,
            chunk_type     = EXCLUDED.chunk_type,
            name           = EXCLUDED.name,
            end_line       = EXCLUDED.end_line
        """,
        records,
    )
    print(f"[indexer] upserted {len(records)} chunks for project {pid}")
    return len(records)


async def prune_chunks_for_files(
    conn: asyncpg.Connection,
    project_id: str | uuid.UUID,
    file_paths: Sequence[str],
) -> int:
    """Delete all chunks for the given (project, files) pairs.

    Use this before re-indexing a file that was just modified — clears
    out stale rows whose start_line no longer corresponds to a current
    chunk (e.g., when a function was deleted or moved up the file).

    Returns the number of rows removed.
    """
    if not file_paths:
        return 0
    result = await conn.execute(
        "DELETE FROM chunks WHERE project_id = $1::uuid AND file_path = ANY($2::text[])",
        str(project_id),
        list(file_paths),
    )
    # asyncpg returns "DELETE <n>"; parse the count for telemetry/logging
    try:
        n = int(result.split()[-1])
    except (ValueError, IndexError):
        n = 0
    print(
        f"[indexer] pruned {n} stale chunks for project {project_id} "
        f"across {len(file_paths)} files"
    )
    return n


async def prune_chunks_not_in_commit(
    conn: asyncpg.Connection,
    project_id: str | uuid.UUID,
    commit_sha: str,
) -> int:
    """Delete chunks whose commit_sha doesn't match the given one.

    Use after a full re-index of a project against a specific commit to
    sweep away any rows that survived from an earlier indexing run
    (e.g., deleted files). Returns the number of rows removed.
    """
    result = await conn.execute(
        """
        DELETE FROM chunks
        WHERE project_id = $1::uuid
          AND (commit_sha IS DISTINCT FROM $2)
        """,
        str(project_id),
        commit_sha,
    )
    try:
        n = int(result.split()[-1])
    except (ValueError, IndexError):
        n = 0
    print(f"[indexer] pruned {n} chunks not from commit {commit_sha[:8]}")
    return n
