"""The minimal project brief for tests that create a project (George 2026-10-13).

``POST /pm/projects`` requires a ``brief`` whenever it creates a row (422 ``brief_required``):
version 1 is always the user's. Tests that only need *a* project send this one.
"""

from typing import Any

MIN_BRIEF: dict[str, Any] = {"notes": "minimal test brief"}


def with_brief(body: dict[str, Any]) -> dict[str, Any]:
    """``body`` plus the minimal brief, unless it already carries one."""
    return {"brief": MIN_BRIEF, **body}
