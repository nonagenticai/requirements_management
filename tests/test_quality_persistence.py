"""Unit + integration tests for the quality persistence pipeline."""

from __future__ import annotations

import pytest
from fastapi import status

from services.quality_service import QualityResult, persist_quality
from tests.tenants import T


@pytest.mark.asyncio
async def test_persist_quality_writes_columns(test_session, sample_requirement):
    """persist_quality writes all five QualityMixin columns onto the entity."""
    result = QualityResult(
        score=0.83,
        issues=["missing acceptance criteria"],
        suggestions=["add INVEST checklist"],
    )

    await persist_quality(test_session, sample_requirement, result)
    await test_session.commit()
    await test_session.refresh(sample_requirement)

    assert sample_requirement.quality_score == pytest.approx(0.83)
    assert sample_requirement.quality_issues == ["missing acceptance criteria"]
    assert sample_requirement.quality_suggestions == ["add INVEST checklist"]
    assert sample_requirement.last_evaluated_at is not None
    assert sample_requirement.last_evaluated_by_agent is None


@pytest.mark.asyncio
async def test_persist_quality_records_agent_name(test_session, sample_requirement):
    """When agent_name is supplied it is recorded on last_evaluated_by_agent."""
    result = QualityResult(score=0.42, agent_name="requirements_quality_agent")

    await persist_quality(test_session, sample_requirement, result)
    await test_session.commit()
    await test_session.refresh(sample_requirement)

    assert sample_requirement.quality_score == pytest.approx(0.42)
    assert sample_requirement.last_evaluated_by_agent == "requirements_quality_agent"
    assert sample_requirement.quality_issues == []
    assert sample_requirement.quality_suggestions == []


@pytest.mark.asyncio
async def test_requirement_quality_endpoint_persists_score(test_client, sample_requirement):
    """POST /requirements/{id}/quality must persist the score to the row."""
    headers = {"X-Tenant-Id": T("user-123")}

    post_resp = await test_client.post(
        f"/requirements/{sample_requirement.id}/quality", headers=headers
    )
    assert post_resp.status_code == status.HTTP_200_OK, post_resp.text
    posted_score = post_resp.json()["score"]

    get_resp = await test_client.get(f"/requirements/{sample_requirement.id}", headers=headers)
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    body = get_resp.json()

    assert body["quality_score"] == pytest.approx(posted_score)
    assert body["last_evaluated_at"] is not None
    assert body["last_evaluated_by_agent"] is None


# ---------------------------------------------------------------------------
# Phase 3 — element / view / decision / principle persistence
# ---------------------------------------------------------------------------

_ELEMENT_PAYLOAD = {
    "title": "Order Service",
    "description": "Microservice handling the order lifecycle end-to-end.",
    "layer": "application",
    "element_type": "application_component",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "qa-project",
}

_VIEW_PAYLOAD = {
    "title": "Application Cooperation View",
    "description": "Interactions between application components in the order domain.",
    "viewpoint": "application_cooperation",
    "element_ids": [],
    "relationship_ids": [],
    "project_id": "qa-project",
}

_DECISION_PAYLOAD = {
    "title": "Pick Postgres",
    "context": "We need a relational store",
    "decision": "Use PostgreSQL 18",
    "consequences": "Operational footprint of one engine",
    "alternatives": ["mysql", "cockroachdb"],
    "status": "accepted",
    "project_id": "qa-project",
    "related_principles": [],
}


@pytest.mark.asyncio
async def test_element_quality_persists(authenticated_client):
    create = await authenticated_client.post("/architecture/elements", json=_ELEMENT_PAYLOAD)
    assert create.status_code == 200, create.text
    eid = create.json()["id"]

    resp = await authenticated_client.post(f"/architecture/elements/{eid}/quality")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert 0.0 <= body["score"] <= 1.0

    refreshed = await authenticated_client.get(f"/architecture/elements/{eid}")
    detail = refreshed.json()
    assert detail["quality_score"] == pytest.approx(body["score"])
    assert detail["last_evaluated_by_agent"] is None


@pytest.mark.asyncio
async def test_view_quality_persists(authenticated_client):
    create = await authenticated_client.post("/architecture/views", json=_VIEW_PAYLOAD)
    assert create.status_code == 200, create.text
    vid = create.json()["id"]

    resp = await authenticated_client.post(f"/architecture/views/{vid}/quality")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    refreshed = await authenticated_client.get(f"/architecture/views/{vid}")
    detail = refreshed.json()
    assert detail["quality_score"] == pytest.approx(body["score"])


@pytest.mark.asyncio
async def test_decision_quality_persists(authenticated_client):
    create = await authenticated_client.post("/decisions", json=_DECISION_PAYLOAD)
    assert create.status_code == 200, create.text
    did = create.json()["id"]

    resp = await authenticated_client.post(f"/decisions/{did}/quality")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    refreshed = await authenticated_client.get(f"/decisions/{did}")
    detail = refreshed.json()
    assert detail["quality_score"] == pytest.approx(body["score"])


@pytest.mark.asyncio
async def test_principle_quality_persists(authenticated_client, sample_principle):
    """POST /principles/{id}/quality persists score derived from the heuristic."""
    resp = await authenticated_client.post(f"/principles/{sample_principle.id}/quality")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    refreshed = await authenticated_client.get(f"/principles/{sample_principle.id}")
    detail = refreshed.json()
    assert detail["quality_score"] == pytest.approx(body["score"])
