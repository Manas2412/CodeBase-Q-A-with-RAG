"""review attribution: commits.review_id FK + reviews in-flight unique index

Two coordinated changes that fix the soft-attribution + in-flight-guard
weaknesses we shipped with in Day 4:

1. `commits.review_id` — nullable FK on commits → reviews(id), ON DELETE SET
   NULL. The polling worker now writes commit rows tagged with the review
   they belong to. The `/reviews/{id}` detail endpoint joins on this column
   for hard attribution, replacing the (previous_review.created_at,
   this.created_at] time-window approximation.

2. Partial unique index on
   `reviews(project_id, branch, before_sha, after_sha) WHERE status IN
   ('pending', 'running')` — turns the per-push review INSERT into an atomic
   claim. When two workers race on the same diff (Beat catch-up burst, the
   bug observed during Day 3 verification), the second INSERT becomes a
   silent no-op via `ON CONFLICT DO NOTHING`. Without this, both polls
   would slip past the application-level guard (which only checks
   status='done', not pending/running) and double-bill on Bedrock.

   The index is partial so completed reviews don't block legitimate
   future re-runs (e.g. operator-initiated baseline-audit). Errored rows
   also don't block — Celery retries get a fresh row each attempt, useful
   for post-mortem.

Revision ID: 003_review_attribution
Revises: 002_review_agent
Create Date: 2026-06-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision: str = "003_review_attribution"
down_revision: Union[str, Sequence[str], None] = "002_review_agent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. commits.review_id FK ──────────────────────────────────────────
    # Nullable so existing rows survive the migration. The reviewer will
    # back-fill on subsequent runs; older Day-3-era commits stay NULL —
    # that's fine, the FK only matters going forward.
    op.add_column(
        "commits",
        sa.Column("review_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_commits_review_id",
        "commits",
        "reviews",
        ["review_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_commits_review_id",
        "commits",
        ["review_id"],
        # Skip rows that haven't been re-attributed yet — keeps the index
        # small until back-fill catches up. PG ignores partial indexes
        # for unrelated queries, no downside.
        postgresql_where=sa.text("review_id IS NOT NULL"),
    )

    # ── 2. Partial unique index on reviews — the atomic claim ───────────
    # Predicate covers only in-flight rows so the index stays small AND
    # so terminal-state rows ('done', 'error') never block legitimate
    # future writes. A retry after a failed review will INSERT a brand
    # new pending row alongside the old 'error' one — useful audit trail.
    op.create_index(
        "ix_reviews_inflight_unique",
        "reviews",
        ["project_id", "branch", "before_sha", "after_sha"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("ix_reviews_inflight_unique", table_name="reviews")
    op.drop_index("ix_commits_review_id", table_name="commits")
    op.drop_constraint("fk_commits_review_id", "commits", type_="foreignkey")
    op.drop_column("commits", "review_id")
