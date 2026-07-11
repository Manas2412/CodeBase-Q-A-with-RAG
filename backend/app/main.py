import json
import os
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import asyncio
import asyncpg
import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app.db.database import needs_ssl, register_jsonb_codecs
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, redis_pool

    dsn = get_dsn()
    db_pool = await asyncpg.create_pool(
        dsn,
        ssl=needs_ssl(dsn),
        init=register_jsonb_codecs,
    )

    redis_pool = aioredis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )
    yield
    await db_pool.close()
    await redis_pool.aclose()


app = FastAPI(lifespan=lifespan)

# ── Session middleware ──────────────────────────────────────────────────
# Signed cookies backed by SESSION_SECRET. Rotating the secret invalidates
# every active session — the intended way to force logout across the team.
# Falls back to a clearly-dev value if the env var isn't set so local
# development doesn't break; production MUST set SESSION_SECRET.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv(auth.ENV_SESSION_SECRET, "dev-only-secret-change-me"),
    session_cookie="codereview_session",
    max_age=auth.SESSION_MAX_AGE_SECONDS,
    # SameSite=Lax lets the cookie ride cross-origin GETs (fine for our
    # same-origin dev proxy setup) but blocks third-party POSTs — CSRF
    # coverage without a token layer.
    same_site="lax",
    # Set https_only=True in production — the dev server is plain HTTP.
    https_only=os.getenv("SESSION_HTTPS_ONLY", "").lower() == "true",
)

# CORS — permissive in dev; production terminates via Caddy same-origin.
# `allow_credentials=True` requires an explicit list, not "*". We derive
# the allowlist from an env var so ops can add trusted hosts without a
# code change.
_cors_allow = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Global auth gate ────────────────────────────────────────────────────
#
# Attach `require_auth` as a router-level dependency below for every
# protected route. Public paths (auth login/logout/me) skip it.
#
# We define it at module scope so the endpoints don't need to import
# `auth` individually.
_RequireAuth = Depends(auth.require_auth)


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


class ProjectOut(BaseModel):
    """Project as it appears in list / detail responses."""

    id: str
    provider: str
    name: str
    repo_url: str
    default_branch: str
    branches_to_review: list[str]
    last_reviewed_sha: dict
    trigger_mode: str
    poll_interval_minutes: int
    auto_watch_new: bool
    checklist_id: str | None
    status: str
    indexed_at: str | None
    last_polled_at: str | None
    created_at: str


class ProjectListResponse(BaseModel):
    projects: list[ProjectOut]
    total: int
    limit: int
    offset: int


# ── Review read models ──────────────────────────────────────────────────
#
# Shape note: list endpoints return summary fields only (cheap query); detail
# endpoints add commits[] and the summary blob. Splitting these keeps the
# dashboard's reviews-list page snappy when projects accumulate hundreds of
# reviews.

class ReviewSummary(BaseModel):
    """Lightweight row in a reviews list — no `summary` text, no joined commits."""

    id: str
    project_id: str
    branch: str
    before_sha: str
    after_sha: str
    status: str
    severity_counts: dict
    token_usage: dict
    checklist_version: int | None
    batch_mode: str
    created_at: str
    completed_at: str | None
    #: Derived count of findings on this review. Cheap via the index on
    #: review_findings(review_id). The dashboard uses this for the "N issues"
    #: badge without paying for a JOIN against the full findings table.
    finding_count: int


class ReviewListResponse(BaseModel):
    reviews: list[ReviewSummary]
    total: int
    limit: int
    offset: int


class CommitOut(BaseModel):
    """A reviewed commit's full attribution + subject."""

    sha: str
    parent_sha: str | None
    branch: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    committed_at: str
    subject: str
    source: str


class ReviewDetail(ReviewSummary):
    """Single-review payload — adds the natural-language summary + commit list."""

    summary: str | None
    commits: list[CommitOut]


class FindingOut(BaseModel):
    id: str
    review_id: str
    commit_id: str | None
    severity: str
    category: str
    file_path: str
    start_line: int | None
    end_line: int | None
    message: str
    suggestion: str | None
    rule_id: str | None
    #: Day-5: server-extracted code around the cited line. Null when the LLM
    #: cited a line outside the diff (rare).
    code_snippet: str | None
    #: Day-5: LLM-emitted code-as-fix. Null for findings that don't carry a
    #: literal code change (e.g. "no tests", "missing docstring").
    suggested_code: str | None


class FindingListResponse(BaseModel):
    findings: list[FindingOut]
    total: int
    limit: int
    offset: int


class CommitListResponse(BaseModel):
    commits: list[CommitOut]
    total: int
    limit: int
    offset: int


# ── BranchEvent models ──────────────────────────────────────────────────
class BranchEventOut(BaseModel):
    """A notable event on a watched branch — force-push, new branch, etc."""

    id: str
    project_id: str
    branch: str
    event_type: str   # 'force_push' | 'new_branch' | 'branch_deleted'
    detail: dict      # event-type-specific payload (e.g. {previous_sha, new_sha})
    resolved: bool
    created_at: str


class BranchEventListResponse(BaseModel):
    events: list[BranchEventOut]
    total: int
    limit: int
    offset: int
    #: Number of unresolved events across ALL filters — handy for the
    #: dashboard's red-dot indicator regardless of which filter is active.
    unresolved_total: int


def _row_to_branch_event(row: asyncpg.Record) -> BranchEventOut:
    return BranchEventOut(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        branch=row["branch"],
        event_type=row["event_type"],
        detail=row["detail"] or {},
        resolved=row["resolved"],
        created_at=row["created_at"].isoformat(),
    )


#: Severity ordering used everywhere a severity sort matters — critical first.
#: Postgres CASE expressions reference this list by index; keep in sync if you
#: add a level.
SEVERITY_ORDER: list[str] = ["critical", "major", "minor", "info"]


def _row_to_review_summary(row: asyncpg.Record) -> ReviewSummary:
    return ReviewSummary(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        branch=row["branch"],
        before_sha=row["before_sha"],
        after_sha=row["after_sha"],
        status=row["status"],
        severity_counts=row["severity_counts"] or {},
        token_usage=row["token_usage"] or {},
        checklist_version=row["checklist_version"],
        batch_mode=row["batch_mode"],
        created_at=row["created_at"].isoformat(),
        completed_at=(
            row["completed_at"].isoformat() if row["completed_at"] else None
        ),
        finding_count=row["finding_count"],
    )


def _row_to_commit(row: asyncpg.Record) -> CommitOut:
    return CommitOut(
        sha=row["sha"],
        parent_sha=row["parent_sha"],
        branch=row["branch"],
        author_name=row["author_name"],
        author_email=row["author_email"],
        committer_name=row["committer_name"],
        committer_email=row["committer_email"],
        committed_at=row["committed_at"].isoformat(),
        subject=row["subject"],
        source=row["source"],
    )


def _row_to_finding(row: asyncpg.Record) -> FindingOut:
    return FindingOut(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        commit_id=str(row["commit_id"]) if row["commit_id"] else None,
        severity=row["severity"],
        category=row["category"],
        file_path=row["file_path"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        message=row["message"],
        suggestion=row["suggestion"],
        rule_id=row["rule_id"],
        code_snippet=row["code_snippet"],
        suggested_code=row["suggested_code"],
    )


#: Review columns used by both list and detail. `finding_count` is a
#: correlated subquery — cheap with the existing index on review_findings(review_id).
#:
#: Note: commit-to-review attribution (which commits "belong" to this push range)
#: is deferred to Day 5, which adds a `commits.review_id` FK. Until then the
#: detail endpoint surfaces recent commits on the branch as a soft approximation.
_REVIEW_SUMMARY_COLUMNS = """
    r.id, r.project_id, r.branch, r.before_sha, r.after_sha, r.status,
    r.severity_counts, r.token_usage, r.checklist_version, r.batch_mode,
    r.created_at, r.completed_at,
    (SELECT COUNT(*) FROM review_findings f WHERE f.review_id = r.id) AS finding_count
"""


def _parse_uuid(s: str, *, field: str) -> uuid.UUID:
    """Validate a path/query string is a UUID. 400 on failure with a useful field name."""
    try:
        return uuid.UUID(s)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"{field} must be a UUID"
        )


# Columns used by both list and detail endpoints — keep the SELECT identical
# so the row → ProjectOut mapping in `_row_to_project` matches.
_PROJECT_COLUMNS = """
    id, provider, name, repo_url, default_branch, branches_to_review,
    last_reviewed_sha, trigger_mode, poll_interval_minutes, auto_watch_new,
    checklist_id, status, indexed_at, last_polled_at, created_at
"""


def _row_to_project(row: asyncpg.Record) -> ProjectOut:
    """Map an asyncpg Record to ProjectOut, normalising types for JSON."""
    return ProjectOut(
        id=str(row["id"]),
        provider=row["provider"],
        name=row["name"],
        repo_url=row["repo_url"],
        default_branch=row["default_branch"],
        branches_to_review=row["branches_to_review"] or [],
        last_reviewed_sha=row["last_reviewed_sha"] or {},
        trigger_mode=row["trigger_mode"],
        poll_interval_minutes=row["poll_interval_minutes"],
        auto_watch_new=row["auto_watch_new"],
        checklist_id=str(row["checklist_id"]) if row["checklist_id"] else None,
        status=row["status"],
        indexed_at=row["indexed_at"].isoformat() if row["indexed_at"] else None,
        last_polled_at=(
            row["last_polled_at"].isoformat() if row["last_polled_at"] else None
        ),
        created_at=row["created_at"].isoformat(),
    )


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


# ── Auth endpoints (public — no require_auth) ───────────────────────────
class LoginRequest(BaseModel):
    password: str


class AuthStatus(BaseModel):
    authenticated: bool
    configured: bool


@app.post("/auth/login", response_model=AuthStatus)
async def login(body: LoginRequest, request: Request):
    """Verify the shared password and set the session cookie.

    Returns 401 on wrong password. A generic message — "Invalid password" —
    same for missing/empty/wrong so we don't leak whether the env var is set.
    """
    if not auth.verify_password(body.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    auth.sign_in(request)
    return AuthStatus(authenticated=True, configured=auth.is_configured())


@app.post("/auth/logout", response_model=AuthStatus)
async def logout(request: Request):
    """Clear the session cookie. Always 200, even if there was no session."""
    auth.sign_out(request)
    return AuthStatus(authenticated=False, configured=auth.is_configured())


@app.get("/auth/me", response_model=AuthStatus)
async def me(request: Request):
    """Report the caller's auth state. The frontend calls this on mount
    to decide whether to render the app or redirect to /login."""
    return AuthStatus(
        authenticated=auth.is_signed_in(request),
        configured=auth.is_configured(),
    )


# Endpoints
@app.post("/repos", dependencies=[_RequireAuth])
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


@app.post("/projects", response_model=CreateProjectResponse, status_code=201,
          dependencies=[_RequireAuth])
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


@app.get("/projects", response_model=ProjectListResponse, dependencies=[_RequireAuth])
async def list_projects(
    limit: int = Query(50, ge=1, le=100, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Skip the first N rows"),
    provider: str | None = Query(None, description="Filter by provider name"),
):
    """List registered projects ordered by most recently created.

    Used by the dashboard's projects list view. Pagination keeps the
    payload bounded — the frontend can render 'load more' or pages.
    """
    where = ""
    params: list = []
    if provider:
        params.append(provider)
        where = f"WHERE provider = ${len(params)}"

    params.extend([limit, offset])
    limit_param = f"${len(params) - 1}"
    offset_param = f"${len(params)}"

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_PROJECT_COLUMNS}
            FROM projects
            {where}
            ORDER BY created_at DESC
            LIMIT {limit_param} OFFSET {offset_param}
            """,
            *params,
        )
        total_params = [provider] if provider else []
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM projects {where}",
            *total_params,
        )

    return ProjectListResponse(
        projects=[_row_to_project(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@app.get("/projects/{project_id}", response_model=ProjectOut, dependencies=[_RequireAuth])
async def get_project(project_id: str):
    """Project detail by ID. 404 if not found."""
    try:
        pid = uuid.UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="project_id must be a UUID")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = $1", pid
        )
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return _row_to_project(row)


@app.get("/projects/{project_id}/index-status", dependencies=[_RequireAuth])
async def project_index_status_stream(project_id: str):
    """Live SSE stream of indexing progress for wizard Screen 4.

    Polls the projects table once a second, emits an SSE `data:` event
    whenever the status field changes, and closes the stream when the
    status becomes terminal (`ready` or `error`).

    The frontend can render a check-mark per step (cloning → chunking →
    embedding → ready) by reading successive events. Status values today:
    pending, indexing, ready, error. More granular sub-steps land in Week 2
    once the Bedrock embedder reports phases.
    """
    try:
        pid = uuid.UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="project_id must be a UUID")

    # Verify the project exists before we start streaming
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM projects WHERE id = $1", pid
        )
    if not exists:
        raise HTTPException(status_code=404, detail="Project not found")

    terminal_states = {"ready", "error"}

    async def event_stream():
        import json as _json

        last_payload: str | None = None
        # Cap the stream at 10 minutes — indexing should never take that long
        # for a single repo, and we don't want zombie connections lingering.
        deadline = asyncio.get_event_loop().time() + 600

        while asyncio.get_event_loop().time() < deadline:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status, indexed_at FROM projects WHERE id = $1", pid
                )
            if not row:
                yield 'event: error\ndata: {"error":"project deleted"}\n\n'
                return

            payload = _json.dumps(
                {
                    "status": row["status"],
                    "indexed_at": (
                        row["indexed_at"].isoformat() if row["indexed_at"] else None
                    ),
                }
            )

            # Only emit when something actually changed — saves bandwidth
            # and lets the frontend treat each event as a meaningful update.
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload

            if row["status"] in terminal_states:
                yield "event: done\ndata: [DONE]\n\n"
                return

            await asyncio.sleep(1)

        # Timed out without reaching terminal state — close cleanly so the
        # client knows to retry or treat it as stuck.
        yield 'event: timeout\ndata: {"error":"stream timed out"}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx/proxy buffering
        },
    )


@app.post("/projects/probe", response_model=ProbeResponse, dependencies=[_RequireAuth])
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


# ── Review read endpoints ───────────────────────────────────────────────
#
# Three dashboard-facing endpoints, all read-only:
#   GET /projects/{id}/reviews        — paginated list per project
#   GET /reviews/{id}                  — single review + commit attribution
#   GET /reviews/{id}/findings         — paginated findings, filterable
# Plus an audit log helper:
#   GET /projects/{id}/commits         — every reviewed commit on a project
#
# Design choices:
#   • Filters as query params, not path segments — the dashboard composes
#     URLs as `/projects/X/reviews?branch=dev&status=done`, which is
#     bookmarkable.
#   • Severity sort uses a CASE in SQL, not Python — keeps pagination cursors
#     stable across pages.
#   • Pagination via limit + offset (not cursor-based) — adequate at Phase 1
#     volumes (<10K reviews per project). Switch to keyset if it ever matters.
#   • Counts come from COUNT(*) with the same WHERE clause as the SELECT —
#     correct for any filter combination.

@app.get("/projects/{project_id}/reviews", response_model=ReviewListResponse,
         dependencies=[_RequireAuth])
async def list_project_reviews(
    project_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    branch: str | None = Query(None, description="Filter by branch name"),
    status: str | None = Query(
        None, description="Filter by review status (pending/running/done/error)"
    ),
):
    """List reviews for a project, newest first.

    Returns summary fields only — no `summary` blob, no joined commits.
    The dashboard's reviews-list view paints this directly.
    """
    pid = _parse_uuid(project_id, field="project_id")

    where_parts = ["r.project_id = $1"]
    params: list = [pid]
    if branch:
        params.append(branch)
        where_parts.append(f"r.branch = ${len(params)}")
    if status:
        params.append(status)
        where_parts.append(f"r.status = ${len(params)}")
    where_sql = "WHERE " + " AND ".join(where_parts)

    list_params = [*params, limit, offset]
    limit_p = f"${len(list_params) - 1}"
    offset_p = f"${len(list_params)}"

    async with db_pool.acquire() as conn:
        # Existence check first — distinguishes "no reviews yet" (200, [])
        # from "no such project" (404). Otherwise an unknown UUID would
        # silently return an empty list, masking client bugs.
        exists = await conn.fetchval(
            "SELECT 1 FROM projects WHERE id = $1", pid
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Project not found")

        rows = await conn.fetch(
            f"""
            SELECT {_REVIEW_SUMMARY_COLUMNS}
              FROM reviews r
              {where_sql}
             ORDER BY r.created_at DESC
             LIMIT {limit_p} OFFSET {offset_p}
            """,
            *list_params,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM reviews r {where_sql}", *params
        )

    return ReviewListResponse(
        reviews=[_row_to_review_summary(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@app.get("/reviews/{review_id}", response_model=ReviewDetail, dependencies=[_RequireAuth])
async def get_review(review_id: str):
    """Single review with full `summary` blob + commit attribution.

    `commits[]` is the list of commits attributed to this review via the
    `commits.review_id` FK (Day 5). Old reviews from before that migration
    have NULL review_id on their commits and will return an empty list —
    that's correct behaviour, those reviews predate hard attribution.
    """
    rid = _parse_uuid(review_id, field="review_id")

    async with db_pool.acquire() as conn:
        review_row = await conn.fetchrow(
            f"""
            SELECT {_REVIEW_SUMMARY_COLUMNS}, r.summary
              FROM reviews r
             WHERE r.id = $1
            """,
            rid,
        )
        if not review_row:
            raise HTTPException(status_code=404, detail="Review not found")

        # Hard attribution via the FK — no more time-window guessing.
        # Commits ordered newest-first so the dashboard's "what changed"
        # panel mirrors `git log`.
        commit_rows = await conn.fetch(
            """
            SELECT sha, parent_sha, branch, author_name, author_email,
                   committer_name, committer_email, committed_at,
                   subject, source
              FROM commits
             WHERE review_id = $1
             ORDER BY committed_at DESC
            """,
            rid,
        )

    summary = _row_to_review_summary(review_row)
    return ReviewDetail(
        **summary.model_dump(),
        summary=review_row["summary"],
        commits=[_row_to_commit(r) for r in commit_rows],
    )


@app.get("/reviews/{review_id}/findings", response_model=FindingListResponse,
         dependencies=[_RequireAuth])
async def list_review_findings(
    review_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    severity: list[str] | None = Query(
        None, description="Filter by severity. Repeat the param for multi-select."
    ),
    category: list[str] | None = Query(
        None, description="Filter by category. Repeat the param for multi-select."
    ),
    file_path: str | None = Query(
        None, description="Substring match on file_path (case-insensitive)"
    ),
):
    """Findings for one review, ordered critical → info, then by file/line.

    Filters compose: severity ∈ {...} AND category ∈ {...} AND file_path
    ILIKE %...%. An empty filter list means "no filter on that field".
    """
    rid = _parse_uuid(review_id, field="review_id")

    where_parts = ["f.review_id = $1"]
    params: list = [rid]
    if severity:
        params.append(severity)
        where_parts.append(f"f.severity = ANY(${len(params)}::text[])")
    if category:
        params.append(category)
        where_parts.append(f"f.category = ANY(${len(params)}::text[])")
    if file_path:
        params.append(f"%{file_path}%")
        where_parts.append(f"f.file_path ILIKE ${len(params)}")
    where_sql = "WHERE " + " AND ".join(where_parts)

    list_params = [*params, limit, offset]
    limit_p = f"${len(list_params) - 1}"
    offset_p = f"${len(list_params)}"

    async with db_pool.acquire() as conn:
        # 404 for unknown review_id, same reasoning as the projects endpoint.
        exists = await conn.fetchval(
            "SELECT 1 FROM reviews WHERE id = $1", rid
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Review not found")

        rows = await conn.fetch(
            f"""
            SELECT f.id, f.review_id, f.commit_id, f.severity, f.category,
                   f.file_path, f.start_line, f.end_line, f.message,
                   f.suggestion, f.rule_id,
                   f.code_snippet, f.suggested_code
              FROM review_findings f
              {where_sql}
             ORDER BY CASE f.severity
                        WHEN 'critical' THEN 0
                        WHEN 'major'    THEN 1
                        WHEN 'minor'    THEN 2
                        WHEN 'info'     THEN 3
                        ELSE 4
                      END,
                      f.file_path,
                      f.start_line NULLS LAST
             LIMIT {limit_p} OFFSET {offset_p}
            """,
            *list_params,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM review_findings f {where_sql}", *params
        )

    return FindingListResponse(
        findings=[_row_to_finding(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@app.get("/projects/{project_id}/commits", response_model=CommitListResponse,
         dependencies=[_RequireAuth])
async def list_project_commits(
    project_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    branch: str | None = Query(None, description="Filter by branch"),
    author_email: str | None = Query(
        None, description="Exact match on author_email"
    ),
):
    """Audit log — every reviewed commit on a project.

    This is the full attribution trail: who pushed what, when, on which
    branch, and via which trigger (poll/webhook/manual). Useful for the
    dashboard's "activity" view and for compliance audits.
    """
    pid = _parse_uuid(project_id, field="project_id")

    where_parts = ["c.project_id = $1"]
    params: list = [pid]
    if branch:
        params.append(branch)
        where_parts.append(f"c.branch = ${len(params)}")
    if author_email:
        params.append(author_email)
        where_parts.append(f"c.author_email = ${len(params)}")
    where_sql = "WHERE " + " AND ".join(where_parts)

    list_params = [*params, limit, offset]
    limit_p = f"${len(list_params) - 1}"
    offset_p = f"${len(list_params)}"

    async with db_pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM projects WHERE id = $1", pid
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Project not found")

        rows = await conn.fetch(
            f"""
            SELECT c.sha, c.parent_sha, c.branch, c.author_name, c.author_email,
                   c.committer_name, c.committer_email, c.committed_at,
                   c.subject, c.source
              FROM commits c
              {where_sql}
             ORDER BY c.committed_at DESC
             LIMIT {limit_p} OFFSET {offset_p}
            """,
            *list_params,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM commits c {where_sql}", *params
        )

    return CommitListResponse(
        commits=[_row_to_commit(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


# ── Branch event endpoints ──────────────────────────────────────────────
#
# Two endpoints power the dashboard's "needs attention" banner:
#   GET  /projects/{id}/branch-events  — list, filter by resolved + branch
#   POST /branch-events/{id}/resolve   — operator dismisses one
#
# Force-pushes are recorded by the polling agent (Week 3 Day 2). New-branch
# and branch-deleted events are recorded in Day 5 once we wire the helper
# into the discovery path. The shape is event-type-agnostic — adding a new
# event_type is a worker-side change only, no schema migration.

@app.get(
    "/projects/{project_id}/branch-events",
    response_model=BranchEventListResponse,
    dependencies=[_RequireAuth],
)
async def list_project_branch_events(
    project_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    resolved: bool | None = Query(
        None,
        description="Filter by resolved status. Omit to return both.",
    ),
    branch: str | None = Query(None, description="Filter by branch name"),
    event_type: str | None = Query(
        None,
        description="Filter by event type (force_push, new_branch, branch_deleted)",
    ),
):
    """List branch events for a project, newest first.

    Returns `unresolved_total` alongside the page so the dashboard can
    show a red-dot count even when the user filters to `resolved=true`.
    """
    pid = _parse_uuid(project_id, field="project_id")

    where_parts = ["e.project_id = $1"]
    params: list = [pid]
    if resolved is not None:
        params.append(resolved)
        where_parts.append(f"e.resolved = ${len(params)}")
    if branch:
        params.append(branch)
        where_parts.append(f"e.branch = ${len(params)}")
    if event_type:
        params.append(event_type)
        where_parts.append(f"e.event_type = ${len(params)}")
    where_sql = "WHERE " + " AND ".join(where_parts)

    list_params = [*params, limit, offset]
    limit_p = f"${len(list_params) - 1}"
    offset_p = f"${len(list_params)}"

    async with db_pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM projects WHERE id = $1", pid
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Project not found")

        rows = await conn.fetch(
            f"""
            SELECT e.id, e.project_id, e.branch, e.event_type, e.detail,
                   e.resolved, e.created_at
              FROM branch_events e
              {where_sql}
             ORDER BY e.created_at DESC
             LIMIT {limit_p} OFFSET {offset_p}
            """,
            *list_params,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM branch_events e {where_sql}", *params
        )
        # Unresolved count is independent of the user's filters — the
        # dashboard always shows the same red-dot number regardless of view.
        unresolved_total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM branch_events
             WHERE project_id = $1 AND resolved = false
            """,
            pid,
        )

    return BranchEventListResponse(
        events=[_row_to_branch_event(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
        unresolved_total=unresolved_total or 0,
    )


@app.post(
    "/branch-events/{event_id}/resolve",
    response_model=BranchEventOut,
    dependencies=[_RequireAuth],
)
async def resolve_branch_event(event_id: str):
    """Mark a branch event as resolved (operator dismissal).

    Returns the updated row. 404 on unknown id. Re-resolving an already-
    resolved event is a no-op — idempotent by design so a double-click on
    the dashboard button doesn't 500.
    """
    eid = _parse_uuid(event_id, field="event_id")

    async with db_pool.acquire() as conn:
        # RETURNING * via a single UPDATE keeps the round-trip count down
        # and avoids a TOCTOU between "exists?" and "update".
        row = await conn.fetchrow(
            """
            UPDATE branch_events
               SET resolved = true
             WHERE id = $1
            RETURNING id, project_id, branch, event_type, detail,
                      resolved, created_at
            """,
            eid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Branch event not found")

    return _row_to_branch_event(row)


# ── PDF export (Week 4 Day 4) ───────────────────────────────────────────
#
# GET /reviews/{id}/pdf renders a printable review — the same data the
# ReviewDetail page shows, packaged as a single-file PDF the reviewer can
# email, archive, or attach to a compliance record.
#
# Rendering happens in-process via reportlab. That's fine for Phase 1 —
# a typical review generates ~50KB of PDF in <200ms. If reports get large
# enough to matter, we push the render into a Celery task and return a
# signed URL to the pre-rendered blob (post-Phase-1).

@app.get("/reviews/{review_id}/pdf", dependencies=[_RequireAuth])
async def get_review_pdf(review_id: str):
    """Serve the review as a PDF attachment.

    Returns a ContentDisposition attachment with a filename that includes
    the project name + short SHA so multi-review archives don't collide.
    """
    from fastapi.responses import Response
    from app.reports import render_review_pdf

    rid = _parse_uuid(review_id, field="review_id")

    async with db_pool.acquire() as conn:
        # Same fields as GET /reviews/{id} — kept as separate SQL so we can
        # skip the read-endpoint's Pydantic round-trip.
        review_row = await conn.fetchrow(
            f"""
            SELECT {_REVIEW_SUMMARY_COLUMNS}, r.summary
              FROM reviews r
             WHERE r.id = $1
            """,
            rid,
        )
        if not review_row:
            raise HTTPException(status_code=404, detail="Review not found")

        project_row = await conn.fetchrow(
            "SELECT name, provider, repo_url FROM projects WHERE id = $1",
            review_row["project_id"],
        )
        commits = await conn.fetch(
            """
            SELECT sha, parent_sha, branch, author_name, author_email,
                   committer_name, committer_email, committed_at,
                   subject, source
              FROM commits
             WHERE review_id = $1
             ORDER BY committed_at DESC
            """,
            rid,
        )
        findings = await conn.fetch(
            """
            SELECT f.id, f.review_id, f.commit_id, f.severity, f.category,
                   f.file_path, f.start_line, f.end_line, f.message,
                   f.suggestion, f.rule_id,
                   f.code_snippet, f.suggested_code
              FROM review_findings f
             WHERE f.review_id = $1
             ORDER BY CASE f.severity
                        WHEN 'critical' THEN 0
                        WHEN 'major'    THEN 1
                        WHEN 'minor'    THEN 2
                        WHEN 'info'     THEN 3
                        ELSE 4
                      END,
                      f.file_path,
                      f.start_line NULLS LAST
            """,
            rid,
        )

    # Build the shape render_review_pdf expects — mostly a straight dict
    # coercion. Timestamps get isoformatted so the renderer's parser side
    # (datetime.fromisoformat) handles them uniformly.
    review_dict = {
        "branch": review_row["branch"],
        "before_sha": review_row["before_sha"],
        "after_sha": review_row["after_sha"],
        "status": review_row["status"],
        "severity_counts": review_row["severity_counts"] or {},
        "token_usage": review_row["token_usage"] or {},
        "summary": review_row["summary"],
        "created_at": review_row["created_at"].isoformat(),
        "completed_at": (
            review_row["completed_at"].isoformat()
            if review_row["completed_at"] else None
        ),
    }
    project_dict = {
        "name": project_row["name"] if project_row else "unknown-project",
        "provider": project_row["provider"] if project_row else None,
        "repo_url": project_row["repo_url"] if project_row else None,
    }
    commit_dicts = [
        {
            "sha": c["sha"],
            "author_name": c["author_name"],
            "author_email": c["author_email"],
            "committed_at": c["committed_at"].isoformat(),
            "subject": c["subject"],
            "source": c["source"],
        }
        for c in commits
    ]
    finding_dicts = [
        {
            "severity": f["severity"],
            "category": f["category"],
            "file_path": f["file_path"],
            "start_line": f["start_line"],
            "end_line": f["end_line"],
            "message": f["message"],
            "suggestion": f["suggestion"],
            "suggested_code": f["suggested_code"],
            "code_snippet": f["code_snippet"],
            "rule_id": f["rule_id"],
        }
        for f in findings
    ]

    pdf_bytes = render_review_pdf(
        review=review_dict,
        project=project_dict,
        commits=commit_dicts,
        findings=finding_dicts,
    )

    # Filename shape: "code-review_<project>_<short-sha>.pdf" — keeps a
    # download dir sortable AND immediately identifies which review it is.
    safe_name = "".join(
        c if c.isalnum() or c in "-_." else "-" for c in project_dict["name"]
    )[:60] or "review"
    short_after = review_dict["after_sha"][:8] if review_dict["after_sha"] else "review"
    filename = f"code-review_{safe_name}_{short_after}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, max-age=60",
        },
    )


@app.get("/repos/{repo_id}/status", dependencies=[_RequireAuth])
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
