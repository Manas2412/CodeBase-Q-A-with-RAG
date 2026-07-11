import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle, ArrowRight, ChevronLeft, Download, ExternalLink, FileText,
  GitBranch, ListFilter, Loader2, Search,
} from "lucide-react";

import { Badge, SeverityBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { CommitRow } from "@/components/reviews/CommitRow";
import { FindingCard } from "@/components/reviews/FindingCard";
import { SeverityBar } from "@/components/reviews/SeverityBar";
import { EmptyState, ErrorState, LoadingState } from "@/components/layout/States";

import { api, qk } from "@/lib/api";
import { cn, formatDateTime, shortSha } from "@/lib/utils";

const SEVERITIES = ["critical", "major", "minor", "info"];

/** Toggle chip for severity filter — click to toggle inclusion in the filter. */
function SeverityChip({ severity, active, onToggle }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors",
        active
          ? "border-primary bg-primary/10 text-primary"
          : "border-border text-muted-foreground hover:border-primary/40 hover:text-foreground"
      )}
      aria-pressed={active}
    >
      <span className="capitalize">{severity}</span>
    </button>
  );
}

export default function ReviewDetail() {
  const { reviewId } = useParams();

  const [severityFilter, setSeverityFilter] = useState([]);   // empty = show all
  const [filePathQuery, setFilePathQuery] = useState("");

  const reviewQuery = useQuery({
    queryKey: qk.reviews.detail(reviewId),
    queryFn: () => api.getReview(reviewId),
  });

  // Findings query params reflect the current filter. Debouncing keeps the
  // request stream calm on file_path typing — we only refire when the user
  // pauses. For severity chips, refire immediately (small state).
  const findingsQuery = useQuery({
    queryKey: qk.reviews.findings(reviewId, {
      severity: severityFilter,
      file_path: filePathQuery,
    }),
    queryFn: () =>
      api.listReviewFindings(reviewId, {
        severity: severityFilter,
        file_path: filePathQuery || null,
        limit: 200,
      }),
    enabled: !!reviewId,
  });

  const review = reviewQuery.data;
  const inFlight = review?.status === "pending" || review?.status === "running";

  const toggleSeverity = (s) =>
    setSeverityFilter((prev) =>
      prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]
    );

  return (
    <section className="space-y-6">
      <div className="flex items-center justify-between gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link
            to={review?.project_id ? `/projects/${review.project_id}` : "/projects"}
          >
            <ChevronLeft className="size-4" />
            {review?.project_id ? "Back to project" : "Back"}
          </Link>
        </Button>
        {review && review.status === "done" ? (
          // The endpoint sends Content-Disposition: attachment so an anchor
          // with `download` fires the browser's native file save. No JS,
          // no blob dance, no auth headers to forward — same-origin.
          <Button asChild variant="outline" size="sm">
            <a href={api.reviewPdfUrl(review.id)} download>
              <Download className="size-4" />
              Download PDF
            </a>
          </Button>
        ) : null}
      </div>

      {reviewQuery.isLoading ? <LoadingState label="Loading review…" /> : null}
      {reviewQuery.isError ? <ErrorState error={reviewQuery.error} /> : null}

      {review ? (
        <>
          {/* Header card */}
          <Card>
            <CardHeader className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <SeverityBar counts={review.severity_counts} />
                {inFlight ? (
                  <Badge variant="muted" className="gap-1.5">
                    <Loader2 className="size-3 animate-spin" />
                    {review.status}
                  </Badge>
                ) : null}
              </div>
              <CardTitle className="flex flex-wrap items-center gap-2 text-lg">
                <GitBranch className="size-4 text-muted-foreground" />
                <span>{review.branch}</span>
                <span className="text-muted-foreground/70">·</span>
                <code className="text-sm">{shortSha(review.before_sha)}</code>
                <ArrowRight className="size-4 text-muted-foreground/70" />
                <code className="text-sm">{shortSha(review.after_sha)}</code>
              </CardTitle>
              <CardDescription className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                <span>{formatDateTime(review.created_at)}</span>
                {review.token_usage?.total ? (
                  <>
                    <span>·</span>
                    <span className="tabular-nums">
                      {review.token_usage.input?.toLocaleString?.() ?? 0} in ·{" "}
                      {review.token_usage.output?.toLocaleString?.() ?? 0} out
                    </span>
                  </>
                ) : null}
                {review.token_usage?.cache_read ? (
                  <>
                    <span>·</span>
                    <span className="tabular-nums">
                      {review.token_usage.cache_read.toLocaleString()} cached
                    </span>
                  </>
                ) : null}
              </CardDescription>
            </CardHeader>

            {/* Natural-language summary from Claude */}
            {review.summary ? (
              <CardContent className="pt-0">
                <p className="text-sm leading-relaxed">{review.summary}</p>
              </CardContent>
            ) : null}
          </Card>

          {/* Attributed commits */}
          {review.commits && review.commits.length > 0 ? (
            <section className="space-y-3">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                Commits in this review
              </h2>
              <Card>
                <CardContent className="py-1">
                  <ul>
                    {review.commits.map((c) => (
                      <CommitRow key={c.sha} commit={c} />
                    ))}
                  </ul>
                </CardContent>
              </Card>
            </section>
          ) : null}

          {/* Findings */}
          <section className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="text-lg font-semibold">Findings</h2>
              {findingsQuery.data ? (
                <span className="text-xs text-muted-foreground">
                  {findingsQuery.data.total} total
                </span>
              ) : null}
            </div>

            {/* Filter bar */}
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex items-center gap-1.5">
                <ListFilter className="size-3.5 text-muted-foreground" />
                <span className="text-xs text-muted-foreground">Severity:</span>
                {SEVERITIES.map((s) => (
                  <SeverityChip
                    key={s}
                    severity={s}
                    active={severityFilter.includes(s)}
                    onToggle={() => toggleSeverity(s)}
                  />
                ))}
                {severityFilter.length > 0 ? (
                  <button
                    type="button"
                    onClick={() => setSeverityFilter([])}
                    className="ml-1 text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    clear
                  </button>
                ) : null}
              </div>
              <div className="ml-auto flex items-center gap-2">
                <Search className="size-3.5 text-muted-foreground" />
                <Input
                  placeholder="Filter by file path…"
                  value={filePathQuery}
                  onChange={(e) => setFilePathQuery(e.target.value)}
                  className="h-8 w-64 text-xs"
                />
              </div>
            </div>

            {findingsQuery.isLoading ? (
              <LoadingState label="Loading findings…" />
            ) : null}
            {findingsQuery.isError ? (
              <ErrorState error={findingsQuery.error} />
            ) : null}
            {findingsQuery.data && findingsQuery.data.findings.length === 0 ? (
              <EmptyState
                icon={AlertCircle}
                title={
                  severityFilter.length || filePathQuery
                    ? "No findings match your filters"
                    : "No findings"
                }
                description={
                  severityFilter.length || filePathQuery
                    ? "Try clearing the filters to see everything on this review."
                    : "The reviewer didn't flag anything for this diff. Clean commit."
                }
              />
            ) : null}

            {findingsQuery.data && findingsQuery.data.findings.length > 0 ? (
              <ul className="space-y-3">
                {findingsQuery.data.findings.map((f) => (
                  <li key={f.id}>
                    <FindingCard finding={f} />
                  </li>
                ))}
              </ul>
            ) : null}
          </section>
        </>
      ) : null}
    </section>
  );
}
