"""Tests for search endpoints.

This module tests the search functionality across all entity types:
- GET /search/requirements - search requirements
- GET /search/principles - search principles
- GET /search/decisions - search decisions
- GET /search/all - unified search across all entities
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.decisions import Decision
from models.principles import Principle
from models.requirements import Requirement
from tests.tenants import T

pytestmark = pytest.mark.usefixtures("licences_reject_unknown_tokens")


@pytest_asyncio.fixture
async def search_test_data(test_session: AsyncSession) -> dict:
    """Create comprehensive test data for search functionality."""
    # Create requirements with diverse content
    requirements = [
        Requirement(
            slug="user-authentication",
            title="User Authentication System",
            description="Implement secure user authentication with JWT tokens and OAuth2 support",
            tenant_id=T("user-123"),
            project_id="test-project",
            links={},
            acceptance=["Login functionality", "Token refresh", "OAuth integration"],
            effort=8.0,
            reach=200.0,
            impact=5.0,
            confidence=0.9,
        ),
        Requirement(
            slug="api-rate-limiting",
            title="API Rate Limiting",
            description="Add rate limiting to prevent abuse and ensure fair usage of API resources",
            tenant_id=T("user-123"),
            project_id="test-project",
            links={},
            acceptance=["Rate limit headers", "429 status codes"],
            effort=5.0,
            reach=150.0,
            impact=4.0,
            confidence=0.8,
        ),
        Requirement(
            slug="data-encryption",
            title="Data Encryption at Rest",
            description="Encrypt sensitive data stored in the database using AES-256",
            tenant_id=T("user-123"),
            project_id="test-project",
            links={},
            acceptance=["Encrypted storage", "Key rotation"],
            effort=10.0,
            reach=100.0,
            impact=5.0,
            confidence=0.7,
        ),
        # Requirement owned by different user
        Requirement(
            slug="other-user-req",
            title="Other User's Requirement",
            description="This should not appear in user-123's search results",
            tenant_id=T("user-789"),
            project_id="test-project",
            links={},
            acceptance=[],
            effort=3.0,
            reach=50.0,
            impact=2.0,
            confidence=0.5,
        ),
    ]

    # Create principles
    principles = [
        Principle(
            slug="security-first",
            title="Security First",
            statement="Security must be a primary consideration in all design decisions",
            rationale="Security vulnerabilities can have severe consequences for users and business",
            implications=["Security reviews required", "Encryption by default"],
            tags=["security", "architecture"],
            tenant_id=T("user-123"),
            active=True,
        ),
        Principle(
            slug="api-design",
            title="RESTful API Design",
            statement="APIs should follow REST principles for consistency and usability",
            rationale="RESTful design makes APIs predictable and easy to use",
            implications=["Resource-based URLs", "Standard HTTP methods"],
            tags=["api", "design"],
            tenant_id=T("user-123"),
            active=True,
        ),
        Principle(
            slug="test-coverage",
            title="High Test Coverage",
            statement="All code must have comprehensive test coverage",
            rationale="Tests prevent regressions and ensure code quality",
            implications=["Unit tests required", "Integration tests"],
            tags=["testing", "quality"],
            tenant_id=T("user-123"),
            active=True,
        ),
        # Principle owned by different user
        Principle(
            slug="other-principle",
            title="Other User's Principle",
            statement="This should not appear in search results",
            rationale="Access control testing",
            implications=[],
            tags=["security"],
            tenant_id=T("user-789"),
            active=True,
        ),
    ]

    # Create decisions
    decisions = [
        Decision(
            slug="use-postgresql",
            title="Use PostgreSQL as Primary Database",
            context="We need a reliable, ACID-compliant database with good JSON support",
            decision="Selected PostgreSQL for its maturity, features, and community support",
            consequences="Team needs PostgreSQL expertise; excellent JSON and full-text search capabilities",
            alternatives=["MySQL", "MongoDB", "CockroachDB"],
            related_principles=[],
            tenant_id=T("user-123"),
            project_id="test-project",
            status="accepted",
        ),
        Decision(
            slug="jwt-authentication",
            title="JWT for API Authentication",
            context="Need stateless authentication for distributed API services",
            decision="Use JWT tokens for authentication with short expiry times",
            consequences="No server-side session storage; tokens must be carefully validated",
            alternatives=["Session cookies", "OAuth2 only"],
            related_principles=[],
            tenant_id=T("user-123"),
            project_id="test-project",
            status="accepted",
        ),
        Decision(
            slug="rate-limit-strategy",
            title="Token Bucket Rate Limiting",
            context="Need to prevent API abuse while allowing burst traffic",
            decision="Implement token bucket algorithm for rate limiting",
            consequences="Fair resource allocation; some implementation complexity",
            alternatives=["Fixed window", "Sliding window"],
            related_principles=[],
            tenant_id=T("user-123"),
            project_id="test-project",
            status="proposed",
        ),
        # Decision owned by different user
        Decision(
            slug="other-decision",
            title="Other User's Decision",
            context="This should not appear in search results",
            decision="Testing access control",
            consequences="Should be isolated by tenant_id",
            alternatives=[],
            related_principles=[],
            tenant_id=T("user-789"),
            project_id="test-project",
            status="proposed",
        ),
    ]

    for req in requirements:
        test_session.add(req)
    for prin in principles:
        test_session.add(prin)
    for dec in decisions:
        test_session.add(dec)

    await test_session.commit()

    # Refresh all objects
    for req in requirements:
        await test_session.refresh(req)
    for prin in principles:
        await test_session.refresh(prin)
    for dec in decisions:
        await test_session.refresh(dec)

    return {
        "requirements": requirements,
        "principles": principles,
        "decisions": decisions,
    }


class TestSearchRequirements:
    """Test GET /search (requirements search)."""

    @pytest.mark.asyncio
    async def test_search_requirements_basic(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Search 'authentication' MUST find 'User Authentication System'."""
        response = await authenticated_client.get("/search?q=authentication")

        assert response.status_code == 200
        results = response.json()
        assert isinstance(results, list)
        assert len(results) >= 1

        titles = [r["title"] for r in results]
        assert "User Authentication System" in titles

        for result in results:
            assert "id" in result
            assert "slug" in result
            assert "title" in result
            assert "snippet" in result
            assert "similarity" in result
            assert 0 <= result["similarity"] <= 1

    @pytest.mark.asyncio
    async def test_search_requirements_empty_query(self, authenticated_client: AsyncClient):
        """Test search with empty query returns empty results."""
        response = await authenticated_client.get("/search?q=")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_search_requirements_no_query_param(self, authenticated_client: AsyncClient):
        """Test search without query parameter."""
        response = await authenticated_client.get("/search")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_search_requirements_no_results(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Search for a nonexistent term must return empty list."""
        response = await authenticated_client.get("/search?q=nonexistent-term-xyz123")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_search_requirements_pagination(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """k=1 must return at most 1 result."""
        response = await authenticated_client.get("/search?q=authentication&k=1")
        assert response.status_code == 200
        results = response.json()
        assert len(results) <= 1
        assert len(results) >= 1  # should find the authentication requirement

    @pytest.mark.asyncio
    async def test_search_requirements_creator_isolation(
        self, test_client: AsyncClient, search_test_data: dict
    ):
        """user-123 must NOT see user-789's data, and vice versa."""
        # Search as user-123
        response = await test_client.get(
            "/search?q=requirement", headers={"X-Tenant-Id": T("user-123")}
        )
        assert response.status_code == 200
        results_123 = response.json()
        titles_123 = [r["title"] for r in results_123]
        assert "Other User's Requirement" not in titles_123

        # Search as user-789
        response = await test_client.get(
            "/search?q=requirement", headers={"X-Tenant-Id": T("user-789")}
        )
        assert response.status_code == 200
        results_789 = response.json()
        titles_789 = [r["title"] for r in results_789]
        assert "User Authentication System" not in titles_789
        assert "API Rate Limiting" not in titles_789
        assert "Data Encryption at Rest" not in titles_789

        # user-789 should find their own requirement
        assert len(results_789) >= 1
        assert "Other User's Requirement" in titles_789

    @pytest.mark.asyncio
    async def test_search_requirements_invalid_license_token(self, test_client: AsyncClient):
        """Test search with invalid license token."""
        response = await test_client.get(
            "/search?q=test", headers={"X-Tenant-Id": "Bearer invalid-token"}
        )
        assert response.status_code == 401


class TestSearchPrinciples:
    """Test GET /search/principles."""

    @pytest.mark.asyncio
    async def test_search_principles_basic(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Search 'security' MUST find 'Security First' principle."""
        response = await authenticated_client.get("/search/principles?q=security")

        assert response.status_code == 200
        results = response.json()
        assert isinstance(results, list)
        assert len(results) >= 1

        titles = [r["title"] for r in results]
        assert "Security First" in titles

        for result in results:
            assert "id" in result
            assert "slug" in result
            assert "title" in result
            assert "snippet" in result
            assert "similarity" in result
            assert 0 <= result["similarity"] <= 1

    @pytest.mark.asyncio
    async def test_search_principles_empty_query(self, authenticated_client: AsyncClient):
        """Test search with empty query returns empty results."""
        response = await authenticated_client.get("/search/principles?q=")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_search_principles_creator_isolation(
        self, test_client: AsyncClient, search_test_data: dict
    ):
        """Test that users only see their own principles."""
        response = await test_client.get(
            "/search/principles?q=principle",
            headers={"X-Tenant-Id": T("user-123")},
        )
        assert response.status_code == 200
        results = response.json()
        titles = [r["title"] for r in results]
        assert "Other User's Principle" not in titles

    @pytest.mark.asyncio
    async def test_search_principles_pagination(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Test k parameter for limiting results."""
        response = await authenticated_client.get("/search/principles?q=security&k=1")
        assert response.status_code == 200
        results = response.json()
        assert len(results) <= 1
        assert len(results) >= 1


class TestSearchDecisions:
    """Test GET /search/decisions."""

    @pytest.mark.asyncio
    async def test_search_decisions_basic(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Search 'postgresql' MUST find 'Use PostgreSQL as Primary Database'."""
        response = await authenticated_client.get("/search/decisions?q=postgresql")

        assert response.status_code == 200
        results = response.json()
        assert isinstance(results, list)
        assert len(results) >= 1

        titles = [r["title"] for r in results]
        assert "Use PostgreSQL as Primary Database" in titles

        for result in results:
            assert "id" in result
            assert "slug" in result
            assert "title" in result
            assert "snippet" in result
            assert "similarity" in result
            assert 0 <= result["similarity"] <= 1

    @pytest.mark.asyncio
    async def test_search_decisions_context_field(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Searching in decision context field should find matches."""
        response = await authenticated_client.get("/search/decisions?q=stateless")
        assert response.status_code == 200
        results = response.json()
        assert len(results) >= 1
        titles = [r["title"] for r in results]
        assert "JWT for API Authentication" in titles

    @pytest.mark.asyncio
    async def test_search_decisions_empty_query(self, authenticated_client: AsyncClient):
        """Test search with empty query returns empty results."""
        response = await authenticated_client.get("/search/decisions?q=")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_search_decisions_creator_isolation(
        self, test_client: AsyncClient, search_test_data: dict
    ):
        """Test that users only see their own decisions."""
        response = await test_client.get(
            "/search/decisions?q=decision",
            headers={"X-Tenant-Id": T("user-123")},
        )
        assert response.status_code == 200
        results = response.json()
        titles = [r["title"] for r in results]
        assert "Other User's Decision" not in titles

    @pytest.mark.asyncio
    async def test_search_decisions_pagination(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Test k parameter for limiting results."""
        response = await authenticated_client.get("/search/decisions?q=postgresql&k=1")
        assert response.status_code == 200
        results = response.json()
        assert len(results) <= 1
        assert len(results) >= 1


class TestSearchAll:
    """Test GET /search/all (unified search across all entities)."""

    @pytest.mark.asyncio
    async def test_search_all_basic(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Unified search should return results from multiple entity types."""
        response = await authenticated_client.get("/search/all?q=authentication")

        assert response.status_code == 200
        results = response.json()
        assert isinstance(results, list)
        assert len(results) >= 1

        for result in results:
            assert "entity_type" in result
            assert result["entity_type"] in ["requirement", "principle", "decision"]
            assert "id" in result
            assert "slug" in result
            assert "title" in result
            assert "snippet" in result
            assert "similarity" in result
            assert 0 <= result["similarity"] <= 1

    @pytest.mark.asyncio
    async def test_search_all_finds_all_types(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Verify all 3 entity types can appear in unified search results."""
        # Use a broad query that matches content in all three entity types
        # "api" appears in: API Rate Limiting (req), RESTful API Design (principle),
        #                    JWT for API Authentication (decision)
        response = await authenticated_client.get("/search/all?q=api")

        assert response.status_code == 200
        results = response.json()
        assert len(results) >= 3

        entity_types = {r["entity_type"] for r in results}
        assert "requirement" in entity_types
        assert "principle" in entity_types
        assert "decision" in entity_types

    @pytest.mark.asyncio
    async def test_search_all_empty_query(self, authenticated_client: AsyncClient):
        """Test search with empty query returns empty results."""
        response = await authenticated_client.get("/search/all?q=")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_search_all_creator_isolation(
        self, test_client: AsyncClient, search_test_data: dict
    ):
        """Test that unified search respects tenant_id isolation."""
        response = await test_client.get(
            "/search/all?q=user other",
            headers={"X-Tenant-Id": T("user-123")},
        )
        assert response.status_code == 200
        results = response.json()

        titles = [r["title"] for r in results]
        assert "Other User's Requirement" not in titles
        assert "Other User's Principle" not in titles
        assert "Other User's Decision" not in titles

    @pytest.mark.asyncio
    async def test_search_all_pagination(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Test k parameter for limiting results."""
        response = await authenticated_client.get("/search/all?q=api&k=2")
        assert response.status_code == 200
        results = response.json()
        assert len(results) <= 2
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_all_sorting(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Results must be sorted by similarity descending."""
        response = await authenticated_client.get("/search/all?q=security encryption")
        assert response.status_code == 200
        results = response.json()
        assert len(results) >= 1

        similarities = [r["similarity"] for r in results]
        assert similarities == sorted(similarities, reverse=True)

    @pytest.mark.asyncio
    async def test_search_all_snippet_truncation(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Test that snippets are truncated to 240 characters."""
        response = await authenticated_client.get("/search/all?q=database")
        assert response.status_code == 200
        results = response.json()
        assert len(results) >= 1

        for result in results:
            assert len(result["snippet"]) <= 240

    @pytest.mark.asyncio
    async def test_search_all_no_results(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Test search that should return no results."""
        response = await authenticated_client.get("/search/all?q=xyznonexistentterm123")
        assert response.status_code == 200
        assert response.json() == []


class TestSearchQueryHandling:
    """Test query parameter handling and edge cases."""

    @pytest.mark.asyncio
    async def test_search_whitespace_query(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Test search with whitespace-only query."""
        response = await authenticated_client.get("/search?q=   ")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_search_special_characters(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Test search with special characters."""
        response = await authenticated_client.get("/search?q=AES-256")
        assert response.status_code == 200
        results = response.json()
        # AES-256 appears in the data encryption requirement description
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_case_insensitive(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Test that search is case-insensitive."""
        response1 = await authenticated_client.get("/search?q=AUTHENTICATION")
        response2 = await authenticated_client.get("/search?q=authentication")

        assert response1.status_code == 200
        assert response2.status_code == 200

        results1 = response1.json()
        results2 = response2.json()
        assert len(results1) == len(results2)
        assert len(results1) >= 1

    @pytest.mark.asyncio
    async def test_search_invalid_k_parameter(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Test search with invalid k parameter."""
        # Negative k should be normalized to 1
        response = await authenticated_client.get("/search?q=authentication&k=-5")
        assert response.status_code == 200

        # Very large k should be capped
        response = await authenticated_client.get("/search?q=authentication&k=10000")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_search_multi_word_query(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Multi-word search should find matching results."""
        response = await authenticated_client.get("/search?q=user authentication system")
        assert response.status_code == 200
        results = response.json()
        assert len(results) >= 1

        titles = [r["title"] for r in results]
        assert "User Authentication System" in titles


class TestSearchSimilarityScores:
    """Test similarity score calculation and normalization."""

    @pytest.mark.asyncio
    async def test_similarity_score_range(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """All similarity scores must be between 0 and 1."""
        response = await authenticated_client.get("/search/all?q=security api database")
        assert response.status_code == 200
        results = response.json()
        assert len(results) >= 1

        for result in results:
            assert isinstance(result["similarity"], float)
            assert 0 <= result["similarity"] <= 1

    @pytest.mark.asyncio
    async def test_exact_match_high_score(
        self, authenticated_client: AsyncClient, search_test_data: dict
    ):
        """Exact title match should get a high similarity score."""
        response = await authenticated_client.get("/search?q=User Authentication System")
        assert response.status_code == 200
        results = response.json()
        assert len(results) >= 1

        top_result = results[0]
        assert top_result["title"] == "User Authentication System"
        assert top_result["similarity"] >= 0.5
