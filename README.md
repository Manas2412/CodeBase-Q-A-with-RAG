# Internal Codebase Review Agent

An internal, agentic code-review system that watches branches on **OpenForge (Tuleap)** and **GitHub**, runs a senior-tech-lead-grade review on every push and merge using **Claude Opus 4.6 via AWS Bedrock**, and produces downloadable PDF reports with full author attribution.

Built on top of the prior **CodeBase Q&A with RAG** project — the RAG pipeline (tree-sitter chunking + pgvector + embeddings) is reused as the model's memory of the codebase, while a new diff-only review pipeline runs on top.

> **Status: Phase 1 in development.** The existing Q&A surface (`/repos`, `/repos/{id}/status`, `/query`) remains functional during the transition. The review pipeline, polling trigger, wizard onboarding, and checklist UI land across the 5 weeks of Phase 1.

---

## Architecture in one line

Two pipelines share one pgvector store:

- **Indexing pipeline** (cheap, no LLM): clones repos, AST-chunks them with tree-sitter, embeds chunks via Cohere Embed v3 on Bedrock, stores in pgvector. The model's "memory of the codebase."
- **Review pipeline** (Claude Opus 4.6 on Bedrock): on every push to a watched branch, computes the diff, retrieves related context from pgvector, sends `diff + context + checklist` to Opus, persists structured findings, generates a PDF report.

The LLM **never sees the whole codebase** — only the diff plus a small RAG-retrieved context window. Cost stays bounded regardless of repo size.

Detailed design: [`docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx`](docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx).

---

## Repository layout

```
.
├── backend/        # FastAPI + Celery + pgvector — see backend/README.md
├── frontend/       # React + Vite SPA — see frontend/README.md
├── docs/           # Implementation plan and design references
├── README.md       # this file
└── .gitignore
```

---

## Tech stack at a glance

| Layer | Tech |
|---|---|
| API | FastAPI + uvicorn |
| Async work | Celery + Celery Beat (Redis broker) |
| Database | PostgreSQL 15+ with pgvector |
| Code chunking | tree-sitter (Python, JS/TS, Java, Go, Rust, C, C++) |
| LLM (review) | AWS Bedrock — `us.anthropic.claude-opus-4-6-v1` |
| Embeddings | AWS Bedrock — `cohere.embed-multilingual-v3` (1024 dim) |
| Frontend | React 18 + Vite + Tailwind |
| Trigger (Phase 1) | Outbound polling (every 5 min). Webhook upgrade path in Phase 1.5. |
| Source platforms | OpenForge (Tuleap) + GitHub |
| Container runtime | Docker + docker-compose |

---

## What works today vs what's coming

| Surface | Today | Phase 1 |
|---|---|---|
| Q&A over a single repo via HyDE → pgvector → LLM | ✅ Working | Retained |
| Add a repo via URL | ✅ `/repos` (POST) | Extended into wizard with branch picker |
| Branch-aware push trigger | ❌ | ✅ Polling agent (every 5 min) |
| AI review with senior-tech-lead checklist | ❌ | ✅ Claude Opus on Bedrock |
| PDF report per review | ❌ | ✅ Downloadable per review |
| Author + committer attribution | ❌ | ✅ From `git log` ranges |
| Force-push detection | ❌ | ✅ Dashboard banner |
| Checklist UI | ❌ | ✅ react-jsonschema-form based |
| Webhook ingress | ❌ | Phase 1.5 |
| Pre-merge conflict prediction | ❌ | Phase 1.5 (zero LLM cost) |
| AI-assisted conflict resolution (advisory) | ❌ | Phase 2 |
| SSO / multi-tenant | ❌ | Phase 3 |

---

## Operational prerequisites (before Phase 1)

1. **OpenForge service account + `tlp.k1` personal access key** → stored as `OPENFORGE_TOKEN`
2. **AWS IAM user** (`code-review-bot`) with `AmazonBedrockFullAccess` → access keys stored in `backend/.env`
3. *(Optional)* **GitHub PAT** if internal GitHub repos are also onboarded → stored as `GITHUB_TOKEN`

No public ingress, no platform-team coordination, no IP allowlisting — by design, the review service makes only outbound calls.

---

## Setup, run, and deploy

Coming in Phase 1 Week 0 — docker-compose files (separate dev-infra and server modes), Dockerfiles for backend and frontend, and a CI workflow. For backend-specific or frontend-specific instructions, see the sub-READMEs.

- Backend setup: [`backend/README.md`](backend/README.md)
- Frontend setup: [`frontend/README.md`](frontend/README.md)

---

## License

See project license if provided; dependencies retain their original licenses.
