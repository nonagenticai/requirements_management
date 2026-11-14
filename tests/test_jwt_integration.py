"""Integration tests for tenant-based authentication and authorization.

Tests verify X-Tenant-Id header authentication across Requirements, Principles, and Decisions.
A plain X-Tenant-Id here is a test licence for that tenant (tests/conftest.py); the
middleware admits licences only.
"""

import pytest
from fastapi import status

from tests.tenants import T

pytestmark = pytest.mark.usefixtures("licences_reject_unknown_tokens")


class TestTenantIdExtraction:
    """Test tenant ID extraction from X-Tenant-Id header."""

    @pytest.mark.asyncio
    async def test_tenant_header_extracts_tenant_id(self, test_client):
        """Test that X-Tenant-Id header sets tenant_id."""
        headers = {"X-Tenant-Id": T("user-valid-001")}

        response = await test_client.get("/health")
        assert response.status_code == status.HTTP_200_OK

        requirement_data = {
            "title": "Token Validation Test",
            "description": "Testing tenant_id extraction",
            "effort": 5.0,
            "reach": 100.0,
            "impact": 3.0,
            "confidence": 0.8,
            "links": {},
            "acceptance": [],
            "tenant_id": T("will-be-overridden"),
        }

        response = await test_client.post("/requirements", json=requirement_data, headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["tenant_id"] == T("user-valid-001")
        assert data["title"] == "Token Validation Test"
        assert data["effort"] == 5.0

    @pytest.mark.asyncio
    async def test_invalid_license_token_rejected(self, test_client):
        """Test that Bearer token in X-Tenant-Id triggers introspection and fails."""
        headers = {"X-Tenant-Id": "Bearer invalid-token-format"}
        response = await test_client.post(
            "/requirements",
            json={"title": "Test", "description": "Test", "effort": 5.0},
            headers=headers,
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestRequirementTenantIntegration:
    """Integration tests for Requirements with tenant-based authentication."""

    @pytest.mark.asyncio
    async def test_create_requirement_with_tenant_header(self, test_client):
        """Test creating requirement with X-Tenant-Id header."""
        headers = {"X-Tenant-Id": T("user-create-001")}

        requirement_data = {
            "title": "Tenant Authenticated Requirement",
            "description": "Created with tenant header",
            "effort": 8.0,
            "reach": 500.0,
            "impact": 5.0,
            "confidence": 0.9,
            "links": {},
            "acceptance": ["Should be created successfully"],
        }

        response = await test_client.post("/requirements", json=requirement_data, headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Tenant Authenticated Requirement"
        assert data["tenant_id"] == T("user-create-001")
        assert data["effort"] == 8.0
        assert data["reach"] == 500.0

    @pytest.mark.asyncio
    async def test_get_requirement_with_ownership_check(self, test_client, sample_requirement):
        """Test getting requirement verifies ownership using tenant header."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.get(f"/requirements/{sample_requirement.id}", headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == str(sample_requirement.id)
        assert data["tenant_id"] == T("user-123")

    @pytest.mark.asyncio
    async def test_get_requirement_forbidden_for_other_user(
        self, test_client, other_user_requirement
    ):
        """Test attempting to access another user's requirement returns 403."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.get(
            f"/requirements/{other_user_requirement.id}", headers=headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_list_requirements_filtered_by_creator(
        self, test_client, sample_requirement, other_user_requirement
    ):
        """Test listing requirements only shows user's own requirements."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.get("/requirements", headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        requirements = data if isinstance(data, list) else data.get("items", [])

        assert len(requirements) >= 1
        for req in requirements:
            assert req["tenant_id"] == T("user-123")

        other_user_ids = [r["id"] for r in requirements]
        assert other_user_requirement.id not in other_user_ids

    @pytest.mark.asyncio
    async def test_bulk_delete_requirements_only_deletes_own(
        self, test_client, sample_requirement, other_user_requirement
    ):
        """Test bulk delete only removes authenticated user's requirements."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.delete(
            "/requirements/bulk", headers=headers, params={"confirm": T("user-123")}
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "deleted"
        assert data["tenant_id"] == T("user-123")


class TestPrincipleTenantIntegration:
    """Integration tests for Principles with tenant-based authentication."""

    @pytest.mark.asyncio
    async def test_create_principle_with_tenant_header(self, test_client):
        """Test creating principle with X-Tenant-Id header."""
        headers = {"X-Tenant-Id": T("user-principle-001")}

        principle_data = {
            "title": "Tenant Authenticated Principle",
            "statement": "All APIs must be authenticated",
            "rationale": "Security is paramount",
            "implications": ["Implement auth", "Validate all requests"],
            "tags": ["security", "authentication"],
            "active": True,
        }

        response = await test_client.post("/principles", json=principle_data, headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Tenant Authenticated Principle"
        assert data["tenant_id"] == T("user-principle-001")
        assert data["statement"] == "All APIs must be authenticated"

    @pytest.mark.asyncio
    async def test_get_principle_forbidden_for_other_user(self, test_client, other_user_principle):
        """Test attempting to access another user's principle returns 403."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.get(f"/principles/{other_user_principle.id}", headers=headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_bulk_delete_principles(self, test_client, sample_principle):
        """Test bulk delete principles for authenticated user."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.delete(
            "/principles/bulk", headers=headers, params={"confirm": T("user-123")}
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "deleted"
        assert data["tenant_id"] == T("user-123")
        assert data["entity_type"] == "principles"


class TestDecisionTenantIntegration:
    """Integration tests for Decisions with tenant-based authentication."""

    @pytest.mark.asyncio
    async def test_create_decision_with_tenant_header(self, test_client):
        """Test creating decision with X-Tenant-Id header."""
        headers = {"X-Tenant-Id": T("user-decision-001")}

        decision_data = {
            "title": "Tenant Authenticated Decision",
            "context": "Need to secure API endpoints",
            "decision": "Use license token authentication",
            "status": "accepted",
            "consequences": "Improved security",
            "alternatives": ["Basic auth", "API keys"],
            "related_principles": [],
        }

        response = await test_client.post("/decisions", json=decision_data, headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Tenant Authenticated Decision"
        assert data["tenant_id"] == T("user-decision-001")
        assert data["status"] == "accepted"
        assert data["decision"] == "Use license token authentication"

    @pytest.mark.asyncio
    async def test_get_decision_forbidden_for_other_user(self, test_client, other_user_decision):
        """Test attempting to access another user's decision returns 403."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.get(f"/decisions/{other_user_decision.id}", headers=headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_bulk_delete_decisions(self, test_client, sample_decision):
        """Test bulk delete decisions for authenticated user."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.delete(
            "/decisions/bulk", headers=headers, params={"confirm": T("user-123")}
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "deleted"
        assert data["tenant_id"] == T("user-123")
        assert data["entity_type"] == "decisions"


class TestBulkDeleteAllEntities:
    """Integration tests for bulk delete all entities endpoint."""

    @pytest.mark.asyncio
    async def test_bulk_delete_all_entities(
        self,
        test_client,
        sample_requirement,
        sample_principle,
        sample_decision,
    ):
        """Test bulk delete all entities for authenticated user."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.delete(
            "/bulk", headers=headers, params={"confirm": T("user-123")}
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "deleted"
        assert data["tenant_id"] == T("user-123")
        assert "total_deleted" in data
        assert "requirements_deleted" in data
        assert "principles_deleted" in data
        assert "decisions_deleted" in data


class TestConcurrentUserAccess:
    """Integration tests for concurrent access by different users."""

    @pytest.mark.asyncio
    async def test_concurrent_requirement_creation(self, test_client):
        """Test that multiple users can create requirements concurrently."""
        users_data = [
            (T("concurrent-user-1"), "User 1 Requirement"),
            (T("concurrent-user-2"), "User 2 Requirement"),
            (T("concurrent-user-3"), "User 3 Requirement"),
        ]

        created_requirements = []

        for tenant_id, title in users_data:
            headers = {"X-Tenant-Id": tenant_id}
            requirement_data = {
                "title": title,
                "description": f"Concurrent test for {title}",
                "effort": 5.0,
                "reach": 100.0,
                "impact": 3.0,
                "confidence": 0.8,
                "links": {},
                "acceptance": [],
            }

            response = await test_client.post(
                "/requirements", json=requirement_data, headers=headers
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["title"] == title
            created_requirements.append(data)

        assert created_requirements[0]["tenant_id"] == T("concurrent-user-1")
        assert created_requirements[1]["tenant_id"] == T("concurrent-user-2")
        assert created_requirements[2]["tenant_id"] == T("concurrent-user-3")


class TestTenantWithQualityAndPriorityEndpoints:
    """Test tenant authentication with quality and priority calculation endpoints."""

    @pytest.mark.asyncio
    async def test_quality_check_with_ownership(self, test_client, sample_requirement):
        """Test quality check endpoint verifies ownership."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.post(
            f"/requirements/{sample_requirement.id}/quality", headers=headers
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "score" in data
        assert "issues" in data
        assert "passes" in data

    @pytest.mark.asyncio
    async def test_quality_check_forbidden_for_other_user(
        self, test_client, other_user_requirement
    ):
        """Test quality check on another user's requirement returns 403."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.post(
            f"/requirements/{other_user_requirement.id}/quality", headers=headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_priority_calculation_with_ownership(self, test_client, sample_requirement):
        """Test priority calculation endpoint verifies ownership."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.post(
            f"/requirements/{sample_requirement.id}/priority?business_value=5&time_criticality=3&risk_reduction=2",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "wsjf" in data or "rice" in data

    @pytest.mark.asyncio
    async def test_priority_calculation_forbidden_for_other_user(
        self, test_client, other_user_requirement
    ):
        """Test priority calculation on another user's requirement returns 403."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.post(
            f"/requirements/{other_user_requirement.id}/priority", headers=headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestTenantWithSearchEndpoints:
    """Test tenant authentication with search endpoints."""

    @pytest.mark.asyncio
    async def test_search_filtered_by_creator(
        self, test_client, sample_requirement, other_user_requirement
    ):
        """Test search endpoint filters results by tenant_id from tenant header."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.get("/search?q=requirement", headers=headers)

        assert response.status_code == status.HTTP_200_OK
