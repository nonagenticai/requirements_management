"""Requirement model — project requirements with traceability and prioritization."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from db import Base, TenantSubjectMixin
from models._quality_mixin import QualityMixin


class Requirement(QualityMixin, TenantSubjectMixin, Base):
    __tablename__ = "requirements"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(String(100), index=True)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    links: Mapped[dict[str, list[str]]] = mapped_column(JSONB, default=dict)
    acceptance: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # CLR reasoning lenses authored by the AA author subroutine (Logical
    # Thinking Process). Shape: {clr_assessment: {<8 CLR booleans>, notes},
    # goal_tree_relevance: {relevant, traces_to}}. Free-form JSONB (the AA
    # output shape may evolve); consumers parse defensively.
    lens_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    effort: Mapped[float] = mapped_column(Float)
    reach: Mapped[float] = mapped_column(Float, default=0.0)
    impact: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    # Register fields (REG-1, migration 2026_10_27_0067). ``phase`` is a phase NAME from the
    # tenant's ``pm_project_phases`` catalogue; ``card_id`` is the Planka card that raised it.
    status: Mapped[str | None] = mapped_column(String(30))
    owner: Mapped[str | None] = mapped_column(String(100))
    phase: Mapped[str | None] = mapped_column(String(64))
    card_id: Mapped[str | None] = mapped_column(String(64))
    raised_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    version_status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    supersedes: Mapped[str | None] = mapped_column(String(120))
    superseded_by: Mapped[str | None] = mapped_column(String(120))
    version_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Plan: ~/.claude/plans/fr-asset-bindings-and-reuse.md (Phase B)
    # Origin captures immutable provenance; bindings (in fr-backend) are
    # the mutable many-to-many to projects.
    origin_kind: Mapped[str] = mapped_column(String(20), default="tenant", server_default="tenant")
    origin_project_id: Mapped[UUID | None] = mapped_column(Uuid)
    origin_workflow_run_id: Mapped[str | None] = mapped_column(String(200))
    # Deterministic AA-intake dedup key (cardId + kind + ordinal). NULL for
    # non-AA callers; when set, the get-or-create key (see the two disjoint
    # partial indexes below).
    stable_key: Mapped[str | None] = mapped_column(String(200))
    created_by_user_id: Mapped[str] = mapped_column(String(255), default="", server_default="")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))",
            persisted=True,
        ),
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_requirements_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_requirements_visibility"),
        Index("ix_requirements_tsv", "tsv", postgresql_using="gin"),
        Index("ix_requirements_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_requirements_tenant_visibility", "tenant_id", "visibility"),
        # AA-intake get-or-create backstop (WS1): the partial unique index is
        # the real race guard behind the best-effort router gate. Declared here
        # (not only in alembic) so ``Base.metadata.create_all()`` builds it for
        # tests; otherwise ``EntityService.get_or_create``'s ``IntegrityError``
        # branch never fires. Partial on ``project_id IS NOT NULL`` so tenant-wide
        # (null-project) rows that legitimately share a title are unconstrained.
        # PARTITIONED on ``stable_key IS NULL``: rows that
        # carry a stable_key dedup ONLY on the stable_key index below, so the two
        # indexes cover disjoint row sets and no insert can cross-collide under
        # get_or_create's single-key IntegrityError recovery (which would 500).
        Index(
            "uq_requirements_tenant_project_title",
            "tenant_id",
            "project_id",
            "title",
            unique=True,
            postgresql_where=text(
                "project_id IS NOT NULL AND stable_key IS NULL AND visibility = 'tenant'"
            ),
        ),
        # Deterministic AA-intake dedup key (cardId + kind + ordinal) — survives
        # LLM title drift across re-runs.
        Index(
            "uq_requirements_tenant_project_stable_key",
            "tenant_id",
            "project_id",
            "stable_key",
            unique=True,
            postgresql_where=text("stable_key IS NOT NULL AND visibility = 'tenant'"),
        ),
        # rm#191: the USER-visibility twins of the two indexes above. Without
        # them a concurrent pair of identical ``X-Visibility: user`` creates only
        # collides when BOTH racers ran ``_generate_unique_slug`` before either
        # committed; if the winner commits first, the loser derives ``<slug>-1``
        # and both INSERTs succeed → a duplicate row. Keyed on ``subject_id``
        # because a user-visible row is subject-private (the get_or_create
        # recovery re-SELECT already subject-scopes, so the key matches it
        # exactly). Disjoint from the tenant indexes (visibility) and from each
        # other (stable_key partition), as get_or_create's single-key recovery
        # requires. Migration 2026_10_02_0049.
        Index(
            "uq_requirements_user_project_title",
            "tenant_id",
            "subject_id",
            "project_id",
            "title",
            unique=True,
            postgresql_where=text(
                "project_id IS NOT NULL AND stable_key IS NULL AND visibility = 'user'"
            ),
        ),
        Index(
            "uq_requirements_user_project_stable_key",
            "tenant_id",
            "subject_id",
            "project_id",
            "stable_key",
            unique=True,
            postgresql_where=text("stable_key IS NOT NULL AND visibility = 'user'"),
        ),
        # G8 / R313: one project copy per (original, project), for life — ``supersedes`` is
        # the original's slug; a promoted (SUPERSEDED) copy is revived by the next
        # project-copy, never duplicated. The race guard behind
        # ``GovernedAssetService.project_copy`` (migration 2026_10_19_0063).
        Index(
            "uq_requirements_tenant_supersedes_origin_project",
            "tenant_id",
            "supersedes",
            "origin_project_id",
            unique=True,
            postgresql_where=text("supersedes IS NOT NULL AND origin_project_id IS NOT NULL"),
        ),
    )
