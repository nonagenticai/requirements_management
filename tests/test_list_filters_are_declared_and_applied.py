"""A query param a list route DECLARES must change the result. Nothing caught this.

⛔ The defect this file exists to catch, measured on ``main``:

``GET /requirements`` declared only ``limit`` and ``offset``. fr's AG-UI list
actions were sending ``project_id``, ``origin``, ``bound_to`` and ``visible_in``.
FastAPI discards an undeclared query param **silently** — no 422, no warning, a
200 — so the caller asked for one project's requirements and received the entire
tenant, with no signal at all that its filter had been dropped. Same shape on
``GET /principles`` (declared ``active_only``/``limit``/``offset``) and on
``GET /decisions`` (``origin_kind`` dropped; ``project_id`` there was already
honoured).

The half that no existing test could see is the *inverse*: a param that IS
declared and is then never applied. That one does not even leave the fingerprint
of an undeclared param — it appears in ``/openapi.json``, it type-checks, a
client generator emits it, and it narrows nothing. So this file asserts the
class, not the instances: for each list route, every query param the OpenAPI
schema says it declares must be provable to change the result set for some
input, and a newly declared param with no probe **fails** rather than passing by
omission.

The other half of the contract is that a filter may only ever narrow WITHIN the
tenant boundary. ``EntityService.list_all`` builds from ``scope(...)`` (tenant +
subject visibility) and adds each filter as a further ``.where``, so widening is
structurally impossible — but "structurally impossible" is the kind of claim
that stops being true quietly, so it is asserted here against real rows planted
in a second tenant.

Two-tenant coverage lives here rather than in ``tests/integration/`` on purpose:
that suite drives one session-scoped client with one ``X-Tenant-Id``, and it may
not contain a skip of any spelling (``tests/test_integration_suite_can_fail.py``
pins the escape ceiling at 0). The per-filter narrowing controls live there.
"""

from __future__ import annotations

import pytest

from main import app
from tests.tenants import T

TENANT = T("list-filters-primary")
OTHER_TENANT = T("list-filters-other")

PROJECT_A = "proj-alpha"
PROJECT_B = "proj-beta"
# Any valid UUID: origin_kind='project' requires origin_project_id (the
# OriginFieldsInMixin validator mirrors the DB CHECK ck_*_origin_consistency).
ORIGIN_PROJECT_ID = "0195f48b-d000-7000-8000-0000000000aa"
# G8: the project that takes a copy of the first seeded row (see ``_seed_project_copy``),
# and a project that has no copies at all.
COPY_PROJECT_ID = "0195f48b-d000-7000-8000-0000000000ab"
NO_COPY_PROJECT_ID = "0195f48b-d000-7000-8000-0000000000ac"


def _h(tenant: str) -> dict[str, str]:
    """Trusted-network auth: ASGITransport reports 127.0.0.1, so a bare uuid works."""
    return {"X-Tenant-Id": tenant}


async def _post(client, path: str, tenant: str, payload: dict) -> dict:
    """Create a row, naming the status and body when it fails. Never skips."""
    r = await client.post(path, headers=_h(tenant), json=payload)
    assert r.status_code in (200, 201), f"setup: POST {path} -> {r.status_code}: {r.text}"
    return r.json()


async def _ids(client, path: str, tenant: str, params: dict) -> list[str]:
    r = await client.get(path, headers=_h(tenant), params=params)
    assert r.status_code == 200, f"GET {path} {params} -> {r.status_code}: {r.text}"
    return [row["id"] for row in r.json()]


# --------------------------------------------------------------------------- #
# Seeds. Each one plants rows such that EVERY declared param of its route has
# both a matching and a non-matching row — otherwise "the filter narrowed" is
# unprovable and the probe below would be vacuous.
# --------------------------------------------------------------------------- #
async def seed_requirements(client, tenant: str) -> list[str]:
    a = await _post(
        client,
        "/requirements",
        tenant,
        {
            "title": f"req alpha {tenant[:8]}",
            "description": "tenant-origin, project A",
            "effort": 1.0,
            "project_id": PROJECT_A,
        },
    )
    b = await _post(
        client,
        "/requirements",
        tenant,
        {
            "title": f"req beta {tenant[:8]}",
            "description": "project-origin, project B",
            "effort": 2.0,
            "project_id": PROJECT_B,
            "origin_kind": "project",
            "origin_project_id": ORIGIN_PROJECT_ID,
        },
    )
    return [a["id"], b["id"]]


async def seed_principles(client, tenant: str) -> list[str]:
    a = await _post(
        client,
        "/principles",
        tenant,
        {
            "title": f"principle tenant-origin {tenant[:8]}",
            "statement": "active, tenant origin",
        },
    )
    b = await _post(
        client,
        "/principles",
        tenant,
        {
            "title": f"principle project-origin {tenant[:8]}",
            "statement": "active, project origin",
            "origin_kind": "project",
            "origin_project_id": ORIGIN_PROJECT_ID,
        },
    )
    c = await _post(
        client,
        "/principles",
        tenant,
        {
            "title": f"principle inactive {tenant[:8]}",
            "statement": "inactive, tenant origin",
            "active": False,
        },
    )
    return [a["id"], b["id"], c["id"]]


async def seed_decisions(client, tenant: str) -> list[str]:
    a = await _post(
        client,
        "/decisions",
        tenant,
        {
            "title": f"decision alpha {tenant[:8]}",
            "context": "c",
            "decision": "d",
            "status": "proposed",
            "project_id": PROJECT_A,
        },
    )
    b = await _post(
        client,
        "/decisions",
        tenant,
        {
            "title": f"decision beta {tenant[:8]}",
            "context": "c",
            "decision": "d",
            "status": "accepted",
            "project_id": PROJECT_B,
            "origin_kind": "project",
            "origin_project_id": ORIGIN_PROJECT_ID,
        },
    )
    return [a["id"], b["id"]]


async def _seed_project_copy(client, path: str, tenant: str, original_id: str) -> str:
    """G8: ``COPY_PROJECT_ID``'s copy of ``original_id`` — the row the project view acts on."""
    r = await client.post(
        f"{path}/{original_id}/project-copy",
        headers=_h(tenant),
        json={"project_id": COPY_PROJECT_ID, "changes": {"title": f"copy {original_id[:8]}"}},
    )
    assert r.status_code == 200, f"setup: project-copy {path} -> {r.status_code}: {r.text}"
    return r.json()["id"]


SEEDERS = {
    "/requirements": seed_requirements,
    "/principles": seed_principles,
    "/decisions": seed_decisions,
}

# One probe per DECLARED query param: a value that must change the result set
# against the seed above. A declared param absent from its route's map is a
# FAILURE, not a pass — that is the whole point of the guard.
PROBES: dict[str, dict[str, dict]] = {
    "/requirements": {
        "project_id": {"project_id": PROJECT_A},
        "origin_kind": {"origin_kind": "project"},
        "limit": {"limit": 1},
        "offset": {"offset": 1},
        # A project with no copy: the other project's copy leaves the list.
        "scope_project_id": {"scope_project_id": NO_COPY_PROJECT_ID},
        "include_shadowed": {"scope_project_id": COPY_PROJECT_ID, "include_shadowed": "true"},
    },
    "/principles": {
        "active_only": {"active_only": "true"},
        "origin_kind": {"origin_kind": "project"},
        "project_id": {"project_id": ORIGIN_PROJECT_ID},
        "limit": {"limit": 1},
        "offset": {"offset": 1},
        "scope_project_id": {"scope_project_id": NO_COPY_PROJECT_ID},
        "include_shadowed": {"scope_project_id": COPY_PROJECT_ID, "include_shadowed": "true"},
    },
    "/decisions": {
        "project_id": {"project_id": PROJECT_A},
        "status": {"status": "accepted"},
        "origin_kind": {"origin_kind": "project"},
        "limit": {"limit": 1},
        "offset": {"offset": 1},
        "scope_project_id": {"scope_project_id": NO_COPY_PROJECT_ID},
        "include_shadowed": {"scope_project_id": COPY_PROJECT_ID, "include_shadowed": "true"},
    },
}

# A param that only acts TOGETHER with another is compared against the list under that
# other param alone, not against the unfiltered list — otherwise the probe would pass on
# the other param's effect and prove nothing about its own.
PROBE_BASELINES: dict[str, dict[str, str]] = {
    "include_shadowed": {"scope_project_id": COPY_PROJECT_ID},
}


def _declared_query_params(path: str) -> set[str]:
    """Query params GET ``path`` declares, read from the app's own OpenAPI schema.

    Read from the schema rather than from ``inspect.signature`` so the guard sees
    exactly what a client generator sees — which is what the caller builds
    against, and therefore what "declared" has to mean here.
    """
    paths = app.openapi()["paths"]
    # The routers mount at ``prefix="/requirements"`` with ``@router.get("")``,
    # so the schema key is the bare prefix. Tolerate the trailing-slash spelling
    # rather than silently returning an empty set if that ever changes — an empty
    # set would make every assertion below pass by finding nothing.
    item = paths.get(path) or paths.get(path + "/")
    assert item is not None, f"GET {path} is not in the OpenAPI schema at all"
    operation = item["get"]
    return {p["name"] for p in operation.get("parameters", []) if p.get("in") == "query"}


class TestDeclaredParamsAreApplied:
    """The class guard: declared ⇒ it must change the result for some input."""

    @pytest.mark.parametrize("path", sorted(SEEDERS))
    async def test_every_declared_query_param_changes_the_result(self, test_client, path):
        seeded = await SEEDERS[path](test_client, TENANT)
        seeded.append(await _seed_project_copy(test_client, path, TENANT, seeded[0]))
        baseline = await _ids(test_client, path, TENANT, {})

        # Vacuity floor. If the seed did not land, every probe below would
        # compare [] against [] and "the filter narrowed" would be unfalsifiable.
        assert sorted(baseline) == sorted(seeded), (
            f"seed did not land for GET {path}: expected {sorted(seeded)}, "
            f"unfiltered list returned {sorted(baseline)}"
        )
        assert len(baseline) >= 2, "need >=2 rows or limit/offset cannot be shown to narrow"

        declared = _declared_query_params(path)
        assert declared, (
            f"no query parameters discovered for GET {path} — the schema walk is "
            "broken, so 'every param is applied' proves nothing"
        )

        probes = PROBES[path]
        stale = set(probes) - declared
        assert not stale, (
            f"GET {path} has probes for {sorted(stale)}, which it no longer "
            "declares. Remove the probe or restore the param."
        )
        unprobed = declared - set(probes)
        assert not unprobed, (
            f"GET {path} declares query param(s) {sorted(unprobed)} with no probe "
            "proving they change anything. A declared-but-unapplied param is "
            "EXACTLY this defect class: it appears in /openapi.json, a generated "
            "client sends it, FastAPI parses it, the handler ignores it, and the "
            "caller gets an unfiltered 200 believing it was filtered. Either "
            "apply the param and add a probe here, or stop declaring it."
        )

        for name in sorted(declared):
            base_params = PROBE_BASELINES.get(name, {})
            base = await _ids(test_client, path, TENANT, base_params) if base_params else baseline
            got = await _ids(test_client, path, TENANT, probes[name])
            assert got != base, (
                f"GET {path}?{probes[name]} returned the SAME rows as GET {path}?{base_params} "
                f"({base}), so the declared param {name!r} changes nothing. It is accepted "
                "and dropped."
            )

    def test_the_schema_walk_can_actually_see_a_param(self):
        """Self-test. A walk that matched nothing would satisfy every assert above."""
        assert "limit" in _declared_query_params("/requirements")
        assert "in" not in _declared_query_params("/requirements"), (
            "the walk is returning parameter metadata keys, not parameter names"
        )
        # Header params must not be counted as query params, or the guard would
        # demand a probe for X-Tenant-Id and fail for the wrong reason.
        assert not {"x-tenant-id", "X-Tenant-Id"} & _declared_query_params("/requirements")


class TestFiltersNarrowAndNeverWiden:
    """The new filters narrow within the tenant, and cannot reach across it."""

    async def test_requirements_project_id_narrows(self, test_client):
        """⛔ Would have FAILED before this change: project_id was undeclared, so
        FastAPI dropped it and this returned BOTH rows with a 200."""
        seeded = await seed_requirements(test_client, TENANT)
        got = await _ids(test_client, "/requirements", TENANT, {"project_id": PROJECT_A})
        assert got == [seeded[0]], f"expected only the project-A row, got {got}"

    async def test_requirements_origin_kind_narrows(self, test_client):
        seeded = await seed_requirements(test_client, TENANT)
        got = await _ids(test_client, "/requirements", TENANT, {"origin_kind": "project"})
        assert got == [seeded[1]], f"expected only the project-origin row, got {got}"

    async def test_principles_origin_kind_narrows(self, test_client):
        seeded = await seed_principles(test_client, TENANT)
        got = await _ids(test_client, "/principles", TENANT, {"origin_kind": "project"})
        assert got == [seeded[1]], f"expected only the project-origin row, got {got}"

    async def test_principles_project_id_publishes_what_it_filters_on(self):
        """The description must say it is provenance (origin_project_id), not scoping."""
        params = app.openapi()["paths"]["/principles"]["get"]["parameters"]
        desc = next(p for p in params if p["name"] == "project_id")["description"]
        assert "origin_project_id" in desc and "MINTED" in desc, desc

    async def test_decisions_origin_kind_narrows(self, test_client):
        seeded = await seed_decisions(test_client, TENANT)
        got = await _ids(test_client, "/decisions", TENANT, {"origin_kind": "project"})
        assert got == [seeded[1]], f"expected only the project-origin row, got {got}"

    @pytest.mark.parametrize(
        "path,params",
        [
            ("/requirements", {"project_id": PROJECT_A}),
            ("/requirements", {"origin_kind": "project"}),
            ("/principles", {"origin_kind": "project"}),
            ("/principles", {"project_id": ORIGIN_PROJECT_ID}),
            ("/decisions", {"origin_kind": "project"}),
            ("/decisions", {"project_id": PROJECT_A}),
        ],
    )
    @pytest.mark.tenant_isolation
    async def test_a_filter_never_reaches_across_the_tenant_boundary(
        self, test_client, path, params
    ):
        """The other tenant's rows match the filter exactly — and must stay invisible.

        This is the assertion that matters: a filter is an extra ``.where`` on a
        query that is ALREADY tenant-scoped, so it can only ever shrink the set.
        The seeds are deliberately identical in both tenants, so a filter that
        somehow replaced the scope instead of narrowing it would show up here as
        the other tenant's ids appearing in the response.
        """
        mine = await SEEDERS[path](test_client, TENANT)
        theirs = await SEEDERS[path](test_client, OTHER_TENANT)

        got = await _ids(test_client, path, TENANT, params)
        assert got, f"filter {params} matched nothing in my own tenant — seed is wrong"
        assert set(got) <= set(mine), f"{path} {params} leaked rows outside my tenant: {got}"
        assert not set(got) & set(theirs), (
            f"{path} {params} returned the OTHER tenant's rows: {sorted(set(got) & set(theirs))}"
        )


class TestFilterValuesBehaveSensibly:
    """Absent, unmatched, and out-of-domain filter values."""

    @pytest.mark.parametrize("path", sorted(SEEDERS))
    async def test_absent_filters_are_unfiltered_within_the_tenant(self, test_client, path):
        mine = await SEEDERS[path](test_client, TENANT)
        theirs = await SEEDERS[path](test_client, OTHER_TENANT)
        got = await _ids(test_client, path, TENANT, {})
        assert sorted(got) == sorted(mine), f"no filter should return my whole tenant, got {got}"
        assert not set(got) & set(theirs), "unfiltered is still tenant-scoped"

    async def test_an_unmatched_project_id_returns_nothing_not_everything(self, test_client):
        """⛔ Would have FAILED before this change: the param was dropped and this
        returned every requirement in the tenant."""
        await seed_requirements(test_client, TENANT)
        got = await _ids(test_client, "/requirements", TENANT, {"project_id": "no-such-project"})
        assert got == [], f"an unmatched project filter must return [], got {got}"

    @pytest.mark.parametrize("path", ["/requirements", "/decisions"])
    async def test_an_empty_project_id_is_not_a_filter(self, test_client, path):
        """``?project_id=`` (empty) is 'no opinion', not 'match the empty string'.

        An empty string is what a form or a JS client sends for an untouched
        select, and ``project_id`` is an open-domain column, so it carries no
        pattern to reject one. ``EntityService.list_all`` skips ``None`` and
        ``""`` (``src/services/entity_service.py:446``), so it must behave as
        absent — still tenant-scoped — rather than returning nothing. This mirrors
        the write side's ``ProjectScope`` normalisation ("" means no project).
        """
        mine = await SEEDERS[path](test_client, TENANT)
        got = await _ids(test_client, path, TENANT, {"project_id": ""})
        assert sorted(got) == sorted(mine), f"empty filter should be no filter, got {got}"

    @pytest.mark.parametrize("path", sorted(SEEDERS))
    async def test_an_out_of_domain_origin_kind_is_422_not_a_silent_no_op(self, test_client, path):
        """``origin_kind``'s domain is closed by the DB CHECK ``ck_*_origin_kind``
        (``tenant``/``project``/``workflow``), so a value outside it can never match
        a row. Accepting it with a 200 would be the same lie in a new place: a knob
        the caller can turn that means nothing. The pattern mirrors
        ``schemas/common.py::OriginFieldsInMixin``.
        """
        r = await test_client.get(
            path, headers=_h(TENANT), params={"origin_kind": "not-a-real-kind"}
        )
        assert r.status_code == 422, (
            f"GET {path}?origin_kind=not-a-real-kind -> {r.status_code}: {r.text}"
        )


class TestParamsThatBelongToFrAreNotDeclaredHere:
    """``card_id``/``bound_to``/``visible_in`` must stay OUT of these routes."""

    @pytest.mark.parametrize("path", sorted(SEEDERS))
    @pytest.mark.parametrize("name", ["card_id", "bound_to", "visible_in"])
    def test_fr_local_params_are_not_declared(self, path, name):
        """Measured: ``card_id`` exists on no model in this estate, and
        ``bound_to``/``visible_in`` are resolved against fr's own
        ``asset_bindings`` table, which this service does not have.

        Declaring one here would recreate the defect in the other direction — a
        param that parses, narrows nothing, and reads to the caller as honoured.
        The list routes have no column to apply it to, so the honest surface is
        not to offer it. If this test ever fails, the fix is to remove the param,
        not to update the test.
        """
        assert name not in _declared_query_params(path), (
            f"GET {path} declares {name!r}, which no column here can satisfy — "
            "it belongs to fr's asset_bindings, not to this service"
        )
