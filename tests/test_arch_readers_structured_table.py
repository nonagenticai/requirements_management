"""Regression test for the T3.1 reader migration.

Proves that the three reader sites resolve a coding-binding from the structured
``architecture_element_realisation_bindings`` table source via the
``code_bindings`` list stamped on by ``architecture_tools._load_elements`` —
i.e. the legacy JSONB ``properties.code_binding`` fallback is not load-bearing.

Two halves:

1. **E2E** — ``GET /architecture/containers`` aggregates a container that exists
   ONLY in the structured table (the seeded Artifact's JSONB has no
   ``code_binding`` key at all).

2. **Unit** — ``_bound_containers`` and ``_artifact_binding_keys`` (the readers
   in the architecture-tools router + conformance tool) operate on plain dicts.
   We feed them dicts whose ``code_bindings`` list is populated but whose
   ``properties`` has NO ``code_binding`` — proving the read path doesn't fall
   through to JSONB.

When the T3.2 follow-up strips the JSONB fallback inside ``_load_elements``,
these tests continue to pass — proving the structured-table read is the
canonical source.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from routers.architecture_tools import _bound_containers
from tools.architecture.conformance import _artifact_binding_keys

_PROJECT = "t31-readers-structured-only"


# ─────────────────────────────────────────────────────────────────────────────
# E2E: structured-only Artifact surfaces via /architecture/containers
# ─────────────────────────────────────────────────────────────────────────────


async def _seed_artifact_without_jsonb_binding(
    client: AsyncClient,
    *,
    title: str,
    project_id: str | None = None,
) -> dict:
    """Create a synthetic Artifact whose JSONB has NO ``code_binding`` key."""
    body: dict = {
        "title": title,
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "properties": {"note": "structured-only fixture"},
    }
    if project_id is not None:
        body["project_id"] = project_id
    resp = await client.post("/architecture/elements", json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _attach_structured_binding(
    client: AsyncClient,
    *,
    element_id: str,
    container: str,
    component_id: str,
) -> dict:
    resp = await client.post(
        f"/architecture/elements/{element_id}/bindings",
        json={
            "binding_kind": "coding_component",
            "container": container,
            "component_id": component_id,
            "properties": {"path": f"src/{component_id.split('.')[-1]}.py"},
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_containers_endpoint_finds_structured_only_binding(
    authenticated_client: AsyncClient,
) -> None:
    """``GET /architecture/containers`` aggregates a container that lives ONLY
    in the structured-bindings table — the seeded Artifact has no JSONB
    ``code_binding`` key at all."""
    art = await _seed_artifact_without_jsonb_binding(
        authenticated_client,
        title="Structured Only Artifact",
        project_id=_PROJECT,
    )
    await _attach_structured_binding(
        authenticated_client,
        element_id=art["id"],
        container="t31-structured-only-fr",
        component_id="t31.structured.only.alpha",
    )

    resp = await authenticated_client.get(f"/architecture/containers?projectId={_PROJECT}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = [c["name"] for c in body.get("containers", [])]
    assert "t31-structured-only-fr" in names, body


# ─────────────────────────────────────────────────────────────────────────────
# Unit: readers honour the resolved ``code_bindings`` list, never JSONB
# ─────────────────────────────────────────────────────────────────────────────


def test_bound_containers_reads_from_structured_field_only() -> None:
    """``_bound_containers`` returns containers from the resolved ``code_binding``
    field. JSONB ``properties.code_binding`` is ignored if the resolved field
    is present (which is the post-_load_elements shape after T3.1)."""
    elements = [
        {
            "id": "e1",
            "element_type": "artifact",
            "code_bindings": [
                {"container": "structured-foo", "component_id": "x.y"},
                {"container": "structured-second", "component_id": "x.z"},
            ],
            "properties": {},
        },
        {
            "id": "e2",
            "element_type": "artifact",
            # No binding resolved (no row in structured table, no JSONB).
            "code_bindings": [],
            "properties": {},
        },
        {
            "id": "e3",
            "element_type": "application_component",  # wrong type — excluded
            "code_bindings": [{"container": "should-not-appear", "component_id": "y"}],
            "properties": {},
        },
    ]
    assert _bound_containers(elements) == ["structured-foo", "structured-second"]


def test_artifact_binding_key_reads_from_structured_field_only() -> None:
    """``_artifact_binding_keys`` reads the resolved ``code_bindings`` list
    (stamped on by ``_load_elements`` from the structured table). JSONB
    ``properties.code_binding`` is never consulted post-T3.2."""
    art = {
        "id": "a1",
        "element_type": "artifact",
        "code_bindings": [{"container": "structured-bar", "component_id": "alpha.beta"}],
        # JSONB intentionally has a DIFFERENT value to prove we read the resolved
        # field, not JSONB.
        "properties": {"code_binding": {"container": "jsonb-wrong", "component_id": "wrong.id"}},
    }
    assert _artifact_binding_keys(art) == [("structured-bar", "alpha.beta")]


def test_artifact_binding_key_returns_none_when_only_jsonb_present() -> None:
    """Post-T3.2 there is no JSONB fallback in ``_artifact_binding_keys``:
    an element whose resolved ``code_bindings`` is empty returns ``[]``
    even when its raw JSONB still carries a legacy ``code_binding`` block."""
    art = {
        "id": "a2",
        "element_type": "artifact",
        # No resolved ``code_bindings`` → JSONB is NOT consulted.
        "properties": {"code_binding": {"container": "legacy", "component_id": "old.id"}},
    }
    assert _artifact_binding_keys(art) == []
