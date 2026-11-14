"""Persisted principle-conflict records (governance backstop for the AA cockpit).

⚠️ The in-process LLM detectors that wrote this table (the principle-consistency
eval and the procedure reconciler) were REMOVED on 2026-10-26. The table, its
rows and the read/dismiss router stay (fr reads them); nothing in this service
writes new conflicts any more. The text below describes how existing rows were
produced.

One side is **always a principle**; the counterparty is either another principle
(``counterparty_kind='principle'`` — redundancy/contradiction between principles)
or a procedure (``counterparty_kind='procedure'`` — an agentic procedure body
that conflicts with a principle, detected against procedure_management bodies by
the reconciler).

Lifecycle was hybrid (formerly ``services.conflict_persistence``): re-detection
auto-resolves conflicts that no longer appear and auto-reopens ones that recur,
while a manually ``dismissed`` row is sticky — never auto-reopened unless the
counterparty content changed (``counterparty_hash`` differs).
"""

from datetime import datetime
from uuid import UUID, uuid7

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db import Base, TenantSubjectMixin


class PrincipleConflictRecord(TenantSubjectMixin, Base):
    __tablename__ = "principle_conflicts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)

    # Ownership / scoping — mirrors the Principle table (tenant-visible governance
    # data; subject_id defaults to tenant_id so scope() returns it to any member).
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    subject_id: Mapped[str] = mapped_column(String(255), default="", server_default="", index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")

    # The principle side is always present. FK cascades so a deleted principle
    # takes its conflict rows with it.
    principle_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("principles.id", ondelete="CASCADE"), index=True
    )
    counterparty_kind: Mapped[str] = mapped_column(String(16))  # 'principle' | 'procedure'
    counterparty_id: Mapped[str] = mapped_column(
        String(255)
    )  # principle uuid OR procedure asset_uuid
    # Fingerprint of the counterparty content used at detection time. A manual
    # 'dismissed' only auto-reopens when this changes (material change).
    counterparty_hash: Mapped[str | None] = mapped_column(String(64))

    conflict_type: Mapped[str] = mapped_column(
        String(40)
    )  # e.g. redundant | contradicts | violates
    severity: Mapped[str | None] = mapped_column(String(20))
    rationale: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16), default="open", server_default="open", index=True
    )  # open | resolved | dismissed
    source: Mapped[str] = mapped_column(
        String(40), default="consistency_agent", server_default="consistency_agent"
    )

    detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        # Idempotency key: one row per distinct conflict within a tenant. Re-eval
        # UPSERTs on this key rather than duplicating.
        UniqueConstraint(
            "tenant_id",
            "principle_id",
            "counterparty_kind",
            "counterparty_id",
            "conflict_type",
            name="uq_principle_conflicts_dedup",
        ),
        Index("ix_principle_conflicts_tenant_status", "tenant_id", "status"),
        Index(
            "ix_principle_conflicts_counterparty",
            "tenant_id",
            "counterparty_kind",
            "counterparty_id",
        ),
    )
