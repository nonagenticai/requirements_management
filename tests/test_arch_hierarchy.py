"""Tests for architecture element hierarchy + cascade delete (Phase 3.2)."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.tenants import T

_BASE_ELEMENT = {
    "description": "Hierarchy fixture element",
    "layer": "application",
    "element_type": "application_component",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "hierarchy-project",
}


async def _create(client: AsyncClient, title: str, parent_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {**_BASE_ELEMENT, "title": title}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = await client.post("/architecture/elements", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_get_children(authenticated_client: AsyncClient):
    """GET /{id}/children returns only direct descendants."""
    root = await _create(authenticated_client, "root component")
    child_a = await _create(authenticated_client, "child a", parent_id=root["id"])
    child_b = await _create(authenticated_client, "child b", parent_id=root["id"])
    grandchild = await _create(authenticated_client, "grandchild", parent_id=child_a["id"])

    resp = await authenticated_client.get(f"/architecture/elements/{root['id']}/children")
    assert resp.status_code == 200, resp.text
    children = resp.json()
    ids = sorted(c["id"] for c in children)
    assert ids == sorted([child_a["id"], child_b["id"]])
    assert grandchild["id"] not in ids


@pytest.mark.asyncio
async def test_get_ancestors_nearest_first(authenticated_client: AsyncClient):
    """GET /{id}/ancestors walks the chain nearest-first, excluding self."""
    root = await _create(authenticated_client, "ancestor root")
    mid = await _create(authenticated_client, "ancestor mid", parent_id=root["id"])
    leaf = await _create(authenticated_client, "ancestor leaf", parent_id=mid["id"])

    resp = await authenticated_client.get(f"/architecture/elements/{leaf['id']}/ancestors")
    assert resp.status_code == 200, resp.text
    chain = resp.json()
    # Ancestors nearest first: mid, root.
    assert [c["id"] for c in chain] == [mid["id"], root["id"]]


@pytest.mark.asyncio
async def test_delete_parent_cascades_to_children(authenticated_client: AsyncClient):
    """Deleting a parent element wipes its descendants depth-first."""
    root = await _create(authenticated_client, "cascade root")
    child = await _create(authenticated_client, "cascade child", parent_id=root["id"])
    grandchild = await _create(authenticated_client, "cascade grandchild", parent_id=child["id"])

    del_resp = await authenticated_client.delete(f"/architecture/elements/{root['id']}")
    assert del_resp.status_code == 200, del_resp.text

    for entity_id in (root["id"], child["id"], grandchild["id"]):
        miss = await authenticated_client.get(f"/architecture/elements/{entity_id}")
        assert miss.status_code == 404, miss.text


@pytest.mark.asyncio
async def test_get_children_tenant_isolated(test_client: AsyncClient):
    """Children belonging to another tenant must not appear."""
    a_headers = {"X-Tenant-Id": T("tenant-a-iso")}
    b_headers = {"X-Tenant-Id": T("tenant-b-iso")}

    create_resp = await test_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "tenant-a parent"},
        headers=a_headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    a_root = create_resp.json()
    child_resp = await test_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "tenant-a child", "parent_id": a_root["id"]},
        headers=a_headers,
    )
    assert child_resp.status_code == 200, child_resp.text

    # Cross-tenant request — element exists, but for tenant A only.
    resp = await test_client.get(
        f"/architecture/elements/{a_root['id']}/children",
        headers=b_headers,
    )
    assert resp.status_code == 404, resp.text
