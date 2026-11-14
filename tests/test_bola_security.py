"""
BOLA (Broken Object Level Authorization) Security Tests for Requirements Management API.

These tests verify that tenant isolation is properly enforced:
1. Same-tenant access succeeds (200)
2. Cross-tenant access returns 403 (access forbidden)
3. List endpoints only return tenant's own items
4. All CRUD operations respect tenant boundaries

BOLA is OWASP API Security Top 10 #1 vulnerability.
"""

import pytest
from fastapi import status

pytestmark = [pytest.mark.security, pytest.mark.bola, pytest.mark.tenant_isolation]


class TestRequirementsBOLASecurity:
    """BOLA tests for /requirements endpoints."""

    @pytest.mark.asyncio
    async def test_get_requirement_same_tenant_succeeds(
        self, authenticated_tenant_a_client, tenant_a_requirement
    ):
        """Tenant A can access their own requirement."""
        response = await authenticated_tenant_a_client.get(
            f"/requirements/{tenant_a_requirement.id}"
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == str(tenant_a_requirement.id)
        assert data["title"] == "Tenant A Requirement"

    @pytest.mark.asyncio
    async def test_get_requirement_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_requirement
    ):
        """Tenant B cannot access Tenant A's requirement - returns 403."""
        response = await authenticated_tenant_b_client.get(
            f"/requirements/{tenant_a_requirement.id}"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["message"] == "not found"

    @pytest.mark.asyncio
    async def test_update_requirement_same_tenant_succeeds(
        self, authenticated_tenant_a_client, tenant_a_requirement
    ):
        """Tenant A can update their own requirement."""
        update_data = {
            "title": "Updated Tenant A Requirement",
            "description": "Updated by tenant A",
            "project_id": "tenant-a-project",
            "links": {},
            "acceptance": [],
            "effort": 5.0,
            "reach": 100.0,
            "impact": 3.0,
            "confidence": 0.8,
        }
        response = await authenticated_tenant_a_client.put(
            f"/requirements/{tenant_a_requirement.id}", json=update_data
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Updated Tenant A Requirement"

    @pytest.mark.asyncio
    async def test_update_requirement_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_requirement
    ):
        """Tenant B cannot update Tenant A's requirement - returns 403."""
        update_data = {
            "title": "Hijacked by Tenant B",
            "description": "This should fail",
            "project_id": "tenant-b-project",
            "links": {},
            "acceptance": [],
            "effort": 5.0,
            "reach": 100.0,
            "impact": 3.0,
            "confidence": 0.8,
        }
        response = await authenticated_tenant_b_client.put(
            f"/requirements/{tenant_a_requirement.id}", json=update_data
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["message"] == "not found"

    @pytest.mark.asyncio
    async def test_delete_requirement_same_tenant_succeeds(
        self, authenticated_tenant_a_client, tenant_a_requirement
    ):
        """Tenant A can delete their own requirement."""
        response = await authenticated_tenant_a_client.delete(
            f"/requirements/{tenant_a_requirement.id}"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_requirement_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_requirement
    ):
        """Tenant B cannot delete Tenant A's requirement - returns 403."""
        response = await authenticated_tenant_b_client.delete(
            f"/requirements/{tenant_a_requirement.id}"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["message"] == "not found"

    @pytest.mark.asyncio
    async def test_quality_endpoint_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_requirement
    ):
        """Tenant B cannot access quality endpoint for Tenant A's requirement."""
        response = await authenticated_tenant_b_client.post(
            f"/requirements/{tenant_a_requirement.id}/quality"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_priority_endpoint_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_requirement
    ):
        """Tenant B cannot access priority endpoint for Tenant A's requirement."""
        response = await authenticated_tenant_b_client.post(
            f"/requirements/{tenant_a_requirement.id}/priority"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestPrinciplesBOLASecurity:
    """BOLA tests for /principles endpoints."""

    @pytest.mark.asyncio
    async def test_get_principle_same_tenant_succeeds(
        self, authenticated_tenant_a_client, tenant_a_principle
    ):
        """Tenant A can access their own principle."""
        response = await authenticated_tenant_a_client.get(f"/principles/{tenant_a_principle.id}")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == str(tenant_a_principle.id)
        assert data["title"] == "Tenant A Principle"

    @pytest.mark.asyncio
    async def test_get_principle_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_principle
    ):
        """Tenant B cannot access Tenant A's principle - returns 403."""
        response = await authenticated_tenant_b_client.get(f"/principles/{tenant_a_principle.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["message"] == "not found"

    @pytest.mark.asyncio
    async def test_update_principle_same_tenant_succeeds(
        self, authenticated_tenant_a_client, tenant_a_principle
    ):
        """Tenant A can update their own principle."""
        update_data = {
            "title": "Updated Tenant A Principle",
            "statement": "Updated statement",
            "rationale": "Updated rationale",
            "implications": ["Updated implication"],
            "tags": ["updated"],
            "active": True,
        }
        response = await authenticated_tenant_a_client.put(
            f"/principles/{tenant_a_principle.id}", json=update_data
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Updated Tenant A Principle"

    @pytest.mark.asyncio
    async def test_update_principle_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_principle
    ):
        """Tenant B cannot update Tenant A's principle - returns 403."""
        update_data = {
            "title": "Hijacked by Tenant B",
            "statement": "This should fail",
            "rationale": "Should not work",
            "implications": [],
            "tags": [],
            "active": False,
        }
        response = await authenticated_tenant_b_client.put(
            f"/principles/{tenant_a_principle.id}", json=update_data
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["message"] == "not found"

    @pytest.mark.asyncio
    async def test_delete_principle_same_tenant_succeeds(
        self, authenticated_tenant_a_client, tenant_a_principle
    ):
        """Tenant A can delete their own principle."""
        response = await authenticated_tenant_a_client.delete(
            f"/principles/{tenant_a_principle.id}"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_principle_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_principle
    ):
        """Tenant B cannot delete Tenant A's principle - returns 403."""
        response = await authenticated_tenant_b_client.delete(
            f"/principles/{tenant_a_principle.id}"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["message"] == "not found"

    @pytest.mark.asyncio
    async def test_version_endpoint_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_principle
    ):
        """Tenant B cannot access version endpoint for Tenant A's principle."""
        response = await authenticated_tenant_b_client.get(
            f"/principles/{tenant_a_principle.id}/version"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestDecisionsBOLASecurity:
    """BOLA tests for /decisions endpoints."""

    @pytest.mark.asyncio
    async def test_get_decision_same_tenant_succeeds(
        self, authenticated_tenant_a_client, tenant_a_decision
    ):
        """Tenant A can access their own decision."""
        response = await authenticated_tenant_a_client.get(f"/decisions/{tenant_a_decision.id}")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == str(tenant_a_decision.id)
        assert data["title"] == "Tenant A Decision"

    @pytest.mark.asyncio
    async def test_get_decision_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_decision
    ):
        """Tenant B cannot access Tenant A's decision - returns 403."""
        response = await authenticated_tenant_b_client.get(f"/decisions/{tenant_a_decision.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["message"] == "not found"

    @pytest.mark.asyncio
    async def test_update_decision_same_tenant_succeeds(
        self, authenticated_tenant_a_client, tenant_a_decision
    ):
        """Tenant A can update their own decision."""
        update_data = {
            "title": "Updated Tenant A Decision",
            "project_id": "tenant-a-project",
            "status": "accepted",
            "context": "Updated context",
            "decision": "Updated decision",
            "consequences": "Updated consequences",
            "alternatives": ["alt1"],
            "related_principles": [],
        }
        response = await authenticated_tenant_a_client.put(
            f"/decisions/{tenant_a_decision.id}", json=update_data
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Updated Tenant A Decision"

    @pytest.mark.asyncio
    async def test_update_decision_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_decision
    ):
        """Tenant B cannot update Tenant A's decision - returns 403."""
        update_data = {
            "title": "Hijacked by Tenant B",
            "project_id": "tenant-b-project",
            "status": "deprecated",
            "context": "This should fail",
            "decision": "Should not work",
            "consequences": "None",
            "alternatives": [],
            "related_principles": [],
        }
        response = await authenticated_tenant_b_client.put(
            f"/decisions/{tenant_a_decision.id}", json=update_data
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["message"] == "not found"

    @pytest.mark.asyncio
    async def test_delete_decision_same_tenant_succeeds(
        self, authenticated_tenant_a_client, tenant_a_decision
    ):
        """Tenant A can delete their own decision."""
        response = await authenticated_tenant_a_client.delete(f"/decisions/{tenant_a_decision.id}")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_decision_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_decision
    ):
        """Tenant B cannot delete Tenant A's decision - returns 403."""
        response = await authenticated_tenant_b_client.delete(f"/decisions/{tenant_a_decision.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["message"] == "not found"

    @pytest.mark.asyncio
    async def test_version_endpoint_cross_tenant_returns_403(
        self, authenticated_tenant_b_client, tenant_a_decision
    ):
        """Tenant B cannot access version endpoint for Tenant A's decision."""
        response = await authenticated_tenant_b_client.get(
            f"/decisions/{tenant_a_decision.id}/version"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestBOLAListFiltering:
    """Verify list endpoints only return tenant's own items."""

    @pytest.mark.asyncio
    async def test_requirements_list_tenant_a_only_sees_own(
        self,
        tenant_a_id,
        authenticated_tenant_a_client,
        tenant_a_requirement,
        tenant_b_requirement,
    ):
        """Tenant A only sees their own requirements in list."""
        response = await authenticated_tenant_a_client.get("/requirements")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 1
        for req in data:
            assert req["tenant_id"] == tenant_a_id

    @pytest.mark.asyncio
    async def test_requirements_list_tenant_b_only_sees_own(
        self,
        tenant_b_id,
        authenticated_tenant_b_client,
        tenant_a_requirement,
        tenant_b_requirement,
    ):
        """Tenant B only sees their own requirements in list."""
        response = await authenticated_tenant_b_client.get("/requirements")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 1
        for req in data:
            assert req["tenant_id"] == tenant_b_id

    @pytest.mark.asyncio
    async def test_principles_list_tenant_a_only_sees_own(
        self,
        tenant_a_id,
        authenticated_tenant_a_client,
        tenant_a_principle,
        tenant_b_principle,
    ):
        """Tenant A only sees their own principles in list."""
        response = await authenticated_tenant_a_client.get("/principles")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 1, (
            "an over-filtering regression returning [] for every tenant would make the "
            "per-item tenant_id loop below pass vacuously"
        )
        for prin in data:
            assert prin["tenant_id"] == tenant_a_id

    @pytest.mark.asyncio
    async def test_principles_list_tenant_b_only_sees_own(
        self,
        tenant_b_id,
        authenticated_tenant_b_client,
        tenant_a_principle,
        tenant_b_principle,
    ):
        """Tenant B only sees their own principles in list."""
        response = await authenticated_tenant_b_client.get("/principles")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 1, (
            "an over-filtering regression returning [] for every tenant would make the "
            "per-item tenant_id loop below pass vacuously"
        )
        for prin in data:
            assert prin["tenant_id"] == tenant_b_id

    @pytest.mark.asyncio
    async def test_decisions_list_tenant_a_only_sees_own(
        self,
        tenant_a_id,
        authenticated_tenant_a_client,
        tenant_a_decision,
        tenant_b_decision,
    ):
        """Tenant A only sees their own decisions in list."""
        response = await authenticated_tenant_a_client.get("/decisions")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 1, (
            "an over-filtering regression returning [] for every tenant would make the "
            "per-item tenant_id loop below pass vacuously"
        )
        for dec in data:
            assert dec["tenant_id"] == tenant_a_id

    @pytest.mark.asyncio
    async def test_decisions_list_tenant_b_only_sees_own(
        self,
        tenant_b_id,
        authenticated_tenant_b_client,
        tenant_a_decision,
        tenant_b_decision,
    ):
        """Tenant B only sees their own decisions in list."""
        response = await authenticated_tenant_b_client.get("/decisions")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 1, (
            "an over-filtering regression returning [] for every tenant would make the "
            "per-item tenant_id loop below pass vacuously"
        )
        for dec in data:
            assert dec["tenant_id"] == tenant_b_id


class TestBOLANonexistentResources:
    """Verify 404 is returned for nonexistent resources (no information leakage)."""

    @pytest.mark.asyncio
    async def test_get_nonexistent_requirement_returns_404(self, authenticated_tenant_a_client):
        """Request for nonexistent requirement returns 404."""
        response = await authenticated_tenant_a_client.get(
            "/requirements/00000000-0000-7000-0000-000000000000"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_get_nonexistent_principle_returns_404(self, authenticated_tenant_a_client):
        """Request for nonexistent principle returns 404."""
        response = await authenticated_tenant_a_client.get(
            "/principles/00000000-0000-7000-0000-000000000000"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_get_nonexistent_decision_returns_404(self, authenticated_tenant_a_client):
        """Request for nonexistent decision returns 404."""
        response = await authenticated_tenant_a_client.get(
            "/decisions/00000000-0000-7000-0000-000000000000"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestBOLACreateIsolation:
    """Verify created resources are properly isolated to creator's tenant."""

    @pytest.mark.asyncio
    async def test_created_requirement_belongs_to_creator(
        self, authenticated_tenant_a_client, tenant_a_id
    ):
        """New requirement is associated with creator's tenant."""
        create_data = {
            "title": "New Requirement from Tenant A",
            "description": "Testing creation isolation",
            "project_id": "test-project",
            "links": {},
            "acceptance": [],
            "effort": 5.0,
            "reach": 100.0,
            "impact": 3.0,
            "confidence": 0.8,
        }
        response = await authenticated_tenant_a_client.post("/requirements", json=create_data)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["tenant_id"] == tenant_a_id

    @pytest.mark.asyncio
    async def test_created_principle_belongs_to_creator(
        self, authenticated_tenant_a_client, tenant_a_id
    ):
        """New principle is associated with creator's tenant."""
        create_data = {
            "title": "New Principle from Tenant A",
            "statement": "Testing creation isolation",
            "rationale": "To verify tenant isolation",
            "implications": [],
            "tags": [],
            "active": True,
        }
        response = await authenticated_tenant_a_client.post("/principles", json=create_data)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["tenant_id"] == tenant_a_id

    @pytest.mark.asyncio
    async def test_created_decision_belongs_to_creator(
        self, authenticated_tenant_a_client, tenant_a_id
    ):
        """New decision is associated with creator's tenant."""
        create_data = {
            "title": "New Decision from Tenant A",
            "project_id": "test-project",
            "status": "proposed",
            "context": "Testing creation isolation",
            "decision": "Create isolated",
            "consequences": "Proper isolation",
            "alternatives": [],
            "related_principles": [],
        }
        response = await authenticated_tenant_a_client.post("/decisions", json=create_data)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["tenant_id"] == tenant_a_id
