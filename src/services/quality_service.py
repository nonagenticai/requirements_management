"""Quality persistence service.

Writes one quality evaluation (heuristic or LLM judge) in two places, in the
same transaction:

1. the five ``QualityMixin`` columns on the entity row — the LATEST score, the
   read shape fr already consumes, unchanged;
2. one APPEND-ONLY row in ``quality_evaluations`` — the history behind it.

Nothing here decides anything: the judges are advisory, and this module only
records what they said.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.quality_evaluation import QualityEvaluation

#: ``__tablename__`` of every QualityMixin model → the ``entity_type`` string the
#: eval dispatcher already uses. Explicit rather than derived so a renamed table
#: cannot silently change the key history is read back by.
ENTITY_TYPE_BY_TABLE: dict[str, str] = {
    "requirements": "requirement",
    "principles": "principle",
    "decisions": "decision",
    "architecture_elements": "architecture_element",
    "architecture_views": "architecture_view",
    "pm_risks": "risk",
    "pm_benefits": "benefit",
    "toc_trees": "toc_tree",
}

#: Named evaluators that are deterministic scorers, not LLM judges. A persist
#: with ``agent_name`` None is the heuristic; anything else is an LLM judge.
NON_LLM_EVALUATORS: frozenset[str] = frozenset({"clr_structural"})

_PROVENANCE_KEYS = ("model_id", "served_by", "prompt_version", "prompt_sha", "correlation_id")


@dataclass
class QualityResult:
    """A single quality evaluation outcome bundle.

    ``agent_name`` None means the heuristic scorer. ``provenance`` is the LLM
    call provenance recorded by the (since-removed) LLM judges; absent keys are
    recorded as NULL. Kept so historical ``evaluator_kind='llm'`` rows and any
    named non-LLM evaluator keep their write/read shape.
    """

    score: float
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    agent_name: str | None = None
    provenance: Mapping[str, str | None] | None = None


def entity_type_of(entity: Any) -> str:
    """The history key for ``entity``. Raises for a model that is not mapped."""
    table = str(getattr(entity, "__tablename__", ""))
    try:
        return ENTITY_TYPE_BY_TABLE[table]
    except KeyError:
        raise ValueError(
            f"no quality-history entity_type for table {table!r}; add it to ENTITY_TYPE_BY_TABLE"
        ) from None


async def persist_quality(session: AsyncSession, entity: Any, result: QualityResult) -> None:
    """Record ``result`` as the entity's latest score AND append it to history.

    The caller is responsible for committing the surrounding transaction.
    ``last_evaluated_at`` is stamped with ``datetime.now(timezone.utc)`` at
    write time so the timestamp reflects persistence, not computation.
    """
    score = float(result.score)
    issues = list(result.issues)
    suggestions = list(result.suggestions)
    entity.quality_score = score
    entity.quality_issues = issues
    entity.quality_suggestions = suggestions
    # One timestamp for both writes, so the latest history row and the entity's
    # ``last_evaluated_at`` are provably the same evaluation.
    now = datetime.now(timezone.utc)
    entity.last_evaluated_at = now
    entity.last_evaluated_by_agent = result.agent_name

    is_llm = bool(result.agent_name) and result.agent_name not in NON_LLM_EVALUATORS
    prov = dict(result.provenance or {}) if is_llm else {}
    session.add(
        QualityEvaluation(
            tenant_id=entity.tenant_id,
            entity_type=entity_type_of(entity),
            entity_id=entity.id,
            score=score,
            issues=issues,
            suggestions=suggestions,
            evaluator_kind="llm" if is_llm else "heuristic",
            evaluator=result.agent_name or "heuristic",
            created_at=now,
            **{k: prov.get(k) for k in _PROVENANCE_KEYS},
        )
    )
    await session.flush()


def history_item(row: QualityEvaluation) -> dict[str, Any]:
    """One history row in the wire shape of ``GET …/quality/history``."""
    return {
        "id": str(row.id),
        "score": row.score,
        "issues": list(row.issues or []),
        "suggestions": list(row.suggestions or []),
        "evaluator_kind": row.evaluator_kind,
        "evaluator": row.evaluator,
        "model_id": row.model_id,
        "served_by": row.served_by,
        "prompt_version": row.prompt_version,
        "prompt_sha": row.prompt_sha,
        "correlation_id": row.correlation_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def read_history(
    session: AsyncSession,
    *,
    tenant_id: str,
    entity_type: str,
    entity_id: UUID,
    limit: int,
) -> Sequence[QualityEvaluation]:
    """Newest-first evaluation history for one entity, tenant-scoped."""
    stmt = (
        select(QualityEvaluation)
        .where(
            QualityEvaluation.tenant_id == tenant_id,
            QualityEvaluation.entity_type == entity_type,
            QualityEvaluation.entity_id == entity_id,
        )
        .order_by(QualityEvaluation.created_at.desc(), QualityEvaluation.id.desc())
        .limit(limit)
    )
    return (await session.execute(stmt)).scalars().all()
