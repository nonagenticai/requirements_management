"""Tests for the principle-conflicts read + manual-lifecycle endpoints.

The in-process LLM detectors (and ``services.conflict_persistence``) were removed
2026-10-26; the table and router stay, so only the endpoint tests remain.
"""

from __future__ import annotations

from uuid import uuid7

from models.principle_conflicts import PrincipleConflictRecord
from models.principles import Principle
from tests.tenants import T

# asyncio_mode=auto (pyproject) runs async tests without an explicit mark.

TENANT = T("user-123")


async def _mk_principle(session, *, tenant_id=TENANT, slug="p", statement="s") -> Principle:
    p = Principle(
        slug=slug,
        title=slug.title(),
        statement=statement,
        tenant_id=tenant_id,
        rationale="r",
        implications=[],
        tags=[],
        active=True,
    )
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
async def _seed_conflict(session, *, tenant_id=TENANT, status="open", counterparty="proc-1"):
    p = await _mk_principle(session, tenant_id=tenant_id, slug=f"seed-{uuid7().hex[-12:]}")
    rec = PrincipleConflictRecord(
        tenant_id=tenant_id,
        tenant_name="",
        subject_id=tenant_id,
        visibility="tenant",
        principle_id=p.id,
        counterparty_kind="procedure",
        counterparty_id=counterparty,
        conflict_type="violates",
        rationale="because",
        status=status,
    )
    session.add(rec)
    await session.commit()
    await session.refresh(rec)
    return rec


async def test_list_endpoint_filters(authenticated_client, test_session):
    await _seed_conflict(test_session, status="open", counterparty="proc-1")
    await _seed_conflict(test_session, status="resolved", counterparty="proc-2")

    resp = await authenticated_client.get("/principle-conflicts")
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = await authenticated_client.get("/principle-conflicts", params={"status": "open"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1 and body[0]["status"] == "open"

    resp = await authenticated_client.get(
        "/principle-conflicts", params={"counterparty_id": "proc-2"}
    )
    assert len(resp.json()) == 1 and resp.json()[0]["counterparty_id"] == "proc-2"


async def test_patch_dismiss_and_validation(authenticated_client, test_session):
    rec = await _seed_conflict(test_session, status="open")

    resp = await authenticated_client.patch(
        f"/principle-conflicts/{rec.id}", json={"status": "dismissed"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"

    # Unsupported transition (resolved is reconciler-owned).
    resp = await authenticated_client.patch(
        f"/principle-conflicts/{rec.id}", json={"status": "resolved"}
    )
    assert resp.status_code == 422

    # Nonexistent id.
    resp = await authenticated_client.patch(
        f"/principle-conflicts/{uuid7()}", json={"status": "dismissed"}
    )
    assert resp.status_code == 404


async def test_tenant_isolation(test_client, test_session):
    rec = await _seed_conflict(test_session, tenant_id=T("tenant-a-uuid-1234"), status="open")

    # Tenant B sees nothing and cannot patch tenant A's row.
    test_client.headers = {"X-Tenant-Id": T("tenant-b-uuid-5678")}
    resp = await test_client.get("/principle-conflicts")
    assert resp.status_code == 200 and resp.json() == []

    resp = await test_client.patch(f"/principle-conflicts/{rec.id}", json={"status": "dismissed"})
    assert resp.status_code == 404
