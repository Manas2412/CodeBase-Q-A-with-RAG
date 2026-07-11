import { Navigate, Route, Routes } from "react-router-dom";

import Shell from "@/components/layout/Shell";
import RequireAuth from "@/components/layout/RequireAuth";
import Login from "@/pages/Login";
import ProjectsList from "@/pages/ProjectsList";
import ProjectDetail from "@/pages/ProjectDetail";
import ReviewDetail from "@/pages/ReviewDetail";
import Wizard from "@/pages/Wizard";
import NotFound from "@/pages/NotFound";

/**
 * Route table.
 *
 * Two layers:
 *   • `<Login />` is public — the only route reachable without a session.
 *   • Everything else is wrapped in `<RequireAuth />` → `<Shell />`.
 *     The guard redirects to /login when unauthenticated and preserves
 *     the target path in `location.state.from` so post-login navigation
 *     lands where the user tried to go.
 *
 * `/projects/new` sits above `/projects/:projectId` — react-router
 * matches literal segments first, but we make it explicit so it doesn't
 * silently reverse when someone reorders.
 */
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth />}>
        <Route element={<Shell />}>
          <Route index element={<Navigate to="/projects" replace />} />
          <Route path="projects" element={<ProjectsList />} />
          <Route path="projects/new" element={<Wizard />} />
          <Route path="projects/:projectId" element={<ProjectDetail />} />
          <Route path="reviews/:reviewId" element={<ReviewDetail />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}
