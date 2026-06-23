"""Celery tasks.

Currently:
  • index_repo_task — initial onboarding job. Clone the repo into the
    persistent clone manager, materialize the default branch, walk + chunk
    the files, mark the project ready.

What's NOT here yet:
  • Embedding + pgvector upsert — moves over in Phase 1 Week 2 when the
    Bedrock embedder lands. Until then we chunk in-memory as a smoke
    signal: confirms the cloner, tree-sitter parser, and chunker all
    work end-to-end on real repositories without spending LLM tokens.
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
from app.providers import ProviderError, UnknownProviderError, get_provider
from app.storage import CloneError, ensure_cloned, fetch, materialize_tree

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
    """Initial-onboarding task.

    Parameters
    ----------
    project_id : The project's UUID.
    repo_url   : Optional. Kept for backward-compat with the existing
                 enqueue site in /repos POST. If omitted, the URL is
                 looked up from the projects row.
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

        # 1. Fetch the project row (and fill repo_url from DB if caller didn't supply)
        row = await conn.fetchrow(
            "SELECT repo_url, default_branch FROM projects WHERE id = $1::uuid",
            project_id,
        )
        if not row:
            print(f"[indexer] project {project_id} not found; skipping")
            return
        repo_url = repo_url or row["repo_url"]
        default_branch = row["default_branch"] or "HEAD"

        # 2. Mark indexing
        await conn.execute(
            "UPDATE projects SET status = 'indexing' WHERE id = $1::uuid",
            project_id,
        )

        # 3. Resolve provider, inject auth into the clone URL
        try:
            provider = get_provider(repo_url)
            parsed = provider.parse(repo_url)
        except (UnknownProviderError, ProviderError) as e:
            raise CloneError(f"Provider error for {repo_url!r}: {e}") from e
        auth_url = provider.auth_url(parsed, provider.get_token())

        # 4. Persistent clone (no-op if already present) + incremental fetch
        await ensure_cloned(project_id, auth_url)
        await fetch(project_id)

        # 5. Materialize the default branch and chunk every supported file.
        #    Embedding + upsert are deferred to Week 2 (Bedrock).
        total_chunks = 0
        async with materialize_tree(project_id, default_branch) as tree_path:
            for file_path, source, language in walk_code_files(tree_path):
                chunks = chunk_file(file_path, source, language)
                total_chunks += len(chunks)
        print(
            f"[indexer] {total_chunks} chunks parsed from {repo_url} "
            f"@ {default_branch} (not embedded yet)"
        )

        # 6. Mark ready
        await conn.execute(
            """
            UPDATE projects
            SET status = 'ready', indexed_at = now()
            WHERE id = $1::uuid
            """,
            project_id,
        )
        print(f"[indexer] done — project {project_id} marked ready")

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
