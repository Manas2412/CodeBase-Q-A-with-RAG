/**
 * Thin fetch wrappers + TanStack Query keys.
 *
 * Why centralise these
 * ====================
 * The dashboard touches the same backend resources from many places —
 * project lists, detail pages, the wizard, the branch-events banner.
 * Having a single source of truth for both endpoint URLs and query keys:
 *
 *   • Lets `queryClient.invalidateQueries(qk.projects.all)` reach every
 *     view of the projects list at once (e.g. after creating a new project).
 *   • Makes the route → request shape explicit; refactors are a single-file
 *     edit instead of a grep.
 *   • Standardises error shape — every wrapper throws `ApiError` with a
 *     human-readable `.message` lifted from FastAPI's `{detail: …}`.
 *
 * Base URL
 * ========
 * In dev, Vite proxies `/projects`, `/reviews`, `/branch-events`, `/repos`
 * to FastAPI on :8000 — same-origin from the React side. In production
 * Caddy reverse-proxies in front of both, also same-origin. So we never
 * set `Host` or absolute URLs in the API client; it's always relative.
 */

export class ApiError extends Error {
  constructor(message, { status, body } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/** Build a URLSearchParams string, omitting null/undefined/"". */
function qs(params) {
  if (!params) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => usp.append(k, x));
    else usp.append(k, v);
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

async function request(path, { method = "GET", body, headers } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(headers || {}),
    },
    // Same-origin cookies (dev proxy + prod behind Caddy). Without this
    // the session cookie doesn't ride on the request in some browser
    // configurations.
    credentials: "same-origin",
    body: body ? JSON.stringify(body) : undefined,
  });

  // 204 / empty body — succeed with null instead of choking on JSON.parse.
  if (res.status === 204) return null;

  const text = await res.text();
  const data = text ? safeJson(text) : null;

  if (!res.ok) {
    const detail =
      (data && data.detail) ||
      (typeof data === "string" ? data : null) ||
      `Request failed (${res.status})`;
    throw new ApiError(detail, { status: res.status, body: data });
  }
  return data;
}

function safeJson(text) {
  try { return JSON.parse(text); } catch { return text; }
}

// ── Endpoints ─────────────────────────────────────────────────────────────
export const api = {
  // Auth
  authMe: () => request("/auth/me"),
  authLogin: (password) => request("/auth/login", { method: "POST", body: { password } }),
  authLogout: () => request("/auth/logout", { method: "POST" }),

  // Projects
  listProjects: (params) => request(`/projects${qs(params)}`),
  getProject: (id) => request(`/projects/${id}`),
  probeProject: (url) => request("/projects/probe", { method: "POST", body: { url } }),
  createProject: (payload) => request("/projects", { method: "POST", body: payload }),

  // Reviews
  listProjectReviews: (id, params) => request(`/projects/${id}/reviews${qs(params)}`),
  getReview: (id) => request(`/reviews/${id}`),
  listReviewFindings: (id, params) => request(`/reviews/${id}/findings${qs(params)}`),

  // PDF is served as a binary attachment — return the path so the browser
  // can drive the download directly. Vite's dev proxy handles same-origin.
  reviewPdfUrl: (id) => `/reviews/${id}/pdf`,

  // Commits (audit log)
  listProjectCommits: (id, params) => request(`/projects/${id}/commits${qs(params)}`),

  // Branch events
  listBranchEvents: (id, params) => request(`/projects/${id}/branch-events${qs(params)}`),
  resolveBranchEvent: (id) => request(`/branch-events/${id}/resolve`, { method: "POST" }),
};

// ── Query keys ────────────────────────────────────────────────────────────
/**
 * Convention: arrays starting with the resource name, then narrowing.
 * Invalidating `qk.projects.all()` clears every project query. Invalidating
 * `qk.projects.detail(id)` clears just that one project.
 */
export const qk = {
  projects: {
    all: () => ["projects"],
    list: (params) => ["projects", "list", params || {}],
    detail: (id) => ["projects", "detail", id],
    reviews: (id, params) => ["projects", id, "reviews", params || {}],
    commits: (id, params) => ["projects", id, "commits", params || {}],
    branchEvents: (id, params) => ["projects", id, "branch-events", params || {}],
  },
  reviews: {
    detail: (id) => ["reviews", "detail", id],
    findings: (id, params) => ["reviews", id, "findings", params || {}],
  },
};
