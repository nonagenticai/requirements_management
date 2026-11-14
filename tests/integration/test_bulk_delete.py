"""Integration tests for bulk delete endpoints."""

import os
from uuid import uuid4

import pytest

# The tenant req_client authenticates as (same env/default as integration/conftest.py).
TENANT_ID = os.environ.get("TENANT_ID", "0195f48b-d000-7000-8000-000000000001")

pytestmark = pytest.mark.integration

# Every bulk route now demands confirm=<the caller's tenant>; see src/routers/bulk.py.
_CONFIRM = {"confirm": TENANT_ID}


def _uid():
    return uuid4().hex[:8]


def _create_requirement(req_client):
    """Helper to create a requirement, returns response data; fails loudly if the create did not."""
    payload = {
        "title": f"BulkDel Req {_uid()}",
        "description": "Created for bulk delete test",
        "effort": 1.0,
        "links": {"business_objectives": ["bulk-test"]},
    }
    r = req_client.post("/requirements", json=payload)
    assert r.status_code in (200, 201), f"setup: create requirement -> {r.status_code}: {r.text}"
    return r.json()


def _create_principle(req_client):
    """Helper to create a principle, returns response data; fails loudly if the create did not."""
    payload = {
        "title": f"BulkDel Prin {_uid()}",
        "statement": "Bulk delete test statement",
        "rationale": "Testing rationale",
        "implications": ["Test implication"],
        "tags": ["bulk-test"],
    }
    r = req_client.post("/principles", json=payload)
    assert r.status_code in (200, 201), f"setup: create principle -> {r.status_code}: {r.text}"
    return r.json()


def _create_decision(req_client):
    """Helper to create a decision, returns response data; fails loudly if the create did not."""
    payload = {
        "title": f"BulkDel Dec {_uid()}",
        "context": "Bulk delete test context",
        "decision": "Use bulk delete",
        "consequences": "All removed",
        "alternatives": ["Manual delete"],
        "status": "proposed",
    }
    r = req_client.post("/decisions", json=payload)
    assert r.status_code in (200, 201), f"setup: create decision -> {r.status_code}: {r.text}"
    return r.json()


class TestBulkDeleteRequirements:
    """Tests for DELETE /requirements/bulk."""

    def test_bulk_delete_requirements(self, req_client):
        """Bulk delete removes requirements and returns expected shape."""
        _create_requirement(req_client)
        _create_requirement(req_client)

        r = req_client.delete("/requirements/bulk", params=_CONFIRM)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "deleted"
        assert data["entity_type"] == "requirements"
        assert isinstance(data["deleted_count"], int)
        assert data["deleted_count"] >= 2

    def test_bulk_delete_requirements_response_has_tenant_id(self, req_client):
        """Response includes tenant_id from auth token."""
        _create_requirement(req_client)
        r = req_client.delete("/requirements/bulk", params=_CONFIRM)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "tenant_id" in data
        assert isinstance(data["tenant_id"], str)
        assert len(data["tenant_id"]) > 0

    def test_bulk_delete_requirements_idempotent(self, req_client):
        """Calling bulk delete twice succeeds; second call deletes 0."""
        _create_requirement(req_client)
        r1 = req_client.delete("/requirements/bulk", params=_CONFIRM)
        assert r1.status_code == 200, r1.text

        r2 = req_client.delete("/requirements/bulk", params=_CONFIRM)
        assert r2.status_code == 200, r2.text
        assert r2.json()["deleted_count"] == 0

    def test_bulk_delete_requirements_entities_gone(self, req_client):
        """After bulk delete, listing requirements returns empty."""
        _create_requirement(req_client)
        req_client.delete("/requirements/bulk", params=_CONFIRM)

        r = req_client.get("/requirements")
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 0


class TestBulkDeletePrinciples:
    """Tests for DELETE /principles/bulk."""

    def test_bulk_delete_principles(self, req_client):
        """Bulk delete removes principles and returns expected shape."""
        _create_principle(req_client)
        _create_principle(req_client)

        r = req_client.delete("/principles/bulk", params=_CONFIRM)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "deleted"
        assert data["entity_type"] == "principles"
        assert isinstance(data["deleted_count"], int)
        assert data["deleted_count"] >= 2

    def test_bulk_delete_principles_idempotent(self, req_client):
        """Second bulk delete returns deleted_count 0."""
        _create_principle(req_client)
        req_client.delete("/principles/bulk", params=_CONFIRM)

        r = req_client.delete("/principles/bulk", params=_CONFIRM)
        assert r.status_code == 200, r.text
        assert r.json()["deleted_count"] == 0


class TestBulkDeleteDecisions:
    """Tests for DELETE /decisions/bulk."""

    def test_bulk_delete_decisions(self, req_client):
        """Bulk delete removes decisions and returns expected shape."""
        _create_decision(req_client)
        _create_decision(req_client)

        r = req_client.delete("/decisions/bulk", params=_CONFIRM)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "deleted"
        assert data["entity_type"] == "decisions"
        assert isinstance(data["deleted_count"], int)
        assert data["deleted_count"] >= 2

    def test_bulk_delete_decisions_idempotent(self, req_client):
        """Second bulk delete returns deleted_count 0."""
        _create_decision(req_client)
        req_client.delete("/decisions/bulk", params=_CONFIRM)

        r = req_client.delete("/decisions/bulk", params=_CONFIRM)
        assert r.status_code == 200, r.text
        assert r.json()["deleted_count"] == 0


class TestBulkDeleteAll:
    """Tests for DELETE /bulk (all entity types).

    The note that used to sit here said these tests "skip gracefully" on a 401
    from a missing ext_authz tenant forward. They no longer skip at all — a skip
    is what let a broken /bulk report success. In-process there is no ext_authz
    in front of the app, the route exists (``src/routers/bulk.py``), and the
    trusted-network branch supplies the tenant, so a 401 here is a real defect.

    ``bulk_delete_all`` resolves ``tenant_id`` + ``subject_id`` and delegates to a
    ``scope()``-filtered query, so it is visibility-scoped, not a cross-tenant
    mass delete — checked before these were allowed to run against the shared
    test database.
    """

    def test_bulk_delete_all(self, req_client):
        """Bulk delete all removes every entity type."""
        _create_requirement(req_client)
        _create_principle(req_client)
        _create_decision(req_client)

        r = req_client.delete("/bulk", params=_CONFIRM)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "deleted"
        assert "tenant_id" in data
        assert isinstance(data["total_deleted"], int)
        assert data["total_deleted"] >= 3

    def test_bulk_delete_all_response_breakdown(self, req_client):
        """Per-type counts sum to total_deleted."""
        _create_requirement(req_client)
        _create_principle(req_client)
        _create_decision(req_client)

        r = req_client.delete("/bulk", params=_CONFIRM)
        assert r.status_code == 200, r.text
        data = r.json()
        expected_total = (
            data["requirements_deleted"] + data["principles_deleted"] + data["decisions_deleted"]
        )
        assert data["total_deleted"] == expected_total

    def test_bulk_delete_all_idempotent(self, req_client):
        """Second call deletes 0 across all types."""
        _create_requirement(req_client)
        # The first call must succeed, or "second call deletes 0" proves nothing.
        r_first = req_client.delete("/bulk", params=_CONFIRM)
        assert r_first.status_code == 200, f"first /bulk -> {r_first.status_code}: {r_first.text}"

        r = req_client.delete("/bulk", params=_CONFIRM)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total_deleted"] == 0

    def test_bulk_delete_all_entities_gone(self, req_client):
        """After per-type bulk delete, listing each type returns empty."""
        _create_requirement(req_client)
        _create_principle(req_client)
        _create_decision(req_client)
        # Use per-type bulk delete since /bulk may not have ext_authz configured
        req_client.delete("/requirements/bulk", params=_CONFIRM)
        req_client.delete("/principles/bulk", params=_CONFIRM)
        req_client.delete("/decisions/bulk", params=_CONFIRM)

        for endpoint in ("/requirements", "/principles", "/decisions"):
            r = req_client.get(endpoint)
            assert r.status_code == 200, f"{endpoint}: {r.text}"
            data = r.json()
            assert isinstance(data, list)
            assert len(data) == 0, f"{endpoint} still has {len(data)} items"
