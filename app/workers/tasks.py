import asyncio
import os
import asyncpg
from celery import Celery
from pgvector.asyncpg import register_vector
from dotenv import load_dotenv

from app.ingestion.cloner import clone_repo, walk_code_files, cleanup_repo
from app.ingestion.chunker import chunk_file
from app.ingestion.embedder import embed_chunks
from app.ingestion.indexer import upsert_chunks


load_dotenv()

celery = Celery(
    "codeqa",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
)


@celery.task(bind=True, max_retries=3, default_retry_delay=10)
def index_repo_task(self, repo_id: str, github_url: str):
    try:
        asyncio.run(_index_repo(repo_id, github_url))
    except Exception as exc:
        raise self.retry(exc=exc)


async def _index_repo(repo_id: str, github_url: str):
    # Fix: asyncpg.connect (not coonect); ssl=True not ssl="required"
    conn = await asyncpg.connect(
        dsn=os.getenv("DATABASE_URL"),
        ssl=True,
    )

    await register_vector(conn)

    try:
        # Mark as indexing
        await conn.execute(
            "UPDATE repos SET status = 'indexing' WHERE id = $1::uuid", repo_id
        )

        repo_path = await clone_repo(github_url)
        try:
            all_chunks = []
            for file_path, source, language in walk_code_files(repo_path):
                chunks = chunk_file(file_path, source, language)
                all_chunks.extend(chunks)

            print(f"[indexer] {len(all_chunks)} chunks from {github_url}")

            embeddings = await embed_chunks(all_chunks)
            await upsert_chunks(conn, repo_id, all_chunks, embeddings)
        finally:
            cleanup_repo(repo_path)  # always delete temp clone

        await conn.execute(
            """
            UPDATE repos
            SET status = 'ready', indexed_at = now()
            WHERE id = $1::uuid
            """,
            repo_id,
        )
        print(f"[indexer] done - repo {repo_id} is ready")

    except Exception as e:
        # Fix: SQL typo WHER -> WHERE
        await conn.execute(
            "UPDATE repos SET status = 'error' WHERE id = $1::uuid", repo_id
        )
        raise

    finally:
        cleanup_repo(repo_path)
        await conn.close()