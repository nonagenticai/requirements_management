"""T3.3 regression — no JSONB ``code_binding`` fallback anywhere post-T3.2.

Three properties of the post-cleanup data model:

1. ``POST /architecture/elements`` strips ``code_binding`` from
   ``properties`` before persisting the element row — the structured
   ``architecture_element_realisation_bindings`` table is the canonical store.
2. An element row whose JSONB still carries a legacy ``code_binding`` block
   (no structured row backing it — only reachable via direct insert; the
   alembic migration ``2026_05_27_0027`` clears these in prod) renders
   ``code_bindings: []`` through ``_load_elements`` — so all downstream
   readers (conformance, proposals, adm-completeness) treat it as unbound.
3. ``find_synthetic_by_code_binding`` matches via the structured table —
   re-POSTing the same synthetic+code_binding payload is idempotent.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select

from models.architecture import ArchitectureElement
from routers.architecture_tools import _load_elements
from tests.tenants import T


async def test_post_strips_jsonb_code_binding_from_properties(
    authenticated_client: AsyncClient, test_session_factory
) -> None:
    """``POST /architecture/elements`` strips ``code_binding`` from
    ``properties`` before persistence. The element row's JSONB only carries
    any non-binding keys the caller sent."""
    resp = await authenticated_client.post(
        "/architecture/elements",
        json={
            "title": "Strip JSONB Probe",
            "layer": "technology",
            "element_type": "artifact",
            "is_synthetic": True,
            "properties": {
                "code_binding": {"container": "stripme", "component_id": "stripme.id"},
                "note": "keep me",
            },
        },
    )
    assert resp.status_code in (200, 201), resp.text
    eid = resp.json()["id"]

    async with test_session_factory() as session:
        row = (
            await session.execute(select(ArchitectureElement).where(ArchitectureElement.id == eid))
        ).scalar_one()
    assert row.properties is not None
    assert "code_binding" not in row.properties, row.properties
    assert row.properties.get("note") == "keep me"


async def test_load_elements_ignores_legacy_jsonb_code_binding(
    authenticated_client: AsyncClient, test_session_factory
) -> None:
    """An element whose JSONB still carries a legacy ``code_binding`` block
    (no structured row backing it) renders ``code_bindings: []`` through
    ``_load_elements`` — proving the JSONB fallback is gone everywhere
    downstream."""
    # Create a regular element via the API first (no binding payload).
    create = await authenticated_client.post(
        "/architecture/elements",
        json={
            "title": "Legacy JSONB only",
            "layer": "technology",
            "element_type": "artifact",
            "is_synthetic": True,
            "project_id": "t33-jsonb-only",
            "properties": {"note": "no binding"},
        },
    )
    assert create.status_code in (200, 201), create.text
    eid = create.json()["id"]

    # Surgically inject a legacy JSONB ``code_binding`` block on the row,
    # bypassing the router strip — simulating a pre-migration historical row.
    async with test_session_factory() as session:
        row = (
            await session.execute(select(ArchitectureElement).where(ArchitectureElement.id == eid))
        ).scalar_one()
        row.properties = {
            "note": "no binding",
            "code_binding": {"container": "legacy-only", "component_id": "legacy.x"},
        }
        await session.commit()

    # Read the project's effective elements via _load_elements — the resolved
    # ``code_bindings`` list must be empty even though JSONB has one.
    async with test_session_factory() as session:
        elements = await _load_elements(session, T("user-123"), "t33-jsonb-only")
    matches = [e for e in elements if e["id"] == eid]
    assert len(matches) == 1, elements
    assert matches[0]["code_bindings"] == [], matches[0]


async def test_repeat_synthetic_post_is_idempotent_via_structured_table(
    authenticated_client: AsyncClient,
) -> None:
    """``find_synthetic_by_code_binding`` reads from the structured table —
    re-POSTing the same synthetic+code_binding payload returns the same
    element ``id`` (upsert), exercising the JOIN-based lookup end-to-end."""
    payload = {
        "title": "T33 Upsert via Structured",
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "project_id": "t33-upsert",
        "properties": {
            "code_binding": {"container": "t33", "component_id": "t33.alpha"},
        },
    }
    first = await authenticated_client.post("/architecture/elements", json=payload)
    assert first.status_code == 201, first.text
    eid1 = first.json()["id"]

    second = await authenticated_client.post("/architecture/elements", json=payload)
    assert second.status_code == 200, second.text
    eid2 = second.json()["id"]
    assert eid1 == eid2
