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

## Setup (manual + Docker)

Coming in Phase 1 Week 0 alongside the docker-compose files.

Quick preview of the manual flow:

```bash
cd frontend
npm install
npm run dev          # Vite dev server at http://127.0.0.1:5173
```

The dev server proxies API calls to the backend running at `http://127.0.0.1:8000`. Production build will be served via Caddy as part of the server-mode docker-compose.

---

## Reference

Full design and phased plan: [`../docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx`](../docs/Codebase_Review_Agent_Implementation_Plan_v3_3_FINAL.docx).
