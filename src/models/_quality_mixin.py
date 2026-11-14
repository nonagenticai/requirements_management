"""QualityMixin — persisted LLM quality evaluation columns.

Mixed into the nine entity models that participate in agent-driven quality
evaluation (LLM enhancements Phase 1). Schema is materialised by
``alembic/versions/2026_05_06_0020_add_quality_columns.py``.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class QualityMixin:
    """Five persisted columns recording the latest agent quality evaluation."""

    quality_score: Mapped[float | None] = mapped_column(nullable=True)
    quality_issues: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    quality_suggestions: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    # ``DateTime(timezone=True)`` is explicit because the annotation alone infers a
    # NAIVE ``DateTime()``. The column materialised by
    # ``2026_05_06_0020_add_quality_columns`` is ``TIMESTAMP WITH TIME ZONE``, so the
    # bare annotation made the model disagree with the DB on all ten tables that mix
    # this in — the single largest non-cosmetic entry in ``alembic check``. Every other
    # datetime column in this codebase already spells the flag out; this one was the
    # only omission.
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_evaluated_by_agent: Mapped[str | None] = mapped_column(nullable=True)
