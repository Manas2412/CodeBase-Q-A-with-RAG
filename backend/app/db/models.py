# app/db/models.py
"""
SQLAlchemy models for the codebase review agent.

Schema overview:
  users           — internal dashboard users (Phase 1: session auth)
  checklists      — versioned review rule sets
  projects        — one row per repository under review (renamed from `repos`)
  commits         — every reviewed commit with author/committer attribution
  reviews         — one review per push range, per branch
  review_findings — individual finding entries grouped under a review
  branch_events   — force-push / new-branch / branch-deleted surfacing
  chunks          — code chunks for RAG retrieval (existing, dimension changed
                    from 1536 → 1024 to match Cohere Embed Multilingual v3)

See implementation plan v3.3 §8 for the design and §11.1 for the Week 1 rollout.
"""

from __future__ import annotations

import datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


# ── reusable column types ────────────────────────────────────────────────
def _uuid_pk() -> Mapped[uuid.UUID]:
    """Standard primary key — UUID with a Python-side default."""
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime.datetime]:
    """server_default = now() so inserts without explicit value still get a timestamp."""
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── User ─────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="viewer", nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = _created_at()


# ── Checklist ────────────────────────────────────────────────────────────
class Checklist(Base):
    __tablename__ = "checklists"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rules: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_checklist_name_version"),
    )


# ── Project ──────────────────────────────────────────────────────────────
class Project(Base):
    """One repository under review. Replaces the prior `Repo` model.

    Phase 1 trigger mode is 'poll' for everyone. Webhook becomes selectable
    in Phase 1.5 once the inbound endpoint and ingress are in place.
    """

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()

    # Provider abstraction (resolved at probe-time from the URL)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    repo_url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)

    # Tuleap-specific helpers — parsed from the URL when provider='openforge'
    tuleap_project: Mapped[str | None] = mapped_column(String, nullable=True)
    tuleap_repo: Mapped[str | None] = mapped_column(String, nullable=True)

    # Branch state
    default_branch: Mapped[str] = mapped_column(String, default="main", nullable=False)
    branches_to_review: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    # last_reviewed_sha: {"dev": "abc123…", "uat": "def456…"}
    last_reviewed_sha: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False
    )

    # Trigger config
    trigger_mode: Mapped[str] = mapped_column(
        String(16), default="poll", nullable=False
    )  # 'poll' | 'webhook' | 'both'
    poll_interval_minutes: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    auto_watch_new: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Active checklist for this project (nullable so deletion of a checklist
    # doesn't orphan projects)
    checklist_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checklists.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    indexed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_polled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = _created_at()

    # ORM relationships (no DB schema impact — query-side ergonomics)
    commits: Mapped[list[Commit]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    reviews: Mapped[list[Review]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    branch_events: Mapped[list[BranchEvent]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


# Backward-compat alias for any lingering import sites — to be removed
# after Week 2 once all references migrate.
Repo = Project


# ── Commit ───────────────────────────────────────────────────────────────
class Commit(Base):
    """Every reviewed commit + its author/committer attribution."""

    __tablename__ = "commits"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    branch: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parent_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)

    author_name: Mapped[str] = mapped_column(String, nullable=False)
    author_email: Mapped[str] = mapped_column(String, nullable=False)
    committer_name: Mapped[str] = mapped_column(String, nullable=False)
    committer_email: Mapped[str] = mapped_column(String, nullable=False)
    committed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)

    # Trigger source for telemetry / debugging "why didn't this push get reviewed?"
    source: Mapped[str] = mapped_column(
        String(16), default="poll", nullable=False
    )  # 'poll' | 'webhook' | 'manual'

    # Day-5: which review covered this commit. Nullable because:
    #   • ON DELETE SET NULL keeps the commit row when its review is deleted
    #   • Day-3-era rows from before this migration stay NULL
    # The /reviews/{id} detail endpoint joins on this column for hard
    # attribution (replacing the Day-4 time-window approximation).
    review_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reviews.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime.datetime] = _created_at()

    project: Mapped[Project] = relationship(back_populates="commits")

    __table_args__ = (
        UniqueConstraint("project_id", "sha", name="uq_commit_project_sha"),
    )


# ── Review ───────────────────────────────────────────────────────────────
class Review(Base):
    """One review covering a push range on a watched branch."""

    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    branch: Mapped[str] = mapped_column(String, nullable=False, index=True)
    before_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    after_sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )  # pending | running | done | error
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity_counts: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False
    )  # {"critical": 1, "major": 2, "minor": 3, "info": 4}
    token_usage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    checklist_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_mode: Mapped[str] = mapped_column(
        String(16), default="batch", nullable=False
    )  # 'batch' | 'per_commit'

    created_at: Mapped[datetime.datetime] = _created_at()
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    project: Mapped[Project] = relationship(back_populates="reviews")
    findings: Mapped[list[ReviewFinding]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )


# ── ReviewFinding ────────────────────────────────────────────────────────
class ReviewFinding(Base):
    """A single line/range-scoped finding from the reviewer."""

    __tablename__ = "review_findings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reviews.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # nullable for batch-mode reviews where findings span multiple commits
    commit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("commits.id", ondelete="SET NULL"),
        nullable=True,
    )

    severity: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # info | minor | major | critical
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    review: Mapped[Review] = relationship(back_populates="findings")


# ── BranchEvent ──────────────────────────────────────────────────────────
class BranchEvent(Base):
    """Notable events on a project's branches that warrant operator attention."""

    __tablename__ = "branch_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    branch: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # 'force_push' | 'new_branch' | 'branch_deleted'
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime.datetime] = _created_at()

    project: Mapped[Project] = relationship(back_populates="branch_events")


# ── Chunk (existing model, updated) ──────────────────────────────────────
class Chunk(Base):
    """Code chunks for RAG retrieval.

    Changes from v1:
      • embedding dim: 1536 → 1024 (Cohere Embed Multilingual v3 on Bedrock)
      • repo_id renamed to project_id (table rename consistency)
      • new commit_sha column for incremental pruning when a file is re-indexed
    """

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(50), nullable=False)
    chunk_type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    context_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    created_at: Mapped[datetime.datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "project_id", "file_path", "start_line", name="uq_chunk_location"
        ),
    )
