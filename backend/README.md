# Backend — Codebase Review Agent

FastAPI service + Celery workers + Celery Beat scheduler. Handles ingestion, retrieval, polling, review, and reporting against PostgreSQL with pgvector.

---

## Stack

| Layer | Tech |
|---|---|
| Language | Python 3.11+ (managed with `uv`) |
| Web framework | FastAPI + uvicorn |
| Async tasks | Celery (Redis broker + result backend) |
| Scheduler | Celery Beat (5-minute polling cadence) |
| Database | PostgreSQL 15+ with pgvector extension |
| ORM | SQLAlchemy 2 + Alembic for migrations |
| Vector dim | 1024 (matches Cohere Embed Multilingual v3) |
| Code chunking | tree-sitter (Phase 1: Python, JS/TS, Java, Go, Rust, C, C++) |
| Diff parsing | unidiff |
| LLM client | boto3 (AWS Bedrock Converse API) |
| Embeddings client | boto3 (AWS Bedrock invoke_model) |
| Git ops | GitPython + subprocess for `ls-remote`, `fetch`, `merge-tree`, `diff` |
| PDF reports | reportlab |

---

## Module map

```
backend/
├── app/
│   ├── main.py                # FastAPI app, routes, lifespan
│   ├── db/                    # SQLAlchemy models, asyncpg pool
│   │   ├── models.py
│   │   └── database.py
│   ├── ingestion/             # Codebase → pgvector (the "memory" pipeline)
│   │   ├── cloner.py          # Clone + file walking
│   │   ├── chunker.py         # tree-sitter AST chunks
│   │   ├── embedder.py        # Bedrock Cohere embeddings (1024 dim)
│   │   └── indexer.py         # pgvector upserts + incremental updates
│   ├── query/                 # Q&A surface (retained from CodeBase Q&A project)
│   │   ├── hyde.py
│   │   ├── retriever.py
│   │   └── answerer.py
│   ├── workers/               # Celery tasks
│   │   └── tasks.py
│   │
│   │ ── Phase 1 additions (incoming, not yet present) ────
│   ├── providers/             # OpenForge + GitHub URL/auth/list_branches
│   ├── storage/               # Persistent clone manager
│   ├── review/                # diff_parser, context_builder, reviewer,
│   │                          #   bedrock_client, checklist, report_pdf
│   ├── auth/                  # Session auth (cookie based in Phase 1)
│   └── scheduling/            # Celery Beat config
│
├── migrations/                # Alembic migrations
├── alembic.ini
├── pyproject.toml
├── uv.lock
├── .python-version
├── .env-sample                # template — copy to .env, fill in values
└── README.md                  # this file
```

---

## Endpoints

### Today (Q&A surface — retained)

| Method | Path | Purpose |
|---|---|---|
| POST | `/repos` | Enqueue indexing for a repo URL; returns `repo_id` + status |
| GET | `/repos/{repo_id}/status` | Poll until `ready` / `error` |
| POST | `/query` | SSE stream of an answer to a natural-language question |

### Phase 1 (incoming)

| Method | Path | Purpose |
|---|---|---|
| POST | `/projects/probe` | URL → branches[] via `git ls-remote` (wizard step 1→2) |
| POST | `/projects` | Create project with selected branches + checklist |
| GET | `/projects` | List registered projects |
| GET | `/projects/{id}` | Project detail + last-review status per branch |
| GET | `/projects/{id}/index-status` | Live SSE for indexing progress (wizard step 4) |
| POST | `/projects/{id}/baseline-audit` | Run a one-off baseline review of the whole codebase |
| GET | `/reviews` | List reviews (filter by project / branch) |
| GET | `/reviews/{id}/findings` | Findings for a review |
| GET | `/reviews/{id}/download` | PDF report |
| GET, POST, PUT | `/checklists` | Checklist CRUD with versioning |
| GET | `/branch-events` | New-branch detections + force-push alerts |

---

## Environment variables

Copy `.env-sample` to `.env` and fill in the values. Required keys (Phase 1):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL DSN (asyncpg-compatible) |
| `REDIS_URL` | Redis broker for Celery + retrieval cache |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` | Bedrock credentials (use the `code-review-bot` IAM user, not root) |
| `BEDROCK_LLM_MODEL` | `us.anthropic.claude-opus-4-6-v1` |
| `BEDROCK_EMBED_MODEL` | `cohere.embed-multilingual-v3` |
| `BEDROCK_EMBED_INPUT_TYPE_DOC` | `search_document` |
| `BEDROCK_EMBED_INPUT_TYPE_QUERY` | `search_query` |
| `EMBED_DIMENSION` | `1024` |
| `OPENFORGE_TOKEN` | `tlp.k1.…` personal access key on a service account |
| `GITHUB_TOKEN` | Optional — only if internal GitHub repos are also onboarded |

> `max_tokens` is **not** an env var. It's a constant (`MAX_OUTPUT_TOKENS = 16384`) in `app/review/bedrock_client.py`. Update there when upgrading models.

---

## Migrations

Alembic, run from `backend/`:

```bash
cd backend
uv run alembic upgrade head    # bring DB to latest schema
uv run alembic revision --autogenerate -m "describe change"
```

Phase 1 Week 1 introduces a single migration (`002_review_agent.py`) that:

- Renames `repos` → `projects` with new columns
- Adds `commits`, `reviews`, `review_findings`, `checklists`, `users`, `branch_events` tables
- Changes `chunks.embedding` from `Vector(1536)` → `Vector(1024)` (one-time re-index required)
- Adds `chunks.commit_sha` for incremental pruning

---

## Tests

Layered structure landing in Phase 1 Week 0:

```
backend/tests/
├── unit/            # Pure functions: parsers, providers, attribution
├── integration/     # Postgres+pgvector via testcontainers, mocked Bedrock
├── e2e/             # Full flows against synthetic repos
└── eval/            # Golden-diff corpus + quality scoring
```

Run with `uv run pytest`. CI guardrail caps real Bedrock token spend per run.

---

## Setup

Two ways to run the backend: natively with `uv` (best for hot-reload during development) or fully containerised via docker-compose (matches the server deployment).

### Prerequisites

- Python 3.11+ (the project pins `>=3.11` and uses `uv` to manage the venv — install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker Desktop (or any Docker daemon) for the Postgres + Redis containers
- `git` on PATH (used by the future polling agent for `clone` / `fetch` / `ls-remote`)

### Step 1 — bring up the datastores

From the **repo root**, not from `backend/`:

```bash
docker compose -f docker-compose.yml up -d
```

This starts Postgres+pgvector on host port 5434 and Redis on host port 6380, with named volumes that persist data across restarts. Verify:

```bash
docker compose ps
docker exec codereview-postgres pg_isready -U codereview
# Expect:  /var/run/postgresql:5432 - accepting connections
```

### Step 2 — copy and fill in `.env`

```bash
cd backend
cp .env-sample .env
```

The defaults already point at the local docker containers. The only values you need to fill in are:

| Variable | Where to get it |
|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | AWS Console → IAM → `code-review-bot` user → Security credentials → Create access key |
| `OPENFORGE_TOKEN` | OpenForge → your service account → Preferences → Personal Access Keys → Generate new key (scopes: REST + Git repository) |
| `GITHUB_TOKEN` *(optional)* | GitHub → Settings → Developer settings → Personal access tokens (only if you also onboard internal GitHub repos) |

### Step 3 — install dependencies

```bash
# Still in backend/
uv venv                    # creates backend/.venv with the pinned Python
uv sync                    # installs everything from pyproject.toml + uv.lock
```

`uv sync` resolves ~70 packages on first run; subsequent runs are near-instant when nothing changes.

### Step 4 — run database migrations

```bash
uv run alembic upgrade head
```

Verify the schema landed:

```bash
docker exec codereview-postgres psql -U codereview -d codereview -c "\dt"
# Expect:  alembic_version, chunks, repos
```

### Step 5 — run the API

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

`--reload` enables hot-reload on source changes — drop it for production-style runs. Verify:

```bash
curl -s -X POST http://127.0.0.1:8000/repos \
  -H "Content-Type: application/json" \
  -d '{"github_url":"https://github.com/octocat/Hello-World"}'
# Expect:  {"repo_id":"<uuid>","status":"pending"}
```

### Step 6 — run the Celery worker (separate shell)

```bash
cd backend
source .venv/bin/activate    # or run via `uv run` if you prefer
celery -A app.workers.tasks.celery worker --loglevel=info
```

### Step 7 — run Celery Beat (separate shell, Phase 1 Week 3 onwards)

Beat is the scheduler that fires the polling agent. Only needed once Week 3 lands; ignore for Week 0–2:

```bash
celery -A app.workers.tasks.celery beat --loglevel=info
```

---

## Running everything in Docker

For server deployment or full-stack testing, skip the native steps above and use:

```bash
# From repo root
docker compose -f docker-compose.yml -f docker-compose.server.yml up -d

# Verify everything is healthy
docker compose -f docker-compose.yml -f docker-compose.server.yml ps

# Test the API via Caddy on :80
curl -s http://localhost/healthz                 # frontend health
curl -s -X POST http://localhost/api/repos \
  -H "Content-Type: application/json" \
  -d '{"github_url":"https://github.com/octocat/Hello-World"}'
# Expect:  {"repo_id":"<uuid>","status":"pending"}

# Bring it all down
docker compose -f docker-compose.yml -f docker-compose.server.yml down
```

This brings up `backend-api`, `backend-worker`, `backend-beat`, `frontend`, and `caddy` — five services on top of the postgres + redis from the base compose.

---

## Tests

```bash
cd backend
uv run pytest                # everything fast (unit + future integration)
uv run pytest tests/unit     # unit only
uv run pytest -v             # verbose, show every test name
uv run pytest -m "not eval"  # everything except the Bedrock-spending eval suite
```

See [`backend/tests/README.md`](tests/README.md) for the layout, markers, and fixtures.

---

## Building the Docker image manually

You usually let `docker compose build` do this, but the standalone command is:

```bash
# From repo root
docker build -t codereview-backend:dev ./backend
```

The image is multi-stage: uv binary → builder venv (deps cached) → `python:3.11-slim` runtime with `git` and a non-root `app` user. Final size: ~610MB.

---

## Reference

Full design and phased plan: [`../docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx`](../docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx).
