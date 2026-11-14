"""Principle model — architecture guidance statements that apply across projects."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import (
    Boolean,
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


class Principle(QualityMixin, TenantSubjectMixin, Base):
    __tablename__ = "principles"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    statement: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    rationale: Mapped[str | None] = mapped_column(Text)
    implications: Mapped[list[str]] = mapped_column(JSONB, default=list)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
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
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(statement, '') || ' ' || coalesce(rationale, ''))",
            persisted=True,
        ),
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_principles_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_principles_visibility"),
        Index("ix_principles_tsv", "tsv", postgresql_using="gin"),
        Index("ix_principles_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_principles_tenant_visibility", "tenant_id", "visibility"),
        # AA-intake get-or-create backstop (WS1). Principle has no ``project_id``
        # column (it is tenant-wide by design); the AA-intake project-scoped
        # identity is ``origin_project_id``. Partial on ``origin_project_id IS
        # NOT NULL`` so tenant-wide (hand-authored) principles that share a title
        # keep minting distinct slugs. See models/requirements.py for why this
        # lives on the model.
        Index(
            "uq_principles_tenant_origin_project_title",
            "tenant_id",
            "origin_project_id",
            "title",
            unique=True,
            postgresql_where=text("origin_project_id IS NOT NULL AND visibility = 'tenant'"),
        ),
        # G8 / R313: one project copy per (original, project), for life — ``supersedes`` is
        # the original's slug; a promoted (SUPERSEDED) copy is revived by the next
        # project-copy, never duplicated. The race guard behind
        # ``GovernedAssetService.project_copy`` (migration 2026_10_19_0063).
        Index(
            "uq_principles_tenant_supersedes_origin_project",
            "tenant_id",
            "supersedes",
            "origin_project_id",
            unique=True,
            postgresql_where=text("supersedes IS NOT NULL AND origin_project_id IS NOT NULL"),
        ),
    )
