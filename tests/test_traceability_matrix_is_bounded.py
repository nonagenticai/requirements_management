"""The traceability matrix must be bounded — it OOM-killed production.

⛔ Measured 2026-09-04 against the live tenant `0195f48b-…-0001` (2,633
architecture elements / 11,486 relationships): a single
``POST /architecture/tools/traceability-matrix`` with ``root_layer="application"``
drove the pod past its **512Mi** limit and the kubelet killed it —
``exitCode: 137, reason: OOMKilled``, twice. ``replicas: 1``, so one authenticated
request was a full outage of the service.

Reproduced offline against ``origin/main``'s implementation on a graph of that
shape: **peak 1,325.8 MB, 17.08s, 6,861,598 traced entries**. The fixed version
on the identical graph: **2.0 MB, 0.02s, 6,350 entries**.

Cause: ``build_traceability_matrix`` ran a full BFS *per root element* over an
undirected graph, so every root in a component rebuilt that component's element
set from scratch — O(roots x (V+E)) time, O(roots x V) memory — and then
materialised every one of those sets into the response.

Every test here names the mutation that must kill it. A test with no killing
mutation is not evidence.
"""

from __future__ import annotations

import random
import tracemalloc

import pytest

from tools.architecture.traceability import (
    DEFAULT_MAX_ROWS,
    DEFAULT_MAX_TRACED_PER_LAYER,
    _build_adjacency,
    build_traceability_matrix,
)

LAYERS = ["strategy", "business", "application", "technology"]


def _graph(n_elements: int, n_relationships: int, *, seed: int = 11, ghosts: int = 0):
    """A connected-ish graph shaped like the production tenant."""
    rng = random.Random(seed)
    elements = [
        {
            "id": f"e{i}",
            "title": f"element-{i}",
            "layer": LAYERS[i % len(LAYERS)],
            "element_type": "application_component",
        }
        for i in range(n_elements)
    ]
    ids = [e["id"] for e in elements] + [f"ghost{i}" for i in range(ghosts)]
    relationships = []
    for _ in range(n_relationships):
        src, tgt = rng.sample(ids, 2)
        relationships.append(
            {
                "source_entity_type": "architecture_elements",
                "target_entity_type": "architecture_elements",
                "source_id": src,
                "target_id": tgt,
            }
        )
    return elements, relationships


def _traced_entries(result: dict) -> int:
    return sum(len(v) for row in result["rows"] for v in row["traced_elements"].values())


def _reference_per_root_bfs(elements, relationships, root_layer):
    """The ORIGINAL per-root BFS, kept as an independent oracle.

    The fix must not change *what* is traced, only how much work it costs and
    how much of it is emitted. This is deliberately a separate, naive
    implementation rather than a call back into the module under test.
    """
    index = {str(e["id"]): e for e in elements}
    adj = _build_adjacency(relationships)
    out = {}
    for root in (e for e in elements if e.get("layer") == root_layer):
        start = str(root["id"])
        visited = {start}
        queue = [start]
        by_layer: dict[str, set[str]] = {}
        if start in index:
            by_layer.setdefault(index[start].get("layer", "unknown"), set()).add(start)
        while queue:
            current = queue.pop(0)
            for neighbour in adj.get(current, []):
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                queue.append(neighbour)
                elem = index.get(neighbour)
                if elem is not None:
                    by_layer.setdefault(elem.get("layer", "unknown"), set()).add(neighbour)
        out[start] = {k: sorted(v) for k, v in by_layer.items()}
    return out


class TestBounded:
    def test_output_is_bounded_on_a_production_scale_graph(self) -> None:
        """KILLING MUTATION: drop the ``[:max_rows]`` slice on root_elements.

        Without the slice this emits one row per root (2,606 on the real
        tenant) and the entry count explodes past a million.
        """
        elements, relationships = _graph(2633, 11486)
        result = build_traceability_matrix(elements, relationships, "application")

        assert len(result["rows"]) <= DEFAULT_MAX_ROWS
        ceiling = DEFAULT_MAX_ROWS * len(LAYERS) * DEFAULT_MAX_TRACED_PER_LAYER
        entries = _traced_entries(result)
        assert entries <= ceiling, f"emitted {entries} traced entries, ceiling {ceiling}"

    def test_peak_memory_stays_far_below_the_container_limit(self) -> None:
        """KILLING MUTATION: same slice removal, or raising the defaults.

        The pod limit is 512Mi. origin/main peaked at 1,325.8 MB on this shape.
        50 MB leaves three orders of magnitude of headroom and still fails
        loudly if the per-root materialisation ever comes back.
        """
        elements, relationships = _graph(2633, 11486)
        tracemalloc.start()
        try:
            build_traceability_matrix(elements, relationships, "application")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < 50 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB"

    def test_truncation_is_reported_never_silent(self) -> None:
        """KILLING MUTATION: hardcode ``"truncated": False`` in the return."""
        elements, relationships = _graph(2633, 11486)
        result = build_traceability_matrix(elements, relationships, "application")

        assert result["truncated"] is True
        assert result["roots_total"] > result["roots_returned"]
        assert result["roots_returned"] == len(result["rows"])

    def test_small_graph_is_not_truncated(self) -> None:
        """Vacuity floor: the flag must be able to read False.

        KILLING MUTATION: hardcode ``"truncated": True``.
        """
        elements, relationships = _graph(12, 8)
        result = build_traceability_matrix(elements, relationships, "application")

        assert result["truncated"] is False
        assert result["roots_total"] == result["roots_returned"]


class TestUnchangedBehaviour:
    @pytest.mark.parametrize("root_layer", LAYERS)
    def test_traced_sets_match_the_naive_per_root_bfs(self, root_layer: str) -> None:
        """The refactor must not change WHAT is traced.

        KILLING MUTATION: in ``_connected_components``, add
        ``and neighbor in element_index`` to the neighbour test — paths that run
        through an element the query did not load are then severed. Measured:
        4 of 4 parametrised cases fail.

        ⚠️ An earlier draft of this docstring named a different mutation
        (seeding the node list from the adjacency endpoints as well). The
        battery proved that mutation is a **no-op** — the walk reaches those
        nodes regardless — so the claim was false and the redundant seeding has
        been removed from the implementation rather than left as decoration.
        """
        elements, relationships = _graph(60, 90, ghosts=6)
        expected = _reference_per_root_bfs(elements, relationships, root_layer)
        result = build_traceability_matrix(
            elements, relationships, root_layer, max_rows=10**6, max_traced_per_layer=10**6
        )

        actual = {
            row["root_element"]["id"]: {
                layer: sorted(e["id"] for e in elems)
                for layer, elems in row["traced_elements"].items()
            }
            for row in result["rows"]
        }
        assert actual == expected

    def test_paths_run_through_unloaded_elements(self) -> None:
        """A relationship may name an element the query did not load; the walk
        must pass THROUGH it, as the per-root BFS did.

        KILLING MUTATION: skip neighbours missing from ``element_index``.
        """
        elements = [
            {"id": "a", "title": "A", "layer": "application", "element_type": "c"},
            {"id": "b", "title": "B", "layer": "technology", "element_type": "c"},
        ]
        relationships = [
            {
                "source_entity_type": "architecture_elements",
                "target_entity_type": "architecture_elements",
                "source_id": "a",
                "target_id": "ghost",
            },
            {
                "source_entity_type": "architecture_elements",
                "target_entity_type": "architecture_elements",
                "source_id": "ghost",
                "target_id": "b",
            },
        ]
        result = build_traceability_matrix(elements, relationships, "application")

        traced = result["rows"][0]["traced_elements"]
        assert [e["id"] for e in traced.get("technology", [])] == ["b"]

    def test_coverage_counts_elements_reachable_only_through_an_unloaded_node(
        self,
    ) -> None:
        """``_connected_components`` feeds COVERAGE only, so its traversal needs
        its own pin.

        ⛔ The mutation that severs traverse-through
        (``and neighbor in element_index``) originally killed the row-level
        equivalence tests. Once the emitted rows moved to
        ``_bfs_layers_bounded``, that mutation SURVIVED — nothing exercised the
        component walk any more. This is that missing pin.

        KILLING MUTATION: ``and neighbor in element_index`` in
        ``_connected_components`` — the technology element becomes unreachable,
        so application->technology coverage falls from 1.0 to 0.0.
        """
        elements = [
            {"id": "a", "title": "A", "layer": "application", "element_type": "c"},
            {"id": "b", "title": "B", "layer": "technology", "element_type": "c"},
        ]
        relationships = [
            {
                "source_entity_type": "architecture_elements",
                "target_entity_type": "architecture_elements",
                "source_id": "a",
                "target_id": "ghost",
            },
            {
                "source_entity_type": "architecture_elements",
                "target_entity_type": "architecture_elements",
                "source_id": "ghost",
                "target_id": "b",
            },
        ]
        result = build_traceability_matrix(elements, relationships, "application")

        assert result["coverage"]["application_to_technology"] == 1.0

    def test_coverage_is_computed_before_truncation(self) -> None:
        """KILLING MUTATION: derive ``traced_per_layer`` from the emitted rows.

        Coverage must describe the whole graph. Deriving it from truncated rows
        under-reports precisely in the large-graph case that matters, and the
        two calls below would then disagree.
        """
        elements, relationships = _graph(2633, 11486)
        truncated = build_traceability_matrix(
            elements, relationships, "application", max_rows=1, max_traced_per_layer=1
        )
        full = build_traceability_matrix(
            elements, relationships, "application", max_rows=10**6, max_traced_per_layer=10**6
        )
        assert truncated["coverage"] == full["coverage"]
        assert any(v is not None for v in full["coverage"].values())

    def test_truncated_rows_are_deterministic(self) -> None:
        """KILLING MUTATION: remove ``root_elements.sort(...)``.

        Without a stable order the 50 rows you get depend on row order from the
        database, so the same request returns different rows run to run.
        """
        elements, relationships = _graph(400, 900)
        shuffled = list(elements)
        random.Random(3).shuffle(shuffled)

        first = build_traceability_matrix(elements, relationships, "application")
        second = build_traceability_matrix(shuffled, relationships, "application")

        assert [r["root_element"]["id"] for r in first["rows"]] == [
            r["root_element"]["id"] for r in second["rows"]
        ]

    def test_the_whole_emitted_payload_is_deterministic(self) -> None:
        """Not just the root ids — the TRACED CONTENT too.

        ⛔ An adversarial audit found the first version of this fix stable only
        in ``root_element.id``. The traced lists were slices of a component list
        built in ``element_index`` order (i.e. database row order), so on the
        production shape only **53 of 400** emitted ids agreed between two runs
        with the input shuffled. The earlier test asserted exactly the
        sub-property that happened to hold.

        KILLING MUTATION: in ``_build_adjacency``, return the raw ``adj``
        instead of ``{node: sorted(set(neighbours))}``.
        """
        elements, relationships = _graph(2633, 11486)
        shuffled_elements = list(elements)
        random.Random(5).shuffle(shuffled_elements)
        shuffled_rels = list(relationships)
        random.Random(6).shuffle(shuffled_rels)

        first = build_traceability_matrix(elements, relationships, "application")
        second = build_traceability_matrix(shuffled_elements, shuffled_rels, "application")

        def payload(result):
            return [
                (
                    row["root_element"]["id"],
                    {k: [e["id"] for e in v] for k, v in row["traced_elements"].items()},
                )
                for row in result["rows"]
            ]

        assert payload(first) == payload(second)

    def test_a_truncated_row_keeps_the_nearest_elements_and_its_own_root(self) -> None:
        """Truncation must keep what is CLOSEST, not an arbitrary slice.

        ⛔ Same audit: on a chain, the first version emitted the 100 elements
        FURTHEST from the root and omitted the root itself — the pre-fix code
        emitted the root first, nearest-first. A bounded BFS restores that.

        KILLING MUTATION: emit ``comps[comp_of[root]]`` sliced to
        ``max_traced_per_layer`` instead of calling ``_bfs_layers_bounded``.
        """
        # A chain e000 - e001 - ... - e199, alternating layers so each layer
        # fills long before the chain ends.
        # ⚠️ REVERSED on purpose. Listed in natural order, the component's own
        # element order coincides with BFS-from-e000 order, and slicing the
        # component list gives the right answer by accident — a mutation that
        # replaced the BFS with a slice SURVIVED this test until the input was
        # reversed. Now component order and BFS order disagree.
        elements = [
            {
                "id": f"e{i:03d}",
                "title": f"n{i}",
                "layer": "application" if i % 2 == 0 else "technology",
                "element_type": "c",
            }
            for i in reversed(range(200))
        ]
        relationships = [
            {
                "source_entity_type": "architecture_elements",
                "target_entity_type": "architecture_elements",
                "source_id": f"e{i:03d}",
                "target_id": f"e{i + 1:03d}",
            }
            for i in range(199)
        ]
        result = build_traceability_matrix(
            elements, relationships, "application", max_rows=1, max_traced_per_layer=10
        )

        row = result["rows"][0]
        assert row["root_element"]["id"] == "e000"
        application = [e["id"] for e in row["traced_elements"]["application"]]
        # Its own root first, then the nearest same-layer nodes in order.
        assert application == [f"e{2 * i:03d}" for i in range(10)]
        technology = [e["id"] for e in row["traced_elements"]["technology"]]
        assert technology == [f"e{2 * i + 1:03d}" for i in range(10)]
