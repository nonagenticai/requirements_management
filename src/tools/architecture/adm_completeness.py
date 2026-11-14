"""TOGAF ADM phase deliverable completeness checker."""

from __future__ import annotations

from typing import Any

ADM_DELIVERABLES: dict[str, dict[str, list[str] | bool]] = {
    "A": {
        "element_types": [
            "stakeholder",
            "driver",
            "goal",
            "outcome",
            "principle",
            "constraint",
        ],
        "relationship_types": ["influence", "association", "realization"],
        "viewpoints": ["motivation", "strategy"],
        "requires_principles": True,
        "requires_decisions": False,
        "requires_requirements": False,
    },
    "B": {
        "element_types": [
            "business_process",
            "business_function",
            "business_service",
            "business_actor",
            "business_role",
            "business_object",
        ],
        "relationship_types": [
            "composition",
            "aggregation",
            "assignment",
            "serving",
            "triggering",
            "flow",
        ],
        "viewpoints": ["organization", "business_process_cooperation"],
        "requires_principles": False,
        "requires_decisions": True,
        "requires_requirements": True,
    },
    "C": {
        "element_types": [
            "application_component",
            "application_service",
            "data_object",
        ],
        "relationship_types": [
            "serving",
            "realization",
            "access",
            "composition",
            "flow",
        ],
        "viewpoints": ["application_cooperation", "information_structure"],
        "requires_principles": False,
        "requires_decisions": True,
        "requires_requirements": True,
    },
    "D": {
        "element_types": [
            "node",
            "device",
            "system_software",
            "artifact",
            "communication_network",
        ],
        "relationship_types": [
            "composition",
            "assignment",
            "realization",
            "serving",
        ],
        "viewpoints": ["technology", "implementation_deployment"],
        "requires_principles": False,
        "requires_decisions": True,
        "requires_requirements": True,
    },
    "E": {
        "element_types": [
            "work_package",
            "deliverable",
            "plateau",
            "gap",
        ],
        "relationship_types": [
            "realization",
            "aggregation",
            "triggering",
            "association",
        ],
        "viewpoints": ["migration"],
        "requires_principles": False,
        "requires_decisions": True,
        "requires_requirements": False,
    },
    "F": {
        "element_types": [
            "plateau",
            "gap",
            "work_package",
        ],
        "relationship_types": [
            "triggering",
            "realization",
            "aggregation",
        ],
        "viewpoints": ["migration", "project"],
        "requires_principles": False,
        "requires_decisions": True,
        "requires_requirements": False,
    },
    "G": {
        "element_types": [
            "work_package",
            "deliverable",
            "implementation_event",
        ],
        "relationship_types": [
            "realization",
            "triggering",
            "assignment",
        ],
        "viewpoints": [],
        "requires_principles": False,
        "requires_decisions": True,
        "requires_requirements": False,
    },
    "H": {
        "element_types": [
            "assessment",
            "constraint",
            "goal",
        ],
        "relationship_types": ["influence", "association"],
        "viewpoints": ["motivation"],
        "requires_principles": True,
        "requires_decisions": True,
        "requires_requirements": False,
    },
}


def check_adm_completeness(
    phase: str,
    elements: list[dict],
    relationships: list[dict],
    views: list[dict],
    standards: list[dict],
    requirements_count: int,
    decisions_count: int,
    principles_count: int,
) -> dict[str, Any]:
    phase_key = phase.upper().strip()
    deliverables = ADM_DELIVERABLES.get(phase_key)
    if deliverables is None:
        # KEPT AS A DEFENSIVE RAISE, not deleted, and deliberately NOT a
        # 200-shaped success.
        #
        # Until 2026-09-04 this branch returned ``completeness_score: 0.0`` with
        # an explanation buried in ``missing[]`` — a value indistinguishable from
        # "this project genuinely has zero ADM completeness" for any caller that
        # reads the score, which is every caller. ``ADMCompletenessRequest.phase``
        # is now ``Literal["A".."H"]``, so the HTTP path can no longer reach here
        # (a typo 422s at the boundary with a named field).
        #
        # It is kept rather than deleted because this function is public and is
        # also called DIRECTLY, bypassing the request schema — ``tests/
        # test_arch_tools.py`` does so throughout, and any future background /
        # agent path would too. For those callers a named ValueError says what is
        # wrong and what is valid; a bare ``KeyError: 'Z'`` from
        # ``ADM_DELIVERABLES[phase_key]`` would not. Through the API an
        # unreachable branch that raises surfaces as a 500 ("this was supposed to
        # be impossible"), which is honest, where a 200 was not.
        raise ValueError(f"unknown ADM phase '{phase_key}'; valid: {', '.join(ADM_DELIVERABLES)}")

    raw_element_types = deliverables.get("element_types", [])
    required_element_types = raw_element_types if isinstance(raw_element_types, list) else []
    raw_rel_types = deliverables.get("relationship_types", [])
    required_rel_types = raw_rel_types if isinstance(raw_rel_types, list) else []
    raw_viewpoints = deliverables.get("viewpoints", [])
    required_viewpoints = raw_viewpoints if isinstance(raw_viewpoints, list) else []
    req_principles = bool(deliverables.get("requires_principles", False))
    req_decisions = bool(deliverables.get("requires_decisions", False))
    req_requirements = bool(deliverables.get("requires_requirements", False))

    present_element_types: set[str] = {e.get("element_type", "") for e in elements}
    present_rel_types: set[str] = {r.get("relationship_type", "") for r in relationships}
    present_viewpoints: set[str] = {v.get("viewpoint", "") for v in views}

    passed: list[str] = []
    missing: list[str] = []
    recommendations: list[str] = []

    checks_total = 0
    checks_passed = 0

    for et in required_element_types:
        checks_total += 1
        if et in present_element_types:
            passed.append(f"element_type '{et}' present")
            checks_passed += 1
        else:
            missing.append(f"element_type '{et}'")
            recommendations.append(f"create at least one '{et}' element")

    for rt in required_rel_types:
        checks_total += 1
        if rt in present_rel_types:
            passed.append(f"relationship_type '{rt}' present")
            checks_passed += 1
        else:
            missing.append(f"relationship_type '{rt}'")
            recommendations.append(f"add '{rt}' relationships between relevant elements")

    for vp in required_viewpoints:
        checks_total += 1
        if vp in present_viewpoints:
            passed.append(f"viewpoint '{vp}' present")
            checks_passed += 1
        else:
            missing.append(f"viewpoint '{vp}'")
            recommendations.append(f"create a '{vp}' view")

    if req_principles:
        checks_total += 1
        if principles_count > 0:
            passed.append(f"principles defined ({principles_count})")
            checks_passed += 1
        else:
            missing.append("principles")
            recommendations.append("define architecture principles")

    if req_decisions:
        checks_total += 1
        if decisions_count > 0:
            passed.append(f"decisions recorded ({decisions_count})")
            checks_passed += 1
        else:
            missing.append("decisions")
            recommendations.append("record architecture decisions (ADRs)")

    if req_requirements:
        checks_total += 1
        if requirements_count > 0:
            passed.append(f"requirements defined ({requirements_count})")
            checks_passed += 1
        else:
            missing.append("requirements")
            recommendations.append("define architecture requirements")

    if len(standards) > 0:
        passed.append(f"standards defined ({len(standards)})")

    # NOT a fabrication (unlike the sibling coverage tools): checks_total is driven
    # by the static ADM_DELIVERABLES spec for this phase, not by project data volume,
    # so for any valid phase A-H it is > 0. An empty project honestly scores 0.0 (0 of
    # N required deliverables present). The unknown-phase path raises above, so
    # the else-0.0 here is unreachable defensive code.
    score = round(checks_passed / checks_total, 4) if checks_total > 0 else 0.0

    return {
        "phase": phase_key,
        "completeness_score": score,
        "passed": passed,
        "missing": missing,
        "recommendations": recommendations,
    }
