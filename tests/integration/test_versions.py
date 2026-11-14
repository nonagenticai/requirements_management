"""Integration tests for version endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestPrincipleVersions:
    def test_get_version(self, req_client, principle_data):
        r = req_client.get(f"/principles/{principle_data['id']}/version")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    def test_get_version_has_fields(self, req_client, principle_data):
        r = req_client.get(f"/principles/{principle_data['id']}/version")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert isinstance(data, dict)
        assert "version" in data or "id" in data, (
            f"Version response missing expected fields: {list(data.keys())}"
        )


class TestPrincipleVersionHistory:
    def test_versions_after_update(self, req_client, principle_data):
        # Update the principle
        update_payload = {
            "title": principle_data.get("title", "Updated") + " v2",
            "statement": "Updated statement",
            "rationale": "Updated rationale",
            "implications": ["Updated implication"],
            "tags": ["integration-test"],
            "version": "2.0.0",
            "version_status": "DRAFT",
        }
        req_client.put(f"/principles/{principle_data['id']}", json=update_payload)

        # Check versions list
        r = req_client.get(f"/principles/{principle_data['id']}/versions")
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1  # At least current version

    def test_version_comparison(self, req_client, principle_data):
        # Update the principle to create a second version
        update_payload = {
            "title": principle_data.get("title", "Updated") + " v3",
            "statement": "Statement for comparison",
            "rationale": "Rationale for comparison",
            "implications": ["Comparison implication"],
            "tags": ["integration-test"],
            "version": "3.0.0",
            "version_status": "DRAFT",
        }
        r_update = req_client.put(f"/principles/{principle_data['id']}", json=update_payload)
        assert r_update.status_code == 200, f"Update failed: {r_update.text}"

        # Get versions list to find version IDs
        r = req_client.get(f"/principles/{principle_data['id']}/versions")
        assert r.status_code == 200, r.text
        versions = r.json()
        assert len(versions) >= 2, f"Expected at least 2 versions after update, got {len(versions)}"

        v1 = versions[-1].get("version", versions[-1].get("id"))
        v2 = versions[0].get("version", versions[0].get("id"))
        # Comparing a version with itself would satisfy every assertion below
        # while proving nothing, so pin that the two ends really differ.
        assert v1 != v2, f"need two distinct versions to compare, got {v1!r} twice"

        # The compare endpoint takes the two versions as PATH segments.
        #
        # This assertion used to request `/versions/compare?version_a=&version_b=`,
        # which matches NO route, so FastAPI answered 404 and the test skipped
        # itself with "Version comparison endpoint not implemented" — forever, on
        # every run, while the endpoint was fully implemented the whole time
        # (src/routers/principles.py, src/services/version_service.py::compare).
        # The old assertion was wrong twice over: it also looked for
        # "changes"/"diff"/"version_a", none of which VersionComparison
        # (src/schemas/common.py) has ever returned.
        r = req_client.get(f"/principles/{principle_data['id']}/versions/{v1}/compare/{v2}")
        assert r.status_code == 200, f"Version compare failed: {r.status_code} {r.text}"
        data = r.json()
        assert data["old_version"] == v1, f"old_version: {data!r}"
        assert data["new_version"] == v2, f"new_version: {data!r}"
        assert set(data) >= {
            "old_version",
            "new_version",
            "is_breaking_change",
            "comparison_result",
            "version_diff",
        }, f"Comparison response missing expected fields: {sorted(data)}"
        # A diff that computed nothing would still carry every key above. The
        # update changed statement/rationale/implications/title, so the diff has
        # to name at least one of them.
        assert data["version_diff"], f"version_diff empty despite an update: {data!r}"
        assert "statement" in data["version_diff"], (
            f"version_diff did not notice the changed statement: {data['version_diff']!r}"
        )


class TestDecisionVersions:
    def test_get_version(self, req_client, decision_data):
        r = req_client.get(f"/decisions/{decision_data['id']}/version")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    def test_get_version_has_fields(self, req_client, decision_data):
        r = req_client.get(f"/decisions/{decision_data['id']}/version")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert isinstance(data, dict)
        assert "version" in data or "id" in data, (
            f"Version response missing expected fields: {list(data.keys())}"
        )
