"""Celery tasks.

Currently:
  • index_repo_task — initial onboarding job. Clone into the persistent
    clone manager, materialize the default branch, walk + chunk + embed +
    pgvector-upsert, mark the project ready.

What's not here yet:
  • Polling / review tasks — Week 3.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
from celery import Celery
from dotenv import load_dotenv
from pgvector.asyncpg import register_vector

from app.db.database import needs_ssl
from app.ingestion.chunker import chunk_file
from app.ingestion.cloner import walk_code_files
from app.ingestion.embedder import embed_chunks
from app.ingestion.indexer import prune_chunks_not_in_commit, upsert_chunks
from app.providers import ProviderError, UnknownProviderError, get_provider
from app.storage import (
    CloneError,
    branch_head,
    ensure_cloned,
    fetch,
    materialize_tree,
)

load_dotenv()

celery = Celery(
    "codereview",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
)


def get_dsn() -> str:
    raw_url = os.getenv("DATABASE_URL", "")
    db_url = (
        raw_url.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgres://", "postgresql://")
    )
    return db_url.split("?")[0]


@celery.task(bind=True, max_retries=3, default_retry_delay=10)
def index_repo_task(self, project_id: str, repo_url: str | None = None):
    """Initial-onboarding indexing.

    Parameters
    ----------
    project_id : The project's UUID.
    repo_url   : Optional. Kept for back-compat with the existing /repos
                 POST call site. Looked up from the projects row when omitted.
    """
    try:
        asyncio.run(_index_repo(project_id, repo_url))
    except Exception as exc:
        raise self.retry(exc=exc)


async def _index_repo(project_id: str, repo_url: str | None) -> None:
    conn: asyncpg.Connection | None = None
    try:
        dsn = get_dsn()
        conn = await asyncpg.connect(dsn=dsn, ssl=needs_ssl(dsn))
        await register_vector(conn)

        # 1. Load project row → resolve repo_url + default_branch from DB
        row = await conn.fetchrow(
            "SELECT repo_url, default_branch FROM projects WHERE id = $1::uuid",
            project_id,
        )
        if not row:
            print(f"[indexer] project {project_id} not found; skipping")
            return
        repo_url = repo_url or row["repo_url"]
        default_branch = row["default_branch"] or "HEAD"

        await conn.execute(
            "UPDATE projects SET status = 'indexing' WHERE id = $1::uuid",
            project_id,
        )

        # 2. Provider abstraction → auth-injected clone URL
        try:
            provider = get_provider(repo_url)
            parsed = provider.parse(repo_url)
        except (UnknownProviderError, ProviderError) as e:
            raise CloneError(f"Provider error for {repo_url!r}: {e}") from e
        auth_url = provider.auth_url(parsed, provider.get_token())

        # 3. Clone (no-op if present) + incremental fetch
        await ensure_cloned(project_id, auth_url)
        await fetch(project_id)

        # 4. Capture the SHA we're indexing so future re-indexes can prune
        commit_sha = await branch_head(project_id, default_branch)

        # 5. Walk files, chunk via tree-sitter, embed via Bedrock Cohere v3,
        #    upsert into pgvector. We accumulate chunks across the whole
        #    walk and flush in one indexer call so the embedding batching
        #    in embed_chunks() can pack 96-text Bedrock calls efficiently.
        all_chunks = []
        async with materialize_tree(project_id, default_branch) as tree_path:
            for file_path, source, language in walk_code_files(tree_path):
                all_chunks.extend(chunk_file(file_path, source, language))

        if not all_chunks:
            print(
                f"[indexer] {repo_url} @ {default_branch} has zero supported-language "
                "chunks; marking ready with empty index"
            )
        else:
            print(
                f"[indexer] embedding {len(all_chunks)} chunks "
                f"from {repo_url} @ {default_branch}"
            )
            embeddings = await embed_chunks(all_chunks)
            await upsert_chunks(
                conn,
                project_id=project_id,
                chunks=all_chunks,
                embeddings=embeddings,
                commit_sha=commit_sha,
            )
            # Sweep away rows from prior indexing runs (deleted files etc.)
            await prune_chunks_not_in_commit(conn, project_id, commit_sha)

        # 6. Mark ready
        await conn.execute(
            """
            UPDATE projects
            SET status = 'ready', indexed_at = now()
            WHERE id = $1::uuid
            """,
            project_id,
        )
        print(f"[indexer] done — project {project_id} marked ready @ {commit_sha[:8]}")

    except Exception as e:
        print(f"[indexer] error indexing {project_id}: {e}")
        if conn:
            await conn.execute(
                "UPDATE projects SET status = 'error' WHERE id = $1::uuid",
                project_id,
            )
        raise
    finally:
        if conn:
            await conn.close()
