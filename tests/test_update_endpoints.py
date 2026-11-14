"""Tests for UPDATE endpoints (PUT) for requirements, principles, and decisions.

This module focuses on:
- Update endpoint functionality
- Ownership validation (can't update others' items)
- Field validation errors
- Version reference validation
- Numeric bounds validation
"""

from uuid import uuid7

import pytest
from fastapi import status

from tests.tenants import T

pytestmark = pytest.mark.usefixtures("licences_reject_unknown_tokens")

HTTP_422_UNPROCESSABLE = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)


class TestRequirementUpdateEndpoint:
    """Test PUT /requirements/{rid} endpoint."""

    @pytest.mark.asyncio
    async def test_update_requirement_success(self, authenticated_client, sample_requirement):
        """Test successfully updating all fields of a requirement."""
        update_data = {
            "title": "Updated Requirement Title",
            "description": "Updated description with more details",
            "project_id": "updated-project",
            "links": {"business_objectives": ["obj-1", "obj-2"]},
            "acceptance": [
                "Updated acceptance criteria 1",
                "Updated acceptance criteria 2",
            ],
            "effort": 15.5,
            "reach": 500.0,
            "impact": 7.5,
            "confidence": 0.95,
            "version": "2.0.0",
            "version_status": "PUBLISHED",  # Uppercase
            "version_notes": "Major update with new features",
            "approved_by": "manager@example.com",
        }

        response = await authenticated_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Updated Requirement Title"
        assert data["description"] == "Updated description with more details"
        assert data["project_id"] == "updated-project"
        assert data["effort"] == 15.5
        assert data["reach"] == 500.0
        assert data["impact"] == 7.5
        assert data["confidence"] == 0.95
        assert data["version"] == "2.0.0"
        assert data["version_status"] == "PUBLISHED"  # Uppercase
        assert data["tenant_id"] == T("user-123")  # Should remain unchanged

    @pytest.mark.asyncio
    async def test_update_requirement_partial_fields(
        self, authenticated_client, sample_requirement
    ):
        """Test updating only some fields preserves others."""
        # Get current state
        get_response = await authenticated_client.get(f"/requirements/{sample_requirement.id}")
        original_data = get_response.json()

        # Update only title and effort
        update_data = {
            "title": "Only Title Changed",
            "description": original_data["description"],
            "effort": 99.0,
            "reach": original_data["reach"],
            "impact": original_data["impact"],
            "confidence": original_data["confidence"],
            "links": original_data.get("links", {}),
            "acceptance": original_data.get("acceptance", []),
        }

        response = await authenticated_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Only Title Changed"
        assert data["effort"] == 99.0
        assert data["description"] == original_data["description"]
        assert data["reach"] == original_data["reach"]

    @pytest.mark.asyncio
    async def test_update_requirement_ownership_validation(
        self, authenticated_client, other_user_requirement
    ):
        """Test cannot update another user's requirement (403 Forbidden)."""
        update_data = {
            "title": "Trying to Hijack",
            "description": "Should not work",
            "effort": 1.0,
            "reach": 1.0,
            "impact": 1.0,
            "confidence": 0.1,
            "links": {},
            "acceptance": [],
        }

        response = await authenticated_client.put(
            f"/requirements/{other_user_requirement.id}", json=update_data
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_update_requirement_not_found(self, authenticated_client):
        """Test updating non-existent requirement returns 404."""
        update_data = {
            "title": "Does Not Exist",
            "description": "Testing 404",
            "effort": 1.0,
            "reach": 1.0,
            "impact": 1.0,
            "confidence": 0.1,
            "links": {},
            "acceptance": [],
        }

        response = await authenticated_client.put(
            "/requirements/00000000-0000-7000-0000-000000000000", json=update_data
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_update_requirement_invalid_effort_bounds(
        self, authenticated_client, sample_requirement
    ):
        """Test validation error for effort outside bounds (0.001 to 999999)."""
        # Test effort too small
        update_data = {
            "title": "Test",
            "description": "Test",
            "effort": 0.0,  # Below minimum 0.001
            "reach": 100.0,
            "impact": 5.0,
            "confidence": 0.8,
            "links": {},
            "acceptance": [],
        }

        response = await authenticated_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

        # Test effort too large
        update_data["effort"] = 1000000  # Above maximum 999999

        response = await authenticated_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

    @pytest.mark.asyncio
    async def test_update_requirement_invalid_reach_bounds(
        self, authenticated_client, sample_requirement
    ):
        """Test validation error for reach outside bounds (0 to 999999999)."""
        update_data = {
            "title": "Test",
            "description": "Test",
            "effort": 5.0,
            "reach": -1.0,  # Below minimum 0
            "impact": 5.0,
            "confidence": 0.8,
            "links": {},
            "acceptance": [],
        }

        response = await authenticated_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

    @pytest.mark.asyncio
    async def test_update_requirement_invalid_impact_bounds(
        self, authenticated_client, sample_requirement
    ):
        """Test validation error for impact outside bounds (0 to 10)."""
        update_data = {
            "title": "Test",
            "description": "Test",
            "effort": 5.0,
            "reach": 100.0,
            "impact": 11.0,  # Above maximum 10
            "confidence": 0.8,
            "links": {},
            "acceptance": [],
        }

        response = await authenticated_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

    @pytest.mark.asyncio
    async def test_update_requirement_invalid_confidence_bounds(
        self, authenticated_client, sample_requirement
    ):
        """Test validation error for confidence outside bounds (0.001 to 1)."""
        # Test confidence too small
        update_data = {
            "title": "Test",
            "description": "Test",
            "effort": 5.0,
            "reach": 100.0,
            "impact": 5.0,
            "confidence": 0.0,  # Below minimum 0.001
            "links": {},
            "acceptance": [],
        }

        response = await authenticated_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

        # Test confidence too large
        update_data["confidence"] = 1.5  # Above maximum 1.0

        response = await authenticated_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

    @pytest.mark.asyncio
    async def test_update_requirement_invalid_version_refs_self_reference(
        self, authenticated_client, sample_requirement
    ):
        """Test validation error when supersedes references own slug."""
        update_data = {
            "title": "Test",
            "description": "Test",
            "effort": 5.0,
            "reach": 100.0,
            "impact": 5.0,
            "confidence": 0.8,
            "links": {},
            "acceptance": [],
            "supersedes": sample_requirement.slug,  # Self-reference not allowed (string, not list)
        }

        response = await authenticated_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE
        # The route raises HTTPException(422, "...cannot reference self...")
        # so the canonical envelope's `error.message` carries the reason
        # directly (handler code is HTTP_422, not the pydantic VALIDATION_ERROR
        # path). Tolerate either landing site for the substring.
        body = response.json()
        message = (body["error"].get("message") or "").lower()
        details = str(body["error"].get("details") or "").lower()
        assert "cannot reference self" in message or "cannot reference self" in details

    @pytest.mark.asyncio
    async def test_update_requirement_invalid_license_token(self, test_client, sample_requirement):
        """Test updating requirement with invalid license token fails."""
        update_data = {
            "title": "Should Fail",
            "description": "No auth",
            "effort": 5.0,
            "reach": 100.0,
            "impact": 5.0,
            "confidence": 0.8,
            "links": {},
            "acceptance": [],
        }

        response = await test_client.put(
            f"/requirements/{sample_requirement.id}",
            json=update_data,
            headers={"X-Tenant-Id": "Bearer invalid-token"},
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestPrincipleUpdateEndpoint:
    """Test PUT /principles/{pid} endpoint."""

    @pytest.mark.asyncio
    async def test_update_principle_success(self, authenticated_client, sample_principle):
        """Test successfully updating all fields of a principle."""
        update_data = {
            "title": "Updated Principle Title",
            "statement": "Updated statement about testing",
            "rationale": "Updated rationale for better clarity",
            "implications": ["New implication 1", "New implication 2"],
            "tags": ["updated", "testing", "quality"],
            "active": False,
            "version": "2.1.0",
            "version_status": "published",
            "version_notes": "Updated with new implications",
            "approved_by": "tech-lead@example.com",
        }

        response = await authenticated_client.put(
            f"/principles/{sample_principle.id}", json=update_data
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Updated Principle Title"
        assert data["statement"] == "Updated statement about testing"
        assert data["rationale"] == "Updated rationale for better clarity"
        assert data["implications"] == ["New implication 1", "New implication 2"]
        assert data["tags"] == ["updated", "testing", "quality"]
        assert data["active"] is False
        assert data["version"] == "2.1.0"
        assert data["tenant_id"] == T("user-123")  # Should remain unchanged

    @pytest.mark.asyncio
    async def test_update_principle_toggle_active(self, authenticated_client, sample_principle):
        """Test updating only the active status."""
        # Get current state
        get_response = await authenticated_client.get(f"/principles/{sample_principle.id}")
        original_data = get_response.json()

        # Toggle active status
        update_data = {
            "title": original_data["title"],
            "statement": original_data["statement"],
            "rationale": original_data.get("rationale"),
            "implications": original_data.get("implications", []),
            "tags": original_data.get("tags", []),
            "active": not original_data["active"],
        }

        response = await authenticated_client.put(
            f"/principles/{sample_principle.id}", json=update_data
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["active"] != original_data["active"]
        assert data["title"] == original_data["title"]
        assert data["statement"] == original_data["statement"]

    @pytest.mark.asyncio
    async def test_update_principle_ownership_validation(
        self, authenticated_client, other_user_principle
    ):
        """Test cannot update another user's principle (403 Forbidden)."""
        update_data = {
            "title": "Trying to Hijack Principle",
            "statement": "Should not work",
            "implications": [],
            "tags": [],
        }

        response = await authenticated_client.put(
            f"/principles/{other_user_principle.id}", json=update_data
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_update_principle_not_found(self, authenticated_client):
        """Test updating non-existent principle returns 404."""
        update_data = {
            "title": "Does Not Exist",
            "statement": "Testing 404",
            "implications": [],
            "tags": [],
        }

        response = await authenticated_client.put(
            "/principles/00000000-0000-7000-0000-000000000000", json=update_data
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_update_principle_invalid_version_refs_self_reference(
        self, authenticated_client, sample_principle
    ):
        """Test validation error when superseded_by references own slug."""
        update_data = {
            "title": "Test",
            "statement": "Test statement",
            "implications": [],
            "tags": [],
            "superseded_by": sample_principle.slug,  # Self-reference not allowed (string, not list)
        }

        response = await authenticated_client.put(
            f"/principles/{sample_principle.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE
        # The route raises HTTPException(422, "...cannot reference self...")
        # so the canonical envelope's `error.message` carries the reason
        # directly (handler code is HTTP_422, not the pydantic VALIDATION_ERROR
        # path). Tolerate either landing site for the substring.
        body = response.json()
        message = (body["error"].get("message") or "").lower()
        details = str(body["error"].get("details") or "").lower()
        assert "cannot reference self" in message or "cannot reference self" in details

    @pytest.mark.asyncio
    async def test_update_principle_empty_title_validation(
        self, authenticated_client, sample_principle
    ):
        """Test validation error for empty title."""
        update_data = {
            "title": "",  # Empty title should fail
            "statement": "Valid statement",
            "implications": [],
            "tags": [],
        }

        response = await authenticated_client.put(
            f"/principles/{sample_principle.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

    @pytest.mark.asyncio
    async def test_update_principle_empty_statement_validation(
        self, authenticated_client, sample_principle
    ):
        """Test validation error for empty statement."""
        update_data = {
            "title": "Valid Title",
            "statement": "",  # Empty statement should fail
            "implications": [],
            "tags": [],
        }

        response = await authenticated_client.put(
            f"/principles/{sample_principle.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

    @pytest.mark.asyncio
    async def test_update_principle_invalid_license_token(self, test_client, sample_principle):
        """Test updating principle with invalid license token fails."""
        update_data = {
            "title": "Should Fail",
            "statement": "No auth",
            "implications": [],
            "tags": [],
        }

        response = await test_client.put(
            f"/principles/{sample_principle.id}",
            json=update_data,
            headers={"X-Tenant-Id": "Bearer invalid-token"},
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestDecisionUpdateEndpoint:
    """Test PUT /decisions/{did} endpoint."""

    @pytest.mark.asyncio
    async def test_update_decision_success(self, authenticated_client, sample_decision):
        """Test successfully updating all fields of a decision."""
        update_data = {
            "title": "Updated Decision Title",
            "project_id": "updated-project",
            "status": "accepted",
            "context": "Updated context for the decision",
            "decision": "Updated decision with more details",
            "consequences": "Updated consequences and impacts",
            "alternatives": ["Alternative A", "Alternative B", "Alternative C"],
            "related_principles": [str(uuid7()), str(uuid7())],
            "version": "3.0.0",
            "version_status": "PUBLISHED",  # Uppercase
            "version_notes": "Major revision after review",
            "approved_by": "cto@example.com",
        }

        response = await authenticated_client.put(
            f"/decisions/{sample_decision.id}", json=update_data
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Updated Decision Title"
        assert data["project_id"] == "updated-project"
        assert data["status"] == "accepted"
        assert data["context"] == "Updated context for the decision"
        assert data["decision"] == "Updated decision with more details"
        assert data["consequences"] == "Updated consequences and impacts"
        assert data["alternatives"] == [
            "Alternative A",
            "Alternative B",
            "Alternative C",
        ]
        assert len(data["related_principles"]) == 2
        assert data["version"] == "3.0.0"
        assert data["tenant_id"] == T("user-123")  # Should remain unchanged

    @pytest.mark.asyncio
    async def test_update_decision_change_status(self, authenticated_client, sample_decision):
        """Test updating decision status from proposed to accepted."""
        # Get current state
        get_response = await authenticated_client.get(f"/decisions/{sample_decision.id}")
        original_data = get_response.json()

        # Change status to deprecated
        update_data = {
            "title": original_data["title"],
            "project_id": original_data.get("project_id"),
            "status": "deprecated",
            "context": original_data["context"],
            "decision": original_data["decision"],
            "consequences": original_data.get("consequences"),
            "alternatives": original_data.get("alternatives", []),
            "related_principles": original_data.get("related_principles", []),
        }

        response = await authenticated_client.put(
            f"/decisions/{sample_decision.id}", json=update_data
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "deprecated"
        assert data["title"] == original_data["title"]

    @pytest.mark.asyncio
    async def test_update_decision_ownership_validation(
        self, authenticated_client, other_user_decision
    ):
        """Test cannot update another user's decision (403 Forbidden)."""
        update_data = {
            "title": "Trying to Hijack Decision",
            "context": "Should not work",
            "decision": "Hacking attempt",
            "status": "accepted",
            "alternatives": [],
            "related_principles": [],
        }

        response = await authenticated_client.put(
            f"/decisions/{other_user_decision.id}", json=update_data
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_update_decision_not_found(self, authenticated_client):
        """Test updating non-existent decision returns 404."""
        update_data = {
            "title": "Does Not Exist",
            "context": "Testing 404",
            "decision": "Test decision",
            "status": "proposed",
            "alternatives": [],
            "related_principles": [],
        }

        response = await authenticated_client.put(
            "/decisions/00000000-0000-7000-0000-000000000000", json=update_data
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_update_decision_invalid_version_refs_self_reference(
        self, authenticated_client, sample_decision
    ):
        """Test validation error when version refs reference own slug."""
        update_data = {
            "title": "Test",
            "context": "Test context",
            "decision": "Test decision",
            "status": "proposed",
            "alternatives": [],
            "related_principles": [],
            "supersedes": sample_decision.slug,  # Self-reference not allowed (string, not list)
        }

        response = await authenticated_client.put(
            f"/decisions/{sample_decision.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE
        # The route raises HTTPException(422, "...cannot reference self...")
        # so the canonical envelope's `error.message` carries the reason
        # directly (handler code is HTTP_422, not the pydantic VALIDATION_ERROR
        # path). Tolerate either landing site for the substring.
        body = response.json()
        message = (body["error"].get("message") or "").lower()
        details = str(body["error"].get("details") or "").lower()
        assert "cannot reference self" in message or "cannot reference self" in details

    @pytest.mark.asyncio
    async def test_update_decision_empty_title_validation(
        self, authenticated_client, sample_decision
    ):
        """Test validation error for empty title."""
        update_data = {
            "title": "",  # Empty title should fail
            "context": "Valid context",
            "decision": "Valid decision",
            "status": "proposed",
            "alternatives": [],
            "related_principles": [],
        }

        response = await authenticated_client.put(
            f"/decisions/{sample_decision.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

    @pytest.mark.asyncio
    async def test_update_decision_empty_context_validation(
        self, authenticated_client, sample_decision
    ):
        """Test validation error for empty context."""
        update_data = {
            "title": "Valid Title",
            "context": "",  # Empty context should fail
            "decision": "Valid decision",
            "status": "proposed",
            "alternatives": [],
            "related_principles": [],
        }

        response = await authenticated_client.put(
            f"/decisions/{sample_decision.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

    @pytest.mark.asyncio
    async def test_update_decision_empty_decision_validation(
        self, authenticated_client, sample_decision
    ):
        """Test validation error for empty decision."""
        update_data = {
            "title": "Valid Title",
            "context": "Valid context",
            "decision": "",  # Empty decision should fail
            "status": "proposed",
            "alternatives": [],
            "related_principles": [],
        }

        response = await authenticated_client.put(
            f"/decisions/{sample_decision.id}", json=update_data
        )

        assert response.status_code == HTTP_422_UNPROCESSABLE

    @pytest.mark.asyncio
    async def test_update_decision_invalid_license_token(self, test_client, sample_decision):
        """Test updating decision with invalid license token fails."""
        update_data = {
            "title": "Should Fail",
            "context": "No auth",
            "decision": "Should not work",
            "status": "proposed",
            "alternatives": [],
            "related_principles": [],
        }

        response = await test_client.put(
            f"/decisions/{sample_decision.id}",
            json=update_data,
            headers={"X-Tenant-Id": "Bearer invalid-token"},
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestCrossUserUpdateAttempts:
    """Test that users cannot update each other's items across all entity types."""

    @pytest.mark.asyncio
    async def test_user_cannot_update_others_requirement(self, test_client, sample_requirement):
        """Test user B cannot update user A's requirement."""
        # sample_requirement belongs to user-123
        headers = {"X-Tenant-Id": T("user-456")}

        update_data = {
            "title": "Hacked",
            "description": "Should fail",
            "effort": 1.0,
            "reach": 1.0,
            "impact": 1.0,
            "confidence": 0.1,
            "links": {},
            "acceptance": [],
        }

        response = await test_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data, headers=headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

        # Verify original data unchanged
        get_response = await test_client.get(
            f"/requirements/{sample_requirement.id}",
            headers={"X-Tenant-Id": T("user-123")},
        )
        assert get_response.status_code == status.HTTP_200_OK
        assert get_response.json()["title"] == "Test Requirement"  # Original title

    @pytest.mark.asyncio
    async def test_user_cannot_update_others_principle(self, test_client, sample_principle):
        """Test user B cannot update user A's principle."""
        # sample_principle belongs to user-123
        headers = {"X-Tenant-Id": T("user-456")}

        update_data = {
            "title": "Hacked Principle",
            "statement": "Should fail",
            "implications": [],
            "tags": [],
        }

        response = await test_client.put(
            f"/principles/{sample_principle.id}", json=update_data, headers=headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_user_cannot_update_others_decision(self, test_client, sample_decision):
        """Test user B cannot update user A's decision."""
        # sample_decision belongs to user-123
        headers = {"X-Tenant-Id": T("user-456")}

        update_data = {
            "title": "Hacked Decision",
            "context": "Should fail",
            "decision": "Hack attempt",
            "status": "accepted",
            "alternatives": [],
            "related_principles": [],
        }

        response = await test_client.put(
            f"/decisions/{sample_decision.id}", json=update_data, headers=headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
