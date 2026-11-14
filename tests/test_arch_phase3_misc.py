"""Phase 3 miscellaneous tests: synthetic flag, applicable-standards, batch-delete, versions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from tests.tenants import T

_BASE_ELEMENT = {
    "description": "Base element used in Phase 3 misc tests",
    "layer": "application",
    "element_type": "application_component",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "phase3-misc",
}


# ---------------------------------------------------------------------------
# Task 3.5 — synthetic flag default-hide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_elements_excludes_synthetic_by_default(authenticated_client: AsyncClient):
    real = await authenticated_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "real component"},
    )
    assert real.status_code == 200, real.text
    syn = await authenticated_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "synthetic component", "is_synthetic": True},
    )
    assert syn.status_code == 200, syn.text

    default = await authenticated_client.get("/architecture/elements")
    assert default.status_code == 200
    titles_default = [r["title"] for r in default.json()]
    assert "real component" in titles_default
    assert "synthetic component" not in titles_default

    only_synth = await authenticated_client.get(
        "/architecture/elements", params={"synthetic": "true"}
    )
    assert only_synth.status_code == 200
    titles_synth = [r["title"] for r in only_synth.json()]
    assert titles_synth == ["synthetic component"]


# ---------------------------------------------------------------------------
# Task 3.6 — applicable-standards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_applicable_standards_filters_by_layer_and_validity(
    authenticated_client: AsyncClient,
):
    el = await authenticated_client.post(
        "/architecture/elements",
        json={
            **_BASE_ELEMENT,
            "title": "applicable-standards element",
            "layer": "technology",
            "element_type": "node",
        },
    )
    assert el.status_code == 200, el.text
    eid = el.json()["id"]

    now = datetime.now(timezone.utc)

    matching = await authenticated_client.post(
        "/architecture/standards",
        json={
            "title": "REST Conventions",
            "description": "Public API conventions",
            "standard_type": "technology_standard",
            "status": "approved",
            "compliance_level": "mandatory",
            "project_id": "phase3-misc",
        },
    )
    assert matching.status_code == 200, matching.text

    expired = await authenticated_client.post(
        "/architecture/standards",
        json={
            "title": "Old SOAP",
            "description": "Retired SOAP standard",
            "standard_type": "technology_standard",
            "status": "retired",
            "compliance_level": "mandatory",
            "valid_from": (now - timedelta(days=30)).isoformat(),
            "valid_until": (now - timedelta(days=1)).isoformat(),
            "project_id": "phase3-misc",
        },
    )
    assert expired.status_code == 200, expired.text

    optional = await authenticated_client.post(
        "/architecture/standards",
        json={
            "title": "Optional Logging",
            "description": "Optional logging guideline",
            "standard_type": "technology_standard",
            "status": "trial",
            "compliance_level": "optional",
            "project_id": "phase3-misc",
        },
    )
    assert optional.status_code == 200, optional.text

    # Wrong layer
    other = await authenticated_client.post(
        "/architecture/standards",
        json={
            "title": "Marketing brand book",
            "description": "Brand standards",
            "standard_type": "business_standard",
            "status": "approved",
            "compliance_level": "mandatory",
            "project_id": "phase3-misc",
        },
    )
    assert other.status_code == 200, other.text

    resp = await authenticated_client.get(
        f"/architecture/standards/element/{eid}/applicable-standards"
    )
    assert resp.status_code == 200, resp.text
    titles = sorted(s["title"] for s in resp.json())
    assert titles == ["REST Conventions"]


# ---------------------------------------------------------------------------
# Task 3.7 — batch delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_delete_by_ids(authenticated_client: AsyncClient):
    created = []
    for i in range(3):
        resp = await authenticated_client.post(
            "/architecture/elements",
            json={**_BASE_ELEMENT, "title": f"batch del {i}"},
        )
        assert resp.status_code == 200, resp.text
        created.append(resp.json())

    payload = {"ids": [e["id"] for e in created]}
    resp = await authenticated_client.post("/architecture/elements/batch-delete", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 3

    for e in created:
        miss = await authenticated_client.get(f"/architecture/elements/{e['id']}")
        assert miss.status_code == 404


@pytest.mark.asyncio
async def test_batch_delete_capped_at_500(authenticated_client: AsyncClient):
    fake_ids = ["00000000-0000-0000-0000-" + f"{i:012d}" for i in range(501)]
    resp = await authenticated_client.post(
        "/architecture/elements/batch-delete", json={"ids": fake_ids}
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_batch_delete_tenant_isolated(test_client: AsyncClient):
    a_headers = {"X-Tenant-Id": T("tenant-bd-a")}
    b_headers = {"X-Tenant-Id": T("tenant-bd-b")}

    a_resp = await test_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "tenant-a victim"},
        headers=a_headers,
    )
    assert a_resp.status_code == 200, a_resp.text
    a_id = a_resp.json()["id"]

    # Tenant B tries to delete tenant A's id; service returns 404 → 0 deletions.
    resp = await test_client.post(
        "/architecture/elements/batch-delete",
        json={"ids": [a_id]},
        headers=b_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 0

    # Confirm element still exists for tenant A.
    still = await test_client.get(f"/architecture/elements/{a_id}", headers=a_headers)
    assert still.status_code == 200, still.text


@pytest.mark.asyncio
async def test_batch_delete_by_filter(authenticated_client: AsyncClient):
    keep = await authenticated_client.post(
        "/architecture/elements",
        json={
            **_BASE_ELEMENT,
            "title": "keep me",
            "layer": "business",
            "element_type": "business_process",
        },
    )
    assert keep.status_code == 200, keep.text

    drop_a = await authenticated_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "drop-a", "layer": "technology", "element_type": "node"},
    )
    drop_b = await authenticated_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "drop-b", "layer": "technology", "element_type": "node"},
    )
    assert drop_a.status_code == 200 and drop_b.status_code == 200

    resp = await authenticated_client.post(
        "/architecture/elements/batch-delete",
        json={"filter": {"layer": "technology"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 2

    resp = await authenticated_client.post(
        "/architecture/elements/batch-delete",
        json={"filter": {"unknown_key": "x"}},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Task 3.8 — version sub-endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_element_versions_endpoints(authenticated_client: AsyncClient):
    create = await authenticated_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "version test element"},
    )
    eid = create.json()["id"]

    # Update to record a v2 snapshot.
    upd = await authenticated_client.put(
        f"/architecture/elements/{eid}",
        json={
            **_BASE_ELEMENT,
            "title": "version test element",
            "description": "updated description",
            "version": "2.0.0",
            "version_notes": "v2",
        },
    )
    assert upd.status_code == 200, upd.text

    info = await authenticated_client.get(f"/architecture/elements/{eid}/version")
    assert info.status_code == 200, info.text

    versions = await authenticated_client.get(f"/architecture/elements/{eid}/versions")
    assert versions.status_code == 200, versions.text
    assert len(versions.json()) >= 1


@pytest.mark.asyncio
async def test_view_versions_endpoints(authenticated_client: AsyncClient):
    create = await authenticated_client.post(
        "/architecture/views",
        json={
            "title": "versioned view",
            "description": "test",
            "viewpoint": "application_cooperation",
            "element_ids": [],
            "relationship_ids": [],
            "project_id": "phase3-misc",
        },
    )
    assert create.status_code == 200, create.text
    vid = create.json()["id"]

    info = await authenticated_client.get(f"/architecture/views/{vid}/version")
    assert info.status_code == 200, info.text


@pytest.mark.asyncio
async def test_standard_versions_endpoints(authenticated_client: AsyncClient):
    create = await authenticated_client.post(
        "/architecture/standards",
        json={
            "title": "versioned standard",
            "description": "for version testing",
            "standard_type": "technology_standard",
            "status": "approved",
            "compliance_level": "recommended",
            "project_id": "phase3-misc",
        },
    )
    assert create.status_code == 200, create.text
    sid = create.json()["id"]

    info = await authenticated_client.get(f"/architecture/standards/{sid}/version")
    assert info.status_code == 200, info.text
