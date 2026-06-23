import { Navigate, Route, Routes } from "react-router-dom";

import Shell from "@/components/layout/Shell";
import ProjectsList from "@/pages/ProjectsList";
import ProjectDetail from "@/pages/ProjectDetail";
import ReviewDetail from "@/pages/ReviewDetail";
import Wizard from "@/pages/Wizard";
import NotFound from "@/pages/NotFound";

/**
 * Route table for the dashboard.
 *
 * Nested under `<Shell />` so every page inherits the top nav + container.
 * `/projects/new` is intentionally above `/projects/:projectId` — react-router
 * matches in order and the literal `new` would otherwise be captured as an id.
 */
export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/projects" replace />} />
        <Route path="projects" element={<ProjectsList />} />
        <Route path="projects/new" element={<Wizard />} />
        <Route path="projects/:projectId" element={<ProjectDetail />} />
        <Route path="reviews/:reviewId" element={<ReviewDetail />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
