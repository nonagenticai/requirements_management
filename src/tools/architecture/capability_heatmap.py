"""Capability scoring heatmap builder."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

DEFAULT_DIMENSIONS: list[str] = ["maturity", "strategic_importance", "investment_need"]

_REALIZATION_LAYER_TYPES: frozenset[str] = frozenset(
    {
        "application_component",
        "application_service",
        "application_function",
        "application_process",
        "application_collaboration",
        "application_interface",
        "node",
        "device",
        "system_software",
        "technology_service",
        "technology_function",
        "technology_process",
        "artifact",
    }
)

_GOAL_OUTCOME_TYPES: frozenset[str] = frozenset({"goal", "outcome"})

_GAP_TYPE: str = "gap"


def _element_summary(elem: dict) -> dict[str, Any]:
    return {
        "id": str(elem.get("id", "")),
        "title": elem.get("title", ""),
        "layer": elem.get("layer", ""),
        "element_type": elem.get("element_type", ""),
    }


def build_capability_heatmap(
    elements: list[dict],
    relationships: list[dict],
    dimensions: list[str] | None = None,
) -> dict[str, Any]:
    dims = dimensions or DEFAULT_DIMENSIONS

    element_index: dict[str, dict] = {str(e["id"]): e for e in elements}

    neighbors: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for rel in relationships:
        src = str(rel["source_id"])
        tgt = str(rel["target_id"])
        rel_type = rel.get("relationship_type", "association")
        neighbors[src].append((tgt, rel_type))
        neighbors[tgt].append((src, rel_type))

    capabilities = [e for e in elements if e.get("element_type") == "capability"]

    results: list[dict] = []

    for cap in capabilities:
        cap_id = str(cap["id"])
        scores: dict[str, float] = {}

        if "maturity" in dims:
            realization_count = 0
            for neighbor_id, rel_type in neighbors.get(cap_id, []):
                if rel_type == "realization":
                    neighbor = element_index.get(neighbor_id, {})
                    if neighbor.get("element_type", "") in _REALIZATION_LAYER_TYPES:
                        realization_count += 1
            scores["maturity"] = round(min(5.0, float(realization_count)), 2)

        if "strategic_importance" in dims:
            goal_count = 0
            for neighbor_id, _rel_type in neighbors.get(cap_id, []):
                neighbor = element_index.get(neighbor_id, {})
                if neighbor.get("element_type", "") in _GOAL_OUTCOME_TYPES:
                    goal_count += 1
            scores["strategic_importance"] = round(min(5.0, float(goal_count)), 2)

        if "investment_need" in dims:
            gap_count = 0
            for neighbor_id, _rel_type in neighbors.get(cap_id, []):
                neighbor = element_index.get(neighbor_id, {})
                if neighbor.get("element_type", "") == _GAP_TYPE:
                    gap_count += 1
            maturity = scores.get("maturity", 0.0)
            scores["investment_need"] = round(min(5.0, (5.0 - maturity) + gap_count), 2)

        results.append(
            {
                "element": _element_summary(cap),
                "scores": scores,
            }
        )

    averages: dict[str, float] = {}
    if results:
        for dim in dims:
            vals = [r["scores"].get(dim, 0.0) for r in results]
            averages[dim] = round(sum(vals) / len(vals), 2)
    else:
        for dim in dims:
            averages[dim] = 0.0

    return {
        "capabilities": results,
        "averages": averages,
    }
