"""Heuristic quality checks for PM entities.

Mirrors the shape of ``quality_arch.py`` — each function returns
``(score, issues, passes)`` where score is a 0..1 ratio of passes/total.
Functions are synchronous and only inspect the entity itself.
"""

from __future__ import annotations

from typing import Any

_DESCRIPTION_MIN_LEN = 30


def _normalised(passes: int, total: int) -> float:
    if total == 0:
        return 0.0
    return passes / total


def risk_quality_check(risk: Any) -> tuple[float, list[str], list[str]]:
    """Heuristic: title, description, response_plan, owner, probability/impact populated."""
    issues: list[str] = []
    passes: list[str] = []

    if not getattr(risk, "title", None):
        issues.append("Title missing")
    else:
        passes.append("Title set")

    description = getattr(risk, "description", None) or ""
    if len(description) < _DESCRIPTION_MIN_LEN:
        issues.append(f"Description too short (<{_DESCRIPTION_MIN_LEN} chars)")
    else:
        passes.append("Description length OK")

    if not getattr(risk, "response_plan", None):
        issues.append("Response plan missing")
    else:
        passes.append("Response plan documented")

    if not getattr(risk, "owner", None):
        issues.append("Owner missing")
    else:
        passes.append("Owner assigned")

    probability = getattr(risk, "probability", None)
    impact = getattr(risk, "impact_score", None)
    if probability is None or impact is None:
        issues.append("Probability or impact missing")
    else:
        passes.append("Probability + impact scored")

    return _normalised(len(passes), len(passes) + len(issues)), issues, passes


def benefit_quality_check(benefit: Any) -> tuple[float, list[str], list[str]]:
    """Heuristic: SMART proxy — measurement, baseline, target, owner."""
    issues: list[str] = []
    passes: list[str] = []

    if not getattr(benefit, "title", None):
        issues.append("Title missing")
    else:
        passes.append("Title set")

    if not getattr(benefit, "measurement", None):
        issues.append("Measurement undefined")
    else:
        passes.append("Measurement defined")

    if getattr(benefit, "baseline_value", None) is None:
        issues.append("Baseline value missing")
    else:
        passes.append("Baseline captured")

    if getattr(benefit, "target_value", None) is None:
        issues.append("Target value missing")
    else:
        passes.append("Target captured")

    if not getattr(benefit, "owner", None):
        issues.append("Owner missing")
    else:
        passes.append("Owner assigned")

    return _normalised(len(passes), len(passes) + len(issues)), issues, passes
