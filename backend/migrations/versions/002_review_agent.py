"""review agent schema

Adds the review-agent tables on top of the prior Q&A schema:
  - rename `repos` → `projects` with new columns
  - rename `chunks.repo_id` → `chunks.project_id`
  - change `chunks.embedding` from vector(1536) → vector(1024)
  - add `chunks.commit_sha`
  - create `users`, `checklists`, `commits`, `reviews`, `review_findings`,
    `branch_events`

Existing rows in `chunks` and `repos` are dropped: the 1536-dim Voyage
embeddings can't be re-used at 1024 dim under Cohere Embed Multilingual v3,
and re-onboarding takes ~5 seconds for a typical test repo.

Revision ID: 002_review_agent
Revises: 9022c7a69343
Create Date: 2026-06-21
"""

from __future__ import annotations

from typing import Sequence, Union

import pgvector
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic
revision: str = "002_review_agent"
down_revision: Union[str, Sequence[str], None] = "9022c7a69343"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Drop existing data so the dim change is safe ──────────────────
    # No HNSW/IVFFlat indexes existed yet on chunks.embedding — if they did
    # we'd `DROP INDEX` first. The TRUNCATE here covers both tables since
    # chunks.repo_id will be foreign-key-cascaded.
    op.execute("TRUNCATE chunks, repos RESTART IDENTITY CASCADE;")

    # ── 2. Rebuild the chunks side ───────────────────────────────────────
    # Rename the column first while it's still named repo_id, so we can
    # then rename the constraint and FK target cleanly.
    op.alter_column("chunks", "repo_id", new_column_name="project_id")

    # Old indexes/constraints on the renamed column survive the rename in
    # Postgres, but their names still mention "repo". We drop and recreate
    # under project-named ids for clarity.
    op.drop_index("ix_chunks_repo_id", table_name="chunks")
    op.drop_constraint("uq_chunk_location", "chunks", type_="unique")
    op.drop_constraint("uq_chunks_repo_file_line", "chunks", type_="unique")
    op.drop_constraint("chunks_repo_id_fkey", "chunks", type_="foreignkey")

    # Switch embedding dimension. Vector type can't be ALTERed in place;
    # easiest is DROP COLUMN + ADD COLUMN. Existing rows are already gone
    # from the TRUNCATE above.
    op.drop_column("chunks", "embedding")
    op.add_column(
        "chunks",
        sa.Column(
            "embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True
        ),
    )

    op.add_column("chunks", sa.Column("commit_sha", sa.String(64), nullable=True))
    op.create_index(
        "ix_chunks_commit_sha", "chunks", ["commit_sha"], unique=False
    )

    # ── 3. Rename `repos` → `projects` and reshape ───────────────────────
    op.rename_table("repos", "projects")
    op.alter_column("projects", "github_url", new_column_name="repo_url")

    # Provider / display
    op.add_column("projects", sa.Column("provider", sa.String(32), nullable=True))
    op.add_column("projects", sa.Column("name", sa.String(), nullable=True))
    op.add_column(
        "projects", sa.Column("tuleap_project", sa.String(), nullable=True)
    )
    op.add_column("projects", sa.Column("tuleap_repo", sa.String(), nullable=True))

    # Branch state
    op.add_column(
        "projects",
        sa.Column(
            "default_branch",
            sa.String(),
            server_default="main",
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "branches_to_review",
            JSONB,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "last_reviewed_sha",
            JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    # Trigger config
    op.add_column(
        "projects",
        sa.Column(
            "trigger_mode",
            sa.String(16),
            server_default="poll",
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "poll_interval_minutes",
            sa.Integer(),
            server_default="5",
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "auto_watch_new",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )

    # Operational state
    op.add_column(
        "projects",
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── 4. Create the new tables (order matters for FK) ──────────────────
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("role", sa.String(32), server_default="viewer", nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "checklists",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "rules", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_checklist_name_version"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
    )

    # Now that checklists exists, attach the FK from projects.checklist_id
    op.add_column(
        "projects", sa.Column("checklist_id", UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_projects_checklist",
        "projects",
        "checklists",
        ["checklist_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "commits",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("branch", sa.String(), nullable=False),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("parent_sha", sa.String(64), nullable=True),
        sa.Column("author_name", sa.String(), nullable=False),
        sa.Column("author_email", sa.String(), nullable=False),
        sa.Column("committer_name", sa.String(), nullable=False),
        sa.Column("committer_email", sa.String(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column(
            "source", sa.String(16), server_default="poll", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "sha", name="uq_commit_project_sha"),
    )
    op.create_index("ix_commits_project_id", "commits", ["project_id"])
    op.create_index("ix_commits_branch", "commits", ["branch"])
    op.create_index("ix_commits_sha", "commits", ["sha"])

    op.create_table(
        "reviews",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("branch", sa.String(), nullable=False),
        sa.Column("before_sha", sa.String(64), nullable=False),
        sa.Column("after_sha", sa.String(64), nullable=False),
        sa.Column(
            "status", sa.String(32), server_default="pending", nullable=False
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "severity_counts",
            JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "token_usage",
            JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("checklist_version", sa.Integer(), nullable=True),
        sa.Column(
            "batch_mode", sa.String(16), server_default="batch", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reviews_project_id", "reviews", ["project_id"])
    op.create_index("ix_reviews_branch", "reviews", ["branch"])
    op.create_index("ix_reviews_after_sha", "reviews", ["after_sha"])

    op.create_table(
        "review_findings",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", UUID(as_uuid=True), nullable=False),
        sa.Column("commit_id", UUID(as_uuid=True), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column("rule_id", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(
            ["review_id"], ["reviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["commit_id"], ["commits.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_review_findings_review_id", "review_findings", ["review_id"]
    )

    op.create_table(
        "branch_events",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("branch", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column(
            "detail", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "resolved", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_branch_events_project_id", "branch_events", ["project_id"]
    )

    # ── 5. Recreate the chunks FK and constraints under project names ────
    op.create_foreign_key(
        "fk_chunks_project",
        "chunks",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_chunks_project_id", "chunks", ["project_id"])
    op.create_unique_constraint(
        "uq_chunk_location",
        "chunks",
        ["project_id", "file_path", "start_line"],
    )


def downgrade() -> None:
    """Reverse of upgrade. Not exercised in Phase 1 but kept honest."""
    # 5 — chunks constraints back to repo-named
    op.drop_constraint("uq_chunk_location", "chunks", type_="unique")
    op.drop_index("ix_chunks_project_id", table_name="chunks")
    op.drop_constraint("fk_chunks_project", "chunks", type_="foreignkey")

    # 4 — drop new tables
    op.drop_index("ix_branch_events_project_id", table_name="branch_events")
    op.drop_table("branch_events")

    op.drop_index("ix_review_findings_review_id", table_name="review_findings")
    op.drop_table("review_findings")

    op.drop_index("ix_reviews_after_sha", table_name="reviews")
    op.drop_index("ix_reviews_branch", table_name="reviews")
    op.drop_index("ix_reviews_project_id", table_name="reviews")
    op.drop_table("reviews")

    op.drop_index("ix_commits_sha", table_name="commits")
    op.drop_index("ix_commits_branch", table_name="commits")
    op.drop_index("ix_commits_project_id", table_name="commits")
    op.drop_table("commits")

    op.drop_constraint("fk_projects_checklist", "projects", type_="foreignkey")
    op.drop_column("projects", "checklist_id")
    op.drop_table("checklists")
    op.drop_table("users")

    # 3 — projects back to repos
    op.drop_column("projects", "last_polled_at")
    op.drop_column("projects", "auto_watch_new")
    op.drop_column("projects", "poll_interval_minutes")
    op.drop_column("projects", "trigger_mode")
    op.drop_column("projects", "last_reviewed_sha")
    op.drop_column("projects", "branches_to_review")
    op.drop_column("projects", "default_branch")
    op.drop_column("projects", "tuleap_repo")
    op.drop_column("projects", "tuleap_project")
    op.drop_column("projects", "name")
    op.drop_column("projects", "provider")
    op.alter_column("projects", "repo_url", new_column_name="github_url")
    op.rename_table("projects", "repos")

    # 2 — chunks side back
    op.drop_index("ix_chunks_commit_sha", table_name="chunks")
    op.drop_column("chunks", "commit_sha")
    op.drop_column("chunks", "embedding")
    op.add_column(
        "chunks",
        sa.Column(
            "embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1536), nullable=True
        ),
    )
    op.create_foreign_key(
        "chunks_repo_id_fkey", "chunks", "repos", ["project_id"], ["id"]
    )
    op.create_unique_constraint(
        "uq_chunks_repo_file_line", "chunks", ["project_id", "file_path", "start_line"]
    )
    op.create_unique_constraint(
        "uq_chunk_location", "chunks", ["project_id", "file_path", "start_line"]
    )
    op.create_index("ix_chunks_repo_id", "chunks", ["project_id"])
    op.alter_column("chunks", "project_id", new_column_name="repo_id")
