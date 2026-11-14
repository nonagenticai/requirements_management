"""Tests for architecture CRUD endpoints, metamodel validator, and architecture tools.

Covers:
- Architecture Element CRUD (create, list, get, update, delete, tenant isolation)
- Architecture Relationship CRUD (create, invalid metamodel, list, graph, delete)
- Architecture View CRUD (create, list, get)
- Architecture Standard CRUD (create, list, get)
- Metamodel validator pure function tests
- Traceability matrix builder
- Gap analysis
- Impact analysis
- Cascade delete (element deletion removes related relationships)
"""

import uuid

from httpx import AsyncClient

from tests.tenants import T

# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

ELEMENT_CAPABILITY = {
    "title": "Order Management",
    "description": "Core order processing capability",
    "layer": "strategy",
    "element_type": "capability",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "test-project",
}

ELEMENT_BUSINESS_PROCESS = {
    "title": "Process Orders",
    "description": "Handles order lifecycle",
    "layer": "business",
    "element_type": "business_process",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "test-project",
}

ELEMENT_APP_COMPONENT = {
    "title": "Order Service",
    "description": "Microservice for orders",
    "layer": "application",
    "element_type": "application_component",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "test-project",
}

VIEW_PAYLOAD = {
    "title": "Application Cooperation View",
    "description": "Shows application components and their interactions",
    "viewpoint": "application_cooperation",
    "element_ids": [],
    "relationship_ids": [],
    "project_id": "test-project",
}

STANDARD_PAYLOAD = {
    "title": "REST API Standard",
    "description": "All APIs must follow REST conventions",
    "standard_type": "technology_standard",
    "status": "approved",
    "compliance_level": "mandatory",
    "project_id": "test-project",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_element(client: AsyncClient, payload: dict | None = None) -> dict:
    resp = await client.post("/architecture/elements", json=payload or ELEMENT_CAPABILITY)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_two_elements(client: AsyncClient) -> tuple[dict, dict]:
    e1 = await _create_element(client, ELEMENT_CAPABILITY)
    e2 = await _create_element(client, ELEMENT_BUSINESS_PROCESS)
    return e1, e2


async def _create_relationship(client: AsyncClient, source_id: str, target_id: str) -> dict:
    """Create a realization from business_process (source) → capability (target).

    realization flows lower→upper: business (level 2) → strategy (level 1).
    Callers create e1=capability, e2=business_process via _create_two_elements,
    then pass (source_id=e2.id, target_id=e1.id).
    """
    payload = {
        "source_entity_type": "architecture_elements",
        "source_id": source_id,
        "target_entity_type": "architecture_elements",
        "target_id": target_id,
        "relationship_type": "realization",
        "project_id": "test-project",
    }
    resp = await client.post("/architecture/relationships", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ===================================================================
# 1. Architecture Element CRUD
# ===================================================================


class TestArchitectureElementCRUD:
    async def test_create_element(self, authenticated_client: AsyncClient):
        data = await _create_element(authenticated_client)
        assert data["title"] == "Order Management"
        assert "id" in data
        assert "slug" in data
        assert data["layer"] == "strategy"
        assert data["element_type"] == "capability"
        assert data["tenant_id"] == T("user-123")

    async def test_list_elements_with_pagination(self, authenticated_client: AsyncClient):
        await _create_element(authenticated_client, ELEMENT_CAPABILITY)
        await _create_element(authenticated_client, ELEMENT_BUSINESS_PROCESS)

        resp = await authenticated_client.get("/architecture/elements")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2
        assert "X-Total-Count" in resp.headers
        assert int(resp.headers["X-Total-Count"]) >= 2

    async def test_get_element_by_id(self, authenticated_client: AsyncClient):
        created = await _create_element(authenticated_client)
        eid = created["id"]

        resp = await authenticated_client.get(f"/architecture/elements/{eid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == eid
        assert data["title"] == "Order Management"

    async def test_update_element(self, authenticated_client: AsyncClient):
        created = await _create_element(authenticated_client)
        eid = created["id"]

        update_payload = {
            **ELEMENT_CAPABILITY,
            "title": "Updated Order Management",
            "description": "Updated description",
        }
        resp = await authenticated_client.put(f"/architecture/elements/{eid}", json=update_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Updated Order Management"
        assert data["description"] == "Updated description"

    async def test_delete_element(self, authenticated_client: AsyncClient):
        created = await _create_element(authenticated_client)
        eid = created["id"]

        resp = await authenticated_client.delete(f"/architecture/elements/{eid}")
        assert resp.status_code == 200

        # Verify it is gone
        resp = await authenticated_client.get(f"/architecture/elements/{eid}")
        assert resp.status_code == 404

    async def test_tenant_isolation(self, test_client: AsyncClient):
        """user-123 cannot see elements created by user-789."""
        # Create as user-123
        test_client.headers = {"X-Tenant-Id": T("user-123")}
        e1 = await _create_element(test_client, ELEMENT_CAPABILITY)

        # Create as user-789
        test_client.headers = {"X-Tenant-Id": T("user-789")}
        await _create_element(test_client, ELEMENT_BUSINESS_PROCESS)

        # List as user-789 should not include user-123's element
        resp = await test_client.get("/architecture/elements")
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()]
        assert e1["id"] not in ids

        # Direct GET by user-789 on user-123's element should 403
        resp = await test_client.get(f"/architecture/elements/{e1['id']}")
        assert resp.status_code == 404

    async def test_list_elements_filter_by_layer(self, authenticated_client: AsyncClient):
        await _create_element(authenticated_client, ELEMENT_CAPABILITY)
        await _create_element(authenticated_client, ELEMENT_BUSINESS_PROCESS)

        resp = await authenticated_client.get("/architecture/elements?layer=strategy")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["layer"] == "strategy" for item in items)

    async def test_get_nonexistent_element(self, authenticated_client: AsyncClient):
        fake_id = str(uuid.uuid4())
        resp = await authenticated_client.get(f"/architecture/elements/{fake_id}")
        assert resp.status_code == 404


# ===================================================================
# 2. Architecture Relationship CRUD
# ===================================================================


class TestArchitectureRelationshipCRUD:
    async def test_create_valid_relationship(self, authenticated_client: AsyncClient):
        """realization from application_component (level 3) to business_process (level 2) is valid."""
        e1 = await _create_element(authenticated_client, ELEMENT_APP_COMPONENT)
        e2 = await _create_element(authenticated_client, ELEMENT_BUSINESS_PROCESS)

        # realization goes from lower abstraction (higher level) to higher abstraction (lower level)
        # application (3) → business (2) is valid
        rel = await _create_relationship(authenticated_client, e1["id"], e2["id"])
        assert "id" in rel
        assert rel["relationship_type"] == "realization"
        assert rel["source_id"] == e1["id"]
        assert rel["target_id"] == e2["id"]

    async def test_create_invalid_relationship_returns_422(self, authenticated_client: AsyncClient):
        """composition across different type categories should fail metamodel validation."""
        e1 = await _create_element(authenticated_client, ELEMENT_APP_COMPONENT)
        e2 = await _create_element(authenticated_client, ELEMENT_BUSINESS_PROCESS)

        payload = {
            "source_entity_type": "architecture_elements",
            "source_id": e1["id"],
            "target_entity_type": "architecture_elements",
            "target_id": e2["id"],
            "relationship_type": "composition",
            "project_id": "test-project",
        }
        resp = await authenticated_client.post("/architecture/relationships", json=payload)
        assert resp.status_code == 422

    async def test_list_relationships(self, authenticated_client: AsyncClient):
        e1, e2 = await _create_two_elements(authenticated_client)
        await _create_relationship(authenticated_client, e2["id"], e1["id"])

        resp = await authenticated_client.get("/architecture/relationships")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 1
        assert "X-Total-Count" in resp.headers

    async def test_list_relationships_filter_by_type(self, authenticated_client: AsyncClient):
        e1, e2 = await _create_two_elements(authenticated_client)
        await _create_relationship(authenticated_client, e2["id"], e1["id"])

        resp = await authenticated_client.get(
            "/architecture/relationships?relationship_type=realization"
        )
        assert resp.status_code == 200
        items = resp.json()
        assert all(r["relationship_type"] == "realization" for r in items)

    async def test_get_entity_graph(self, authenticated_client: AsyncClient):
        e1, e2 = await _create_two_elements(authenticated_client)
        await _create_relationship(authenticated_client, e2["id"], e1["id"])

        resp = await authenticated_client.get(
            f"/architecture/relationships/graph/architecture_elements/{e1['id']}"
        )
        assert resp.status_code == 200
        rels = resp.json()
        assert len(rels) >= 1
        # The relationship should reference e1 as source or target
        rel_ids = set()
        for r in rels:
            rel_ids.add(r["source_id"])
            rel_ids.add(r["target_id"])
        assert e1["id"] in rel_ids

    async def test_delete_relationship(self, authenticated_client: AsyncClient):
        e1, e2 = await _create_two_elements(authenticated_client)
        rel = await _create_relationship(authenticated_client, e2["id"], e1["id"])
        rid = rel["id"]

        resp = await authenticated_client.delete(f"/architecture/relationships/{rid}")
        assert resp.status_code == 200

        # Verify gone
        resp = await authenticated_client.get(f"/architecture/relationships/{rid}")
        assert resp.status_code == 404


# ===================================================================
# 3. Architecture View CRUD
# ===================================================================


class TestArchitectureViewCRUD:
    async def test_create_view(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post("/architecture/views", json=VIEW_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Application Cooperation View"
        assert data["viewpoint"] == "application_cooperation"
        assert "id" in data
        assert "slug" in data

    async def test_list_views(self, authenticated_client: AsyncClient):
        await authenticated_client.post("/architecture/views", json=VIEW_PAYLOAD)
        second_view = {
            **VIEW_PAYLOAD,
            "title": "Technology Usage View",
            "viewpoint": "technology_usage",
        }
        await authenticated_client.post("/architecture/views", json=second_view)

        resp = await authenticated_client.get("/architecture/views")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2

    async def test_get_view_by_id(self, authenticated_client: AsyncClient):
        create_resp = await authenticated_client.post("/architecture/views", json=VIEW_PAYLOAD)
        vid = create_resp.json()["id"]

        resp = await authenticated_client.get(f"/architecture/views/{vid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == vid


# ===================================================================
# 4. Architecture Standard CRUD
# ===================================================================


class TestArchitectureStandardCRUD:
    async def test_create_standard(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post("/architecture/standards", json=STANDARD_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "REST API Standard"
        assert data["standard_type"] == "technology_standard"
        assert data["status"] == "approved"
        assert data["compliance_level"] == "mandatory"
        assert "id" in data
        assert "slug" in data

    async def test_list_standards(self, authenticated_client: AsyncClient):
        await authenticated_client.post("/architecture/standards", json=STANDARD_PAYLOAD)
        second = {
            **STANDARD_PAYLOAD,
            "title": "Data Standard",
            "standard_type": "data_standard",
            "status": "trial",
        }
        await authenticated_client.post("/architecture/standards", json=second)

        resp = await authenticated_client.get("/architecture/standards")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2

    async def test_get_standard_by_id(self, authenticated_client: AsyncClient):
        create_resp = await authenticated_client.post(
            "/architecture/standards", json=STANDARD_PAYLOAD
        )
        sid = create_resp.json()["id"]

        resp = await authenticated_client.get(f"/architecture/standards/{sid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == sid


# ===================================================================
# 5. Metamodel Validator (pure function)
# ===================================================================


class TestMetamodelValidator:
    """Tests for tools.architecture.metamodel.validate_relationship."""

    def test_realization_app_to_business_valid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="application",
            source_element_type="application_component",
            target_layer="business",
            target_element_type="business_process",
            relationship_type="realization",
        )
        assert result.valid is True
        assert result.violations == []

    def test_composition_same_type_valid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="application",
            source_element_type="application_component",
            target_layer="application",
            target_element_type="application_component",
            relationship_type="composition",
        )
        assert result.valid is True

    def test_composition_same_category_same_layer_valid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="application",
            source_element_type="application_component",
            target_layer="application",
            target_element_type="application_interface",
            relationship_type="composition",
        )
        assert result.valid is True

    def test_association_always_valid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="strategy",
            source_element_type="capability",
            target_layer="technology",
            target_element_type="node",
            relationship_type="association",
        )
        assert result.valid is True
        assert result.violations == []

    def test_composition_cross_category_invalid(self):
        """composition between active_structure and behavior across layers is invalid."""
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="application",
            source_element_type="application_component",
            target_layer="business",
            target_element_type="business_process",
            relationship_type="composition",
        )
        assert result.valid is False
        assert len(result.violations) > 0
        assert len(result.suggestions) > 0

    def test_garbage_element_types_still_validates_layer(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="fake_layer",
            source_element_type="fake_type",
            target_layer="strategy",
            target_element_type="capability",
            relationship_type="realization",
        )
        assert result.valid is False
        assert any("unknown source element type" in v for v in result.violations)

    def test_unknown_relationship_type(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="business",
            source_element_type="business_process",
            target_layer="business",
            target_element_type="business_function",
            relationship_type="nonexistent_type",
        )
        assert result.valid is False
        assert any("is not an ArchiMate relationship type" in v for v in result.violations)

    def test_specialization_same_type_valid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="business",
            source_element_type="business_process",
            target_layer="business",
            target_element_type="business_process",
            relationship_type="specialization",
        )
        assert result.valid is True

    def test_specialization_different_type_invalid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="business",
            source_element_type="business_process",
            target_layer="business",
            target_element_type="business_function",
            relationship_type="specialization",
        )
        assert result.valid is False

    def test_assignment_within_same_layer_valid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="business",
            source_element_type="business_actor",
            target_layer="business",
            target_element_type="business_process",
            relationship_type="assignment",
        )
        assert result.valid is True

    def test_assignment_cross_layer_invalid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="business",
            source_element_type="business_actor",
            target_layer="application",
            target_element_type="application_process",
            relationship_type="assignment",
        )
        assert result.valid is False

    def test_flow_between_behavior_elements_valid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="business",
            source_element_type="business_process",
            target_layer="business",
            target_element_type="business_function",
            relationship_type="flow",
        )
        assert result.valid is True

    def test_flow_from_active_structure_follows_the_archi_matrix(self):
        """The old category rule ("flow only between behaviour") rejected this;
        Archi 5.10.0's matrix permits it (``ApplicationComponent → ApplicationProcess``
        carries ``f``). The matrix is the authority, not a category guess."""
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="application",
            source_element_type="application_component",
            target_layer="application",
            target_element_type="application_process",
            relationship_type="flow",
        )
        assert result.valid is True

    def test_flow_from_passive_structure_is_invalid(self):
        from tools.architecture.metamodel import validate_relationship

        result = validate_relationship(
            source_layer="application",
            source_element_type="data_object",
            target_layer="application",
            target_element_type="application_process",
            relationship_type="flow",
        )
        assert result.valid is False

    def test_non_archimate_types_are_invalid_between_archimate_elements(self):
        """Finding A03: ``traceability`` and TOC types had no dispatch branch and
        came back ``valid=True`` between ArchiMate elements.
        They are other domains' links; between two ArchiMate elements they are
        refused."""
        from tools.architecture.metamodel import validate_relationship

        for rel in ("traceability", "toc_conflict"):
            result = validate_relationship(
                source_layer="strategy",
                source_element_type="capability",
                target_layer="technology",
                target_element_type="node",
                relationship_type=rel,
            )
            assert result.valid is False, rel


# ===================================================================
# 6. Architecture Tool Unit Tests (pure functions)
# ===================================================================

# Shared test data for tool tests
_UUID1 = "00000000-0000-0000-0000-000000000001"
_UUID2 = "00000000-0000-0000-0000-000000000002"
_UUID3 = "00000000-0000-0000-0000-000000000003"
_UUID4 = "00000000-0000-0000-0000-000000000004"

_TEST_ELEMENTS = [
    {
        "id": _UUID1,
        "slug": "cap-1",
        "title": "Capability 1",
        "layer": "strategy",
        "element_type": "capability",
        "properties": {},
        "status": "draft",
        "architecture_state": "baseline",
        "parent_id": None,
        "description": "Test capability",
    },
    {
        "id": _UUID2,
        "slug": "bp-1",
        "title": "Business Process 1",
        "layer": "business",
        "element_type": "business_process",
        "properties": {},
        "status": "draft",
        "architecture_state": "baseline",
        "parent_id": None,
        "description": "Test process",
    },
    {
        "id": _UUID3,
        "slug": "app-comp-1",
        "title": "Application Component 1",
        "layer": "application",
        "element_type": "application_component",
        "properties": {},
        "status": "draft",
        "architecture_state": "baseline",
        "parent_id": None,
        "description": "Test app component",
    },
]

_TEST_RELATIONSHIPS = [
    {
        "id": "00000000-0000-0000-0000-000000000010",
        "source_entity_type": "architecture_elements",
        "source_id": _UUID1,
        "target_entity_type": "architecture_elements",
        "target_id": _UUID2,
        "relationship_type": "realization",
        "properties": {},
    },
    {
        "id": "00000000-0000-0000-0000-000000000011",
        "source_entity_type": "architecture_elements",
        "source_id": _UUID2,
        "target_entity_type": "architecture_elements",
        "target_id": _UUID3,
        "relationship_type": "serving",
        "properties": {},
    },
]


class TestTraceabilityMatrix:
    def test_builds_matrix_from_strategy_root(self):
        from tools.architecture.traceability import build_traceability_matrix

        result = build_traceability_matrix(
            _TEST_ELEMENTS, _TEST_RELATIONSHIPS, root_layer="strategy"
        )
        assert "rows" in result
        assert "coverage" in result
        assert len(result["rows"]) == 1  # only one strategy element

        row = result["rows"][0]
        assert row["root_element"]["id"] == _UUID1
        # The BFS should find elements across layers
        traced = row["traced_elements"]
        traced_layers = set(traced.keys())
        assert "strategy" in traced_layers
        assert "business" in traced_layers

    def test_empty_elements_returns_empty_matrix(self):
        from tools.architecture.traceability import build_traceability_matrix

        result = build_traceability_matrix([], [], root_layer="strategy")
        assert result["rows"] == []

    def test_coverage_ratios(self):
        from tools.architecture.traceability import build_traceability_matrix

        result = build_traceability_matrix(
            _TEST_ELEMENTS, _TEST_RELATIONSHIPS, root_layer="strategy"
        )
        coverage = result["coverage"]
        # strategy_to_business should have coverage since cap-1 traces to bp-1
        assert "strategy_to_business" in coverage

    def test_degenerate_layer_pairs_are_not_applicable(self):
        from tools.architecture.traceability import build_traceability_matrix

        # The test model has strategy/business/application elements but NO
        # technology, physical, or motivation elements. A layer pair whose source
        # or target layer is absent is not applicable => None (honest "no data"),
        # NOT a fabricated 1.0 (empty source) or 0.0 (empty target).
        result = build_traceability_matrix(
            _TEST_ELEMENTS, _TEST_RELATIONSHIPS, root_layer="strategy"
        )
        coverage = result["coverage"]
        # empty target layer (no technology/physical elements)
        assert coverage["strategy_to_technology"] is None
        assert coverage["technology_to_physical"] is None
        # empty source layer (no motivation elements) — was a fabricated 1.0/0.0
        assert coverage["motivation_to_strategy"] is None
        assert coverage["motivation_to_business"] is None
        # a fully-present pair still reports a real ratio
        assert coverage["strategy_to_business"] is not None


class TestGapAnalysis:
    def test_identifies_new_and_eliminated(self):
        from tools.architecture.gap_analysis import analyze_gaps

        baseline_elem = {
            "id": _UUID1,
            "slug": "old-cap",
            "title": "Old Capability",
            "layer": "strategy",
            "element_type": "capability",
            "properties": {},
            "architecture_state": "baseline",
            "description": "Old",
        }
        target_elem = {
            "id": _UUID2,
            "slug": "new-cap",
            "title": "New Capability",
            "layer": "strategy",
            "element_type": "capability",
            "properties": {},
            "architecture_state": "target",
            "description": "New",
        }
        result = analyze_gaps([baseline_elem, target_elem], [])

        assert len(result["new"]) == 1
        assert len(result["eliminated"]) == 1
        assert result["new"][0]["element"]["id"] == _UUID2
        assert result["eliminated"][0]["element"]["id"] == _UUID1

    def test_retained_elements(self):
        from tools.architecture.gap_analysis import analyze_gaps

        base = {
            "id": _UUID1,
            "slug": "cap-1",
            "title": "Shared Capability",
            "layer": "strategy",
            "element_type": "capability",
            "properties": {},
            "architecture_state": "baseline",
            "description": "Same",
        }
        target = {
            "id": _UUID2,
            "slug": "cap-1",
            "title": "Shared Capability",
            "layer": "strategy",
            "element_type": "capability",
            "properties": {},
            "architecture_state": "target",
            "description": "Same",
        }
        result = analyze_gaps([base, target], [])
        assert len(result["retained"]) == 1
        assert len(result["new"]) == 0
        assert len(result["eliminated"]) == 0

    def test_modified_elements(self):
        from tools.architecture.gap_analysis import analyze_gaps

        base = {
            "id": _UUID1,
            "slug": "cap-1",
            "title": "Capability",
            "layer": "strategy",
            "element_type": "capability",
            "properties": {},
            "architecture_state": "baseline",
            "description": "Version 1",
        }
        target = {
            "id": _UUID2,
            "slug": "cap-1",
            "title": "Capability",
            "layer": "strategy",
            "element_type": "capability",
            "properties": {},
            "architecture_state": "target",
            "description": "Version 2 - enhanced",
        }
        result = analyze_gaps([base, target], [])
        assert len(result["modified"]) == 1
        assert len(result["retained"]) == 0

    def test_gap_resolution_with_work_package(self):
        from tools.architecture.gap_analysis import analyze_gaps

        wp = {
            "id": _UUID3,
            "slug": "wp-1",
            "title": "Migration WP",
            "layer": "implementation_migration",
            "element_type": "work_package",
            "properties": {},
            "architecture_state": "target",
            "description": "Work package",
        }
        target_elem = {
            "id": _UUID2,
            "slug": "new-cap",
            "title": "New Capability",
            "layer": "strategy",
            "element_type": "capability",
            "properties": {},
            "architecture_state": "target",
            "description": "New",
        }
        rel = {
            "id": _UUID4,
            "source_id": _UUID2,
            "target_id": _UUID3,
            "source_entity_type": "architecture_elements",
            "target_entity_type": "architecture_elements",
            "relationship_type": "realization",
            "properties": {},
        }
        result = analyze_gaps([target_elem, wp], [rel])
        # Both target elements are "new" (no matching baseline)
        assert len(result["new"]) == 2
        # Find the capability entry -- it should be linked to the work package
        cap_entry = next(n for n in result["new"] if n["element"]["element_type"] == "capability")
        assert cap_entry["has_work_package"] is True
        # The wp itself is not linked to another wp, so only 1 of 2 resolved
        assert result["gap_resolution_pct"] == 0.5

    def test_no_gaps_is_not_applicable(self):
        from tools.architecture.gap_analysis import analyze_gaps

        # No elements => no gaps needing resolution => no resolution denominator.
        # Honest "no data" is None, NOT a fabricated 1.0 fully-resolved.
        result = analyze_gaps([], [])
        assert result["gap_resolution_pct"] is None
        assert result["new"] == []
        assert result["modified"] == []


class TestImpactAnalysis:
    def test_finds_affected_elements(self):
        from tools.architecture.impact import analyze_impact

        result = analyze_impact(
            element_id=_UUID1,
            elements=_TEST_ELEMENTS,
            relationships=_TEST_RELATIONSHIPS,
            max_depth=3,
        )
        assert result["source"]["id"] == _UUID1
        assert len(result["affected"]) >= 1

        # bp-1 should be directly affected (distance 1)
        affected_ids = [a["element"]["id"] for a in result["affected"]]
        assert _UUID2 in affected_ids

    def test_impact_strength_decreases_with_distance(self):
        from tools.architecture.impact import analyze_impact

        result = analyze_impact(
            element_id=_UUID1,
            elements=_TEST_ELEMENTS,
            relationships=_TEST_RELATIONSHIPS,
            max_depth=3,
        )
        affected = result["affected"]
        # Precondition, asserted rather than tested: the property below is
        # vacuous with fewer than two affected elements, so an empty or
        # single-element result must FAIL here instead of passing silently.
        # _TEST_RELATIONSHIPS is cap-1 -> bp-1 -> app-comp-1, so a BFS from
        # cap-1 at max_depth=3 reaches exactly two elements. This was an
        # `if len(affected) >= 2:` and skipped every assertion when it did not.
        assert len(affected) >= 2, (
            f"fixture must yield >=2 affected elements for this property to mean "
            f"anything; got {len(affected)}: {affected}"
        )
        # Results are sorted by -impact_strength, distance
        assert affected[0]["impact_strength"] >= affected[-1]["impact_strength"]

    def test_unknown_element_returns_empty(self):
        from tools.architecture.impact import analyze_impact

        result = analyze_impact(
            element_id="nonexistent-id",
            elements=_TEST_ELEMENTS,
            relationships=_TEST_RELATIONSHIPS,
        )
        assert result["source"]["title"] == "unknown"
        assert result["affected"] == []

    def test_max_depth_limits_traversal(self):
        from tools.architecture.impact import analyze_impact

        result = analyze_impact(
            element_id=_UUID1,
            elements=_TEST_ELEMENTS,
            relationships=_TEST_RELATIONSHIPS,
            max_depth=1,
        )
        assert result["affected"]  # analyze_impact returns [] for an unknown id
        for a in result["affected"]:
            assert a["distance"] <= 1

    def test_bfs_finds_transitive_impact(self):
        """cap-1 -> bp-1 -> app-comp-1: app-comp-1 should be found at distance 2."""
        from tools.architecture.impact import analyze_impact

        result = analyze_impact(
            element_id=_UUID1,
            elements=_TEST_ELEMENTS,
            relationships=_TEST_RELATIONSHIPS,
            max_depth=3,
        )
        affected_ids = [a["element"]["id"] for a in result["affected"]]
        assert _UUID3 in affected_ids

        # Verify app-comp-1 has distance 2
        app_entry = next(a for a in result["affected"] if a["element"]["id"] == _UUID3)
        assert app_entry["distance"] == 2


# ===================================================================
# 7. Cascade Delete Test
# ===================================================================


class TestCascadeDelete:
    async def test_delete_element_cascades_to_relationships(
        self, authenticated_client: AsyncClient
    ):
        """Deleting an element should also delete relationships referencing it."""
        e1, e2 = await _create_two_elements(authenticated_client)
        rel = await _create_relationship(authenticated_client, e2["id"], e1["id"])
        rel_id = rel["id"]

        # Delete e1 (source of the relationship)
        resp = await authenticated_client.delete(f"/architecture/elements/{e1['id']}")
        assert resp.status_code == 200

        # The relationship should be gone too
        resp = await authenticated_client.get(f"/architecture/relationships/{rel_id}")
        assert resp.status_code == 404

    async def test_delete_target_element_cascades(self, authenticated_client: AsyncClient):
        """Deleting the target element should also clean up the relationship."""
        e1, e2 = await _create_two_elements(authenticated_client)
        rel = await _create_relationship(authenticated_client, e2["id"], e1["id"])
        rel_id = rel["id"]

        # Delete e2 (target of the relationship)
        resp = await authenticated_client.delete(f"/architecture/elements/{e2['id']}")
        assert resp.status_code == 200

        # The relationship should be gone
        resp = await authenticated_client.get(f"/architecture/relationships/{rel_id}")
        assert resp.status_code == 404
