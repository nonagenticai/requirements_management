"""Cross-layer traceability matrix builder."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

LAYER_ORDER: list[str] = [
    "strategy",
    "motivation",
    "business",
    "application",
    "technology",
    "physical",
    "implementation_migration",
    "other",
]

LAYER_PAIRS: list[tuple[str, str]] = [
    ("strategy", "business"),
    ("strategy", "application"),
    ("strategy", "technology"),
    ("business", "application"),
    ("business", "technology"),
    ("application", "technology"),
    ("technology", "physical"),
    ("motivation", "strategy"),
    ("motivation", "business"),
]


def _build_adjacency(
    relationships: list[dict],
) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = defaultdict(list)
    for rel in relationships:
        if (
            rel.get("source_entity_type") == "architecture_elements"
            and rel.get("target_entity_type") == "architecture_elements"
        ):
            src = str(rel["source_id"])
            tgt = str(rel["target_id"])
            adj[src].append(tgt)
            adj[tgt].append(src)
    # Sorted so a BFS from a given root visits neighbours in a fixed order.
    # Without this the emitted (truncated) rows follow relationship row order
    # from the database and change run to run.
    return {node: sorted(set(neighbours)) for node, neighbours in adj.items()}


def _connected_components(
    adj: dict[str, list[str]],
    element_index: dict[str, dict],
) -> tuple[dict[str, int], list[dict[str, list[dict]]]]:
    """Partition the undirected graph into connected components, ONCE.

    The adjacency built by :func:`_build_adjacency` is undirected, so every
    root in a given component reaches exactly the same set of elements. The
    previous implementation ran a full BFS *per root element* and rebuilt that
    identical set every time: O(roots x (V+E)) time and O(roots x V) memory.

    ⛔ That is not a theoretical concern. Measured 2026-09-04 against the live
    production tenant (2,633 elements / 11,486 relationships), a single
    ``POST /architecture/tools/traceability-matrix`` with
    ``root_layer="application"`` drove the pod past its 512Mi limit and the
    kubelet OOM-killed it (``exitCode: 137``, ``reason: OOMKilled``) — twice.
    ``replicas: 1``, so one request was a full outage of the service.

    Nodes named only by a relationship (an endpoint whose element row was not
    loaded) are traversed but never collected, exactly as the per-root BFS did:
    a path may run *through* an unloaded element to reach a loaded one. That is
    what the ``neighbor not in comp_of`` test preserves — adding an
    ``and neighbor in element_index`` guard there severs those paths and is the
    mutation that kills the equivalence tests.
    """
    comp_of: dict[str, int] = {}
    comps: list[dict[str, list[dict]]] = []

    # Seeded from the loaded elements only. Seeding from the adjacency
    # endpoints as well was measured to be a no-op: an unloaded node is still
    # reached (and assigned a component) by the walk below, and a component
    # holding no loaded element can never contain a root, so it is never
    # emitted. Removed rather than kept as decoration — a mutation proved it
    # changed nothing.
    for node in list(element_index):
        if node in comp_of:
            continue
        idx = len(comps)
        by_layer: dict[str, list[dict]] = defaultdict(list)
        comp_of[node] = idx
        queue: deque[str] = deque([node])
        while queue:
            current = queue.popleft()
            elem = element_index.get(current)
            if elem is not None:
                by_layer[elem.get("layer", "unknown")].append(elem)
            for neighbor in adj.get(current, []):
                if neighbor not in comp_of:
                    comp_of[neighbor] = idx
                    queue.append(neighbor)
        comps.append(dict(by_layer))

    return comp_of, comps


def _coverage_ratios(
    traced_per_layer: dict[str, set[str]],
    elements: list[dict],
) -> dict[str, float | None]:
    """Coverage over the FULL traced set.

    ⚠️ Deliberately NOT derived from the emitted ``rows``: those are truncated
    for memory safety, and deriving coverage from them would silently
    under-report it in exactly the large-graph case that matters.
    """
    layer_totals: dict[str, int] = defaultdict(int)
    for elem in elements:
        layer_totals[elem.get("layer", "unknown")] += 1

    coverage: dict[str, float | None] = {}
    for src_layer, tgt_layer in LAYER_PAIRS:
        key = f"{src_layer}_to_{tgt_layer}"
        src_total = layer_totals.get(src_layer, 0)
        total = layer_totals.get(tgt_layer, 0)
        # A src->tgt coverage pair is only meaningful when BOTH layers have
        # elements. An empty source layer (no such elements exist) or empty
        # target layer (no denominator) => None (honest "not applicable"), NOT a
        # fabricated 1.0 (perfect) or 0.0 (failure) — the same 0/0 class the
        # sibling coverage tools fixed.
        if src_total == 0 or total == 0:
            coverage[key] = None
        else:
            traced = len(traced_per_layer.get(tgt_layer, set()))
            coverage[key] = round(min(1.0, traced / total), 4)

    return coverage


def _bfs_layers_bounded(
    start_id: str,
    adj: dict[str, list[str]],
    element_index: dict[str, dict],
    max_per_layer: int,
) -> tuple[dict[str, list[dict]], bool]:
    """Breadth-first from one root, capped per layer — nearest elements first.

    ⚠️ Used ONLY for the rows actually emitted (at most ``max_rows`` of them),
    never for coverage. Coverage comes from :func:`_connected_components`, which
    runs once for the whole graph.

    Why a BFS at all, when components already give the reachable set: an
    adversarial audit showed that slicing a component's element list is
    *semantically wrong as well as unstable*. On a 200-node chain the row for
    the first root emitted the 100 elements FURTHEST from it and omitted the
    root itself, and across two runs with the input rows shuffled only 53 of
    400 emitted ids agreed. Truncating a BFS keeps the original
    nearest-first-including-the-root ordering, and ``_build_adjacency`` now
    sorts neighbours so the walk is reproducible.
    """
    visited: set[str] = {start_id}
    queue: deque[str] = deque([start_id])
    by_layer: dict[str, list[dict]] = defaultdict(list)
    truncated = False

    start_elem = element_index.get(start_id)
    if start_elem is not None:
        by_layer[start_elem.get("layer", "unknown")].append(start_elem)

    while queue:
        current = queue.popleft()
        for neighbor in adj.get(current, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append(neighbor)
            elem = element_index.get(neighbor)
            if elem is None:
                continue
            layer = elem.get("layer", "unknown")
            if len(by_layer[layer]) >= max_per_layer:
                truncated = True
                continue
            by_layer[layer].append(elem)

    return dict(by_layer), truncated


DEFAULT_MAX_ROWS = 50
DEFAULT_MAX_TRACED_PER_LAYER = 100


def build_traceability_matrix(
    elements: list[dict],
    relationships: list[dict],
    root_layer: str = "strategy",
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_traced_per_layer: int = DEFAULT_MAX_TRACED_PER_LAYER,
) -> dict[str, Any]:
    """Build a cross-layer traceability matrix.

    Bounded by construction. The emitted payload can never exceed
    ``max_rows x layers x max_traced_per_layer`` traced entries, and the
    component walk runs once for the whole graph rather than once per root.
    See :func:`_connected_components` for the OOM this replaced.

    Truncation is REPORTED, never silent: ``roots_total`` vs ``roots_returned``
    and a ``truncated`` flag, plus ``traced_truncated`` per row. Roots are
    sorted by id so a truncated result is reproducible rather than dependent on
    row order from the database.
    """
    element_index: dict[str, dict] = {}
    for elem in elements:
        element_index[str(elem["id"])] = elem

    adj = _build_adjacency(relationships)
    comp_of, comps = _connected_components(adj, element_index)

    root_elements = [elem for elem in elements if elem.get("layer") == root_layer]
    root_elements.sort(key=lambda e: str(e["id"]))
    roots_total = len(root_elements)

    # Coverage is computed over every component reachable from ANY root, before
    # truncation — each component is unioned once, not once per root in it.
    traced_per_layer: dict[str, set[str]] = defaultdict(set)
    seen_components: set[int] = set()
    for root in root_elements:
        comp_idx = comp_of.get(str(root["id"]))
        if comp_idx is None or comp_idx in seen_components:
            continue
        seen_components.add(comp_idx)
        for layer, elems in comps[comp_idx].items():
            for e in elems:
                traced_per_layer[layer].add(str(e["id"]))

    rows: list[dict] = []
    any_traced_truncated = False
    for root in root_elements[:max_rows]:
        by_layer, row_truncated = _bfs_layers_bounded(
            str(root["id"]), adj, element_index, max_traced_per_layer
        )
        traced_elements: dict[str, list[dict]] = {
            layer: [
                {
                    "id": str(e["id"]),
                    "title": e.get("title", ""),
                    "element_type": e.get("element_type", ""),
                }
                for e in elems
            ]
            for layer, elems in by_layer.items()
        }
        any_traced_truncated = any_traced_truncated or row_truncated
        rows.append(
            {
                "root_element": {
                    "id": str(root["id"]),
                    "title": root.get("title", ""),
                    "layer": root.get("layer", ""),
                    "element_type": root.get("element_type", ""),
                },
                "traced_elements": traced_elements,
                "traced_truncated": row_truncated,
            }
        )

    coverage = _coverage_ratios(traced_per_layer, elements)

    return {
        "rows": rows,
        "coverage": coverage,
        "roots_total": roots_total,
        "roots_returned": len(rows),
        "truncated": roots_total > len(rows) or any_traced_truncated,
    }
