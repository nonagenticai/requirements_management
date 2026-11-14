"""Decision version-compare cases kept from the deleted coverage-padding suite.

These two are the only tests of ``GET /decisions/{did}/versions/{v1}/compare/{v2}``
(route-coverage gate, measured 2026-10-29), so they stay when the rest of the file
went. Real DB via ``authenticated_client``.
"""

import pytest


class TestDecisionRouterEdgeCases:
    @pytest.mark.asyncio
    async def test_decision_version_compare(self, authenticated_client):
        """GET /decisions/{id}/versions/{v1}/compare/{v2} compares snapshots."""
        resp = await authenticated_client.post(
            "/decisions",
            json={
                "title": "Compare D",
                "context": "ctx v1",
                "decision": "dec",
                "version": "1.0.0",
            },
        )
        assert resp.status_code == 200
        did = resp.json()["id"]

        resp = await authenticated_client.put(
            f"/decisions/{did}",
            json={
                "title": "Compare D Updated",
                "context": "ctx v2",
                "decision": "dec updated",
                "version": "2.0.0",
            },
        )
        assert resp.status_code == 200

        resp = await authenticated_client.get(f"/decisions/{did}/versions/1.0.0/compare/2.0.0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_breaking_change"] is True
        assert len(data["version_diff"]) > 0

    @pytest.mark.asyncio
    async def test_decision_version_compare_missing_snapshot(self, authenticated_client):
        """Version compare returns 404 when snapshot doesn't exist."""
        resp = await authenticated_client.post(
            "/decisions",
            json={"title": "Missing Snap", "context": "ctx", "decision": "dec"},
        )
        assert resp.status_code == 200
        did = resp.json()["id"]

        resp = await authenticated_client.get(f"/decisions/{did}/versions/9.0.0/compare/10.0.0")
        assert resp.status_code == 404
