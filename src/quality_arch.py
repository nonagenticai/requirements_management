"""Heuristic quality checks for architecture-domain entities.

Each function returns ``(score, issues, passes)`` where ``score`` is the
ratio of passed checks to total checks (0..1). Functions are intentionally
narrow and synchronous — they only inspect the entity itself, not the DB.
"""

from __future__ import annotations

from typing import Any

_DESCRIPTION_MIN_LEN = 30


def _normalised(score_passes: int, score_total: int) -> float:
    if score_total == 0:
        return 0.0
    return score_passes / score_total


def element_quality_check(el: Any) -> tuple[float, list[str], list[str]]:
    """Check ArchitectureElement description, layer, and element_type."""
    issues: list[str] = []
    passes: list[str] = []

    description = getattr(el, "description", None)
    if not description or len(description) < _DESCRIPTION_MIN_LEN:
        issues.append(f"Description too short (<{_DESCRIPTION_MIN_LEN} chars)")
    else:
        passes.append("Description length OK")

    if not getattr(el, "layer", None):
        issues.append("ArchiMate layer not set")
    else:
        passes.append("Layer set")

    if not getattr(el, "element_type", None):
        issues.append("Element type not set")
    else:
        passes.append("Element type set")

    return _normalised(len(passes), len(passes) + len(issues)), issues, passes


def view_quality_check(view: Any) -> tuple[float, list[str], list[str]]:
    """Check that a view has a viewpoint, populated elements, and a description."""
    issues: list[str] = []
    passes: list[str] = []

    if not getattr(view, "viewpoint", None):
        issues.append("Viewpoint not set")
    else:
        passes.append("Viewpoint set")

    description = getattr(view, "description", None)
    if not description or len(description) < _DESCRIPTION_MIN_LEN:
        issues.append(f"Description too short (<{_DESCRIPTION_MIN_LEN} chars)")
    else:
        passes.append("Description length OK")

    element_ids = getattr(view, "element_ids", None) or []
    if not element_ids:
        issues.append("View has no elements")
    else:
        passes.append("View has elements")

    return _normalised(len(passes), len(passes) + len(issues)), issues, passes


def decision_quality_check(decision: Any) -> tuple[float, list[str], list[str]]:
    """Check ADR has context, decision body, consequences, and alternatives."""
    issues: list[str] = []
    passes: list[str] = []

    if not getattr(decision, "context", None):
        issues.append("Context missing")
    else:
        passes.append("Context populated")

    if not getattr(decision, "decision", None):
        issues.append("Decision body missing")
    else:
        passes.append("Decision populated")

    if not getattr(decision, "consequences", None):
        issues.append("Consequences missing")
    else:
        passes.append("Consequences populated")

    alternatives = getattr(decision, "alternatives", None) or []
    if not alternatives:
        issues.append("No alternatives recorded")
    else:
        passes.append("Alternatives recorded")

    return _normalised(len(passes), len(passes) + len(issues)), issues, passes


def principle_quality_check(principle: Any) -> tuple[float, list[str], list[str]]:
    """Check Principle has a statement, rationale, and at least one implication."""
    issues: list[str] = []
    passes: list[str] = []

    if not getattr(principle, "statement", None):
        issues.append("Statement missing")
    else:
        passes.append("Statement populated")

    if not getattr(principle, "rationale", None):
        issues.append("Rationale missing")
    else:
        passes.append("Rationale populated")

    implications = getattr(principle, "implications", None) or []
    if not implications:
        issues.append("No implications recorded")
    else:
        passes.append("Implications recorded")

    return _normalised(len(passes), len(passes) + len(issues)), issues, passes
