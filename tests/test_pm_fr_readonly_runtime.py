"""George 2026-10-13: fr (delegated principals) is read-only on the PM RUNTIME entities.

``/pm/risks``, ``/pm/resources`` and ``/pm/benefits`` are all linked to a project, so —
unlike ``POST /pm/projects`` — there is no create exception: every POST/PUT/PATCH/DELETE by a
fr-delegated caller is 403 ``project_writes_are_pm_only``.

The route walk below goes over the BUILT app (recursing through FastAPI's lazy
``_IncludedRouter`` wrappers and carrying their prefixes), so a mutating route added under a
guarded prefix without the guard goes red here, naming it. The behaviour tests hit real
PostgreSQL; only licence introspection is faked (the ``licences`` fixture).
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.routing import APIRoute, _IncludedRouter
from httpx import AsyncClient

from tests.briefs import MIN_BRIEF
from tests.project_types import typed
from tests.test_pm_project_intake import (  # noqa: F401 — `licences` is a fixture
    CUSTOMER_TENANT,
    DELEGATED,
    licences,
)

PM_ONLY = "project_writes_are_pm_only"
# The conveyor in the customer's tenant: a plain (non-delegated) licence.
CONVEYOR = {"X-Tenant-Id": CUSTOMER_TENANT}

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
GUARDED_PREFIXES = (
    "/pm/risks",
    "/pm/resources",
    "/pm/benefits",
    "/pm/cards",
)
EXPECTED_GUARDED = 12  # 4 risks + 3 resources + 4 benefits + 1 ledger


def _app_routes() -> list[tuple[str, APIRoute]]:
    """Every (full path, APIRoute) of the built app, prefixes carried through the lazy
    ``_IncludedRouter`` wrappers (a naive ``app.routes`` walk sees ~1 APIRoute)."""
    from main import app

    found: list[tuple[str, APIRoute]] = []

    def walk(routes: list, prefix: str) -> None:
        for r in routes:
            if isinstance(r, _IncludedRouter):
                walk(r.original_router.routes, prefix + r.include_context.prefix)
            elif isinstance(r, APIRoute):
                found.append((prefix + r.path, r))

    walk(app.routes, "")
    return found


def _calls(route: APIRoute) -> set[Callable[..., Any]]:
    return {d.call for d in route.dependant.dependencies if d.call is not None}


def _classify() -> tuple[list[str], list[str]]:
    from auth.pm_only import require_pm_writer

    guarded, missing = [], []
    for path, r in _app_routes():
        if not path.startswith(GUARDED_PREFIXES):
            continue
        for method in sorted(r.methods & MUTATING):
            key = f"{method} {path}"
            (guarded if require_pm_writer in _calls(r) else missing).append(key)
    return guarded, missing


def test_every_mutating_runtime_route_carries_the_guard():
    """A mutating route under a guarded prefix without ``require_pm_writer`` goes red here."""
    guarded, missing = _classify()
    assert missing == [], f"mutating PM runtime routes without the fr read-only guard: {missing}"
    # Vacuity floor on the population examined, not on violations.
    assert len(guarded) == EXPECTED_GUARDED, sorted(guarded)


def test_the_guard_stays_out_of_the_catalogue_programmes_and_governance():
    """Only ``/pm/projects/{eid}...`` and the runtime prefixes carry the guard."""
    from auth.pm_only import require_pm_writer

    stray = [
        f"{sorted(r.methods)} {path}"
        for path, r in _app_routes()
        if not path.startswith((*GUARDED_PREFIXES, "/pm/projects"))
        and require_pm_writer in _calls(r)
    ]
    assert stray == []


# ---------------------------------------------------------------------------
# Behaviour: per-entity delegated 403, conveyor 2xx
# ---------------------------------------------------------------------------


def _code(resp) -> str | None:
    body = resp.json()
    return (body.get("error") or {}).get("code") if isinstance(body, dict) else None


async def _ok(resp) -> dict:
    assert resp.status_code in (200, 201, 204), resp.text
    return resp.json() if resp.status_code != 204 else {}


def _bodies(pid: str) -> dict[str, dict]:
    """A valid create body per entity, in the project ``pid``."""
    return {
        "risks": {
            "title": "a risk",
            "project_id": pid,
            "probability": 0.3,
            "impact_score": 4.0,
            "category": "technical",
            "response_strategy": "mitigate",
        },
        "resources": {"title": "dev", "resource_type": "person", "capacity_per_day": 7.5},
        "benefits": {
            "title": "a benefit",
            "benefit_type": "non_financial",
            "measurement": "minutes",
        },
    }


# The id-bound writes per entity (beyond create); every one is a DB write.
WRITES: dict[str, list[tuple[str, str]]] = {
    "risks": [("put", ""), ("post", "/quality"), ("delete", "")],
    "resources": [("put", ""), ("delete", "")],
    "benefits": [("put", ""), ("post", "/quality"), ("delete", "")],
}


async def _seed(c: AsyncClient) -> tuple[str, str]:
    """A project fr requested, and a resource the conveyor planned for it."""
    p = await _ok(
        await c.post(
            "/pm/projects",
            json=await typed(c, {"title": "fr-ro", "brief": MIN_BRIEF}, DELEGATED),
            headers=DELEGATED,
        )
    )
    pid = p["id"]
    r1 = (
        await _ok(await c.post("/pm/resources", json=_bodies(pid)["resources"], headers=CONVEYOR))
    )["id"]
    return pid, r1


async def _exists(c: AsyncClient, entity: str, eid: str) -> bool:
    return (await c.get(f"/pm/{entity}/{eid}", headers=CONVEYOR)).status_code == 200


@pytest.mark.usefixtures("licences")
@pytest.mark.parametrize("entity", sorted(WRITES))
async def test_delegated_writes_are_403_per_entity(test_client: AsyncClient, entity: str):
    """fr may not create, update, score or delete a runtime entity: 403 and nothing changes."""
    c = test_client
    pid, _ = await _seed(c)
    body = _bodies(pid)[entity]
    # The conveyor's create, so there is an existing row for the id-bound writes.
    eid = (await _ok(await c.post(f"/pm/{entity}", json=body, headers=CONVEYOR)))["id"]

    resp = await c.post(f"/pm/{entity}", json=body, headers=DELEGATED)
    assert resp.status_code == 403, resp.text
    assert _code(resp) == PM_ONLY
    for method, suffix in WRITES[entity]:
        kwargs: dict[str, Any] = {"headers": DELEGATED}
        if method == "put":
            kwargs["json"] = body
        resp = await getattr(c, method)(f"/pm/{entity}/{eid}{suffix}", **kwargs)
        assert resp.status_code == 403, (method, suffix, resp.text)
        assert _code(resp) == PM_ONLY, (method, suffix)

    assert await _exists(c, entity, eid), "a refused delete removed the row"
    # Reads stay open to fr.
    assert (await c.get(f"/pm/{entity}", headers=DELEGATED)).status_code == 200


@pytest.mark.usefixtures("licences")
async def test_the_conveyor_still_writes_every_entity(test_client: AsyncClient):
    """Non-delegated callers are unaffected: create, update and delete are 2xx."""
    c = test_client
    pid, _ = await _seed(c)
    bodies = _bodies(pid)
    for entity in sorted(WRITES):
        eid = (await _ok(await c.post(f"/pm/{entity}", json=bodies[entity], headers=CONVEYOR)))[
            "id"
        ]
        if ("put", "") in WRITES[entity]:
            await _ok(await c.put(f"/pm/{entity}/{eid}", json=bodies[entity], headers=CONVEYOR))
        if ("post", "/quality") in WRITES[entity]:
            await _ok(await c.post(f"/pm/{entity}/{eid}/quality", headers=CONVEYOR))
        await _ok(await c.delete(f"/pm/{entity}/{eid}", headers=CONVEYOR))
        assert not await _exists(c, entity, eid), entity


# ---------------------------------------------------------------------------
# The side door: /architecture/relationships can write an edge with a PM runtime end
# ---------------------------------------------------------------------------

RELS = "/architecture/relationships"


def _rel(src_type: str, src: str, tgt_type: str, tgt: str, rel: str) -> dict:
    return {
        "source_entity_type": src_type,
        "source_id": src,
        "target_entity_type": tgt_type,
        "target_id": tgt,
        "relationship_type": rel,
    }


def _req_edge() -> dict:
    """A plain (non-PM) edge: requirement -> requirement (no metamodel endpoint lookup)."""
    import uuid

    return _rel(
        "requirements", str(uuid.uuid4()), "requirements", str(uuid.uuid4()), "traceability"
    )


def _pm_edge(r1: str) -> dict:
    """An edge with a PM runtime end: resource -> requirement."""
    import uuid

    return _rel("pm_resources", r1, "requirements", str(uuid.uuid4()), "association")


async def _pm_edge_ids(c: AsyncClient, r1: str) -> set[str]:
    rows = (
        await c.get(
            RELS, params={"source_entity_type": "pm_resources", "source_id": r1}, headers=CONVEYOR
        )
    ).json()
    return {r["id"] for r in rows}


@pytest.mark.usefixtures("licences")
async def test_delegated_pm_edge_writes_via_architecture_relationships_are_403(
    test_client: AsyncClient,
):
    """fr may not create, update or delete an edge with a PM runtime end through the generic
    relationships route: 403 ``project_writes_are_pm_only``, and nothing changes."""
    c = test_client
    _, r1 = await _seed(c)
    edge = _pm_edge(r1)

    resp = await c.post(RELS, json=edge, headers=DELEGATED)
    assert resp.status_code == 403, resp.text
    assert _code(resp) == PM_ONLY
    assert await _pm_edge_ids(c, r1) == set(), "a refused create wrote an edge"

    rid = (await _ok(await c.post(RELS, json=edge, headers=CONVEYOR)))["id"]
    resp = await c.put(f"{RELS}/{rid}", json={"name": "renamed"}, headers=DELEGATED)
    assert resp.status_code == 403, resp.text
    assert _code(resp) == PM_ONLY
    resp = await c.delete(f"{RELS}/{rid}", headers=DELEGATED)
    assert resp.status_code == 403, resp.text
    assert _code(resp) == PM_ONLY
    assert await _pm_edge_ids(c, r1) == {rid}, "a refused delete removed the edge"
    assert (await c.get(f"{RELS}/{rid}", headers=CONVEYOR)).json()["name"] is None


@pytest.mark.usefixtures("licences")
async def test_delegated_update_turning_an_edge_into_a_pm_edge_is_403(test_client: AsyncClient):
    """The AFTER state counts too: fr may not re-point a plain edge at a PM resource."""
    c = test_client
    _, r1 = await _seed(c)
    rid = (await _ok(await c.post(RELS, json=_req_edge(), headers=DELEGATED)))["id"]
    resp = await c.put(
        f"{RELS}/{rid}",
        json={"target_entity_type": "pm_resources", "target_id": r1},
        headers=DELEGATED,
    )
    assert resp.status_code == 403, resp.text
    assert _code(resp) == PM_ONLY
    row = (await c.get(f"{RELS}/{rid}", headers=CONVEYOR)).json()
    assert row["target_entity_type"] == "requirements"


@pytest.mark.usefixtures("licences")
async def test_delegated_plain_architecture_relationship_writes_stay_open(
    test_client: AsyncClient,
):
    """Neither end a PM runtime entity: fr may create, update and delete it."""
    c = test_client
    rid = (await _ok(await c.post(RELS, json=_req_edge(), headers=DELEGATED)))["id"]
    await _ok(await c.put(f"{RELS}/{rid}", json={"name": "renamed"}, headers=DELEGATED))
    await _ok(await c.delete(f"{RELS}/{rid}", headers=DELEGATED))


@pytest.mark.usefixtures("licences")
async def test_the_conveyor_writes_pm_edges_via_architecture_relationships(
    test_client: AsyncClient,
):
    """Non-delegated: the edge with a PM runtime end is created, updated and deleted."""
    c = test_client
    _, r1 = await _seed(c)
    resp = await c.post(RELS, json=_pm_edge(r1), headers=CONVEYOR)
    assert resp.status_code == 200, resp.text
    rid = resp.json()["id"]
    assert rid in await _pm_edge_ids(c, r1)
    await _ok(await c.put(f"{RELS}/{rid}", json={"name": "renamed"}, headers=CONVEYOR))
    await _ok(await c.delete(f"{RELS}/{rid}", headers=CONVEYOR))
    assert rid not in await _pm_edge_ids(c, r1)
