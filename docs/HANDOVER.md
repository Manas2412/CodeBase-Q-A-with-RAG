# Handover — Code Review Agent (Phase 1)

> Ops-facing runbook. If you're onboarding new engineers to run this system,
> point them here.

## Architecture at a glance

```
┌────────────┐    HTTPS     ┌────────────┐    async     ┌──────────────┐
│  Browser   │ ───────────► │  FastAPI   │ ───────────► │  Postgres    │
│  (React)   │              │  (uvicorn) │              │  + pgvector  │
└────────────┘              └────────────┘              └──────────────┘
                                   │                            ▲
                                   │ enqueues                   │
                                   ▼                            │
                            ┌────────────┐                      │
                            │   Redis    │                      │
                            └────────────┘                      │
                                   ▲                            │
                                   │ drains                     │
                            ┌──────┴──────┐                     │
                            │   Celery    │ ── clones repos ──► │
                            │   worker    │    embeds, reviews  │
                            └─────────────┘                     │
                                   ▲                            │
                                   │ every 5 min                │
                            ┌──────┴──────┐                     │
                            │  Celery     │ ─── fan-out ───────►│
                            │  Beat       │
                            └─────────────┘
```

**Data flow:**
1. Beat ticks → enqueues `poll_all_projects_task` on Redis
2. Worker drains the queue, spawns one `check_project_for_changes_task` per project
3. Each check compares the branch tip against `last_reviewed_sha`; on forward move it enqueues `review_push_task`
4. `review_push_task` claims a `reviews` row atomically, upserts commit attribution (with FK), calls Bedrock (Claude Opus + Cohere embed), writes findings, bumps `last_reviewed_sha`
5. Dashboard reads through FastAPI

## Environment

Copy `backend/.env-sample` → `backend/.env` and fill in.

**Absolutely required:**
- `DATABASE_URL` — Postgres 15+ with pgvector extension
- `REDIS_URL`
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — IAM user with Bedrock invoke perms (never root)
- `AWS_REGION` — must be a Bedrock region (us-east-1 works)
- `OPENFORGE_TOKEN` — Tuleap PAT with REST + Git scope, from a service account
- `DASHBOARD_PASSWORD` — shared password for dashboard access
- `SESSION_SECRET` — long random string; rotate to force logout

**Recommended:**
- `SESSION_HTTPS_ONLY=true` in production
- `CORS_ORIGINS` — production hostnames
- `GITHUB_TOKEN` — only if GitHub repos are onboarded

## Running

### Development (5 terminals)

```bash
# 1. Infra
docker compose -f docker-compose.yml -f docker-compose.dev-infra.yml up -d postgres redis

# 2. FastAPI
cd backend && source .venv/bin/activate
uv run uvicorn app.main:app --reload --port 8000

# 3. Worker
uv run celery -A app.workers.tasks.celery worker --loglevel=info

# 4. Beat (15s cadence for dev; production defaults to 5min)
POLL_INTERVAL_SECONDS=15 uv run celery -A app.workers.tasks.celery beat --loglevel=info

# 5. Frontend
cd ../frontend && npm run dev
```

Dashboard: `http://localhost:5173/`. Log in with `DASHBOARD_PASSWORD`.

### Production

`docker compose -f docker-compose.yml -f docker-compose.server.yml up -d` launches the same four processes behind Caddy on ports 80/443. Ensure all env vars are set on the compose host.

## Common tasks

### Onboard a project

1. Log into the dashboard
2. Click **New project**
3. Paste the git URL → **Continue** (probe fetches branches)
4. Pick branches to watch → **Create project**
5. Watch indexing → **Open project** when green

The polling agent baselines each watched branch at its current HEAD. Reviews fire on subsequent pushes.

### Rotate the dashboard password

```bash
# 1. Change DASHBOARD_PASSWORD in backend/.env
# 2. Restart FastAPI (uvicorn reload catches env changes on start)
```

Existing sessions keep working until they expire (14 days) or the user logs out. To force all sessions to end immediately, also rotate `SESSION_SECRET`.

### Rotate the OpenForge / GitHub PAT

```bash
# 1. Update OPENFORGE_TOKEN (or GITHUB_TOKEN) in backend/.env
# 2. Restart FastAPI AND the Celery worker (provider clients read env at task time)
```

### Debug a stuck review

```bash
# Which reviews are in-flight?
docker exec codereview-postgres psql -U codereview -d codereview -c \
  "SELECT id, project_id, branch, status, created_at
     FROM reviews WHERE status IN ('pending', 'running')
    ORDER BY created_at DESC;"

# Worker's live logs
docker logs -f codereview-worker  # or tail the terminal it's running in

# Bedrock call traces
grep -i bedrock backend-logs/... | tail -30
```

**Common failure modes:**
- `expected maxLength: 2048` from Cohere — a code chunk exceeded the per-text limit. Fixed in embedder, shouldn't recur.
- `ValidationException: Operation not allowed` from Bedrock — likely using AWS root creds. Switch to an IAM user with `bedrock:InvokeModel`.
- Force-push detected → banner shows on the project page; resolve after human review.

### Re-index a project from scratch

```bash
# Delete the persisted clone (worker will re-mirror on next task)
rm -rf ~/.codereview/repos/<project_id>

# Delete chunks for that project
docker exec codereview-postgres psql -U codereview -d codereview -c \
  "DELETE FROM chunks WHERE project_id = '<project_id>';"

# Reset status and let the polling agent kick indexing again
docker exec codereview-postgres psql -U codereview -d codereview -c \
  "UPDATE projects SET status = 'pending', indexed_at = NULL WHERE id = '<project_id>';"
```

### Backups

- **Postgres** — nightly `pg_dump` including the vector extension. `pg_dump -h ... -U codereview codereview | gzip > backup.sql.gz`.
- **Persistent clones** at `~/.codereview/repos/` — cheap to rebuild from upstream, no backup required.
- **Redis** — ephemeral queue state; no backup required.

## Where things live

| Concern | File |
|---|---|
| Provider abstraction (OpenForge / GitHub) | `backend/app/providers/` |
| Clone management (mirror + worktrees) | `backend/app/storage/clone_manager.py` |
| Chunker + embedder | `backend/app/ingestion/` |
| Diff parser + context builder | `backend/app/review/diff_parser.py`, `context_builder.py` |
| Reviewer (LLM + persistence) | `backend/app/review/reviewer.py` |
| Bedrock client | `backend/app/review/bedrock_client.py` |
| Polling worker | `backend/app/workers/tasks.py` |
| Beat schedule | `backend/app/scheduling/beat.py` |
| API routes | `backend/app/main.py` |
| PDF renderer | `backend/app/reports/pdf.py` |
| Auth | `backend/app/auth.py` |
| Migrations | `backend/migrations/versions/` |
| Frontend routes | `frontend/src/App.jsx` |
| Dashboard pages | `frontend/src/pages/` |
| API client + query keys | `frontend/src/lib/api.js` |

## Phase 2 / roadmap

Held over for post-Phase-1:

- **Pre-merge review via webhooks** — review on PR raise, post findings as a PR comment
- **AI-assisted merge conflict resolution**
- **Per-user auth** — small users table + password hashes
- **Multi-language tree-sitter** — Go, Java, Rust, C++
- **RJSF checklist editor** in the dashboard
- **`POST /projects/{id}/baseline-audit`** — the "review everything since I onboarded" button
- **Cache-hit telemetry** — investigate why `cache_read` reports 0 on subsequent reviews
