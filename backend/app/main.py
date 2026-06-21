import os
import uuid
import asyncpg
import redis.asyncio as aioredis
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from app.db.database import needs_ssl
from app.workers.tasks import index_repo_task

load_dotenv()

# Connection pool
db_pool: asyncpg.Pool = None
redis_pool: aioredis.Redis = None


def get_dsn() -> str:
    raw_url = os.getenv("DATABASE_URL", "")
    db_url = (
        raw_url
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgres://", "postgresql://")
    )
    return db_url.split("?")[0]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, redis_pool

    dsn = get_dsn()
    db_pool = await asyncpg.create_pool(dsn, ssl=needs_ssl(dsn))

    redis_pool = aioredis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )
    yield
    await db_pool.close()
    await redis_pool.aclose()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request/response models
class AddRequest(BaseModel):
    github_url: str


# Endpoints
@app.post("/repos")
async def add_repo(body: AddRequest):
    """Enqueue indexing job. Returns immediately — indexing runs in background."""
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM repos WHERE github_url = $1", body.github_url
        )
        if existing:
            return {"repo_id": str(existing["id"]), "status": existing["status"]}

        repo_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO repos (id, github_url, status)
            VALUES ($1, $2, 'pending')
            """,
            repo_id,
            body.github_url,
        )

    index_repo_task.delay(str(repo_id), body.github_url)
    return {"repo_id": str(repo_id), "status": "pending"}


@app.get("/repos/{repo_id}/status")
async def repo_status(repo_id: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, indexed_at FROM repos WHERE id = $1::uuid", repo_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Repo not found")
    return {
        "status": row["status"],
        "indexed_at": str(row["indexed_at"]) if row["indexed_at"] else None,
    }


# Note: /query and /webhooks/github removed in pre-build cleanup.
# - /query: the Voyage/Ollama/Cohere-based Q&A surface is being replaced with
#   the Bedrock-driven review pipeline. Q&A may return on Bedrock if needed.
# - /webhooks/github: superseded by the polling agent in Phase 1 Week 3
#   (Plan v3.3 §4 — Variant A polling trigger layer).
