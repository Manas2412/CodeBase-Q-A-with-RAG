import json
import os
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import asyncpg
import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.db.database import needs_ssl
from app.providers import (
    ProviderError,
    UnknownProviderError,
    get_provider,
)
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


async def _init_asyncpg_connection(conn: asyncpg.Connection) -> None:
    """Register codecs on every pool connection.

    Without this, asyncpg returns JSONB columns as raw strings — Pydantic
    then chokes when we feed them straight into list[str]/dict-typed
    fields. With the codec, JSONB round-trips as Python lists / dicts.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, redis_pool

    dsn = get_dsn()
    db_pool = await asyncpg.create_pool(
        dsn,
        ssl=needs_ssl(dsn),
        init=_init_asyncpg_connection,
    )

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
    # Kept as `github_url` for backward-compat with the existing frontend.
    # The probe / wizard introduced in Week 1 onwards prefers `repo_url`.
    github_url: str


class ProbeRequest(BaseModel):
    url: str


class BranchOut(BaseModel):
    name: str
    sha: str
    is_default: bool


class ProbeResponse(BaseModel):
    provider: str
    default_branch: str
    branches: list[BranchOut]


class CreateProjectRequest(BaseModel):
    """Payload for wizard Screen 3 → 'Create project'.

    Only `url` is required. Everything else has sensible defaults:
      • `name` is derived from the URL if omitted.
      • `branches_to_review` defaults to [default_branch] when empty.
      • `checklist_id` is nullable — projects can run with no checklist.
    """

    url: str
    name: str | None = None
    branches_to_review: list[str] = Field(default_factory=list)
    checklist_id: str | None = None
    auto_watch_new: bool = False
    poll_interval_minutes: int = Field(default=5, ge=1, le=1440)


class CreateProjectResponse(BaseModel):
    project_id: str
    status: str
    provider: str
    name: str
    default_branch: str
    branches_to_review: list[str]
    created: bool  # True for fresh inserts, False if the URL was already registered


def _detect_provider(url: str) -> str:
    """Tiny URL-based provider detector.

    Phase 1 supports OpenForge (Tuleap) and GitHub. The full provider registry
    in app/providers/ replaces this once we wire it up later this week.
    """
    host = (urlparse(url).hostname or "").lower()
    if "openforge.gov.in" in host or "/plugins/git/" in url:
        return "openforge"
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    return "unknown"


def _derive_name(url: str) -> str:
    """Last path segment minus .git — good enough as a default display name."""
    path = (urlparse(url).path or "").strip("/")
    last = path.rsplit("/", 1)[-1] if path else url
    return last[:-4] if last.endswith(".git") else last or url


# Endpoints
@app.post("/repos")
async def add_repo(body: AddRequest):
    """Onboard a repo. Returns immediately — indexing runs in the background.

    Note: this endpoint writes to the `projects` table (renamed from `repos`
    in migration 002_review_agent). The route path is kept as /repos for
    backward compat until the wizard flow lands in Phase 1 Week 4.
    """
    repo_url = body.github_url
    provider = _detect_provider(repo_url)
    name = _derive_name(repo_url)

    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM projects WHERE repo_url = $1", repo_url
        )
        if existing:
            return {"repo_id": str(existing["id"]), "status": existing["status"]}

        project_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO projects (id, provider, repo_url, name, status)
            VALUES ($1, $2, $3, $4, 'pending')
            """,
            project_id,
            provider,
            repo_url,
            name,
        )

    index_repo_task.delay(str(project_id), repo_url)
    return {"repo_id": str(project_id), "status": "pending"}


@app.post("/projects", response_model=CreateProjectResponse, status_code=201)
async def create_project(body: CreateProjectRequest):
    """Wizard Screen 3 → Create. The visible bottom-of-wizard action.

    Flow:
      1. Detect provider, parse URL  → 400 if unrecognised / malformed.
      2. Probe the remote (git ls-remote) → 400 if unreachable or auth
         fails. We need a live default_branch so the dashboard knows
         which branch to feature.
      3. Look up by repo_url:
           • exists → return existing row with `created=False` (lets the
             frontend redirect to the project page without surprise).
           • absent → insert a new row with all the wizard fields.
      4. Kick off the background indexing task and return 201.
    """
    try:
        provider = get_provider(body.url)
        parsed = provider.parse(body.url)
    except UnknownProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))

    token = provider.get_token()
    try:
        default_branch, _branches = await provider.list_branches(parsed, token)
    except ProviderError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Couldn't reach the repository: {e}. Check the URL and the "
                f"service-account token ({provider.token_env})."
            ),
        )

    name = body.name or parsed.repo or _derive_name(body.url)
    branches_to_review = body.branches_to_review or [default_branch]

    # Provider-specific niceties — leave NULL for providers that don't use them
    is_openforge = provider.name == "openforge"
    tuleap_project = parsed.org_or_project if is_openforge else None
    tuleap_repo = parsed.repo if is_openforge else None

    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status, default_branch, branches_to_review "
            "FROM projects WHERE repo_url = $1",
            body.url,
        )
        if existing:
            return CreateProjectResponse(
                project_id=str(existing["id"]),
                status=existing["status"],
                provider=provider.name,
                name=name,
                default_branch=existing["default_branch"] or default_branch,
                branches_to_review=existing["branches_to_review"] or branches_to_review,
                created=False,
            )

        project_id = uuid.uuid4()
        # NOTE: branches_to_review is passed as a Python list, NOT
        # json.dumps(...). asyncpg's registered JSONB codec encodes it for us.
        # Pre-encoding here would double-encode (codec calls json.dumps again,
        # producing a JSON-string scalar instead of a JSON array).
        await conn.execute(
            """
            INSERT INTO projects (
                id, provider, repo_url, name,
                tuleap_project, tuleap_repo,
                default_branch, branches_to_review, last_reviewed_sha,
                trigger_mode, poll_interval_minutes, auto_watch_new,
                checklist_id, status
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6,
                $7, $8, $9,
                'poll', $10, $11,
                $12, 'pending'
            )
            """,
            project_id,
            provider.name,
            body.url,
            name,
            tuleap_project,
            tuleap_repo,
            default_branch,
            branches_to_review,    # Python list → codec encodes to jsonb array
            {},                    # Python dict → codec encodes to '{}' jsonb
            body.poll_interval_minutes,
            body.auto_watch_new,
            uuid.UUID(body.checklist_id) if body.checklist_id else None,
        )

    # Kick off cloning + chunking in the background. The polling agent (Week 3)
    # takes over from then on.
    index_repo_task.delay(str(project_id), body.url)

    return CreateProjectResponse(
        project_id=str(project_id),
        status="pending",
        provider=provider.name,
        name=name,
        default_branch=default_branch,
        branches_to_review=branches_to_review,
        created=True,
    )


@app.post("/projects/probe", response_model=ProbeResponse)
async def probe_project(body: ProbeRequest):
    """Wizard Screen 1 → Screen 2 — paste a URL, get the actual branches.

    Uses `git ls-remote --heads --symref` so we don't have to clone the repo
    to enumerate branches. Returns provider, default branch, and the full
    branch list with SHAs so the UI can show last-pushed info per branch.

    Errors raise HTTPException(400) with a short message — the wizard
    surfaces them as inline validation on Screen 1.
    """
    try:
        provider = get_provider(body.url)
    except UnknownProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        parsed = provider.parse(body.url)
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))

    token = provider.get_token()
    try:
        default_branch, branches = await provider.list_branches(parsed, token)
    except ProviderError as e:
        # auth or network failure — same response envelope as a 400 from validation
        # so the frontend can render the message uniformly
        raise HTTPException(
            status_code=400,
            detail=(
                f"Couldn't reach the repository: {e}. Check that the URL is "
                f"correct and that the service-account token ({provider.token_env}) "
                "has access."
            ),
        )

    return ProbeResponse(
        provider=provider.name,
        default_branch=default_branch,
        branches=[
            BranchOut(name=b.name, sha=b.sha, is_default=b.is_default)
            for b in branches
        ],
    )


@app.get("/repos/{repo_id}/status")
async def repo_status(repo_id: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, indexed_at FROM projects WHERE id = $1::uuid", repo_id
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
