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

## Setup (manual + Docker)

Coming in Phase 1 Week 0. The plan:

- `docker-compose.yml` + `docker-compose.dev-infra.yml` for local infra (Postgres+pgvector, Redis, Gitea for tests)
- `docker-compose.server.yml` for full server deployment
- `backend/Dockerfile` (multi-stage build)
- Manual instructions for running natively with `uv` against the dev-infra compose

Quick preview of what the manual flow will look like:

```bash
cd backend
uv venv && uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# in a second shell:
uv run celery -A app.workers.tasks.celery worker --loglevel=info
uv run celery -A app.workers.tasks.celery beat --loglevel=info
```

Detailed install + Docker docs land alongside the compose files.

---

## Reference

Full design and phased plan: [`../docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx`](../docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx).
