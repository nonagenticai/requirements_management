"""Register the synchronous quality heuristic for every evaluable entity.

Called once at app startup (from ``lifespan``). Idempotent — re-running
``register_all`` overwrites prior registrations with the same callables.

On entity create/update the entity services call :func:`run_heuristic_now`
inside the request-bound session, so the persisted quality score is in the
response. Heuristics must be synchronous and pure
(``entity → (score, issues, passes)``).

There is no LLM evaluation lane any more (removed 2026-10-26); historical
``quality_evaluations`` rows with ``evaluator_kind='llm'`` remain readable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from quality import quality_check as _requirement_heuristic
from quality_arch import (
    decision_quality_check,
    element_quality_check,
    principle_quality_check,
    view_quality_check,
)
from quality_pm import (
    benefit_quality_check,
    risk_quality_check,
)
from services.quality_service import QualityResult, persist_quality

HeuristicFn = Callable[[Any], tuple[float, list[str], list[str]]]

HEURISTICS: dict[str, HeuristicFn] = {}


def register_heuristic(entity_type: str, fn: HeuristicFn) -> None:
    """Register a synchronous heuristic for ``entity_type``."""
    HEURISTICS[entity_type] = fn


async def run_heuristic_now(session: Any, entity: Any, entity_type: str) -> bool:
    """Run the heuristic synchronously and persist (no commit).

    Returns True if a heuristic ran, False if none is registered.
    The caller is responsible for committing the surrounding transaction.
    """
    fn = HEURISTICS.get(entity_type)
    if fn is None:
        return False
    score, issues, _passes = fn(entity)
    await persist_quality(
        session,
        entity,
        QualityResult(score=score, issues=issues, suggestions=[], agent_name=None),
    )
    return True


def _requirement_heuristic_adapter(r: Any) -> tuple[float, list[str], list[str]]:
    """Requirement's heuristic takes 3 fields, not the row."""
    return _requirement_heuristic(r.title or "", r.description or "", r.acceptance or [])


def register_all() -> None:
    """Register every heuristic on startup."""
    register_heuristic("requirement", _requirement_heuristic_adapter)
    register_heuristic("principle", principle_quality_check)
    register_heuristic("decision", decision_quality_check)
    register_heuristic("architecture_element", element_quality_check)
    register_heuristic("architecture_view", view_quality_check)
    register_heuristic("risk", risk_quality_check)
    register_heuristic("benefit", benefit_quality_check)
