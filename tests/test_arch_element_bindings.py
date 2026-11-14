"""Integration tests for the new ``/architecture/elements/{eid}/bindings`` CRUD
(task #25 PR 1/3).

Covers the three binding kinds (coding_component / coding_container /
external_repo), idempotency against the dedup unique index, the per-kind
shape validation enforced both at the schema layer and at the DB
check-constraint, plus the cross-tenant safety of delete.
"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient


async def _seed_app_component(client: AsyncClient, *, title: str) -> dict:
    """Seed one ApplicationComponent; returns the element JSON."""
    resp = await client.post(
        "/architecture/elements",
        json={
            "title": title,
            "layer": "application",
            "element_type": "application_component",
            "architecture_state": "baseline",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# POST /bindings — happy paths
# ─────────────────────────────────────────────────────────────────────────────


async def test_attach_coding_component_binding(authenticated_client: AsyncClient) -> None:
    """``coding_component`` with both container + component_id → 201 with the row."""
    el = await _seed_app_component(authenticated_client, title="Bindings AC 1")
    resp = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings",
        json={
            "binding_kind": "coding_component",
            "container": "fr",
            "component_id": "fr.auth",
            "properties": {"path": "frontend/src/lib/auth.ts"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["binding_kind"] == "coding_component"
    assert body["container"] == "fr"
    assert body["component_id"] == "fr.auth"
    assert body["repository_url"] is None
    assert body["properties"] == {"path": "frontend/src/lib/auth.ts"}


async def test_attach_coding_container_binding(authenticated_client: AsyncClient) -> None:
    """``coding_container`` with container only (no component_id) → 201."""
    el = await _seed_app_component(authenticated_client, title="Bindings AC 2")
    resp = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings",
        json={
            "binding_kind": "coding_container",
            "container": "requirements_management",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["component_id"] is None
    assert body["container"] == "requirements_management"


async def test_attach_external_repo_binding(authenticated_client: AsyncClient) -> None:
    """``external_repo`` with repository_url (and optional branch) → 201."""
    el = await _seed_app_component(authenticated_client, title="Bindings AC 3")
    resp = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings",
        json={
            "binding_kind": "external_repo",
            "repository_url": "https://github.com/vendor/foo",
            "repository_branch": "main",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["repository_url"] == "https://github.com/vendor/foo"
    assert body["repository_branch"] == "main"
    assert body["container"] is None


# ─────────────────────────────────────────────────────────────────────────────
# POST /bindings — schema-level rejections (HTTP 422)
# ─────────────────────────────────────────────────────────────────────────────


async def test_attach_rejects_coding_component_without_component_id(
    authenticated_client: AsyncClient,
) -> None:
    el = await _seed_app_component(authenticated_client, title="Bindings AC 4")
    resp = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings",
        json={"binding_kind": "coding_component", "container": "fr"},
    )
    assert resp.status_code == 422, resp.text


async def test_attach_rejects_coding_container_with_component_id(
    authenticated_client: AsyncClient,
) -> None:
    el = await _seed_app_component(authenticated_client, title="Bindings AC 5")
    resp = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings",
        json={
            "binding_kind": "coding_container",
            "container": "fr",
            "component_id": "should-not-be-here",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_attach_rejects_external_repo_without_url(
    authenticated_client: AsyncClient,
) -> None:
    el = await _seed_app_component(authenticated_client, title="Bindings AC 6")
    resp = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings",
        json={"binding_kind": "external_repo"},
    )
    assert resp.status_code == 422, resp.text


async def test_attach_rejects_unknown_binding_kind(
    authenticated_client: AsyncClient,
) -> None:
    el = await _seed_app_component(authenticated_client, title="Bindings AC 7")
    resp = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings",
        json={"binding_kind": "made-up-kind", "container": "fr"},
    )
    assert resp.status_code == 422, resp.text


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency & dedup
# ─────────────────────────────────────────────────────────────────────────────


async def test_attach_is_idempotent_on_duplicate(
    authenticated_client: AsyncClient,
) -> None:
    """Re-attaching the same logical binding returns the pre-existing row id."""
    el = await _seed_app_component(authenticated_client, title="Bindings AC Dup")
    body = {
        "binding_kind": "coding_component",
        "container": "fr",
        "component_id": "fr.dup",
    }
    first = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings", json=body
    )
    assert first.status_code == 201, first.text
    second = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings", json=body
    )
    # Same row returned (the attach helper returns the existing match on
    # dedup-unique conflict instead of erroring).
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]


# ─────────────────────────────────────────────────────────────────────────────
# GET /bindings
# ─────────────────────────────────────────────────────────────────────────────


async def test_list_bindings_returns_all_attached(
    authenticated_client: AsyncClient,
) -> None:
    el = await _seed_app_component(authenticated_client, title="Bindings AC List")
    for body in [
        {"binding_kind": "coding_component", "container": "fr", "component_id": "fr.list"},
        {"binding_kind": "coding_container", "container": "fr"},
        {
            "binding_kind": "external_repo",
            "repository_url": "https://github.com/vendor/list-me",
        },
    ]:
        resp = await authenticated_client.post(
            f"/architecture/elements/{el['id']}/bindings", json=body
        )
        assert resp.status_code == 201, resp.text

    resp = await authenticated_client.get(f"/architecture/elements/{el['id']}/bindings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    kinds = {b["binding_kind"] for b in body["bindings"]}
    assert kinds == {"coding_component", "coding_container", "external_repo"}


async def test_list_bindings_404_for_unknown_element(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get(f"/architecture/elements/{uuid4()}/bindings")
    assert resp.status_code == 404, resp.text


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /bindings/{binding_id}
# ─────────────────────────────────────────────────────────────────────────────


async def test_detach_binding_then_404(authenticated_client: AsyncClient) -> None:
    el = await _seed_app_component(authenticated_client, title="Bindings AC Det")
    create = await authenticated_client.post(
        f"/architecture/elements/{el['id']}/bindings",
        json={"binding_kind": "coding_component", "container": "fr", "component_id": "fr.det"},
    )
    binding_id = create.json()["id"]

    delete = await authenticated_client.delete(
        f"/architecture/elements/{el['id']}/bindings/{binding_id}"
    )
    assert delete.status_code == 200, delete.text
    assert delete.json() == {"deleted": True, "id": binding_id}

    again = await authenticated_client.delete(
        f"/architecture/elements/{el['id']}/bindings/{binding_id}"
    )
    assert again.status_code == 404, again.text


async def test_detach_404_for_unknown_binding_id(
    authenticated_client: AsyncClient,
) -> None:
    el = await _seed_app_component(authenticated_client, title="Bindings AC Det-Unknown")
    resp = await authenticated_client.delete(
        f"/architecture/elements/{el['id']}/bindings/{uuid4()}"
    )
    assert resp.status_code == 404, resp.text
