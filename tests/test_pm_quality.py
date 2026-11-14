"""Tests for PM entity quality endpoints (Task 4.7)."""

from __future__ import annotations

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


RISK_PAYLOAD = {
    "title": "DB migration could cause data loss",
    "description": "Risk of data loss during the PostgreSQL migration window.",
    "project_id": "qa-pm-project",
    "probability": 0.4,
    "impact_score": 4.0,
    "category": "technical",
    "response_strategy": "mitigate",
    "response_plan": "Run migration in staging first; have rollback scripts ready.",
    "status": "identified",
    "owner": "dba-team",
}


class TestRiskQuality:
    async def test_risk_quality_heuristic_persists(self, authenticated_client: AsyncClient):
        created = (await authenticated_client.post("/pm/risks", json=RISK_PAYLOAD)).json()
        resp = await authenticated_client.post(f"/pm/risks/{created['id']}/quality")
        assert resp.status_code == 200
        data = resp.json()
        assert 0.0 <= data["score"] <= 1.0
        assert "passes" in data and "issues" in data


# ---------------------------------------------------------------------------
# Benefit
# ---------------------------------------------------------------------------


BENEFIT_PAYLOAD = {
    "title": "Faster deployments",
    "description": "Automated CI/CD reduces deployment time substantially.",
    "benefit_type": "non_financial",
    "measurement": "Average deployment duration in minutes",
    "baseline_value": 120.0,
    "target_value": 15.0,
    "status": "expected",
    "owner": "platform-team",
}


class TestBenefitQuality:
    async def test_benefit_quality_heuristic(self, authenticated_client: AsyncClient):
        created = (await authenticated_client.post("/pm/benefits", json=BENEFIT_PAYLOAD)).json()
        resp = await authenticated_client.post(f"/pm/benefits/{created['id']}/quality")
        assert resp.status_code == 200
        data = resp.json()
        assert 0.0 <= data["score"] <= 1.0
