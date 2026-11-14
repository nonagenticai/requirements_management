"""Metamodel validation must be enforced at the SERVICE layer, not only the route.

Regression coverage for closing the ``RelationshipService.create`` bypass: the
metamodel check used to live in the HTTP route, so any non-router caller
(``get_or_create`` AA-intake, direct service use) could persist an ArchiMate-
illegal edge. Validation now lives in the service, so:

  * the HTTP create route still 422s an illegal edge (the route delegates), and
  * a DIRECT ``svc.create`` call also 422s — the bypass is closed.

``composition`` between a ``business_process`` (source) and a ``capability``
(target) is not permitted by the ArchiMate metamodel (mirrors the existing
update-revalidation test).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from schemas.architecture import ArchitectureRelationshipIn
from services import architecture_relationship_service as rel_svc
from tests.tenants import T

_CAPABILITY = {
    "title": "Order Management Capability",
    "description": "Core order processing capability",
    "layer": "strategy",
    "element_type": "capability",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "test-project",
}

_PROCESS = {
    "title": "Process Orders Workflow",
    "description": "Handles the order lifecycle end-to-end",
    "layer": "business",
    "element_type": "business_process",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "test-project",
}


async def _create_element(client: AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post("/architecture/elements", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_create_invalid_relationship_rejected_via_http(authenticated_client: AsyncClient):
    """The create route delegates to the service; an illegal edge still 422s."""
    cap = await _create_element(authenticated_client, _CAPABILITY)
    proc = await _create_element(authenticated_client, _PROCESS)

    resp = await authenticated_client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": "architecture_elements",
            "source_id": proc["id"],
            "target_entity_type": "architecture_elements",
            "target_id": cap["id"],
            "relationship_type": "composition",
            "project_id": "test-project",
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_service_create_enforces_metamodel_bypass_closed(
    authenticated_client: AsyncClient, test_session
):
    """Calling RelationshipService.create DIRECTLY (the old bypass) now 422s."""
    cap = await _create_element(authenticated_client, _CAPABILITY)
    proc = await _create_element(authenticated_client, _PROCESS)

    payload = ArchitectureRelationshipIn(
        source_entity_type="architecture_elements",
        source_id=proc["id"],
        target_entity_type="architecture_elements",
        target_id=cap["id"],
        relationship_type="composition",
        project_id="test-project",
    )

    with pytest.raises(HTTPException) as exc_info:
        await rel_svc.create(test_session, payload, T("user-123"))
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_service_create_allows_valid_relationship(
    authenticated_client: AsyncClient, test_session
):
    """A metamodel-valid edge (realization) still creates cleanly via the service."""
    cap = await _create_element(authenticated_client, _CAPABILITY)
    proc = await _create_element(authenticated_client, _PROCESS)

    payload = ArchitectureRelationshipIn(
        source_entity_type="architecture_elements",
        source_id=proc["id"],
        target_entity_type="architecture_elements",
        target_id=cap["id"],
        relationship_type="realization",
        project_id="test-project",
    )

    out = await rel_svc.create(test_session, payload, T("user-123"))
    assert out.relationship_type == "realization"
