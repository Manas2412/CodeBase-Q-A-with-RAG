"""Celery tasks.

Currently:
  • index_repo_task — initial onboarding. Clone, chunk, embed, upsert.
  • poll_all_projects_task — Beat-scheduled fan-out (every 5 min):
        list every project with status='ready' and trigger_mode='poll'|'both',
        enqueue one `check_project_for_changes_task` per project.
  • check_project_for_changes_task — STUB for Week 3 Day 1.
        Fully implemented in Day 2: git fetch, compare branch HEADs
        against last_reviewed_sha, enqueue review_push_task per branch
        that moved; force-push detection.

Not here yet:
  • review_push_task — Week 3 Day 3.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
from celery import Celery
from dotenv import load_dotenv
from pgvector.asyncpg import register_vector

from app.db.database import needs_ssl, register_jsonb_codecs
from app.ingestion.chunker import chunk_file
from app.ingestion.cloner import walk_code_files
from app.ingestion.embedder import embed_chunks
from app.ingestion.indexer import prune_chunks_not_in_commit, upsert_chunks
from app.providers import ProviderError, UnknownProviderError, get_provider
from app.review import commits_between, run_review_for_push
from app.scheduling.beat import BEAT_SCHEDULE
from app.storage import (
    CloneError,
    branch_head,
    ensure_cloned,
    fetch,
    is_ancestor,
    materialize_tree,
)

load_dotenv()

celery = Celery(
    "codereview",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
)
# Wire the Beat schedule onto the celery app so `celery beat -A app.workers.tasks.celery`
# picks it up without extra configuration.
celery.conf.beat_schedule = BEAT_SCHEDULE
celery.conf.timezone = "UTC"


def get_dsn() -> str:
    raw_url = os.getenv("DATABASE_URL", "")
    db_url = (
        raw_url.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgres://", "postgresql://")
    )
    return db_url.split("?")[0]


async def _open_conn() -> asyncpg.Connection:
    """Open a worker-side asyncpg connection with JSONB codecs registered."""
    dsn = get_dsn()
    conn = await asyncpg.connect(dsn=dsn, ssl=needs_ssl(dsn))
    await register_jsonb_codecs(conn)
    return conn


# ══ Onboarding (Week 2 Day 5) ════════════════════════════════════════════
@celery.task(bind=True, max_retries=3, default_retry_delay=10)
def index_repo_task(self, project_id: str, repo_url: str | None = None):
    """Initial-onboarding indexing.

    Parameters
    ----------
    project_id : The project's UUID.
    repo_url   : Optional. Looked up from the projects row when omitted.
    """
    try:
        asyncio.run(_index_repo(project_id, repo_url))
    except Exception as exc:
        raise self.retry(exc=exc)


async def _index_repo(project_id: str, repo_url: str | None) -> None:
    conn: asyncpg.Connection | None = None
    try:
        conn = await _open_conn()
        await register_vector(conn)

        row = await conn.fetchrow(
            "SELECT repo_url, default_branch FROM projects WHERE id = $1::uuid",
            project_id,
        )
        if not row:
            print(f"[indexer] project {project_id} not found; skipping")
            return
        repo_url = repo_url or row["repo_url"]
        default_branch = row["default_branch"] or "HEAD"

        await conn.execute(
            "UPDATE projects SET status = 'indexing' WHERE id = $1::uuid",
            project_id,
        )

        try:
            provider = get_provider(repo_url)
            parsed = provider.parse(repo_url)
        except (UnknownProviderError, ProviderError) as e:
            raise CloneError(f"Provider error for {repo_url!r}: {e}") from e
        auth_url = provider.auth_url(parsed, provider.get_token())

        await ensure_cloned(project_id, auth_url)
        await fetch(project_id)

        commit_sha = await branch_head(project_id, default_branch)

        all_chunks = []
        async with materialize_tree(project_id, default_branch) as tree_path:
            for file_path, source, language in walk_code_files(tree_path):
                all_chunks.extend(chunk_file(file_path, source, language))

        if not all_chunks:
            print(
                f"[indexer] {repo_url} @ {default_branch} has zero supported-language "
                "chunks; marking ready with empty index"
            )
        else:
            print(
                f"[indexer] embedding {len(all_chunks)} chunks "
                f"from {repo_url} @ {default_branch}"
            )
            embeddings = await embed_chunks(all_chunks)
            await upsert_chunks(
                conn,
                project_id=project_id,
                chunks=all_chunks,
                embeddings=embeddings,
                commit_sha=commit_sha,
            )
            await prune_chunks_not_in_commit(conn, project_id, commit_sha)

        await conn.execute(
            """
            UPDATE projects
            SET status = 'ready', indexed_at = now()
            WHERE id = $1::uuid
            """,
            project_id,
        )
        print(f"[indexer] done — project {project_id} marked ready @ {commit_sha[:8]}")

    except Exception as e:
        print(f"[indexer] error indexing {project_id}: {e}")
        if conn:
            await conn.execute(
                "UPDATE projects SET status = 'error' WHERE id = $1::uuid",
                project_id,
            )
        raise
    finally:
        if conn:
            await conn.close()


# ══ Polling fan-out (Week 3 Day 1) ═══════════════════════════════════════
@celery.task
def poll_all_projects_task():
    """Beat-scheduled fan-out. Lists ready+poll-mode projects and queues
    a check_project_for_changes_task for each.

    Returns the count so Celery's task results show how many polls fired.
    """
    n = asyncio.run(_poll_all_projects())
    print(f"[poll] dispatched {n} project checks")
    return n


async def _poll_all_projects() -> int:
    """Pure async helper — separated for testability."""
    conn = await _open_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT id::text AS id
              FROM projects
             WHERE status = 'ready'
               AND trigger_mode IN ('poll', 'both')
               AND jsonb_array_length(branches_to_review) > 0
            """
        )
    finally:
        await conn.close()

    for row in rows:
        check_project_for_changes_task.delay(row["id"])
    return len(rows)


@celery.task(bind=True, max_retries=3, default_retry_delay=30)
def check_project_for_changes_task(self, project_id: str):
    """Per-project polling work.

    1. git fetch the persistent clone (incremental, cheap)
    2. for each watched branch, compare current branch_head() with
       project.last_reviewed_sha[branch]:
         • first time seeing the branch → baseline (record SHA, no review)
         • SHA unchanged → skip
         • forward move (ancestor check passes) → enqueue review_push_task
         • non-ancestor move → force-push; record a branch_event and
           update last_reviewed_sha to the new tip so we don't re-flag
           the same force-push on every subsequent poll
    3. update projects.last_polled_at

    Errors that surface from git (CloneError) or the DB get retried via
    Celery's retry machinery (3 attempts, 30s delay).
    """
    try:
        asyncio.run(_check_project_for_changes(project_id))
    except Exception as exc:
        raise self.retry(exc=exc)


async def _check_project_for_changes(project_id: str) -> None:
    conn = await _open_conn()
    try:
        row = await conn.fetchrow(
            """
            SELECT repo_url, branches_to_review, last_reviewed_sha
              FROM projects
             WHERE id = $1::uuid
            """,
            project_id,
        )
        if not row:
            print(f"[poll] project {project_id} not found; skipping")
            return

        repo_url = row["repo_url"]
        branches = list(row["branches_to_review"] or [])
        last_shas: dict = dict(row["last_reviewed_sha"] or {})

        # 1. Resolve provider + auth URL (the persistent clone was made with
        #    auth; fetching uses the same URL via origin's stored remote)
        try:
            provider = get_provider(repo_url)
        except UnknownProviderError as e:
            print(f"[poll] {project_id}: unknown provider {repo_url!r}; bailing: {e}")
            return

        # 2. Fetch — cheap incremental, brings refs/* up to date
        try:
            await fetch(project_id)
        except CloneError as e:
            print(f"[poll] {project_id}: git fetch failed: {e}")
            raise  # let Celery retry

        # 3. Walk every watched branch, decide what to do per branch
        dispatched = 0
        new_last_shas = dict(last_shas)  # build the updated state
        for branch in branches:
            try:
                new_sha = await branch_head(project_id, branch)
            except CloneError as e:
                # Branch may have been deleted upstream. Day 5 surfaces
                # this as a 'branch_deleted' event; for now log + skip.
                print(
                    f"[poll] {project_id}: branch {branch} not found "
                    f"in clone (deleted?): {e}"
                )
                continue

            old_sha = last_shas.get(branch)

            # First time we see this branch → baseline. The
            # "Notify me about new branches" behaviour: existing
            # content isn't reviewed, only future changes.
            if old_sha is None:
                new_last_shas[branch] = new_sha
                print(
                    f"[poll] {project_id} {branch}: baselined at {new_sha[:8]}"
                )
                continue

            # No change
            if new_sha == old_sha:
                continue

            # Forward move? (new SHA contains old SHA in its ancestry)
            try:
                forward = await is_ancestor(project_id, old_sha, new_sha)
            except CloneError as e:
                print(
                    f"[poll] {project_id} {branch}: ancestor check failed: {e}"
                )
                # Treat as force-push since we can't prove it's a forward move
                forward = False

            if not forward:
                # Force-push (or otherwise non-linear update). Record the
                # event for the operator, but DO NOT auto-review — humans
                # need to decide whether to re-baseline. Update the stored
                # SHA so we don't re-fire the same event every 5 minutes.
                await _record_branch_event(
                    conn,
                    project_id,
                    branch,
                    "force_push",
                    {"previous_sha": old_sha, "new_sha": new_sha},
                )
                new_last_shas[branch] = new_sha
                print(
                    f"[poll] {project_id} {branch}: force-push detected, "
                    f"event recorded ({old_sha[:8]} → {new_sha[:8]})"
                )
                continue

            # Normal forward move → enqueue a review.
            # Guard against double-dispatch: if a review for this exact
            # (branch, before, after) is already pending/running from a
            # prior poll cycle, don't queue another one.
            in_flight = await conn.fetchval(
                """
                SELECT 1 FROM reviews
                 WHERE project_id = $1::uuid
                   AND branch     = $2
                   AND before_sha = $3
                   AND after_sha  = $4
                   AND status IN ('pending', 'running')
                 LIMIT 1
                """,
                project_id,
                branch,
                old_sha,
                new_sha,
            )
            if in_flight:
                print(
                    f"[poll] {project_id} {branch}: review for "
                    f"{old_sha[:8]}..{new_sha[:8]} already in flight, skipping"
                )
                continue

            review_push_task.delay(project_id, branch, old_sha, new_sha)
            dispatched += 1
            # Note: we DON'T update new_last_shas[branch] here — the review
            # task does that after the review completes. If the review
            # fails, the next poll will re-enqueue it.

        # 4. Persist updated baselines + last_polled_at
        await conn.execute(
            """
            UPDATE projects
               SET last_reviewed_sha = $1,
                   last_polled_at    = now()
             WHERE id = $2::uuid
            """,
            new_last_shas,
            project_id,
        )
        if dispatched:
            print(f"[poll] {project_id}: dispatched {dispatched} review(s)")
    finally:
        await conn.close()


async def _record_branch_event(
    conn: asyncpg.Connection,
    project_id: str,
    branch: str,
    event_type: str,
    detail: dict,
) -> None:
    """Insert one row into branch_events."""
    await conn.execute(
        """
        INSERT INTO branch_events (
            id, project_id, branch, event_type, detail, resolved
        ) VALUES ($1::uuid, $2::uuid, $3, $4, $5, false)
        """,
        str(uuid.uuid4()),
        project_id,
        branch,
        event_type,
        detail,
    )


# ══ Review (Week 3 Day 3) ════════════════════════════════════════════════
@celery.task(bind=True, max_retries=3, default_retry_delay=30)
def review_push_task(
    self,
    project_id: str,
    branch: str,
    before_sha: str,
    after_sha: str,
):
    """Per-push review task.

    1. Idempotency: if a `status='done'` review for this exact
       (project, branch, before, after) already exists (a retry of a
       partially-succeeded task), skip the LLM call entirely and just
       bump last_reviewed_sha. Saves real money on Bedrock retries.
    2. Walk `commits_between(before, after)` and INSERT into the
       `commits` table with `source='poll'`. Idempotent via the
       `uq_commit_project_sha` constraint.
    3. Call `run_review_for_push()` — that does diff → context → LLM →
       persists reviews + review_findings rows internally.
    4. UPDATE `projects.last_reviewed_sha` so the polling agent doesn't
       re-enqueue. The UPDATE is the LAST step so a Celery retry on
       LLM failure cleanly re-fires the review.
    """
    try:
        asyncio.run(
            _review_push(project_id, branch, before_sha, after_sha)
        )
    except Exception as exc:
        raise self.retry(exc=exc)


async def _review_push(
    project_id: str,
    branch: str,
    before_sha: str,
    after_sha: str,
) -> None:
    """Run one review end-to-end.

    Day-5 flow (atomic claim + FK attribution):
      1. Idempotency: if a done review exists for this exact diff, skip
         the LLM and just bump last_reviewed_sha.
      2. Atomic claim: INSERT a `status='pending'` review row with
         `ON CONFLICT DO NOTHING` against the partial unique index on
         (project, branch, before, after) WHERE status IN ('pending',
         'running'). If RETURNING is empty, another worker is already
         on this diff — bail silently.
      3. Mark the claim 'running' (purely cosmetic — the row is already
         taken at this point).
      4. Persist commits with the review_id FK so attribution survives.
      5. Run the LLM (UPDATEs the claimed row to 'done').
      6. Bump last_reviewed_sha LAST so Celery retry semantics work.

    On any exception inside the try/except below, the claimed row is
    marked 'error' so the partial unique index unblocks future retries
    AND the error history is preserved for post-mortem.
    """
    conn = await _open_conn()
    review_id: uuid.UUID | None = None
    try:
        await register_vector(conn)

        # 1. Idempotency check — completed review already exists?
        existing = await conn.fetchval(
            """
            SELECT id FROM reviews
             WHERE project_id = $1::uuid
               AND branch     = $2
               AND before_sha = $3
               AND after_sha  = $4
               AND status     = 'done'
             LIMIT 1
            """,
            project_id,
            branch,
            before_sha,
            after_sha,
        )
        if existing:
            await _bump_last_reviewed_sha(conn, project_id, branch, after_sha)
            print(
                f"[review] {project_id} {branch}: review already done "
                f"({before_sha[:8]}..{after_sha[:8]}), bumping sha only"
            )
            return

        # 2. Atomic claim. The partial unique index
        #    `ix_reviews_inflight_unique` ensures only one row in
        #    {pending, running} can exist per (project, branch, before,
        #    after). A racing worker's INSERT returns NULL — we bail
        #    silently without burning Bedrock tokens.
        new_id = uuid.uuid4()
        review_id = await conn.fetchval(
            """
            INSERT INTO reviews (
                id, project_id, branch, before_sha, after_sha,
                status, severity_counts, token_usage,
                checklist_version, batch_mode
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4, $5,
                'pending', $6, $6,
                NULL, 'batch'
            )
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            new_id,
            project_id,
            branch,
            before_sha,
            after_sha,
            {},   # empty severity_counts + token_usage placeholder
        )
        if review_id is None:
            print(
                f"[review] {project_id} {branch}: claim lost to another "
                f"worker ({before_sha[:8]}..{after_sha[:8]}), skipping"
            )
            return

        # 3. Move to 'running' for visibility in the dashboard while we work.
        await conn.execute(
            "UPDATE reviews SET status = 'running' WHERE id = $1::uuid",
            review_id,
        )

        # 4. Commits with FK attribution. Done BEFORE the LLM call so
        #    the attribution survives a Bedrock outage.
        commits = await commits_between(project_id, before_sha, after_sha)
        if commits:
            await _upsert_commits(
                conn, project_id, branch, commits, review_id=review_id
            )

        # 5. Run the review — UPDATEs the pending row in place
        #    (no longer inserts a new row).
        result = await run_review_for_push(
            project_id=project_id,
            before=before_sha,
            after=after_sha,
            branch=branch,
            conn=conn,
            review_id=review_id,
        )

        # 6. Bump the polling baseline LAST. If anything above raised,
        #    this UPDATE never runs and the next poll re-enqueues.
        await _bump_last_reviewed_sha(conn, project_id, branch, after_sha)

        print(
            f"[review] {project_id} {branch} done: "
            f"review_id={result.review_id} "
            f"findings={len(result.findings)} "
            f"severity={result.severity_counts} "
            f"tokens={result.token_usage}"
        )
    except Exception:
        # Mark our claim 'error' so the partial unique index unblocks
        # AND we keep the failed attempt as audit history. Don't mask
        # the original exception if the bookkeeping UPDATE itself fails.
        if review_id is not None:
            try:
                await conn.execute(
                    """
                    UPDATE reviews
                       SET status = 'error',
                           completed_at = now()
                     WHERE id = $1::uuid
                    """,
                    review_id,
                )
            except Exception as inner:
                print(
                    f"[review] failed to mark review {review_id} 'error': {inner!r}"
                )
        raise
    finally:
        await conn.close()


async def _upsert_commits(
    conn: asyncpg.Connection,
    project_id: str,
    branch: str,
    commits: list,
    *,
    source: str = "poll",
    review_id: uuid.UUID | None = None,
) -> None:
    """Bulk-insert commit rows tagged with the review they belong to.

    `ON CONFLICT (project_id, sha) DO UPDATE` is used so the SAME commit
    appearing in multiple branches (e.g. cherry-picked, or a branch
    pointing back at main) gets its `review_id` re-attributed if a more
    recent review covered it. The trade-off: the latest review wins,
    which matches the dashboard's "most recent attribution" semantics.
    """
    if not commits:
        return
    records = [
        (
            str(uuid.uuid4()),
            project_id,
            branch,
            c.sha,
            c.parent_sha,
            c.author_name,
            c.author_email,
            c.committer_name,
            c.committer_email,
            c.committed_at,
            c.subject,
            source,
            review_id,
        )
        for c in commits
    ]
    await conn.executemany(
        """
        INSERT INTO commits (
            id, project_id, branch, sha, parent_sha,
            author_name, author_email, committer_name, committer_email,
            committed_at, subject, source, review_id
        ) VALUES (
            $1::uuid, $2::uuid, $3, $4, $5,
            $6, $7, $8, $9,
            $10, $11, $12, $13
        )
        ON CONFLICT (project_id, sha) DO UPDATE
           SET review_id = EXCLUDED.review_id
         WHERE EXCLUDED.review_id IS NOT NULL
        """,
        records,
    )
    print(f"[review] persisted {len(records)} commit rows for {project_id} {branch}")


async def _bump_last_reviewed_sha(
    conn: asyncpg.Connection,
    project_id: str,
    branch: str,
    after_sha: str,
) -> None:
    """Merge `{branch: after_sha}` into projects.last_reviewed_sha.

    Uses the JSONB `||` operator which merges top-level keys (right-hand
    wins on conflict). The codec round-trips the Python dict.
    """
    await conn.execute(
        """
        UPDATE projects
           SET last_reviewed_sha = COALESCE(last_reviewed_sha, '{}'::jsonb) || $1
         WHERE id = $2::uuid
        """,
        {branch: after_sha},
        project_id,
    )
