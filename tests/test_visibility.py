"""Per-user-within-tenant visibility tests across all 15 entity tables.

For each entity we exercise a 6-test matrix:

    1. tenant-visible row created by user A is seen by user B in the same tenant
    2. user-visible row created by user A is HIDDEN from user B in the same tenant
    3. user-visible row created by user A is visible to user A
    4. cross-tenant isolation holds (both visibility values)
    5. user A can mutate (PUT/DELETE) their own user-visible row
    6. user B cannot mutate user A's user-visible row (404 — don't leak existence)

Mirrors procedure_management's pilot pattern (commit 7f372e0).

Auth
----
A plain X-Tenant-Id (no ``Bearer ``) + X-Subject-Id becomes a test licence
for that tenant/subject (``tests/conftest.py``), so the middleware's licence
path yields that tenant_id and subject_id.

Entities with structural asymmetries
------------------------------------
- ``architecture_relationships``: no ``title`` / ``slug`` semantics, no PUT
  endpoint, requires real architecture elements as endpoints. Tests #5 use
  DELETE-only as the mutation; tests #6 verify DELETE returns 404 cross-user.
- ``architecture_views`` / ``architecture_standards``: standard 6-test cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient

from tests.briefs import MIN_BRIEF
from tests.project_types import typed
from tests.tenants import T

# ---------------------------------------------------------------------------
# Tenants and subjects used throughout
# ---------------------------------------------------------------------------

TENANT_A = T("tenant-a-vis")
TENANT_B = T("tenant-b-vis")
USER_A = "user-alice-vis"
USER_B = "user-bob-vis"


def _hdrs(tenant: str, subject: str, visibility: str | None = None) -> dict[str, str]:
    """Build test-licence headers: tenant + subject (+ optional visibility)."""
    h = {"X-Tenant-Id": tenant, "X-Subject-Id": subject}
    if visibility is not None:
        h["X-Visibility"] = visibility
    return h


# ---------------------------------------------------------------------------
# Per-entity payload factories
# ---------------------------------------------------------------------------


def _unique_title(prefix: str) -> str:
    """Title unique enough to dodge tenant slug collisions across tests."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _payload_requirement() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-requirement"),
        "description": "Visibility test requirement",
        "links": {"business_objectives": ["b-1"]},
        "acceptance": ["criterion 1"],
        "effort": 5.0,
        "reach": 10.0,
        "impact": 3.0,
        "confidence": 0.8,
    }


def _update_requirement() -> dict[str, Any]:
    p = _payload_requirement()
    p["description"] = "Updated description"
    return p


def _payload_principle() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-principle"),
        "statement": "Always test visibility",
        "rationale": "Because BOLA",
        "implications": ["Add tests"],
        "tags": ["test"],
        "active": True,
    }


def _update_principle() -> dict[str, Any]:
    p = _payload_principle()
    p["statement"] = "Updated statement"
    return p


def _payload_decision() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-decision"),
        "context": "We need visibility tests",
        "decision": "Write the tests",
        "consequences": "Better coverage",
        "alternatives": ["skip them"],
        "status": "proposed",
        "related_principles": [],
    }


def _update_decision() -> dict[str, Any]:
    p = _payload_decision()
    p["decision"] = "Updated decision"
    return p


def _payload_arch_element() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-arch-elem"),
        "description": "vis test element",
        "layer": "application",
        "element_type": "application_component",
        "status": "draft",
    }


def _update_arch_element() -> dict[str, Any]:
    p = _payload_arch_element()
    p["description"] = "Updated"
    return p


def _payload_arch_view() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-arch-view"),
        "description": "vis test view",
        "viewpoint": "layered",
        "element_ids": [],
        "relationship_ids": [],
        "layout_data": {},
    }


def _update_arch_view() -> dict[str, Any]:
    p = _payload_arch_view()
    p["description"] = "Updated"
    return p


def _payload_arch_standard() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-standard"),
        "description": "vis test standard",
        "standard_type": "technology_standard",
        "status": "assess",
        "compliance_level": "recommended",
        "related_principle_ids": [],
    }


def _update_arch_standard() -> dict[str, Any]:
    p = _payload_arch_standard()
    p["description"] = "Updated"
    return p


def _payload_programme() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-programme"),
        "description": "vis test programme",
        "status": "identifying",
    }


def _update_programme() -> dict[str, Any]:
    p = _payload_programme()
    p["description"] = "Updated"
    return p


def _payload_project() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-project"),
        "description": "vis test project",
        "status": "initiating",
        "budget": 1000.0,
        "currency": "EUR",
        "brief": MIN_BRIEF,
    }


def _update_project() -> dict[str, Any]:
    p = _payload_project()
    p["description"] = "Updated"
    return p


def _payload_resource() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-resource"),
        "description": "vis test resource",
        "resource_type": "person",
        "capacity_per_day": 8.0,
        "skills": ["python"],
    }


def _update_resource() -> dict[str, Any]:
    p = _payload_resource()
    p["description"] = "Updated"
    return p


def _payload_risk() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-risk"),
        "description": "vis test risk",
        "project_id": "vis-project",
        "probability": 0.5,
        "impact_score": 3.0,
        "category": "technical",
        "response_strategy": "mitigate",
        "status": "identified",
    }


def _update_risk() -> dict[str, Any]:
    p = _payload_risk()
    p["description"] = "Updated"
    return p


def _payload_benefit() -> dict[str, Any]:
    return {
        "title": _unique_title("vis-benefit"),
        "description": "vis test benefit",
        "benefit_type": "non_financial",
        "measurement": "things measured",
        "status": "expected",
    }


def _update_benefit() -> dict[str, Any]:
    p = _payload_benefit()
    p["description"] = "Updated"
    return p


# ---------------------------------------------------------------------------
# Entity descriptors
# ---------------------------------------------------------------------------
#
# Each descriptor describes a CRUD-shaped owned entity:
#
#   id            : pytest parameter id
#   list_path     : POST creates here, GET lists here
#   item_path(id) : GET / PUT / DELETE for that row
#   make_payload  : returns a fresh create payload
#   update_payload: returns an update payload (used by PUT)
#   has_put       : False for relationships (no PUT)
#
# The 12 CRUD-shaped owned entities.

ENTITIES: list[dict[str, Any]] = [
    {
        "id": "requirement",
        "list_path": "/requirements",
        "item_path": lambda eid: f"/requirements/{eid}",
        "make_payload": _payload_requirement,
        "update_payload": _update_requirement,
        "has_put": True,
    },
    {
        "id": "principle",
        "list_path": "/principles",
        "item_path": lambda eid: f"/principles/{eid}",
        "make_payload": _payload_principle,
        "update_payload": _update_principle,
        "has_put": True,
    },
    {
        "id": "decision",
        "list_path": "/decisions",
        "item_path": lambda eid: f"/decisions/{eid}",
        "make_payload": _payload_decision,
        "update_payload": _update_decision,
        "has_put": True,
    },
    {
        "id": "arch_element",
        "list_path": "/architecture/elements",
        "item_path": lambda eid: f"/architecture/elements/{eid}",
        "make_payload": _payload_arch_element,
        "update_payload": _update_arch_element,
        "has_put": True,
    },
    {
        "id": "arch_view",
        "list_path": "/architecture/views",
        "item_path": lambda eid: f"/architecture/views/{eid}",
        "make_payload": _payload_arch_view,
        "update_payload": _update_arch_view,
        "has_put": True,
    },
    {
        "id": "arch_standard",
        "list_path": "/architecture/standards",
        "item_path": lambda eid: f"/architecture/standards/{eid}",
        "make_payload": _payload_arch_standard,
        "update_payload": _update_arch_standard,
        "has_put": True,
    },
    {
        "id": "programme",
        "list_path": "/pm/programmes",
        "item_path": lambda eid: f"/pm/programmes/{eid}",
        "make_payload": _payload_programme,
        "update_payload": _update_programme,
        "has_put": True,
    },
    {
        "id": "project",
        "list_path": "/pm/projects",
        "item_path": lambda eid: f"/pm/projects/{eid}",
        "make_payload": _payload_project,
        "update_payload": _update_project,
        "has_put": True,
    },
    {
        "id": "resource",
        "list_path": "/pm/resources",
        "item_path": lambda eid: f"/pm/resources/{eid}",
        "make_payload": _payload_resource,
        "update_payload": _update_resource,
        "has_put": True,
    },
    {
        "id": "risk",
        "list_path": "/pm/risks",
        "item_path": lambda eid: f"/pm/risks/{eid}",
        "make_payload": _payload_risk,
        "update_payload": _update_risk,
        "has_put": True,
    },
    {
        "id": "benefit",
        "list_path": "/pm/benefits",
        "item_path": lambda eid: f"/pm/benefits/{eid}",
        "make_payload": _payload_benefit,
        "update_payload": _update_benefit,
        "has_put": True,
    },
    # ArchitectureRelationship is special: it requires real architecture
    # elements as source/target, so its create payload is built dynamically
    # in the test methods below from elements created in the same tenant.
    # It's still parametrized in this list with a marker that the tests
    # check for to dispatch to the relationship-specific helper.
    {
        "id": "arch_relationship",
        "list_path": "/architecture/relationships",
        "item_path": lambda eid: f"/architecture/relationships/{eid}",
        "make_payload": None,  # built per-test
        "update_payload": None,  # no PUT
        "has_put": False,
        "is_relationship": True,
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create(
    client: AsyncClient,
    entity: dict[str, Any],
    tenant: str,
    subject: str,
    visibility: str = "tenant",
) -> dict[str, Any]:
    """POST and assert success. Returns the created row JSON."""
    if entity.get("is_relationship"):
        # Build endpoints (two arch elements) in the SAME tenant + subject.
        src = await _create_arch_element_for_rel(client, tenant, subject, visibility)
        tgt = await _create_arch_element_for_rel(client, tenant, subject, visibility)
        payload = {
            "source_entity_type": "architecture_elements",
            "source_id": src["id"],
            "target_entity_type": "architecture_elements",
            "target_id": tgt["id"],
            "relationship_type": "association",
            "name": f"vis-rel-{uuid.uuid4().hex[:6]}",
        }
    else:
        payload = entity["make_payload"]()
        if entity["id"] == "project":
            payload = await typed(client, payload, _hdrs(tenant, subject))

    resp = await client.post(
        entity["list_path"], json=payload, headers=_hdrs(tenant, subject, visibility)
    )
    assert resp.status_code in (200, 201), (
        f"create {entity['id']} failed (visibility={visibility}, "
        f"tenant={tenant}, subject={subject}): {resp.status_code} {resp.text}"
    )
    return resp.json()


async def _create_arch_element_for_rel(
    client: AsyncClient, tenant: str, subject: str, visibility: str
) -> dict[str, Any]:
    """Helper to mint an architecture element for a relationship under the
    same scope as the relationship itself (so the relationship's metamodel
    validation can find both endpoints)."""
    payload = {
        "title": _unique_title("rel-elem"),
        "layer": "application",
        "element_type": "application_component",
        "status": "draft",
    }
    resp = await client.post(
        "/architecture/elements",
        json=payload,
        headers=_hdrs(tenant, subject, visibility),
    )
    assert resp.status_code in (200, 201), (
        f"create rel endpoint element failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


async def _get(client: AsyncClient, entity: dict[str, Any], eid: str, tenant: str, subject: str):
    return await client.get(entity["item_path"](eid), headers=_hdrs(tenant, subject))


async def _list(client: AsyncClient, entity: dict[str, Any], tenant: str, subject: str):
    return await client.get(entity["list_path"], headers=_hdrs(tenant, subject))


async def _put(
    client: AsyncClient,
    entity: dict[str, Any],
    eid: str,
    tenant: str,
    subject: str,
    payload_factory: Callable[[], dict[str, Any]] | None = None,
):
    payload = (payload_factory or entity["update_payload"])()
    return await client.put(entity["item_path"](eid), json=payload, headers=_hdrs(tenant, subject))


async def _delete(client: AsyncClient, entity: dict[str, Any], eid: str, tenant: str, subject: str):
    return await client.delete(entity["item_path"](eid), headers=_hdrs(tenant, subject))


# ---------------------------------------------------------------------------
# Tests — parametrized over all 12 entities × 6 logical tests = 72 instances
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity", ENTITIES, ids=[e["id"] for e in ENTITIES])
class TestVisibility:
    """Six visibility tests applied to each of the 15 owned entities."""

    # -----------------------------------------------------------------
    # 1. tenant-visible row seen by other user in same tenant
    # -----------------------------------------------------------------
    async def test_tenant_visible_seen_by_other_user(
        self, test_client: AsyncClient, entity: dict[str, Any]
    ):
        created = await _create(test_client, entity, TENANT_A, USER_A, "tenant")
        eid = created["id"]

        # Bob (same tenant, different subject) can GET it
        resp = await _get(test_client, entity, eid, TENANT_A, USER_B)
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == eid

        # And it shows in the LIST as bob too
        list_resp = await _list(test_client, entity, TENANT_A, USER_B)
        assert list_resp.status_code == 200, list_resp.text
        ids = {row["id"] for row in list_resp.json()}
        assert eid in ids, f"tenant-visible {entity['id']} not in bob's list"

    # -----------------------------------------------------------------
    # 2. user-visible row HIDDEN from other user in same tenant
    # -----------------------------------------------------------------
    async def test_user_visible_hidden_from_other_user(
        self, test_client: AsyncClient, entity: dict[str, Any]
    ):
        created = await _create(test_client, entity, TENANT_A, USER_A, "user")
        eid = created["id"]

        # Bob (same tenant) cannot see it
        resp = await _get(test_client, entity, eid, TENANT_A, USER_B)
        assert resp.status_code == 404, (
            f"BOLA leak: {entity['id']} GET returned {resp.status_code} for non-creator"
        )

        # And it doesn't show up in bob's list either
        list_resp = await _list(test_client, entity, TENANT_A, USER_B)
        assert list_resp.status_code == 200, list_resp.text
        ids = {row["id"] for row in list_resp.json()}
        assert eid not in ids, f"user-visible {entity['id']} leaked into bob's list"

    # -----------------------------------------------------------------
    # 3. user-visible row visible to creator
    # -----------------------------------------------------------------
    async def test_user_visible_seen_by_creator(
        self, test_client: AsyncClient, entity: dict[str, Any]
    ):
        created = await _create(test_client, entity, TENANT_A, USER_A, "user")
        eid = created["id"]

        # Alice can still see her own private row
        resp = await _get(test_client, entity, eid, TENANT_A, USER_A)
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == eid

        list_resp = await _list(test_client, entity, TENANT_A, USER_A)
        assert list_resp.status_code == 200, list_resp.text
        ids = {row["id"] for row in list_resp.json()}
        assert eid in ids

    # -----------------------------------------------------------------
    # 4. cross-tenant isolation holds (both visibilities)
    # -----------------------------------------------------------------
    async def test_cross_tenant_isolation(self, test_client: AsyncClient, entity: dict[str, Any]):
        tenant_row = await _create(test_client, entity, TENANT_A, USER_A, "tenant")
        user_row = await _create(test_client, entity, TENANT_A, USER_A, "user")

        # Tenant B (any subject) sees neither
        for row in (tenant_row, user_row):
            resp = await _get(test_client, entity, row["id"], TENANT_B, USER_A)
            assert resp.status_code == 404, (
                f"cross-tenant leak: {entity['id']} {row['id']} visible to TENANT_B "
                f"(visibility={'user' if row is user_row else 'tenant'})"
            )

        list_resp = await _list(test_client, entity, TENANT_B, USER_A)
        assert list_resp.status_code == 200, list_resp.text
        ids = {row["id"] for row in list_resp.json()}
        assert tenant_row["id"] not in ids
        assert user_row["id"] not in ids

    # -----------------------------------------------------------------
    # 5. creator can mutate their own user-visible row
    # -----------------------------------------------------------------
    async def test_creator_can_mutate_own_user_row(
        self, test_client: AsyncClient, entity: dict[str, Any]
    ):
        created = await _create(test_client, entity, TENANT_A, USER_A, "user")
        eid = created["id"]

        if entity["has_put"]:
            put_resp = await _put(test_client, entity, eid, TENANT_A, USER_A)
            assert put_resp.status_code == 200, (
                f"creator PUT on own user row failed: {put_resp.status_code} {put_resp.text}"
            )

        # Creator can DELETE
        del_resp = await _delete(test_client, entity, eid, TENANT_A, USER_A)
        assert del_resp.status_code in (200, 204), (
            f"creator DELETE on own user row failed: {del_resp.status_code} {del_resp.text}"
        )

        # And after delete the row is gone for the creator too
        gone = await _get(test_client, entity, eid, TENANT_A, USER_A)
        assert gone.status_code == 404

    # -----------------------------------------------------------------
    # 6. other user CANNOT mutate user-visible row (404, not 403)
    # -----------------------------------------------------------------
    async def test_other_user_cannot_mutate_user_row(
        self, test_client: AsyncClient, entity: dict[str, Any]
    ):
        created = await _create(test_client, entity, TENANT_A, USER_A, "user")
        eid = created["id"]

        if entity["has_put"]:
            # Bob tries to PUT — must 404 (not 403, don't leak existence)
            put_resp = await _put(test_client, entity, eid, TENANT_A, USER_B)
            assert put_resp.status_code == 404, (
                f"BOLA leak: {entity['id']} PUT returned {put_resp.status_code} for "
                f"non-creator, expected 404"
            )

        # Bob also cannot DELETE
        del_resp = await _delete(test_client, entity, eid, TENANT_A, USER_B)
        assert del_resp.status_code == 404, (
            f"BOLA leak: {entity['id']} DELETE returned {del_resp.status_code} for "
            f"non-creator, expected 404"
        )

        # And after the failed mutations, alice's row is still intact
        intact = await _get(test_client, entity, eid, TENANT_A, USER_A)
        assert intact.status_code == 200
