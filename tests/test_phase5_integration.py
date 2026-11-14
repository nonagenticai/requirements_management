"""Tests for Phase 5: Integration tools."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# 1. Pure integration tool tests (no DB, no mocking needed)
# ---------------------------------------------------------------------------


class TestGapToWorkpackages:
    """Tests for gap_to_wp.generate_workpackage_proposals()."""

    def test_empty_inputs(self) -> None:
        from tools.integration.gap_to_wp import generate_workpackage_proposals

        result = generate_workpackage_proposals([], [])
        assert result["proposals"] == []
        assert result["unresolved_count"] == 0
        assert result["resolved_count"] == 0

    def test_no_gaps(self) -> None:
        """All elements are baseline — no target means no gaps."""
        from tools.integration.gap_to_wp import generate_workpackage_proposals

        elements = [
            {
                "id": "aaa",
                "slug": "cap-a",
                "title": "Capability A",
                "description": "desc",
                "layer": "strategy",
                "element_type": "capability",
                "properties": {},
                "status": "baseline",
                "architecture_state": "baseline",
                "parent_id": None,
            }
        ]
        result = generate_workpackage_proposals(elements, [])
        assert result["proposals"] == []

    def test_new_target_generates_proposal(self) -> None:
        """A target element with no baseline match and no WP link = proposal."""
        from tools.integration.gap_to_wp import generate_workpackage_proposals

        elements = [
            {
                "id": "t1",
                "slug": "new-cap",
                "title": "New Capability",
                "description": "A new capability",
                "layer": "strategy",
                "element_type": "capability",
                "properties": {},
                "status": "target",
                "architecture_state": "target",
                "parent_id": None,
            },
        ]
        result = generate_workpackage_proposals(elements, [])
        assert result["unresolved_count"] == 1
        assert len(result["proposals"]) == 1
        proposal = result["proposals"][0]
        assert "New Capability" in proposal["suggested_title"]
        assert proposal["element_type"] == "work_package"
        assert proposal["reason"] == "new_in_target"

    def test_resolved_gap_no_proposal(self) -> None:
        """A target element linked to a work_package via relationship = resolved."""
        from tools.integration.gap_to_wp import generate_workpackage_proposals

        elements = [
            {
                "id": "t1",
                "slug": "new-cap",
                "title": "New Capability",
                "description": "desc",
                "layer": "strategy",
                "element_type": "capability",
                "properties": {},
                "status": "target",
                "architecture_state": "target",
                "parent_id": None,
            },
            {
                "id": "wp1",
                "slug": "wp-cap",
                "title": "WP Implement Cap",
                "description": "work package",
                "layer": "implementation_migration",
                "element_type": "work_package",
                "properties": {},
                "status": "draft",
                "architecture_state": "target",
                "parent_id": None,
            },
        ]
        relationships = [
            {
                "id": "r1",
                "source_entity_type": "architecture_elements",
                "source_id": "wp1",
                "target_entity_type": "architecture_elements",
                "target_id": "t1",
                "relationship_type": "realization",
                "properties": {},
            }
        ]
        result = generate_workpackage_proposals(elements, relationships)
        # t1 is resolved (linked to wp1), but wp1 itself is a new target
        # without its own work_package link — that's expected behavior
        resolved_proposals = [p for p in result["proposals"] if p["linked_gap_element_id"] == "t1"]
        assert len(resolved_proposals) == 0  # t1 should NOT appear as a proposal


class TestRequirementArchTrace:
    """Tests for req_arch_trace.trace_requirements()."""

    def test_empty_inputs(self) -> None:
        from tools.integration.req_arch_trace import trace_requirements

        result = trace_requirements([], [], [])
        # 0/0 is undefined — an empty project has no coverage denominator, so the
        # honest answer is None, not a fabricated 1.0.
        assert result["requirement_coverage"] is None
        assert result["traced"] == []
        assert result["untraced_requirements"] == []
        assert result["total_requirements"] == 0
        assert result["dangling_endpoints"] == []

    def test_untraced_requirement(self) -> None:
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [{"id": "r1", "title": "Req 1", "slug": "req-1"}]
        elems = [
            {
                "id": "e1",
                "title": "Element 1",
                "layer": "application",
                "element_type": "application_component",
            }
        ]
        result = trace_requirements(reqs, elems, [])
        assert result["requirement_coverage"] == 0.0
        assert len(result["untraced_requirements"]) == 1
        assert len(result["untraced_elements"]) == 1

    def test_traced_requirement(self) -> None:
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [{"id": "r1", "title": "Req 1", "slug": "req-1"}]
        elems = [
            {
                "id": "e1",
                "title": "Element 1",
                "layer": "application",
                "element_type": "application_component",
            }
        ]
        rels = [
            {
                "source_entity_type": "requirements",
                "source_id": "r1",
                "target_entity_type": "architecture_elements",
                "target_id": "e1",
                "relationship_type": "traceability",
            }
        ]
        result = trace_requirements(reqs, elems, rels)
        assert result["requirement_coverage"] == 1.0
        assert len(result["traced"]) == 1
        assert len(result["untraced_requirements"]) == 0
        assert len(result["untraced_elements"]) == 0

    def test_reverse_direction_trace(self) -> None:
        """Traceability from element → requirement should also count."""
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [{"id": "r1", "title": "Req 1", "slug": "req-1"}]
        elems = [
            {
                "id": "e1",
                "title": "Element 1",
                "layer": "application",
                "element_type": "application_component",
            }
        ]
        rels = [
            {
                "source_entity_type": "architecture_elements",
                "source_id": "e1",
                "target_entity_type": "requirements",
                "target_id": "r1",
                "relationship_type": "realization",
            }
        ]
        result = trace_requirements(reqs, elems, rels)
        assert result["requirement_coverage"] == 1.0

    def test_partial_coverage(self) -> None:
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [
            {"id": "r1", "title": "Req 1", "slug": "req-1"},
            {"id": "r2", "title": "Req 2", "slug": "req-2"},
        ]
        elems = [
            {
                "id": "e1",
                "title": "Element 1",
                "layer": "application",
                "element_type": "application_component",
            }
        ]
        rels = [
            {
                "source_entity_type": "requirements",
                "source_id": "r1",
                "target_entity_type": "architecture_elements",
                "target_id": "e1",
                "relationship_type": "traceability",
            }
        ]
        result = trace_requirements(reqs, elems, rels)
        assert result["requirement_coverage"] == 0.5
        assert len(result["traced"]) == 1
        assert len(result["untraced_requirements"]) == 1

    def test_dangling_requirement_endpoint(self) -> None:
        """An edge to a requirement id outside the roster is reported, not swallowed."""
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [{"id": "r1", "title": "Req 1", "slug": "req-1"}]
        elems = [
            {
                "id": "e1",
                "title": "Element 1",
                "layer": "application",
                "element_type": "application_component",
            }
        ]
        rels = [
            {
                "id": "rel-real",
                "source_entity_type": "requirements",
                "source_id": "r1",
                "target_entity_type": "architecture_elements",
                "target_id": "e1",
                "relationship_type": "traceability",
            },
            {
                "id": "rel-ghost",
                "source_entity_type": "requirements",
                "source_id": "ghost",  # not in the roster
                "target_entity_type": "architecture_elements",
                "target_id": "e1",
                "relationship_type": "traceability",
            },
        ]
        result = trace_requirements(reqs, elems, rels)
        assert result["traced_count"] == 1
        assert result["requirement_coverage"] == 1.0
        dangling = result["dangling_endpoints"]
        assert len(dangling) == 1
        assert dangling[0]["endpoint"] == "requirement"
        assert dangling[0]["missing_id"] == "ghost"
        assert dangling[0]["relationship_id"] == "rel-ghost"

    def test_out_of_scope_element_endpoint_not_fabricated_trace(self) -> None:
        """Regression: a requirement whose only edge points at an unknown element
        must be UNTRACED (previously counted as traced with empty linked_elements → a
        fabricated coverage of 1.0 on a non-empty project)."""
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [{"id": "r1", "title": "Req 1", "slug": "req-1"}]
        elems: list[dict] = []  # e_ghost is not loaded
        rels = [
            {
                "id": "rel-ghost-elem",
                "source_entity_type": "requirements",
                "source_id": "r1",
                "target_entity_type": "architecture_elements",
                "target_id": "e_ghost",
                "relationship_type": "traceability",
            }
        ]
        result = trace_requirements(reqs, elems, rels)
        assert result["traced"] == []
        assert result["traced_count"] == 0
        assert result["requirement_coverage"] == 0.0  # was a fabricated 1.0
        assert len(result["untraced_requirements"]) == 1
        dangling = result["dangling_endpoints"]
        assert len(dangling) == 1
        assert dangling[0]["endpoint"] == "element"
        assert dangling[0]["missing_id"] == "e_ghost"

    def test_duplicate_and_reverse_edges_deduped(self) -> None:
        """The same pair authored twice, and once in each direction, links once."""
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [{"id": "r1", "title": "Req 1", "slug": "req-1"}]
        elems = [
            {
                "id": "e1",
                "title": "Element 1",
                "layer": "application",
                "element_type": "application_component",
            }
        ]
        rels = [
            {
                "source_entity_type": "requirements",
                "source_id": "r1",
                "target_entity_type": "architecture_elements",
                "target_id": "e1",
                "relationship_type": "traceability",
            },
            {  # exact duplicate
                "source_entity_type": "requirements",
                "source_id": "r1",
                "target_entity_type": "architecture_elements",
                "target_id": "e1",
                "relationship_type": "traceability",
            },
            {  # reverse direction, same pair
                "source_entity_type": "architecture_elements",
                "source_id": "e1",
                "target_entity_type": "requirements",
                "target_id": "r1",
                "relationship_type": "realization",
            },
        ]
        result = trace_requirements(reqs, elems, rels)
        assert result["requirement_coverage"] == 1.0
        assert len(result["traced"]) == 1
        assert len(result["traced"][0]["linked_elements"]) == 1
        assert result["untraced_elements"] == []

    def test_malformed_edge_does_not_crash(self) -> None:
        """A trace edge missing its endpoint ids is skipped, not a 500."""
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [{"id": "r1", "title": "Req 1", "slug": "req-1"}]
        rels = [
            {
                "source_entity_type": "requirements",
                "target_entity_type": "architecture_elements",
                "relationship_type": "traceability",
                # no source_id / target_id
            }
        ]
        result = trace_requirements(reqs, [], rels)
        assert result["requirement_coverage"] == 0.0
        assert result["traced_count"] == 0
        assert result["dangling_endpoints"] == []

    def test_falsy_integer_endpoint_id_not_swallowed(self) -> None:
        """A valid in-roster endpoint whose id is the integer 0 must trace, not be
        dropped as malformed by an `or ""` coercion."""
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [{"id": "r1", "title": "Req 1", "slug": "req-1"}]
        elems = [{"id": 0, "title": "Element Zero", "layer": "application", "element_type": "node"}]
        rels = [
            {
                "source_entity_type": "requirements",
                "source_id": "r1",
                "target_entity_type": "architecture_elements",
                "target_id": 0,  # integer 0 — falsy but valid
                "relationship_type": "traceability",
            }
        ]
        result = trace_requirements(reqs, elems, rels)
        assert result["requirement_coverage"] == 1.0
        assert result["traced_count"] == 1
        assert result["dangling_endpoints"] == []

    def test_duplicate_roster_id_counts_reconcile(self) -> None:
        """total_requirements must equal traced + untraced even with duplicate ids."""
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [
            {"id": "r1", "title": "Req 1", "slug": "req-1"},
            {"id": "r1", "title": "Req 1 dup", "slug": "req-1"},
        ]
        result = trace_requirements(reqs, [], [])
        assert result["total_requirements"] == len(result["traced"]) + len(
            result["untraced_requirements"]
        )
        assert result["requirement_coverage"] == 0.0

    def test_non_dict_relationship_skipped(self) -> None:
        """Junk (non-dict) entries in the relationship list are skipped, not a crash."""
        from tools.integration.req_arch_trace import trace_requirements

        reqs = [{"id": "r1", "title": "Req 1", "slug": "req-1"}]
        # Deliberately malformed input: non-dict junk where edges are expected.
        rels: list = ["not-a-dict", 123, None]
        result = trace_requirements(reqs, [], rels)
        assert result["traced_count"] == 0
        assert result["requirement_coverage"] == 0.0


# ---------------------------------------------------------------------------
# 4. Integration router endpoint tests
# ---------------------------------------------------------------------------


class TestIntegrationRouterEndpoints:
    """Test integration tool router endpoints against real DB."""

    @pytest.mark.asyncio
    async def test_gap_to_workpackages_empty_project(
        self, authenticated_client: AsyncClient
    ) -> None:
        resp = await authenticated_client.post(
            "/integration/tools/gap-to-workpackages",
            json={"project_id": "nonexistent-project"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["proposals"] == []
        assert data["unresolved_count"] == 0

    @pytest.mark.asyncio
    async def test_requirement_trace_empty_project(self, authenticated_client: AsyncClient) -> None:
        resp = await authenticated_client.post(
            "/integration/tools/requirement-trace",
            json={"project_id": "nonexistent-project"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Empty project → coverage is undefined (JSON null), not a fabricated 1.0.
        assert data["requirement_coverage"] is None
        assert data["traced"] == []
