"""Tests for endpoint authorization and tenant-based access control.

The auth middleware admits only a licence (``X-Tenant-Id: Bearer <token>`` or
``Authorization: Bearer``) → introspect. No token ⇒ 401, from any peer.

A plain ``X-Tenant-Id: <t>`` in these tests is turned into a test licence for
tenant ``t`` by the conftest shim, so we test:
- Tenant isolation (different X-Tenant-Id values)
- Invalid license tokens (Bearer prefix triggers introspection → 401)
- Public endpoint access
"""

import pytest
from fastapi import status

from tests.tenants import T

pytestmark = pytest.mark.usefixtures("licences_reject_unknown_tokens")


class TestRequirementEndpointsAuth:
    """Test authentication and authorization for requirement endpoints."""

    @pytest.mark.asyncio
    async def test_create_requirement_with_tenant_header(self, test_client):
        """Test creating a requirement with X-Tenant-Id header succeeds."""
        headers = {"X-Tenant-Id": T("user-123")}

        requirement_data = {
            "title": "Authenticated Requirement",
            "description": "Created with tenant header",
            "effort": 5.0,
            "reach": 100.0,
            "impact": 3.0,
            "confidence": 0.8,
            "links": {},
            "acceptance": [],
        }

        response = await test_client.post("/requirements", json=requirement_data, headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Authenticated Requirement"
        assert data["description"] == "Created with tenant header"
        assert data["tenant_id"] == T("user-123")
        assert data["effort"] == 5.0

    @pytest.mark.asyncio
    async def test_create_requirement_without_a_token_is_401(self, test_client):
        """No licence ⇒ 401 from loopback too; there is no default-tenant fallback."""
        requirement_data = {
            "title": "Default Tenant Requirement",
            "description": "No credential at all",
            "effort": 5.0,
            "links": {},
            "acceptance": [],
        }

        response = await test_client.post("/requirements", json=requirement_data)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_create_requirement_invalid_license_token(self, test_client):
        """Test that invalid license token in X-Tenant-Id is rejected."""
        headers = {"X-Tenant-Id": "Bearer invalid-token-here"}

        requirement_data = {
            "title": "Bad Token Request",
            "description": "Should fail",
            "effort": 5.0,
            "links": {},
            "acceptance": [],
        }

        response = await test_client.post("/requirements", json=requirement_data, headers=headers)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_get_requirements_only_returns_own_data(
        self, test_client, sample_requirement, other_user_requirement
    ):
        """Test that GET /requirements only returns the authenticated user's data."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.get("/requirements", headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        requirements = data if isinstance(data, list) else data.get("items", [])

        tenant_ids = [req.get("tenant_id") for req in requirements]
        # Non-empty FIRST: an over-filtering regression that returned [] for every
        # tenant would satisfy the loop below without ever comparing anything.
        assert len(tenant_ids) >= 1
        # Should only contain user-123's requirements
        for cid in tenant_ids:
            assert cid == T("user-123")

    @pytest.mark.asyncio
    async def test_cannot_access_other_creator_data(self, test_client, other_user_requirement):
        """Test that users cannot access requirements created by others."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.get(
            f"/requirements/{other_user_requirement.id}", headers=headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_update_own_requirement_success(self, test_client, sample_requirement):
        """Test successfully updating own requirement."""
        headers = {"X-Tenant-Id": T("user-123")}

        update_data = {
            "title": "Updated Title",
            "description": sample_requirement.description,
            "effort": 10.0,
            "links": {},
            "acceptance": [],
        }

        response = await test_client.put(
            f"/requirements/{sample_requirement.id}", json=update_data, headers=headers
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Updated Title"
        assert data["effort"] == 10.0
        assert data["tenant_id"] == T("user-123")

    @pytest.mark.asyncio
    async def test_update_other_creator_requirement_forbidden(
        self, test_client, other_user_requirement
    ):
        """Test that updating another user's requirement is forbidden."""
        headers = {"X-Tenant-Id": T("user-123")}

        update_data = {
            "title": "Trying to update other's requirement",
            "description": "Should fail",
            "effort": 5.0,
            "links": {},
            "acceptance": [],
        }

        response = await test_client.put(
            f"/requirements/{other_user_requirement.id}",
            json=update_data,
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_delete_own_requirement_success(self, test_client, sample_requirement):
        """Test successfully deleting own requirement."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.delete(
            f"/requirements/{sample_requirement.id}", headers=headers
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "deleted"

        get_response = await test_client.get(
            f"/requirements/{sample_requirement.id}", headers=headers
        )
        assert get_response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_delete_other_creator_requirement_forbidden(
        self, test_client, other_user_requirement
    ):
        """Test that deleting another user's requirement is forbidden."""
        headers = {"X-Tenant-Id": T("user-123")}

        response = await test_client.delete(
            f"/requirements/{other_user_requirement.id}", headers=headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestPrincipleEndpointsAuth:
    """Test authentication and authorization for principle endpoints."""

    @pytest.mark.asyncio
    async def test_create_principle_with_tenant_header(self, test_client):
        """Test creating a principle with X-Tenant-Id header."""
        headers = {"X-Tenant-Id": T("user-123")}

        principle_data = {
            "title": "Authenticated Principle",
            "statement": "Test principle statement",
            "rationale": "Testing auth",
            "implications": ["Test implication"],
            "tags": ["test"],
            "active": True,
        }

        response = await test_client.post("/principles", json=principle_data, headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Authenticated Principle"
        assert data["tenant_id"] == T("user-123")

    @pytest.mark.asyncio
    async def test_create_principle_invalid_license_token(self, test_client):
        """Test creating a principle with invalid license token fails."""
        headers = {"X-Tenant-Id": "Bearer invalid-token"}

        principle_data = {
            "title": "Unauthenticated Principle",
            "statement": "Should fail",
            "implications": [],
            "tags": [],
        }

        response = await test_client.post("/principles", json=principle_data, headers=headers)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_get_principles_with_tenant_header(self, test_client, sample_principle):
        """Test that GET /principles works with tenant header."""
        headers = {"X-Tenant-Id": T("user-123")}
        response = await test_client.get("/principles", headers=headers)

        assert response.status_code == status.HTTP_200_OK


class TestDecisionEndpointsAuth:
    """Test authentication and authorization for decision endpoints."""

    @pytest.mark.asyncio
    async def test_create_decision_with_tenant_header(self, test_client):
        """Test creating a decision with X-Tenant-Id header."""
        headers = {"X-Tenant-Id": T("user-123")}

        decision_data = {
            "title": "Authenticated Decision",
            "context": "Test context",
            "decision": "Test decision",
            "status": "proposed",
            "alternatives": [],
            "related_principles": [],
        }

        response = await test_client.post("/decisions", json=decision_data, headers=headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Authenticated Decision"
        assert data["tenant_id"] == T("user-123")

    @pytest.mark.asyncio
    async def test_create_decision_invalid_license_token(self, test_client):
        """Test creating a decision with invalid license token fails."""
        headers = {"X-Tenant-Id": "Bearer invalid-token"}

        decision_data = {
            "title": "Unauthenticated Decision",
            "context": "Should fail",
            "decision": "Should fail",
            "status": "proposed",
            "alternatives": [],
            "related_principles": [],
        }

        response = await test_client.post("/decisions", json=decision_data, headers=headers)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_get_decisions_with_tenant_header(self, test_client, sample_decision):
        """Test that GET /decisions works with tenant header."""
        headers = {"X-Tenant-Id": T("user-123")}
        response = await test_client.get("/decisions", headers=headers)

        assert response.status_code == status.HTTP_200_OK


class TestPublicEndpoints:
    """Test that public endpoints don't require authentication."""

    @pytest.mark.asyncio
    async def test_health_endpoint_public(self, test_client):
        """Test that /health endpoint is publicly accessible."""
        response = await test_client.get("/health")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/", "/docs", "/redoc"])
    async def test_root_and_docs_require_a_licence(self, test_client, path):
        """Only the probes and /openapi.json are public; /, /docs, /redoc are not."""
        response = await test_client.get(path)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_root_with_a_licence_redirects_to_docs(self, test_client):
        """With a (test) licence, / redirects to /docs and /docs is served."""
        headers = {"X-Tenant-Id": T("user-123")}
        redirect = await test_client.get("/", headers=headers)
        assert redirect.status_code == status.HTTP_307_TEMPORARY_REDIRECT
        assert redirect.headers["location"] == "/docs"

        followed = await test_client.get("/", headers=headers, follow_redirects=True)
        assert followed.status_code == status.HTTP_200_OK

    @pytest.mark.asyncio
    async def test_openapi_endpoint_public(self, test_client):
        """Test that /openapi.json endpoint is publicly accessible."""
        response = await test_client.get("/openapi.json")

        assert response.status_code == status.HTTP_200_OK


class TestMultiUserScenarios:
    """Test scenarios involving multiple users."""

    @pytest.mark.asyncio
    async def test_different_users_see_own_data(self, test_client, test_session):
        """Test that different users only see their own requirements."""
        from models.requirements import Requirement

        req1 = Requirement(
            slug="user1-req",
            title="User 1 Requirement",
            description="Owned by user 1",
            tenant_id=T("user-111"),
            links={},
            acceptance=[],
            effort=5.0,
        )
        req2 = Requirement(
            slug="user2-req",
            title="User 2 Requirement",
            description="Owned by user 2",
            tenant_id=T("user-222"),
            links={},
            acceptance=[],
            effort=3.0,
        )
        test_session.add_all([req1, req2])
        await test_session.commit()

        # Check user-111 only sees their data
        response1 = await test_client.get("/requirements", headers={"X-Tenant-Id": T("user-111")})
        assert response1.status_code == status.HTTP_200_OK
        data1 = response1.json()
        reqs1 = data1 if isinstance(data1, list) else data1.get("items", [])
        assert len(reqs1) >= 1, "empty for both tenants would pass this isolation check vacuously"
        for req in reqs1:
            assert req["tenant_id"] == T("user-111")

        # Check user-222 only sees their data
        response2 = await test_client.get("/requirements", headers={"X-Tenant-Id": T("user-222")})
        assert response2.status_code == status.HTTP_200_OK
        data2 = response2.json()
        reqs2 = data2 if isinstance(data2, list) else data2.get("items", [])
        assert len(reqs2) >= 1, "empty for both tenants would pass this isolation check vacuously"
        for req in reqs2:
            assert req["tenant_id"] == T("user-222")

    @pytest.mark.asyncio
    async def test_user_cannot_modify_another_users_requirement(self, test_client, test_session):
        """Test that user A cannot modify user B's requirement."""
        from models.requirements import Requirement

        req_b = Requirement(
            slug="user-b-req",
            title="User B Requirement",
            description="Owned by user B",
            tenant_id=T("user-bbb"),
            links={},
            acceptance=[],
            effort=5.0,
        )
        test_session.add(req_b)
        await test_session.commit()
        await test_session.refresh(req_b)

        update_data = {
            "title": "Hacked Title",
            "description": "User A trying to modify",
            "effort": 10.0,
            "links": {},
            "acceptance": [],
        }

        response = await test_client.put(
            f"/requirements/{req_b.id}",
            json=update_data,
            headers={"X-Tenant-Id": T("user-aaa")},
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestInvalidLicenseToken:
    """Test that invalid license tokens (Bearer prefix) are rejected."""

    @pytest.mark.asyncio
    async def test_invalid_bearer_token_rejected(self, test_client):
        """Test that Bearer token in X-Tenant-Id triggers introspection and fails."""
        headers = {"X-Tenant-Id": "Bearer fake-license-token"}

        response = await test_client.get("/requirements", headers=headers)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_invalid_bearer_token_cannot_create_resource(self, test_client):
        """Test that invalid Bearer token cannot create resources."""
        headers = {"X-Tenant-Id": "Bearer another-fake-token"}

        requirement_data = {
            "title": "Should Fail",
            "description": "Invalid token",
            "effort": 5.0,
            "links": {},
            "acceptance": [],
        }

        response = await test_client.post("/requirements", json=requirement_data, headers=headers)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_empty_bearer_token_is_401(self, test_client):
        """An empty Bearer token (stripped to empty) is no token ⇒ 401."""
        headers = {"X-Tenant-Id": "Bearer "}

        response = await test_client.get("/requirements", headers=headers)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
