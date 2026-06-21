# Frontend — Codebase Review Agent

React + Vite single-page app. Internal-only dashboard for the review agent.

---

## Stack

| Layer | Tech |
|---|---|
| Framework | React 18 + Vite |
| Language | JavaScript (JSX). Migration to TypeScript not blocking. |
| Styling | Tailwind utility classes |
| Icons | lucide-react |
| Forms (Phase 1) | react-jsonschema-form (RJSF) for the checklist editor |
| Markdown rendering | react-markdown (for the existing Q&A surface) |
| Routing (Phase 1) | react-router-dom |
| State | Local component state for now. No global store needed in Phase 1. |
| Build | Vite (ESBuild + Rollup) |

---

## Structure

```
frontend/
├── index.html
├── package.json
├── package-lock.json
├── vite.config.js
├── eslint.config.js
├── .gitignore
├── public/
│   ├── favicon.svg
│   └── icons.svg
└── src/
    ├── main.jsx
    ├── App.jsx          # current single-page Q&A UI (retained during transition)
    ├── index.css        # Tailwind entry + tokens
    │
    │ ── Phase 1 additions (incoming, not yet present) ────
    ├── routes/          # react-router-dom route definitions
    ├── wizard/          # 4-step onboarding: Connect → Branches → Checklist → Indexing
    ├── dashboard/       # ProjectsList, ProjectDetail, ReviewDetail, FindingCard
    ├── checklist/       # ChecklistsList, ChecklistEditor (RJSF form + JSON view)
    ├── auth/            # Login screen, session helpers
    └── shared/          # API client, hooks, formatters
```

---

## Today (single-page Q&A UI)

`src/App.jsx` is the entire current frontend — a single-page Q&A interface against the backend's `/repos`, `/repos/{id}/status`, and `/query` endpoints. It hits `http://127.0.0.1:8000` by default (see `API_BASE_URL` in `App.jsx`).

This remains functional throughout Phase 1 so the Q&A surface doesn't break during the refactor.

---

## Phase 1 — what the frontend becomes

A multi-route dashboard for the review agent. Routes:

| Route | Purpose |
|---|---|
| `/login` | Username + password (session cookie) |
| `/projects` | List of registered projects with last-review status per branch |
| `/projects/new` | The 4-screen onboarding wizard |
| `/projects/:id` | Project detail with branch chips, recent reviews, settings |
| `/projects/:id/reviews/:reviewId` | Findings grouped by severity, commit chain with authors, "Download PDF" |
| `/checklists` | Checklist list and version history |
| `/checklists/new` | RJSF-based editor (form view + raw JSON view) |
| `/settings` | Service-account tokens (display-only), defaults |

---

## Setup

### Prerequisites

- Node.js 20 or newer (`brew install node@20` on macOS)
- The backend running somewhere reachable — either natively on `http://127.0.0.1:8000` or via the server compose at `http://localhost/api`

### Step 1 — install dependencies

```bash
cd frontend
npm install
```

### Step 2 — run the Vite dev server

```bash
npm run dev
```

Opens at **http://127.0.0.1:5173** with hot-module reload. Currently shows the single-page Q&A UI from the original CodeBase Q&A project — the wizard + dashboard land during Phase 1 Week 4.

### Step 3 — point at the backend

The current `src/App.jsx` hard-codes:

```javascript
const API_BASE_URL = 'http://127.0.0.1:8000';
```

Adjust if the backend is on a different port or behind Caddy. A proper env-driven config (`VITE_API_BASE_URL`) lands during the Phase 1 Week 4 refactor.

### Build for production

```bash
npm run build
```

Produces `dist/` — the static bundle served by nginx in the production Docker image.

```bash
npm run preview
```

Serves the built `dist/` locally for a final sanity check before deploying.

---

## Running in Docker

For server-style runs (everything behind Caddy + nginx serving the built SPA):

```bash
# From repo root, not frontend/
docker compose -f docker-compose.yml -f docker-compose.server.yml up -d

# Open the dashboard via Caddy on :80
open http://localhost/
```

Bring it down:

```bash
docker compose -f docker-compose.yml -f docker-compose.server.yml down
```

---

## Building the Docker image manually

```bash
# From repo root
docker build -t codereview-frontend:dev ./frontend
```

The image is two-stage: `node:20-alpine` runs `npm install` + `npm run build`, then `nginx:1.27-alpine` serves the `dist/`. Final size: ~76MB.

Verify the image:

```bash
docker run -d --rm --name codereview-frontend-test -p 8081:80 codereview-frontend:dev
sleep 2
curl -s http://localhost:8081/healthz
# Expect:  ok
docker stop codereview-frontend-test
```

---

## Notes on `npm ci` vs `npm install` inside Docker

The Dockerfile uses `npm install` (not `npm ci`) because some npm versions resolve platform-conditional transitive dependencies (`@emnapi/*`) differently. `npm install` is forgiving; `npm ci` is strict and refuses to install when the lockfile is "out of sync" — even when nothing is actually broken.

If you want strict reproducible builds, upgrade your local npm to match the container (`npm install -g npm@latest`) and revert the Dockerfile to `npm ci`.

---

## Reference

Full design and phased plan: [`../docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx`](../docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx).
