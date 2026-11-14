"""A project's own WIP limit on ``pm_projects`` + the effective limit + ``GET .../wip-limit``.

George 2026-10-13: "RM's default limit lets only one card per project be in DOING at a
time. That is the WIP limit, and we should define wip-limit in RM on the project fields of
the API." The project value is the project TOTAL and wins; otherwise the tenant's catalogue
project-total row (per project type, else tenant-global) applies; otherwise none. All tests
hit real PostgreSQL through the FastAPI test client.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.briefs import with_brief
from tests.project_types import typed
from tests.test_pm_catalogue import _As, _post


async def _defaults(c: "AsyncClient | _As") -> dict[str, dict]:
    """Import the default catalogue (1 per project type); project types by name."""
    await _post(c, "/pm/catalogue/import-defaults", {})
    return {t["name"]: t for t in (await c.get("/pm/project-types")).json()}


async def _project(c: "AsyncClient | _As", title: str, **kw) -> dict:
    return await _post(c, "/pm/projects", await typed(c, with_brief({"title": title, **kw})))


async def _wip(c: "AsyncClient | _As", pid: str, **params) -> dict:
    resp = await c.get(f"/pm/projects/{pid}/wip-limit", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _eff(p: dict) -> tuple:
    return p["wip_limit"], p["effective_wip_limit"], p["wip_limit_source"]


class TestField:
    async def test_negative_is_422_on_create_and_put(self, authenticated_client: AsyncClient):
        c = authenticated_client
        resp = await c.post(
            "/pm/projects", json=await typed(c, with_brief({"title": "x", "wip_limit": -1}))
        )
        assert resp.status_code == 422, resp.text
        assert (await c.get("/pm/projects")).json() == []
        p = await _project(c, "p", wip_limit=2)
        resp = await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "wip_limit": -5})
        assert resp.status_code == 422, resp.text
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["wip_limit"] == 2

    async def test_database_check_rejects_negative(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        p = await _project(authenticated_client, "p")
        with pytest.raises(IntegrityError, match="ck_pm_projects_wip_limit_nonneg"):
            await test_session.execute(
                text("UPDATE pm_projects SET wip_limit = -1 WHERE id = :id"), {"id": p["id"]}
            )
        await test_session.rollback()
        # Control: the same statement with 0 is accepted, so the CHECK is what refused -1.
        await test_session.execute(
            text("UPDATE pm_projects SET wip_limit = 0 WHERE id = :id"), {"id": p["id"]}
        )
        await test_session.commit()

    async def test_put_omitting_keeps_and_explicit_null_clears(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _project(c, "p", wip_limit=3)
        assert _eff(p) == (3, 3, "project")
        kept = await c.put(f"/pm/projects/{p['id']}", json={"title": "renamed"})
        assert kept.status_code == 200, kept.text
        assert kept.json()["title"] == "renamed"
        assert kept.json()["wip_limit"] == 3
        changed = await c.put(f"/pm/projects/{p['id']}", json={"title": "r", "wip_limit": 5})
        assert changed.json()["wip_limit"] == 5
        cleared = await c.put(f"/pm/projects/{p['id']}", json={"title": "r", "wip_limit": None})
        assert cleared.status_code == 200, cleared.text
        assert _eff(cleared.json()) == (None, None, "none")
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["wip_limit"] is None

    async def test_zero_is_honoured(self, authenticated_client: AsyncClient):
        c = authenticated_client
        types = await _defaults(c)
        p = await _project(c, "paused", wip_limit=0, project_type_id=types["it"]["id"])
        assert _eff(p) == (0, 0, "project"), "0 is a pause, not 'unset'"
        w = await _wip(c, p["id"])
        assert w["effective_wip_limit"] == 0 and w["source"] == "project"
        assert w["catalogue_limit_id"] is None


class TestEffective:
    async def test_from_project_catalogue_and_none(self, authenticated_client: AsyncClient):
        c = authenticated_client
        none = await _project(c, "bare")
        assert _eff(none) == (None, None, "none")
        assert (await _wip(c, none["id"])) == {
            "project_id": none["id"],
            "wip_limit": None,
            "effective_wip_limit": None,
            "source": "none",
            "catalogue_limit_id": None,
            "scoped": None,
        }

        types = await _defaults(c)
        tid = types["it"]["id"]
        typed = await _project(c, "typed", project_type_id=tid)
        assert _eff(typed) == (None, 1, "catalogue")
        row = (await c.get("/pm/wip-limits/resolve", params={"project_type_id": tid})).json()
        w = await _wip(c, typed["id"])
        assert w["source"] == "catalogue" and w["effective_wip_limit"] == 1
        assert w["catalogue_limit_id"] == row["id"]

        own = await _project(c, "own", project_type_id=tid, wip_limit=4)
        assert _eff(own) == (4, 4, "project"), "the project value wins over the catalogue"
        assert (await _wip(c, own["id"]))["catalogue_limit_id"] is None

        # No per-type row → no limit; a tenant-global row → that one.
        assert _eff((await c.get(f"/pm/projects/{none['id']}")).json())[2] == "none"
        glob = await _post(c, "/pm/wip-limits", {"wip_limit": 7})
        got = (await c.get(f"/pm/projects/{none['id']}")).json()
        assert _eff(got) == (None, 7, "catalogue")
        assert (await _wip(c, none["id"]))["catalogue_limit_id"] == glob["id"]
        # The per-type row still beats the tenant-global one.
        assert _eff((await c.get(f"/pm/projects/{typed['id']}")).json()) == (None, 1, "catalogue")

    async def test_setting_a_type_follows_the_tenants_catalogue(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        types = await _defaults(c)
        p = await _project(c, "p")
        assert _eff(p) == (None, None, "none")
        # The v4 IT type: it selects ``setup``, the phase the project is in (kaizen does not —
        # R184: one IT type ``it``).
        tid = types["it"]["id"]
        rows = (await c.get("/pm/wip-limits", params={"project_type_id": tid})).json()
        total = next(r for r in rows if r["card_type_id"] is None and r["phase_id"] is None)
        await c.put(f"/pm/wip-limits/{total['id']}", json={"project_type_id": tid, "wip_limit": 2})
        upd = await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "project_type_id": tid})
        assert upd.status_code == 200, upd.text
        assert _eff(upd.json()) == (None, 2, "catalogue")
        listed = {x["id"]: x for x in (await c.get("/pm/projects")).json()}
        assert _eff(listed[p["id"]]) == (None, 2, "catalogue")

    async def test_scoped_card_type_row(self, authenticated_client: AsyncClient):
        c = authenticated_client
        types = await _defaults(c)
        tid = types["it"]["id"]
        cts = (await c.get("/pm/card-types", params={"limit": 1000})).json()
        ct, other_ct = cts[0], cts[1]
        cap_row = await _post(
            c, "/pm/wip-limits", {"project_type_id": tid, "card_type_id": ct["id"], "wip_limit": 3}
        )
        p = await _project(c, "p", project_type_id=tid, wip_limit=2)

        w = await _wip(c, p["id"], card_type_id=ct["id"])
        assert w["effective_wip_limit"] == 2 and w["source"] == "project", "total: project wins"
        assert w["scoped"] == {
            "id": cap_row["id"],
            "wip_limit": 3,
            "matched_on": ["project_type_id", "card_type_id"],
        }
        assert (await _wip(c, p["id"], card_type_id=other_ct["id"]))["scoped"] is None
        assert (await _wip(c, p["id"]))["scoped"] is None, "no dimension given → no scoped"


class TestIsolation:
    async def test_another_tenants_catalogue_is_never_used(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        a_types = await _defaults(a)
        a_tid = a_types["it"]["id"]
        await _post(a, "/pm/wip-limits", {"wip_limit": 9})  # A's tenant-global row
        cts = (await a.get("/pm/card-types", params={"limit": 1000})).json()
        await _post(
            a,
            "/pm/wip-limits",
            {"project_type_id": a_tid, "card_type_id": cts[0]["id"], "wip_limit": 5},
        )

        bp = await _project(b, "b-project")
        assert _eff(bp) == (None, None, "none")
        w = await _wip(b, bp["id"], card_type_id=cts[0]["id"])
        assert w["source"] == "none" and w["scoped"] is None
        listed = (await b.get("/pm/projects")).json()
        assert [_eff(x) for x in listed] == [(None, None, "none")]
        # B cannot even read A's project's limit.
        ap = await _project(a, "a-project", project_type_id=a_tid)
        assert _eff(ap) == (None, 1, "catalogue")
        assert (await b.get(f"/pm/projects/{ap['id']}/wip-limit")).status_code == 404

    async def test_scoped_lookup_never_matches_another_tenants_card_type_row(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        """A's row scoped ONLY by card type would win B's query if the scoped lookup
        (``_wip_stmt``) lost its tenant filter: B's untyped project, asked with A's card
        type id, matches every dimension of it. B must get nothing back."""
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        await _defaults(a)
        await _defaults(b)
        a_cts = {ct["name"]: ct for ct in (await a.get("/pm/card-types?limit=1000")).json()}
        b_cts = {ct["name"]: ct for ct in (await b.get("/pm/card-types?limit=1000")).json()}
        name = sorted(a_cts.keys() & b_cts.keys())[0]
        a_ct, b_ct = a_cts[name]["id"], b_cts[name]["id"]
        assert a_ct != b_ct, "each tenant owns its own card type of the same name"
        a_row = await _post(a, "/pm/wip-limits", {"card_type_id": a_ct, "wip_limit": 5})

        # Control: the row does match the query shape, for its owner.
        ap = await _project(a, "a-untyped")
        assert (await _wip(a, ap["id"], card_type_id=a_ct))["scoped"]["id"] == a_row["id"]
        a_res = await a.get("/pm/wip-limits/resolve", params={"card_type_id": a_ct})
        assert a_res.status_code == 200 and a_res.json()["id"] == a_row["id"], a_res.text

        bp = await _project(b, "b-untyped")
        for ct in (a_ct, b_ct):
            w = await _wip(b, bp["id"], card_type_id=ct)
            assert w["scoped"] is None, f"B got A's row: {w}"
            assert (w["source"], w["catalogue_limit_id"]) == ("none", None)
            res = await b.get("/pm/wip-limits/resolve", params={"card_type_id": ct})
            assert res.status_code in (404, 422), f"B resolved A's row: {res.text}"
            assert a_row["id"] not in res.text

    async def test_system_project_never_uses_the_callers_catalogue(
        self, test_client: AsyncClient, test_session: AsyncSession
    ):
        """A PUBLISHED system-tenant project inherited into a platform tenant's read keeps
        its own value or ``none``; the viewer's catalogue (global + card-type rows that
        would match an untyped project) is never applied to it."""
        from settings import get_settings

        system = get_settings().system_tenant_id
        viewer_id = "0195f48b-d000-7000-8000-000000000003"  # execution: a platform tenant
        assert viewer_id in get_settings().platform_tenant_id_set
        sys_c, viewer = _As(test_client, system), _As(test_client, viewer_id)

        sp = await _project(sys_c, "system-untyped")
        await test_session.execute(
            text("UPDATE pm_projects SET version_status = 'PUBLISHED' WHERE id = :id"),
            {"id": sp["id"]},
        )
        await test_session.commit()

        await _defaults(viewer)
        ct = (await viewer.get("/pm/card-types", params={"limit": 1000})).json()[0]["id"]
        await _post(viewer, "/pm/wip-limits", {"wip_limit": 9})
        await _post(viewer, "/pm/wip-limits", {"card_type_id": ct, "wip_limit": 5})

        # Control: the viewer's own untyped project DOES use those rows.
        own = await _project(viewer, "own-untyped")
        assert _eff(own) == (None, 9, "catalogue")
        assert (await _wip(viewer, own["id"], card_type_id=ct))["scoped"]["wip_limit"] == 5

        got = await viewer.get(f"/pm/projects/{sp['id']}")
        assert got.status_code == 200, f"the system project must be visible: {got.text}"
        assert got.json()["is_system"] is True
        assert _eff(got.json()) == (None, None, "none")
        listed = {x["id"]: x for x in (await viewer.get("/pm/projects")).json()}
        assert _eff(listed[sp["id"]]) == (None, None, "none")
        w = await _wip(viewer, sp["id"], card_type_id=ct)
        assert (w["effective_wip_limit"], w["source"]) == (None, "none")
        assert w["catalogue_limit_id"] is None and w["scoped"] is None

    async def test_unknown_project_is_404(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.get(f"/pm/projects/{uuid.uuid4()}/wip-limit")
        assert resp.status_code == 404, resp.text


class TestBoundedQueries:
    async def test_list_does_not_query_per_project(
        self, authenticated_client: AsyncClient, test_engine
    ):
        c = authenticated_client
        # Only types that select ``setup`` can create a project: ``it`` (R184) and ``kata``
        # (Kata K15). The others have no phases; ``py2rust`` is retired (0083, 2026-11-07).
        types = [t for t in (await _defaults(c)).values() if t["phases"]]
        assert [t["name"] for t in types] == ["it", "kata"]
        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        async def _count_list() -> tuple[int, int]:
            statements.clear()
            event.listen(test_engine.sync_engine, "before_cursor_execute", _record)
            try:
                resp = await c.get("/pm/projects", params={"limit": 1000})
            finally:
                event.remove(test_engine.sync_engine, "before_cursor_execute", _record)
            assert resp.status_code == 200, resp.text
            wip_selects = [s for s in statements if "FROM pm_wip_limits" in s]
            return len(statements), len(wip_selects)

        await _project(c, "p0", project_type_id=types[0]["id"])
        small, small_wip = await _count_list()
        assert small_wip == 1, f"the listener is blind or the lookup is missing: {statements}"

        for i, t in enumerate(types * 2):
            await _project(c, f"p{i + 1}", project_type_id=t["id"])
        await _project(c, "untyped")
        await _project(c, "own", wip_limit=2)
        listed = (await c.get("/pm/projects", params={"limit": 1000})).json()
        assert len(listed) == 2 * len(types) + 3
        assert all(
            x["wip_limit_source"] in ("project", "catalogue")
            for x in listed
            if x["title"] != "untyped"
        )

        big, big_wip = await _count_list()
        assert big_wip == 1, "one catalogue query per page, never one per project"
        assert big == small, f"query count grew with the page: {small} → {big}"


class TestReworkLimit:
    """``pm_projects.rework_limit``: NULL = ``DEFAULT_REWORK_LIMIT`` (2)."""

    async def test_default_is_the_named_constant(self, authenticated_client: AsyncClient):
        from schemas.pm import DEFAULT_REWORK_LIMIT

        assert DEFAULT_REWORK_LIMIT == 2
        p = await _project(authenticated_client, "p")
        assert (p["rework_limit"], p["effective_rework_limit"]) == (None, 2)

    async def test_negative_is_422_on_create_and_put(self, authenticated_client: AsyncClient):
        c = authenticated_client
        resp = await c.post(
            "/pm/projects", json=await typed(c, with_brief({"title": "x", "rework_limit": -1}))
        )
        assert resp.status_code == 422, resp.text
        assert (await c.get("/pm/projects")).json() == []
        p = await _project(c, "p", rework_limit=4)
        resp = await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "rework_limit": -2})
        assert resp.status_code == 422, resp.text
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["rework_limit"] == 4

    async def test_database_check_rejects_negative(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        p = await _project(authenticated_client, "p")
        with pytest.raises(IntegrityError, match="ck_pm_projects_rework_limit_nonneg"):
            await test_session.execute(
                text("UPDATE pm_projects SET rework_limit = -1 WHERE id = :id"), {"id": p["id"]}
            )
        await test_session.rollback()
        await test_session.execute(
            text("UPDATE pm_projects SET rework_limit = 0 WHERE id = :id"), {"id": p["id"]}
        )
        await test_session.commit()

    async def test_put_omitting_keeps_and_explicit_null_clears(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _project(c, "p", rework_limit=5)
        assert (p["rework_limit"], p["effective_rework_limit"]) == (5, 5)
        kept = (await c.put(f"/pm/projects/{p['id']}", json={"title": "renamed"})).json()
        assert (kept["title"], kept["rework_limit"], kept["effective_rework_limit"]) == (
            "renamed",
            5,
            5,
        )
        cleared = await c.put(f"/pm/projects/{p['id']}", json={"title": "r", "rework_limit": None})
        assert cleared.status_code == 200, cleared.text
        assert (cleared.json()["rework_limit"], cleared.json()["effective_rework_limit"]) == (
            None,
            2,
        )
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert (got["rework_limit"], got["effective_rework_limit"]) == (None, 2)

    async def test_zero_is_honoured(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _project(c, "p", rework_limit=0)
        assert (p["rework_limit"], p["effective_rework_limit"]) == (0, 0), "0 is not 'unset'"
        listed = (await c.get("/pm/projects")).json()
        assert (listed[0]["rework_limit"], listed[0]["effective_rework_limit"]) == (0, 0)
