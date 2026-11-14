"""Tests for PUT /architecture/relationships/{rid} (Phase 3.1)."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

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


async def _create_relationship(
    client: AsyncClient, source_id: str, target_id: str, rel_type: str = "realization"
) -> dict[str, Any]:
    resp = await client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": "architecture_elements",
            "source_id": source_id,
            "target_entity_type": "architecture_elements",
            "target_id": target_id,
            "relationship_type": rel_type,
            "project_id": "test-project",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_update_relationship_metadata_only(authenticated_client: AsyncClient):
    """PUT with just description updates the row without re-validating."""
    cap = await _create_element(authenticated_client, _CAPABILITY)
    proc = await _create_element(authenticated_client, _PROCESS)
    rel = await _create_relationship(authenticated_client, proc["id"], cap["id"])

    resp = await authenticated_client.put(
        f"/architecture/relationships/{rel['id']}",
        json={"description": "fixed typo in description"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "fixed typo in description"
    assert body["relationship_type"] == "realization"


@pytest.mark.asyncio
async def test_update_relationship_revalidates_metamodel(authenticated_client: AsyncClient):
    """Changing relationship_type to an invalid one re-runs metamodel validation."""
    cap = await _create_element(authenticated_client, _CAPABILITY)
    proc = await _create_element(authenticated_client, _PROCESS)
    rel = await _create_relationship(authenticated_client, proc["id"], cap["id"])

    # 'composition' between business_process and capability is not valid
    # under the ArchiMate metamodel; the update endpoint must reject it 422.
    resp = await authenticated_client.put(
        f"/architecture/relationships/{rel['id']}",
        json={"relationship_type": "composition"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_update_relationship_tenant_isolation(test_client: AsyncClient):
    """A PUT against another tenant's relationship returns 404."""
    a_headers = {"X-Tenant-Id": T("tenant-rel-a")}
    b_headers = {"X-Tenant-Id": T("tenant-rel-b")}

    cap_resp = await test_client.post("/architecture/elements", json=_CAPABILITY, headers=a_headers)
    assert cap_resp.status_code == 200, cap_resp.text
    cap = cap_resp.json()

    proc_resp = await test_client.post("/architecture/elements", json=_PROCESS, headers=a_headers)
    assert proc_resp.status_code == 200, proc_resp.text
    proc = proc_resp.json()

    rel_resp = await test_client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": "architecture_elements",
            "source_id": proc["id"],
            "target_entity_type": "architecture_elements",
            "target_id": cap["id"],
            "relationship_type": "realization",
            "project_id": "test-project",
        },
        headers=a_headers,
    )
    assert rel_resp.status_code == 200, rel_resp.text
    rel = rel_resp.json()

    resp = await test_client.put(
        f"/architecture/relationships/{rel['id']}",
        json={"description": "tenant B should not see this"},
        headers=b_headers,
    )
    assert resp.status_code == 404, resp.text
