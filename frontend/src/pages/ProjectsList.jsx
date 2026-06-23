import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  FolderGit2, GitBranch, Github, Globe, Plus, ShieldAlert, Circle,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/layout/States";
import { api, qk } from "@/lib/api";
import { cn, formatDateTime } from "@/lib/utils";

/** Subtle status dot keyed off the project's index/poll lifecycle. */
function StatusDot({ status }) {
  const tone = {
    ready:    "text-severity-info",
    indexing: "text-severity-major animate-pulse",
    pending:  "text-muted-foreground",
    error:    "text-destructive",
  }[status] || "text-muted-foreground";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <Circle className={cn("size-2 fill-current", tone)} />
      {status}
    </span>
  );
}

function ProviderIcon({ provider }) {
  if (provider === "github") return <Github className="size-4" />;
  if (provider === "openforge") return <Globe className="size-4" />;
  return <FolderGit2 className="size-4" />;
}

/**
 * One card per project. Click anywhere on the card to navigate to its detail.
 * Carries a small "N unresolved" badge if branch_events has anything pending —
 * the dashboard's "needs attention" signal.
 */
function ProjectCard({ project }) {
  // Per-project branch-events probe — cheap because backend caches
  // unresolved_total alongside the list. Disabled while project is still
  // indexing (no events possible yet).
  const eventsQuery = useQuery({
    queryKey: qk.projects.branchEvents(project.id, { limit: 1 }),
    queryFn: () => api.listBranchEvents(project.id, { limit: 1 }),
    enabled: project.status === "ready",
    staleTime: 30_000,
  });
  const unresolved = eventsQuery.data?.unresolved_total ?? 0;

  return (
    <Link
      to={`/projects/${project.id}`}
      className="group block focus:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-lg"
    >
      <Card className="transition-colors group-hover:border-primary/40">
        <CardHeader className="space-y-3">
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-1">
              <CardTitle className="flex items-center gap-2 text-base">
                <ProviderIcon provider={project.provider} />
                {project.name}
              </CardTitle>
              <CardDescription className="line-clamp-1 font-mono text-xs">
                {project.repo_url}
              </CardDescription>
            </div>
            {unresolved > 0 ? (
              <Badge variant="outline" className="border-severity-major/40 text-severity-major">
                <ShieldAlert className="mr-1 size-3" />
                {unresolved}
              </Badge>
            ) : null}
          </div>
        </CardHeader>

        <CardContent className="space-y-2 pt-0">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <GitBranch className="size-3" />
              {project.default_branch}
            </span>
            <span aria-hidden>·</span>
            <span>{project.branches_to_review.length} watched</span>
            <span aria-hidden>·</span>
            <StatusDot status={project.status} />
          </div>
          <div className="text-xs text-muted-foreground">
            Added {formatDateTime(project.created_at)}
            {project.last_polled_at ? (
              <> · last polled {formatDateTime(project.last_polled_at)}</>
            ) : null}
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

export default function ProjectsList() {
  const query = useQuery({
    queryKey: qk.projects.list({ limit: 100 }),
    queryFn: () => api.listProjects({ limit: 100 }),
  });

  return (
    <section className="space-y-6">
      <header className="flex items-end justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">Projects</h1>
          <p className="text-sm text-muted-foreground">
            Repositories the review agent is watching. Reviews are produced on every push
            to a watched branch.
          </p>
        </div>
        <Button asChild>
          <Link to="/projects/new">
            <Plus className="size-4" />
            New project
          </Link>
        </Button>
      </header>

      {query.isLoading ? <LoadingState label="Loading projects…" /> : null}
      {query.isError ? <ErrorState error={query.error} /> : null}

      {query.data && query.data.projects.length === 0 ? (
        <EmptyState
          icon={FolderGit2}
          title="No projects yet"
          description="Connect a repository to start receiving senior-reviewer-grade code reviews on every push."
          action={
            <Button asChild>
              <Link to="/projects/new">
                <Plus className="size-4" />
                Add your first project
              </Link>
            </Button>
          }
        />
      ) : null}

      {query.data && query.data.projects.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {query.data.projects.map((p) => (
            <ProjectCard key={p.id} project={p} />
          ))}
        </div>
      ) : null}
    </section>
  );
}
