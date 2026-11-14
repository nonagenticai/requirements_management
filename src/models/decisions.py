"""Decision model — architecture decision records (ADRs) for documenting choices."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
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


class Decision(QualityMixin, TenantSubjectMixin, Base):
    __tablename__ = "decisions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    project_id: Mapped[str | None] = mapped_column(String(100), index=True)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    context: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    consequences: Mapped[str | None] = mapped_column(Text)
    alternatives: Mapped[list[str]] = mapped_column(JSONB, default=list)
    related_principles: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Register fields (REG-1, migration 2026_10_27_0067). ``phase`` is a phase NAME from the
    # tenant's ``pm_project_phases`` catalogue; ``card_id`` is the Planka card that raised it.
    phase: Mapped[str | None] = mapped_column(String(64))
    card_id: Mapped[str | None] = mapped_column(String(64))
    raised_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    decided_by_kind: Mapped[str | None] = mapped_column(String(16))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # CLR reasoning lenses authored by the AA author subroutine (Logical
    # Thinking Process). Shape: {clr_assessment: {<8 CLR booleans>, notes},
    # goal_tree_relevance: {relevant, traces_to}}. Free-form JSONB; consumers
    # parse defensively.
    lens_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    version_status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    supersedes: Mapped[str | None] = mapped_column(String(120))
    superseded_by: Mapped[str | None] = mapped_column(String(120))
    version_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Asset-bindings provenance (~/.claude/plans/fr-asset-bindings-and-reuse.md Phase G).
    origin_kind: Mapped[str] = mapped_column(String(20), default="tenant", server_default="tenant")
    origin_project_id: Mapped[UUID | None] = mapped_column(Uuid)
    origin_workflow_run_id: Mapped[str | None] = mapped_column(String(200))
    # Deterministic AA-intake dedup key (cardId + kind + ordinal). See
    # requirements.py for the disjoint-index rationale.
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
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(context, '') || ' ' || coalesce(decision, '') || ' ' || coalesce(consequences, ''))",
            persisted=True,
        ),
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_decisions_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_decisions_visibility"),
        CheckConstraint(
            "decided_by_kind IN ('human', 'agent', 'rule')", name="ck_decisions_decided_by_kind"
        ),
        Index("ix_decisions_tsv", "tsv", postgresql_using="gin"),
        Index("ix_decisions_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_decisions_tenant_visibility", "tenant_id", "visibility"),
        # AA-intake get-or-create backstop (WS1). See requirements.py for the
        # rationale — declared on the model so create_all() builds it for tests.
        # PARTITIONED on ``stable_key IS NULL`` (mirrors requirements)
        # so the title and stable_key indexes cover disjoint row sets.
        Index(
            "uq_decisions_tenant_project_title",
            "tenant_id",
            "project_id",
            "title",
            unique=True,
            postgresql_where=text(
                "project_id IS NOT NULL AND stable_key IS NULL AND visibility = 'tenant'"
            ),
        ),
        # Deterministic AA-intake dedup key (cardId + kind + ordinal).
        Index(
            "uq_decisions_tenant_project_stable_key",
            "tenant_id",
            "project_id",
            "stable_key",
            unique=True,
            postgresql_where=text("stable_key IS NOT NULL AND visibility = 'tenant'"),
        ),
        # G8 / R313: one project copy per (original, project), for life — ``supersedes`` is
        # the original's slug; a promoted (SUPERSEDED) copy is revived by the next
        # project-copy, never duplicated. The race guard behind
        # ``GovernedAssetService.project_copy`` (migration 2026_10_19_0063).
        Index(
            "uq_decisions_tenant_supersedes_origin_project",
            "tenant_id",
            "supersedes",
            "origin_project_id",
            unique=True,
            postgresql_where=text("supersedes IS NOT NULL AND origin_project_id IS NOT NULL"),
        ),
    )
