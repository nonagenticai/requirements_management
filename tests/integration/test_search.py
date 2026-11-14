"""Integration tests for search endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestSearchRequirements:
    def test_search_returns_200(self, req_client, requirement_data):
        r = req_client.get("/search", params={"q": "integration"})
        assert r.status_code == 200, r.text

    def test_search_returns_results(self, req_client, requirement_data):
        r = req_client.get("/search", params={"q": "integration"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list), f"Expected list, got {type(data)}"
        assert len(data) > 0, (
            "Search for 'integration' should return results (requirement_data has it in description)"
        )

    def test_search_result_has_required_fields(self, req_client, requirement_data):
        r = req_client.get("/search", params={"q": "integration"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data) > 0, "Need at least one result to check fields"
        first = data[0]
        assert "id" in first
        assert "title" in first


class TestSearchPrinciples:
    def test_search_principles(self, req_client, principle_data):
        r = req_client.get("/search/principles", params={"q": "integration"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list), f"Expected list, got {type(data)}"
        assert len(data) > 0, "Search for 'integration' should find the test principle"


class TestSearchDecisions:
    def test_search_decisions(self, req_client, decision_data):
        r = req_client.get("/search/decisions", params={"q": "integration"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list), f"Expected list, got {type(data)}"
        assert len(data) > 0, "Search for 'integration' should find the test decision"


class TestSearchAll:
    def test_search_all(self, req_client, requirement_data, principle_data, decision_data):
        r = req_client.get("/search/all", params={"q": "integration"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list), f"Expected list, got {type(data)}"
        assert len(data) > 0, (
            "Search all for 'integration' should return results across entity types"
        )
        # Results should have entity_type distinguishing requirements/principles/decisions
        entity_types = {item["entity_type"] for item in data if "entity_type" in item}
        assert len(entity_types) > 0, (
            f"Results should have entity_type field, got keys: {list(data[0].keys())}"
        )

    def test_search_all_with_params(self, req_client, requirement_data):
        r = req_client.get(
            "/search/all", params={"q": "integration", "k": 5, "min_similarity": 0.1}
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list), f"Expected list, got {type(data)}"
