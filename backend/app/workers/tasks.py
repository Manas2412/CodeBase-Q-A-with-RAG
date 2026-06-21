import asyncio
import os
import asyncpg
from celery import Celery
from pgvector.asyncpg import register_vector
from dotenv import load_dotenv

from app.db.database import needs_ssl
from app.ingestion.cloner import clone_repo, walk_code_files, cleanup_repo
from app.ingestion.chunker import chunk_file

load_dotenv()

celery = Celery(
    "codereview",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
)


def get_dsn() -> str:
    raw_url = os.getenv("DATABASE_URL", "")
    db_url = (
        raw_url
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgres://", "postgresql://")
    )
    return db_url.split("?")[0]


@celery.task(bind=True, max_retries=3, default_retry_delay=10)
def index_repo_task(self, repo_id: str, github_url: str):
    """
    Phase-0 stub. Currently clones the repo and produces tree-sitter chunks
    in memory, then marks the project as 'ready'. No embedding/upsert yet —
    the Bedrock-backed embedder and indexer come online in Phase 1 Week 2.
    """
    try:
        asyncio.run(_index_repo(repo_id, github_url))
    except Exception as exc:
        raise self.retry(exc=exc)


async def _index_repo(repo_id: str, github_url: str):
    conn = None
    repo_path = None
    try:
        dsn = get_dsn()
        conn = await asyncpg.connect(dsn=dsn, ssl=needs_ssl(dsn))
        await register_vector(conn)

        await conn.execute(
            "UPDATE repos SET status = 'indexing' WHERE id = $1::uuid", repo_id
        )

        repo_path = await clone_repo(github_url)

        # Chunk only — embedding + vector upsert disabled until Bedrock lands.
        # This still validates: provider URL parsing, persistent clone, tree-sitter
        # parsing across supported languages. Useful smoke signal until Week 2.
        total_chunks = 0
        for file_path, source, language in walk_code_files(repo_path):
            chunks = chunk_file(file_path, source, language)
            total_chunks += len(chunks)
        print(f"[indexer] {total_chunks} chunks parsed from {github_url} (not embedded yet)")

        await conn.execute(
            """
            UPDATE repos
            SET status = 'ready', indexed_at = now()
            WHERE id = $1::uuid
            """,
            repo_id,
        )
        print(f"[indexer] done — repo {repo_id} marked ready")

    except Exception as e:
        print(f"[indexer] error indexing {github_url}: {e}")
        if conn:
            await conn.execute(
                "UPDATE repos SET status = 'error' WHERE id = $1::uuid", repo_id
            )
        raise

    finally:
        if repo_path:
            cleanup_repo(repo_path)
        if conn:
            await conn.close()
