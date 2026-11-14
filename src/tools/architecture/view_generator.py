"""Auto-generate ArchiMate views from viewpoint filters."""

from __future__ import annotations

from typing import Any

from tools.architecture.registry import get_registry

VIEWPOINT_FILTERS: dict[str, dict[str, list[str] | None]] = {
    "organization": {
        "layers": ["business"],
        "element_types": [
            "business_actor",
            "business_role",
            "business_collaboration",
            "business_interface",
        ],
        "relationship_types": None,
    },
    "business_process_cooperation": {
        "layers": ["business"],
        "element_types": [
            "business_process",
            "business_function",
            "business_interaction",
            "business_event",
            "business_service",
            "business_object",
        ],
        "relationship_types": ["composition", "aggregation", "triggering", "flow", "serving"],
    },
    "product": {
        "layers": ["business"],
        "element_types": ["product", "business_service", "contract", "business_interface"],
        "relationship_types": ["composition", "aggregation", "serving", "assignment"],
    },
    "application_cooperation": {
        "layers": ["application"],
        "element_types": None,
        "relationship_types": ["serving", "flow", "triggering", "composition", "realization"],
    },
    "application_usage": {
        "layers": ["application", "business"],
        "element_types": None,
        "relationship_types": ["serving", "realization", "assignment", "access"],
    },
    "technology_usage": {
        "layers": ["technology", "application"],
        "element_types": None,
        "relationship_types": ["serving", "realization", "assignment", "composition"],
    },
    "technology": {
        "layers": ["technology"],
        "element_types": None,
        "relationship_types": None,
    },
    "information_structure": {
        "layers": ["business", "application"],
        "element_types": ["data_object", "business_object", "artifact", "representation"],
        "relationship_types": [
            "composition",
            "aggregation",
            "association",
            "access",
            "realization",
        ],
    },
    "service_realization": {
        "layers": ["business", "application", "technology"],
        "element_types": [
            "business_service",
            "application_service",
            "technology_service",
            "application_component",
            "application_function",
            "business_process",
            "business_function",
        ],
        "relationship_types": ["realization", "serving", "composition", "assignment"],
    },
    "implementation_deployment": {
        "layers": ["technology", "physical"],
        "element_types": None,
        "relationship_types": ["assignment", "realization", "serving", "composition"],
    },
    "physical": {
        "layers": ["physical"],
        "element_types": None,
        "relationship_types": None,
    },
    "layered": {
        "layers": None,
        "element_types": None,
        "relationship_types": None,
    },
    "landscape": {
        "layers": None,
        "element_types": [
            "business_service",
            "application_service",
            "technology_service",
            "business_interface",
            "application_interface",
            "technology_interface",
            "application_component",
            "node",
        ],
        "relationship_types": ["serving", "realization", "flow"],
    },
    "capability_map": {
        "layers": ["strategy"],
        "element_types": ["capability", "resource"],
        "relationship_types": ["composition", "aggregation", "association", "realization"],
    },
    "value_stream": {
        "layers": ["strategy"],
        "element_types": ["value_stream", "capability", "course_of_action"],
        "relationship_types": ["triggering", "flow", "composition", "association", "realization"],
    },
    "motivation": {
        "layers": ["motivation"],
        "element_types": None,
        "relationship_types": ["influence", "association", "realization", "aggregation"],
    },
    "strategy": {
        "layers": ["strategy", "motivation"],
        "element_types": None,
        "relationship_types": None,
    },
    "migration": {
        "layers": ["implementation_migration"],
        "element_types": None,
        "relationship_types": ["triggering", "realization", "aggregation", "association"],
    },
    "project": {
        "layers": ["implementation_migration"],
        "element_types": [
            "work_package",
            "deliverable",
            "implementation_event",
            "plateau",
            "gap",
        ],
        "relationship_types": ["triggering", "realization", "aggregation", "composition"],
    },
    "stakeholder": {
        "layers": ["motivation"],
        "element_types": ["stakeholder", "driver", "assessment"],
        "relationship_types": ["influence", "association", "aggregation"],
    },
    "goal_realization": {
        "layers": ["motivation", "strategy"],
        "element_types": [
            "goal",
            "outcome",
            "principle",
            "constraint",
            "capability",
            "course_of_action",
        ],
        "relationship_types": ["realization", "influence", "aggregation", "association"],
    },
    "requirements_realization": {
        "layers": None,
        "element_types": None,
        "relationship_types": ["traceability", "realization"],
    },
}

# The five Archi 5.10.0 viewpoints the table above predates (finding A11).
# Their generation filter IS the viewpoint's allowed-concept list from the
# Archi-derived registry: there is no service-specific preference to encode,
# and a hand-copied list would be one more place to drift.
for _vp_id in (
    "application_structure",
    "capability",
    "implementation_migration",
    "outcome_realization",
    "resource",
):
    VIEWPOINT_FILTERS[_vp_id] = {
        "layers": None,
        "element_types": sorted(get_registry().viewpoints[_vp_id].allowed_concepts),
        "relationship_types": None,
    }


def generate_view(
    viewpoint: str,
    elements: list[dict],
    relationships: list[dict],
) -> dict[str, Any]:
    vp = viewpoint.strip().lower()
    filters = VIEWPOINT_FILTERS.get(vp)
    if filters is None:
        return {
            "viewpoint": vp,
            "element_ids": [],
            "boundary_element_ids": [],
            "relationship_ids": [],
            "element_count": 0,
            "relationship_count": 0,
        }

    allowed_layers = set(filters["layers"]) if filters["layers"] else None
    allowed_types = set(filters["element_types"]) if filters["element_types"] else None
    allowed_rels = set(filters["relationship_types"]) if filters["relationship_types"] else None

    matched_element_ids: set[str] = set()
    for elem in elements:
        layer = elem.get("layer", "")
        etype = elem.get("element_type", "")
        if allowed_layers and layer not in allowed_layers:
            continue
        if allowed_types and etype not in allowed_types:
            continue
        matched_element_ids.add(str(elem["id"]))

    # A relationship is included when either end matches, so the other end may
    # be outside the viewpoint. It used to be left out of the result entirely —
    # an edge pointing at nothing the view declares (finding A05). It is now
    # named as a BOUNDARY occurrence: shown so the edge is drawable, but not a
    # member the viewpoint selected.
    matched_relationship_ids: list[str] = []
    boundary_ids: set[str] = set()
    for rel in relationships:
        src = str(rel.get("source_id", ""))
        tgt = str(rel.get("target_id", ""))
        rel_type = rel.get("relationship_type", "")

        if src not in matched_element_ids and tgt not in matched_element_ids:
            continue
        if allowed_rels and rel_type not in allowed_rels:
            continue
        matched_relationship_ids.append(str(rel["id"]))
        boundary_ids.update({src, tgt} - matched_element_ids)

    return {
        "viewpoint": vp,
        "element_ids": sorted(matched_element_ids),
        "boundary_element_ids": sorted(boundary_ids),
        "relationship_ids": sorted(matched_relationship_ids),
        "element_count": len(matched_element_ids),
        "relationship_count": len(matched_relationship_ids),
    }
