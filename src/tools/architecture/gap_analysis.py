"""Baseline vs target gap analysis."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _normalize(s: str) -> str:
    return s.strip().lower().replace("-", "_").replace(" ", "_")


def _fuzzy_match(a: dict, b: dict) -> bool:
    if a.get("slug") and b.get("slug") and _normalize(a["slug"]) == _normalize(b["slug"]):
        return True
    return (
        _normalize(a.get("layer", "")) == _normalize(b.get("layer", ""))
        and _normalize(a.get("element_type", "")) == _normalize(b.get("element_type", ""))
        and _normalize(a.get("title", "")) == _normalize(b.get("title", ""))
    )


def _element_summary(elem: dict) -> dict[str, Any]:
    return {
        "id": str(elem.get("id", "")),
        "slug": elem.get("slug", ""),
        "title": elem.get("title", ""),
        "layer": elem.get("layer", ""),
        "element_type": elem.get("element_type", ""),
        "architecture_state": elem.get("architecture_state", ""),
    }


def _is_linked_to_work_package(
    element_id: str,
    rel_index: dict[str, list[dict]],
    element_index: dict[str, dict],
) -> bool:
    for rel in rel_index.get(element_id, []):
        other_id = (
            str(rel["target_id"]) if str(rel["source_id"]) == element_id else str(rel["source_id"])
        )
        other = element_index.get(other_id, {})
        if other.get("element_type") == "work_package":
            return True
    return False


def analyze_gaps(
    elements: list[dict],
    relationships: list[dict],
) -> dict[str, Any]:
    baseline: list[dict] = []
    target: list[dict] = []
    for elem in elements:
        state = (elem.get("architecture_state") or "").lower()
        if state == "baseline":
            baseline.append(elem)
        elif state == "target":
            target.append(elem)

    element_index: dict[str, dict] = {str(e["id"]): e for e in elements}

    rel_index: dict[str, list[dict]] = defaultdict(list)
    for rel in relationships:
        src = str(rel["source_id"])
        tgt = str(rel["target_id"])
        rel_index[src].append(rel)
        rel_index[tgt].append(rel)

    matched_baseline: set[int] = set()
    matched_target: set[int] = set()

    retained: list[dict] = []
    modified: list[dict] = []

    for ti, t_elem in enumerate(target):
        for bi, b_elem in enumerate(baseline):
            if bi in matched_baseline:
                continue
            if _fuzzy_match(b_elem, t_elem):
                matched_baseline.add(bi)
                matched_target.add(ti)
                b_props = b_elem.get("properties") or {}
                t_props = t_elem.get("properties") or {}
                b_desc = b_elem.get("description", "") or ""
                t_desc = t_elem.get("description", "") or ""
                if b_props == t_props and b_desc == t_desc:
                    retained.append(
                        {
                            "baseline": _element_summary(b_elem),
                            "target": _element_summary(t_elem),
                        }
                    )
                else:
                    entry: dict = {
                        "baseline": _element_summary(b_elem),
                        "target": _element_summary(t_elem),
                        "has_work_package": _is_linked_to_work_package(
                            str(t_elem["id"]), rel_index, element_index
                        ),
                    }
                    modified.append(entry)
                break

    new_items: list[dict] = []
    for ti, t_elem in enumerate(target):
        if ti not in matched_target:
            new_items.append(
                {
                    "element": _element_summary(t_elem),
                    "has_work_package": _is_linked_to_work_package(
                        str(t_elem["id"]), rel_index, element_index
                    ),
                }
            )

    eliminated: list[dict] = []
    for bi, b_elem in enumerate(baseline):
        if bi not in matched_baseline:
            eliminated.append({"element": _element_summary(b_elem)})

    gaps_needing_resolution = new_items + modified
    total_gaps = len(gaps_needing_resolution)
    resolved = sum(1 for item in gaps_needing_resolution if item.get("has_work_package", False))
    # No gaps needing resolution => no resolution denominator => None (honest
    # "no data"), NOT a fabricated 1.0.
    gap_resolution_pct = round(resolved / total_gaps, 4) if total_gaps > 0 else None

    unresolved: list[dict] = []
    for item in gaps_needing_resolution:
        if not item.get("has_work_package", False):
            elem_data = item.get("element") or item.get("target", {})
            unresolved.append(elem_data)

    return {
        "retained": retained,
        "modified": modified,
        "new": new_items,
        "eliminated": eliminated,
        "gap_resolution_pct": gap_resolution_pct,
        "unresolved_gaps": unresolved,
    }
