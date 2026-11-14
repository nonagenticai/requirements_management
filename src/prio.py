"""
Prioritisation helper functions.

WSJF and RICE are simple heuristics to sequence and compare requirements.
WSJF (Weighted Shortest Job First) takes a single Cost of Delay value and
divides it by the job size (effort). RICE multiplies reach, impact and
confidence, then divides by effort. A helper ``cod_from_components``
sums business value, time criticality and risk reduction for SAFe-style
WSJF.
"""

import math


def wsjf(cost_of_delay: float, duration: float) -> float | None:
    """Compute a WSJF score.

    If ``duration`` is zero or negative, returns ``None``.
    """
    if duration <= 0:
        return None
    return cost_of_delay / duration


def rice(reach: float, impact: float, confidence: float, effort: float) -> float | None:
    """Compute a RICE score.

    If ``effort`` is zero or negative, returns ``None``.
    Checks for overflow and infinity, capping at a reasonable maximum.
    """
    if effort <= 0:
        return None
    result = (reach * impact * confidence) / effort
    if math.isinf(result) or math.isnan(result):
        return None
    # Cap at reasonable maximum
    return min(result, 1e12)


def cod_from_components(
    business_value: float, time_criticality: float, risk_reduction: float
) -> float:
    """Sum SAFe Cost of Delay components.

    The SAFe framework breaks Cost of Delay down into Business Value,
    Time Criticality and Risk Reduction/Opportunity Enablement. This helper
    simply adds those three components.
    """
    return business_value + time_criticality + risk_reduction
