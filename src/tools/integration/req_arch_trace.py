"""Requirement ↔ architecture element traceability analysis."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

TRACE_RELATIONSHIP_TYPES: frozenset[str] = frozenset({"traceability", "realization"})

REQ_ENTITY_TYPES: frozenset[str] = frozenset({"requirements"})
ARCH_ENTITY_TYPES: frozenset[str] = frozenset({"architecture_elements"})


def trace_requirements(
    requirements: list[dict],
    elements: list[dict],
    relationships: list[dict],
) -> dict[str, Any]:
    """Compute coverage of requirements traced to architecture elements.

    Returns which requirements have architecture traces, which don't,
    and which architecture elements lack requirement linkage.
    """
    req_index = {str(r["id"]): r for r in requirements}
    elem_index = {str(e["id"]): e for e in elements}

    # Build traceability links. Each inner dict is an insertion-ordered set: it
    # dedupes duplicate/reverse-direction edges (the same (req, elem) authored
    # twice, or once each way) while preserving first-seen order.
    req_to_elems: dict[str, dict[str, None]] = defaultdict(dict)
    elem_to_reqs: dict[str, dict[str, None]] = defaultdict(dict)
    # Edges pointing at an endpoint that is not in the loaded roster (no FK on
    # source_id/target_id + no metamodel validation for req↔elem edges, so a
    # dangling or cross-project id is accepted at write time). Report them rather
    # than silently dropping — a swallowed edge understates coverage invisibly,
    # and a half-dangling edge previously counted a requirement as "traced" with
    # an empty linked_elements list (a fabricated trace).
    dangling_endpoints: list[dict[str, Any]] = []

    for rel in relationships:
        if not isinstance(rel, dict):
            continue  # non-dict junk in the relationship list — not an edge
        if rel.get("relationship_type", "") not in TRACE_RELATIONSHIP_TYPES:
            continue

        src_type = rel.get("source_entity_type", "")
        tgt_type = rel.get("target_entity_type", "")
        # `is not None` (not `or ""`): a falsy-but-valid endpoint id such as the
        # integer 0 must NOT be coerced to "" and dropped as malformed — that
        # would be the silent swallow this function exists to prevent.
        src_raw = rel.get("source_id")
        tgt_raw = rel.get("target_id")
        src_id = str(src_raw) if src_raw is not None else ""
        tgt_id = str(tgt_raw) if tgt_raw is not None else ""
        if not src_id or not tgt_id:
            continue  # malformed edge — cannot resolve either endpoint

        # Normalise orientation to (requirement, element).
        if src_type in REQ_ENTITY_TYPES and tgt_type in ARCH_ENTITY_TYPES:
            req_id, elem_id = src_id, tgt_id
        elif src_type in ARCH_ENTITY_TYPES and tgt_type in REQ_ENTITY_TYPES:
            req_id, elem_id = tgt_id, src_id
        else:
            continue

        req_missing = req_id not in req_index
        elem_missing = elem_id not in elem_index
        if req_missing:
            dangling_endpoints.append(_dangling(rel, "requirement", req_id))
        if elem_missing:
            dangling_endpoints.append(_dangling(rel, "element", elem_id))
        if req_missing or elem_missing:
            continue  # never record a link to an out-of-scope entity

        req_to_elems[req_id][elem_id] = None
        elem_to_reqs[elem_id][req_id] = None

    traced: list[dict] = []
    untraced_requirements: list[dict] = []

    for rid, req in req_index.items():
        linked_elem_ids = list(req_to_elems.get(rid, {}))
        if linked_elem_ids:
            traced.append(
                {
                    "requirement": _req_summary(req),
                    "linked_elements": [_elem_summary(elem_index[eid]) for eid in linked_elem_ids],
                }
            )
        else:
            untraced_requirements.append(_req_summary(req))

    untraced_elements: list[dict] = [
        _elem_summary(elem) for eid, elem in elem_index.items() if eid not in elem_to_reqs
    ]

    # Denominator uses the deduped roster (req_index), not the raw list, so the
    # honesty-critical counts reconcile: traced + untraced == total_requirements
    # even if the caller passed duplicate requirement ids.
    total_reqs = len(req_index)
    traced_count = len(traced)
    # 0/0 is undefined: an empty project has no denominator, so coverage is None
    # (honest "no data"), NOT 1.0 (fabricated perfect) or 0.0 (fabricated failure).
    coverage = round(traced_count / total_reqs, 4) if total_reqs > 0 else None

    return {
        "requirement_coverage": coverage,
        "traced_count": traced_count,
        "total_requirements": total_reqs,
        "traced": traced,
        "untraced_requirements": untraced_requirements,
        "untraced_elements": untraced_elements,
        "dangling_endpoints": dangling_endpoints,
    }


def _dangling(rel: dict, endpoint: str, missing_id: str) -> dict[str, Any]:
    """Summarise a trace edge whose ``endpoint`` id is not in the loaded roster."""
    return {
        "relationship_id": str(rel.get("id", "")),
        "relationship_type": rel.get("relationship_type", ""),
        "endpoint": endpoint,  # "requirement" | "element"
        "missing_id": missing_id,
    }


def _req_summary(req: dict) -> dict[str, Any]:
    return {
        "id": str(req.get("id", "")),
        "title": req.get("title", ""),
        "slug": req.get("slug", ""),
    }


def _elem_summary(elem: dict) -> dict[str, Any]:
    return {
        "id": str(elem.get("id", "")),
        "title": elem.get("title", ""),
        "layer": elem.get("layer", ""),
        "element_type": elem.get("element_type", ""),
    }
