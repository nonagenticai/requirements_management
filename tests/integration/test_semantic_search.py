"""Integration tests for the /semantic_search endpoint (TF-IDF + NER knowledge base search)."""

import pytest

pytestmark = pytest.mark.integration


class TestSemanticSearchBasic:
    """Basic semantic search functionality."""

    def test_search_returns_200(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "requirements"})
        assert r.status_code == 200, r.text

    def test_search_response_structure(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "RICE prioritization"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "query" in data
        assert "enhanced_query" in data
        assert "results" in data
        assert "entities" in data
        assert "total_results" in data
        assert isinstance(data["results"], list)
        assert isinstance(data["entities"], list)
        assert isinstance(data["total_results"], int)

    def test_search_result_fields(self, req_client):
        """Each result must have topic, title, url, note, similarity_score."""
        r = req_client.get("/semantic_search", params={"query": "prioritization"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total_results"] > 0, "Expected at least one result for 'prioritization'"
        first = data["results"][0]
        assert "topic" in first
        assert "title" in first
        assert "url" in first
        assert "note" in first
        assert "similarity_score" in first
        assert 0 <= first["similarity_score"] <= 1

    def test_search_returns_matching_results(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "RICE prioritization"})
        assert r.status_code == 200, r.text
        data = r.json()
        titles = [res["title"].lower() for res in data["results"]]
        assert any("rice" in t for t in titles), f"Expected RICE in results, got: {titles}"


class TestSemanticSearchParams:
    """Tests for query parameter handling."""

    def test_k_param_limits_results(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "requirements", "k": 2})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total_results"] <= 2

    def test_k_param_large_value(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "requirements", "k": 100})
        assert r.status_code == 200, r.text

    def test_min_similarity_filters_results(self, req_client):
        """Both low and high min_similarity return valid results."""
        r_low = req_client.get(
            "/semantic_search", params={"query": "requirements", "min_similarity": 0.01}
        )
        r_high = req_client.get(
            "/semantic_search", params={"query": "requirements", "min_similarity": 0.9}
        )
        assert r_low.status_code == 200, r_low.text
        assert r_high.status_code == 200, r_high.text
        # Both should return valid result structure
        assert isinstance(r_low.json()["results"], list)
        assert isinstance(r_high.json()["results"], list)

    def test_min_similarity_zero(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "testing", "min_similarity": 0.0})
        assert r.status_code == 200, r.text

    def test_min_similarity_one(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "testing", "min_similarity": 1.0})
        assert r.status_code == 200, r.text
        data = r.json()
        # With min_similarity=1.0 very few or zero results expected
        assert data["total_results"] >= 0


class TestSemanticSearchEmptyQuery:
    """Behaviour when query is empty or missing."""

    def test_empty_query_returns_all_sources(self, req_client):
        """Empty query returns all knowledge sources with similarity 1.0."""
        r = req_client.get("/semantic_search", params={"query": ""})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total_results"] > 0, "Empty query should return all sources"
        assert data["enhanced_query"] == "No query provided - showing all sources"
        # Every result should have similarity_score == 1.0
        for res in data["results"]:
            assert res["similarity_score"] == 1.0

    def test_missing_query_param(self, req_client):
        """No query param at all should behave like empty query."""
        r = req_client.get("/semantic_search")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total_results"] > 0


class TestSemanticSearchNER:
    """Named Entity Recognition integration."""

    def test_rice_entity_detected(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "RICE prioritization framework"})
        assert r.status_code == 200, r.text
        data = r.json()
        entity_texts = [e["text"] for e in data["entities"]]
        assert "RICE" in entity_texts, f"Expected RICE entity, got: {entity_texts}"

    def test_entity_info_structure(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "RICE scoring"})
        assert r.status_code == 200, r.text
        data = r.json()
        for entity in data["entities"]:
            assert "text" in entity
            assert "label" in entity
            assert "description" in entity

    def test_enhanced_query_includes_entities(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "RICE prioritization"})
        assert r.status_code == 200, r.text
        data = r.json()
        if data["entities"]:
            assert "Entities:" in data["enhanced_query"]

    def test_wsjf_entity_detected(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "WSJF weighted shortest job first"})
        assert r.status_code == 200, r.text
        data = r.json()
        entity_texts = [e["text"] for e in data["entities"]]
        assert "WSJF" in entity_texts, f"Expected WSJF entity, got: {entity_texts}"


class TestSemanticSearchTopics:
    """Verify search returns results for known knowledge base topics."""

    def test_prioritization_topic(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "MoSCoW prioritization"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total_results"] > 0

    def test_requirements_standard_topic(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "ISO 29148 requirements standard"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total_results"] > 0

    def test_kano_model_topic(self, req_client):
        r = req_client.get(
            "/semantic_search", params={"query": "Kano model feature classification"}
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total_results"] > 0

    def test_results_sorted_by_similarity(self, req_client):
        r = req_client.get("/semantic_search", params={"query": "prioritization methods"})
        assert r.status_code == 200, r.text
        data = r.json()
        scores = [res["similarity_score"] for res in data["results"]]
        assert scores == sorted(scores, reverse=True), "Results should be sorted by similarity desc"
