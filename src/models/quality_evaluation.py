"""Append-only history of every quality evaluation (heuristic or LLM judge).

``QualityMixin`` keeps only the LATEST score on the entity row, so every new
evaluation overwrote the previous one and nobody could tell whether a judge was
stable, drifting, or had just been re-run against a different model. This table
records each evaluation as its own row and is never updated in place.

The entity row's ``quality_*`` columns stay the "latest" read shape fr already
consumes; this table is the history behind them.

Rows are polymorphic over the entity tables (``entity_type`` + ``entity_id``) and
therefore carry no foreign key. Deleting an entity leaves its history rows in
place — they are tenant-scoped (RLS) and are what an audit of "what did the
judge say about the thing that was deleted" needs.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import DateTime, Float, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base

#: ``evaluator_kind`` values. ``heuristic`` = a deterministic rule-based scorer
#: (``evaluator`` is ``heuristic`` or e.g. ``clr_structural``), ``llm`` = an LLM
#: judge (``evaluator`` names the agent).
EVALUATOR_KINDS = ("heuristic", "llm")


class QualityEvaluation(Base):
    __tablename__ = "quality_evaluations"
    __table_args__ = (
        Index(
            "ix_quality_evaluations_entity_created",
            "tenant_id",
            "entity_type",
            "entity_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[UUID] = mapped_column(Uuid)

    # NOT NULL on purpose: a row exists only when a real score was produced. A
    # judge that had no verdict raises before anything is written.
    score: Mapped[float] = mapped_column(Float)
    issues: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    suggestions: Mapped[list[Any]] = mapped_column(JSONB, default=list)

    evaluator_kind: Mapped[str] = mapped_column(String(16))
    # Agent name, or "heuristic" for the unnamed rule-based scorer.
    evaluator: Mapped[str] = mapped_column(String(80))

    # Provenance as worker_management reported it. NULL = not captured (never a
    # guess). Always NULL for heuristic rows.
    model_id: Mapped[str | None] = mapped_column(String(255))
    served_by: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(255))
    prompt_sha: Mapped[str | None] = mapped_column(String(255))
    correlation_id: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
