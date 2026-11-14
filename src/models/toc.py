"""Logical Thinking Process (Theory of Constraints) models.

H. William Dettmer's Logical Thinking Process is a set of six logic
structures plus the Categories of Legitimate Reservation (CLR). Each
structure is a typed graph of statement-nodes connected by necessity-
or sufficiency-logic edges:

* ``io_map``            Intermediate Objectives Map (Goal-Tree, necessity)
* ``evaporating_cloud`` the conflict cloud (necessity + a conflict wedge)
* ``crt``               Current Reality Tree (sufficiency, UDEs → root causes)
* ``frt``               Future Reality Tree (sufficiency, injections → effects)
* ``prt``               Prerequisite Tree (necessity, obstacles → IOs)
* ``trt``               Transition Tree (sufficiency, need/action/effect)

Nodes live in ``toc_nodes``; edges reuse the universal
``architecture_relationships`` table (relationship_type ``toc_*``,
``source_entity_type``/``target_entity_type`` == ``toc_nodes``). This file
adds no edge table — see ``models/architecture.py:RELATIONSHIP_TYPES``.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
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

# Valid tree types (the six structures of the Logical Thinking Process).
TREE_TYPES = frozenset({"io_map", "evaporating_cloud", "crt", "frt", "prt", "trt"})

# Valid node kinds per tree type. The router validates a node's kind against
# the set allowed for its tree's type before persisting.
NODE_KINDS_BY_TREE_TYPE: dict[str, frozenset[str]] = {
    "io_map": frozenset({"goal", "csf", "necessary_condition"}),
    "evaporating_cloud": frozenset(
        {"objective", "requirement", "prerequisite", "assumption", "injection"}
    ),
    "crt": frozenset({"ude", "intermediate_cause", "root_cause"}),
    "frt": frozenset({"injection", "intermediate_effect", "desired_effect"}),
    "prt": frozenset({"objective", "obstacle", "intermediate_objective"}),
    "trt": frozenset({"need", "action", "expected_effect"}),
}

# Flat union for the DB CheckConstraint (cross-tree-type uniqueness of kinds
# is not required; the router enforces the per-tree-type subset).
ALL_NODE_KINDS = frozenset().union(*NODE_KINDS_BY_TREE_TYPE.values())


class TocTree(QualityMixin, TenantSubjectMixin, Base):
    """A single Logical-Thinking-Process structure (container of nodes)."""

    __tablename__ = "toc_trees"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    project_id: Mapped[str | None] = mapped_column(String(100), index=True)
    tree_type: Mapped[str] = mapped_column(String(30), index=True)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    version_status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    supersedes: Mapped[str | None] = mapped_column(String(120))
    superseded_by: Mapped[str | None] = mapped_column(String(120))
    version_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))",
            persisted=True,
        ),
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_toc_trees_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_toc_trees_visibility"),
        CheckConstraint(
            "tree_type IN ('io_map', 'evaporating_cloud', 'crt', 'frt', 'prt', 'trt')",
            name="ck_toc_trees_tree_type",
        ),
        Index("ix_toc_trees_tsv", "tsv", postgresql_using="gin"),
        Index("ix_toc_trees_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_toc_trees_tenant_visibility", "tenant_id", "visibility"),
        Index("ix_toc_trees_tenant_type", "tenant_id", "tree_type"),
        # AA-intake get-or-create backstop (WS1, Group A). Identity includes
        # tree_type: the same project may hold one tree of each of the six types.
        Index(
            "uq_toc_trees_tenant_project_type_title",
            "tenant_id",
            "project_id",
            "tree_type",
            "title",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL AND visibility = 'tenant'"),
        ),
    )


class TocNode(TenantSubjectMixin, Base):
    """A single statement (entity) inside a TOC tree."""

    __tablename__ = "toc_nodes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    tree_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("toc_trees.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    statement: Mapped[str] = mapped_column(Text)
    node_kind: Mapped[str] = mapped_column(String(40), index=True)
    layout: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=dict)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    project_id: Mapped[str | None] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    version_status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    supersedes: Mapped[str | None] = mapped_column(String(120))
    superseded_by: Mapped[str | None] = mapped_column(String(120))
    version_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_toc_nodes_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_toc_nodes_visibility"),
        Index("ix_toc_nodes_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_toc_nodes_tenant_visibility", "tenant_id", "visibility"),
        Index("ix_toc_nodes_tree", "tree_id"),
        # AA-intake get-or-create backstop (WS1, Group A). tree_id is NOT NULL so
        # no IS NOT NULL predicate is needed; partial only on visibility='tenant'.
        Index(
            "uq_toc_nodes_tenant_tree_title",
            "tenant_id",
            "tree_id",
            "title",
            unique=True,
            postgresql_where=text("visibility = 'tenant'"),
        ),
    )


class TocSettings(Base):
    """3-level (global → tenant → project) Logical-Thinking-Process config.

    Resolution order at read time (``services.toc_service.resolve_settings``):
    project row → tenant row → global row (``tenant_id IS NULL``) → hardcoded
    defaults. ``project_id`` is a non-null sentinel ('' == the tenant-level row)
    so a plain ``(tenant_id, project_id)`` unique key works without COALESCE
    index gymnastics.

    ⚠️ THE GLOBAL ROW IS ``tenant_id IS NULL``, NOT A SENTINEL STRING. It used to
    be the literal ``'__global__'``, which stopped being expressible when
    ``tenant_id`` became a ``uuid`` column in ``2026_08_24_0042`` — but the
    string was already the wrong mechanism, and swapping it for a sentinel UUID
    would have kept the real bug. ``kag_common.database.rls`` gives a NULLABLE
    ``tenant_id`` a "global-or-own" policy (``tenant_id IS NULL OR tenant_id =
    <guc>``) instead of strict equality, so under ``TENANT_RLS_MODE=enforce``
    every tenant can READ the global row while writes stay scoped. With the old
    string sentinel the strict policy hid the global row from every tenant, so
    the global tier resolved to nothing and silently fell through to the
    hardcoded defaults. The table is empty in production, so nothing had ever
    exercised it. Writes are still scoped: nobody can create or edit the global
    row through the request path.

    ``NULLS NOT DISTINCT`` on the unique key is load-bearing — without it
    Postgres treats every NULL as unique and you get unlimited global rows.
    """

    __tablename__ = "toc_settings"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    tenant_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), index=True, nullable=True)
    project_id: Mapped[str] = mapped_column(String(100), default="", server_default="")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            name="uq_toc_settings_tenant_project",
            postgresql_nulls_not_distinct=True,
        ),
    )


# Global-default sentinel for the config table: the row whose tenant_id IS NULL.
# ``None``, not a string — see TocSettings' docstring.
GLOBAL_SETTINGS_TENANT: str | None = None

# Hardcoded fallback used when no global row has been seeded.
DEFAULT_TOC_SETTINGS: dict[str, Any] = {
    "ec_auto_severity_threshold": "medium",
    "enabled_lenses": ["clr", "necessity_sufficiency", "goal_tree_relevance"],
}
