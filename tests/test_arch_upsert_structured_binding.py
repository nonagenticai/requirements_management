"""Structured-binding write contract test for element create/update.

Asserts that any ``POST`` or ``PUT`` to ``/architecture/elements`` whose
request payload's ``properties.code_binding`` carries a complete
``(container, component_id)`` pair produces (or refreshes) exactly one row in
``architecture_element_realisation_bindings`` of kind ``coding_component`` —
the structured table is the canonical store. The request's ``code_binding``
block is stripped from the element row's JSONB ``properties`` before
persistence (verified in ``test_arch_no_jsonb_fallback.py``).
"""

from __future__ import annotations

from httpx import AsyncClient


async def test_create_with_code_binding_writes_structured_row(
    authenticated_client: AsyncClient,
) -> None:
    """A synthetic Artifact with code_binding produces 1 structured row.

    Visible via ``GET /architecture/elements/{eid}/bindings`` — the route
    reads the structured table directly.
    """
    create = await authenticated_client.post(
        "/architecture/elements",
        json={
            "title": "Structured Write Svc",
            "layer": "technology",
            "element_type": "artifact",
            "is_synthetic": True,
            "properties": {
                "code_binding": {
                    "container": "fr",
                    "component_id": "fr.structured",
                    "path": "frontend/src/lib/structured.ts",
                    "kind": "module",
                }
            },
        },
    )
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    listing = await authenticated_client.get(f"/architecture/elements/{eid}/bindings")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    row = body["bindings"][0]
    assert row["binding_kind"] == "coding_component"
    assert row["container"] == "fr"
    assert row["component_id"] == "fr.structured"
    # Extra metadata (path, kind, node_id) is captured on the binding row.
    assert row["properties"].get("path") == "frontend/src/lib/structured.ts"
    assert row["properties"].get("kind") == "module"


async def test_repeat_create_is_idempotent_no_duplicate_row(
    authenticated_client: AsyncClient,
) -> None:
    """Re-POSTing the same synthetic-Artifact upsert payload does NOT create
    a duplicate row in the structured table — the partial-unique dedup index
    makes the write idempotent and ``find_synthetic_by_code_binding`` matches
    via the structured table.
    """
    payload = {
        "title": "Structured Idempotent",
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "properties": {"code_binding": {"container": "fr", "component_id": "fr.idem"}},
    }
    first = await authenticated_client.post("/architecture/elements", json=payload)
    assert first.status_code in (200, 201), first.text
    eid1 = first.json()["id"]

    second = await authenticated_client.post("/architecture/elements", json=payload)
    assert second.status_code in (200, 201), second.text
    eid2 = second.json()["id"]
    # Upsert by code_binding key → both calls return the same element row.
    assert eid1 == eid2

    listing = await authenticated_client.get(f"/architecture/elements/{eid1}/bindings")
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1


async def test_update_refreshes_binding_properties(
    authenticated_client: AsyncClient,
) -> None:
    """Updating the element's code_binding metadata refreshes the binding
    row's ``properties`` (no duplicate row, but new path/kind reflected).
    """
    base = {
        "title": "Structured Refresh",
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "properties": {
            "code_binding": {
                "container": "fr",
                "component_id": "fr.refresh",
                "path": "old/path.ts",
            }
        },
    }
    create = await authenticated_client.post("/architecture/elements", json=base)
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    # PUT a refreshed code_binding path.
    refreshed = dict(base)
    refreshed["properties"] = {
        "code_binding": {
            "container": "fr",
            "component_id": "fr.refresh",
            "path": "new/path.ts",
            "kind": "service",
        }
    }
    update = await authenticated_client.put(f"/architecture/elements/{eid}", json=refreshed)
    assert update.status_code == 200, update.text

    listing = await authenticated_client.get(f"/architecture/elements/{eid}/bindings")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    row = body["bindings"][0]
    assert row["properties"].get("path") == "new/path.ts"
    assert row["properties"].get("kind") == "service"
