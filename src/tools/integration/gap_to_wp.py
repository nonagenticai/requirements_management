"""Generate work package proposals from architecture gap analysis."""

from __future__ import annotations

from typing import Any

from tools.architecture.gap_analysis import analyze_gaps


def generate_workpackage_proposals(
    elements: list[dict],
    relationships: list[dict],
) -> dict[str, Any]:
    """Produce work package proposals for unresolved architecture gaps.

    Runs gap analysis internally, then generates a proposal for each
    new or modified element that lacks a linked work_package.

    Returns proposals only — the caller decides whether to persist them.
    """
    gap_result = analyze_gaps(elements, relationships)

    proposals: list[dict] = []

    for item in gap_result.get("new", []):
        if item.get("has_work_package"):
            continue
        elem = item.get("element", {})
        proposals.append(_proposal_from_element(elem, reason="new_in_target"))

    for item in gap_result.get("modified", []):
        if item.get("has_work_package"):
            continue
        elem = item.get("target", {})
        proposals.append(_proposal_from_element(elem, reason="modified"))

    total = len(gap_result.get("new", [])) + len(gap_result.get("modified", []))
    resolved = total - len(proposals)

    return {
        "proposals": proposals,
        "unresolved_count": len(proposals),
        "resolved_count": resolved,
        "gap_summary": {
            "retained": len(gap_result.get("retained", [])),
            "modified": len(gap_result.get("modified", [])),
            "new": len(gap_result.get("new", [])),
            "eliminated": len(gap_result.get("eliminated", [])),
        },
    }


def _proposal_from_element(elem: dict, reason: str) -> dict[str, Any]:
    title = elem.get("title", "Untitled")
    layer = elem.get("layer", "")
    etype = elem.get("element_type", "")
    return {
        "suggested_title": f"WP: Implement {title}",
        "suggested_description": (
            f"Work package to realize the {reason.replace('_', ' ')} {layer} {etype} '{title}'."
        ),
        "layer": "implementation_migration",
        "element_type": "work_package",
        "linked_gap_element_id": elem.get("id", ""),
        "linked_gap_element_title": title,
        "reason": reason,
    }
