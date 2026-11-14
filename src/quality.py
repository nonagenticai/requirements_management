"""
Requirement quality heuristics.

This module defines lightweight checks to evaluate the clarity and
testability of requirement descriptions. Ambiguous wording is flagged,
presence of at least one Gherkin scenario is required for testability,
and descriptions are encouraged to contain measurable values.
"""

import re

# Terms that often indicate ambiguity. These should be avoided in
# requirement descriptions because they are subjective or vague.
AMBIGUOUS_TERMS = [
    "optimize",
    "improve",
    "fast",
    "user-friendly",
    "simple",
    "etc",
    "approximately",
    "as soon as possible",
    "robust",
    "seamless",
    "state-of-the-art",
    "minimize",
    "maximize",
]


def _has_gherkin(acceptance: list[str]) -> bool:
    """Return True if any acceptance criterion looks like a Gherkin scenario."""
    pattern = re.compile(r"(?si)^\s*given .*?\n\s*when .*?\n\s*then .*?$")
    return any(pattern.search(item) for item in acceptance)


def quality_check(
    title: str, description: str, acceptance: list[str]
) -> tuple[float, list[str], list[str]]:
    """Check a requirement for common quality issues.

    Returns a tuple ``(score, issues, passes)`` where ``score`` is a float between 0.0 and 1.0 (ratio of checks passed),
    ``issues`` is a list of descriptive problem messages, and ``passes`` is a list of
    positive feedback messages.
    """
    issues: list[str] = []
    passes: list[str] = []

    # 1) Clarity: flag ambiguous terms present in the description
    found = [w for w in AMBIGUOUS_TERMS if re.search(rf"\b{re.escape(w)}\b", description, re.I)]
    if found:
        issues.append(f"Ambiguous wording: {', '.join(sorted(set(found)))}")
    else:
        passes.append("No obvious ambiguous wording found.")

    # 2) Testability: require at least one Gherkin scenario
    if _has_gherkin(acceptance):
        passes.append("At least one Gherkin scenario present.")
    else:
        issues.append("Add at least one Given/When/Then scenario.")

    # 3) Measurability: look for numbers or thresholds
    if re.search(r"\b\d+(\.\d+)?\b", description):
        passes.append("Contains measurable values.")
    else:
        issues.append("Add measurable values/thresholds to description.")

    total_checks = 3
    score = len(passes) / total_checks  # Return 0-1 scale, not percentage
    return score, issues, passes
