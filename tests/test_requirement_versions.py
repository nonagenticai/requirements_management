"""Tests for requirement version history endpoints.

Requirements gained `/{rid}/versions` parity with principles and decisions.
Every create/update records a snapshot in entity_version_history.
"""

import pytest


class TestRequirementVersionHistory:
    @pytest.mark.asyncio
    async def test_create_records_version_1(self, authenticated_client):
        """Creating a requirement records version 1 snapshot."""
        resp = await authenticated_client.post(
            "/requirements",
            json={
                "title": "Versioned Requirement",
                "description": "desc",
                "effort": 1.0,
            },
        )
        assert resp.status_code == 200
        rid = resp.json()["id"]

        resp = await authenticated_client.get(f"/requirements/{rid}/versions")
        assert resp.status_code == 200
        versions = resp.json()
        assert len(versions) == 1
        assert versions[0]["version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_update_records_additional_snapshot(self, authenticated_client):
        """Updating a requirement records an additional snapshot in order."""
        resp = await authenticated_client.post(
            "/requirements",
            json={
                "title": "Update Me",
                "description": "orig",
                "effort": 2.0,
            },
        )
        assert resp.status_code == 200
        rid = resp.json()["id"]

        resp = await authenticated_client.put(
            f"/requirements/{rid}",
            json={
                "title": "Updated Title",
                "description": "updated desc",
                "effort": 3.0,
                "version": "1.1.0",
            },
        )
        assert resp.status_code == 200

        resp = await authenticated_client.get(f"/requirements/{rid}/versions")
        assert resp.status_code == 200
        versions = resp.json()
        assert len(versions) == 2
        returned = {v["version"] for v in versions}
        assert returned == {"1.0.0", "1.1.0"}

    @pytest.mark.asyncio
    async def test_get_version_info(self, authenticated_client, sample_requirement):
        """GET /{rid}/version returns current version info."""
        resp = await authenticated_client.get(f"/requirements/{sample_requirement.id}/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == sample_requirement.version
        assert data["version_status"] == sample_requirement.version_status

    @pytest.mark.asyncio
    async def test_versions_requires_ownership(self, authenticated_client, other_user_requirement):
        """Cannot list versions of a requirement owned by another user."""
        resp = await authenticated_client.get(f"/requirements/{other_user_requirement.id}/versions")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_compare_invalid_versions_returns_422(self, authenticated_client):
        """Comparing with invalid version strings returns 422."""
        resp = await authenticated_client.post(
            "/requirements",
            json={"title": "Cmp", "description": "d", "effort": 1.0},
        )
        rid = resp.json()["id"]
        resp = await authenticated_client.get(
            f"/requirements/{rid}/versions/not..valid/compare/also..bad"
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_principle_version_history_ordered(self, authenticated_client):
        """Principle update records a second snapshot (regression coverage)."""
        resp = await authenticated_client.post(
            "/principles",
            json={"title": "P1", "statement": "s1"},
        )
        assert resp.status_code == 200
        pid = resp.json()["id"]

        resp = await authenticated_client.put(
            f"/principles/{pid}",
            json={
                "title": "P1 updated",
                "statement": "s1 updated",
                "version": "1.1.0",
            },
        )
        assert resp.status_code == 200

        resp = await authenticated_client.get(f"/principles/{pid}/versions")
        assert resp.status_code == 200
        versions = resp.json()
        assert len(versions) == 2

    @pytest.mark.asyncio
    async def test_decision_version_history_ordered(self, authenticated_client):
        """Decision update records a second snapshot (regression coverage)."""
        resp = await authenticated_client.post(
            "/decisions",
            json={"title": "D1", "context": "c1", "decision": "d1"},
        )
        assert resp.status_code == 200
        did = resp.json()["id"]

        resp = await authenticated_client.put(
            f"/decisions/{did}",
            json={
                "title": "D1 updated",
                "context": "c1 updated",
                "decision": "d1 updated",
                "version": "1.1.0",
            },
        )
        assert resp.status_code == 200

        resp = await authenticated_client.get(f"/decisions/{did}/versions")
        assert resp.status_code == 200
        versions = resp.json()
        assert len(versions) == 2
