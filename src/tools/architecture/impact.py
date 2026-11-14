"""Change impact propagation analysis."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

RELATIONSHIP_WEIGHTS: dict[str, float] = {
    "composition": 1.0,
    "aggregation": 1.0,
    "realization": 0.8,
    "assignment": 0.8,
    "serving": 0.6,
    "triggering": 0.6,
    "flow": 0.6,
    "access": 0.4,
    "influence": 0.4,
    "association": 0.2,
    "specialization": 0.2,
    "traceability": 0.4,
}


def _build_bidirectional_adjacency(
    relationships: list[dict],
) -> dict[str, list[tuple[str, str, str]]]:
    adj: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for rel in relationships:
        src = str(rel["source_id"])
        tgt = str(rel["target_id"])
        rel_type = rel.get("relationship_type", "association")
        rel_id = str(rel.get("id", ""))
        adj[src].append((tgt, rel_type, rel_id))
        adj[tgt].append((src, rel_type, rel_id))
    return adj


def _element_summary(elem: dict) -> dict[str, Any]:
    return {
        "id": str(elem.get("id", "")),
        "title": elem.get("title", ""),
        "layer": elem.get("layer", ""),
        "element_type": elem.get("element_type", ""),
    }


def analyze_impact(
    element_id: str,
    elements: list[dict],
    relationships: list[dict],
    max_depth: int = 3,
) -> dict[str, Any]:
    element_index: dict[str, dict] = {str(e["id"]): e for e in elements}
    source = element_index.get(element_id)
    if source is None:
        return {
            "source": {"id": element_id, "title": "unknown", "layer": "", "element_type": ""},
            "affected": [],
        }

    adj = _build_bidirectional_adjacency(relationships)

    visited: dict[str, int] = {element_id: 0}
    parent_path: dict[str, list[str]] = {element_id: [element_id]}
    best_weight: dict[str, float] = {element_id: 1.0}

    queue: deque[tuple[str, int, float, list[str]]] = deque()
    queue.append((element_id, 0, 1.0, [element_id]))

    affected: list[dict] = []

    while queue:
        current_id, depth, cumulative_weight, path = queue.popleft()

        if depth >= max_depth:
            continue

        for neighbor_id, rel_type, _rel_id in adj.get(current_id, []):
            new_depth = depth + 1
            type_weight = RELATIONSHIP_WEIGHTS.get(rel_type, 0.2)
            distance_factor = 1.0 / new_depth
            impact_strength = round(distance_factor * type_weight, 4)

            if neighbor_id not in visited or impact_strength > best_weight.get(neighbor_id, 0):
                visited[neighbor_id] = new_depth
                new_path = path + [neighbor_id]
                parent_path[neighbor_id] = new_path
                best_weight[neighbor_id] = impact_strength
                queue.append((neighbor_id, new_depth, impact_strength, new_path))

    for eid, depth in visited.items():
        if eid == element_id:
            continue
        elem = element_index.get(eid)
        if elem is None:
            continue
        affected.append(
            {
                "element": _element_summary(elem),
                "distance": depth,
                "path": parent_path.get(eid, []),
                "impact_strength": best_weight.get(eid, 0.0),
            }
        )

    affected.sort(key=lambda x: (-x["impact_strength"], x["distance"]))

    return {
        "source": _element_summary(source),
        "affected": affected,
    }
