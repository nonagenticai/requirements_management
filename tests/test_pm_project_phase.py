"""Project TYPE + current PHASE on pm_projects; ``PUT /pm/projects/{eid}/phase``; phase events.

George 2026-10-13: RM owns a project's type and current phase; n8n is the only writer;
fr reads them. v4 ruling 2 (D16): every project is created in its type's ``setup`` system
phase, so ``_kind`` puts ``setup`` first in every test type. All tests hit real PostgreSQL through the FastAPI test client — no mocks,
except the race tests, which disable a pre-check to reach the database constraint behind it.
"""

import uuid
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.briefs import MIN_BRIEF, with_brief
from tests.test_pm_catalogue import _As, _phase, _post

SPINE = ("request", "preliminary", "contract", "execution", "release")
SETUP = "setup"
TYPE_SPINE = (SETUP, *SPINE)


async def _kind(c: "AsyncClient | _As", type_name: str = "feature", phases=SPINE) -> dict:
    """A project type over ``setup`` + ``phases`` (created in the tenant's catalogue as
    needed). Every project starts in ``setup`` (v4 ruling 2), so every type selects it."""
    phases = (SETUP, *phases)
    have = {p["name"]: p for p in (await c.get("/pm/project-phases")).json()}
    ph: dict = {}
    for n in phases:
        ph[n] = have.get(n) or await _phase(c, n)
    pt = await _post(
        c, "/pm/project-types", {"name": type_name, "phase_ids": [ph[n]["id"] for n in phases]}
    )
    return {"type": pt, "phases": ph}


async def _project(c: "AsyncClient | _As", title: str, type_id: str, **kw) -> dict:
    body: dict = with_brief({"title": title, "project_type_id": type_id, **kw})
    return await _post(c, "/pm/projects", body)


async def _set(c: "AsyncClient | _As", pid: str, **body):
    return await c.put(f"/pm/projects/{pid}/phase", json=body)


async def _events(c: "AsyncClient | _As", pid: str, **params) -> list[dict]:
    resp = await c.get(f"/pm/projects/{pid}/phase-events", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _msg(resp) -> str:
    return resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Project fields
# ---------------------------------------------------------------------------


class TestProjectTypeField:
    async def test_create_starts_in_setup_with_the_first_event(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        assert p["project_type_id"] == k["type"]["id"]
        assert (p["current_phase"], p["current_phase_id"]) == (SETUP, k["phases"][SETUP]["id"])
        assert p["phase_updated_at"] is not None
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert got["current_phase"] == SETUP
        events = await _events(c, p["id"])
        assert [(e["from_phase_id"], e["to_phase_id"]) for e in events] == [
            (None, k["phases"][SETUP]["id"])
        ]
        assert [(e["from_phase"], e["to_phase"]) for e in events] == [(None, SETUP)]
        assert events[0]["at"][:19] == p["phase_updated_at"][:19]

    @pytest.mark.parametrize("body", [{}, {"project_type_id": None}])
    async def test_create_without_a_type_is_422_project_type_required(
        self, authenticated_client: AsyncClient, body: dict
    ):
        c = authenticated_client
        resp = await c.post("/pm/projects", json=with_brief({"title": "untyped", **body}))
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "project_type_required"
        assert (await c.get("/pm/projects")).json() == []

    async def test_type_without_setup_is_409_and_writes_nothing(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        req = await _phase(c, "request")
        bare = await _post(c, "/pm/project-types", {"name": "no_setup", "phase_ids": [req["id"]]})
        resp = await c.post(
            "/pm/projects", json=with_brief({"title": "x", "project_type_id": bare["id"]})
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "project_type_missing_setup_phase"
        assert (await c.get("/pm/projects")).json() == []
        # A setup phase in the tenant's catalogue is not enough: the TYPE must select it.
        await _phase(c, SETUP)
        again = await c.post(
            "/pm/projects", json=with_brief({"title": "x", "project_type_id": bare["id"]})
        )
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "project_type_missing_setup_phase"

    async def test_unknown_type_is_422(self, authenticated_client: AsyncClient):
        c = authenticated_client
        resp = await c.post(
            "/pm/projects",
            json=with_brief({"title": "x", "project_type_id": str(uuid.uuid4())}),
        )
        assert resp.status_code == 422, resp.text
        assert (await c.get("/pm/projects")).json() == []

    async def test_put_omitting_type_keeps_it(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        assert (await _set(c, p["id"], phase="request")).status_code == 200
        upd = await c.put(f"/pm/projects/{p['id']}", json={"title": "renamed"})
        assert upd.status_code == 200, upd.text
        body = upd.json()
        assert body["title"] == "renamed"
        assert body["project_type_id"] == k["type"]["id"]
        assert body["current_phase"] == "request"

    async def test_type_change_keeping_the_phase_is_allowed(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        other = await _kind(c, "bug_hunt", ("request", "execution"))
        p = await _project(c, "p1", k["type"]["id"])
        await _set(c, p["id"], phase="execution")
        upd = await c.put(
            f"/pm/projects/{p['id']}",
            json={"title": "p1", "project_type_id": other["type"]["id"]},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["project_type_id"] == other["type"]["id"]
        assert upd.json()["current_phase"] == "execution"

    async def test_type_change_that_strands_the_phase_is_409(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        other = await _kind(c, "bug_hunt", ("request", "execution"))
        p = await _project(c, "p1", k["type"]["id"])
        await _set(c, p["id"], phase="contract")
        for new_type in (other["type"]["id"], None):
            resp = await c.put(
                f"/pm/projects/{p['id']}", json={"title": "p1", "project_type_id": new_type}
            )
            assert resp.status_code == 409, resp.text
            assert "contract" in _msg(resp)
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert got["project_type_id"] == k["type"]["id"], "never silently changed"
        assert got["current_phase"] == "contract", "never silently cleared"

    async def test_type_change_in_setup_keeps_setup(self, authenticated_client: AsyncClient):
        """A project in ``setup`` may move to another type (every type selects setup); it
        can never lose its type, because the phase is never null and never cleared."""
        c = authenticated_client
        k = await _kind(c)
        other = await _kind(c, "bug_hunt", ("request",))
        p = await _project(c, "p1", k["type"]["id"])
        resp = await c.put(
            f"/pm/projects/{p['id']}", json={"title": "p1", "project_type_id": other["type"]["id"]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["project_type_id"] == other["type"]["id"]
        assert resp.json()["current_phase"] == SETUP
        untyped = await c.put(
            f"/pm/projects/{p['id']}", json={"title": "p1", "project_type_id": None}
        )
        assert untyped.status_code == 409, untyped.text
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["project_type_id"] == other["type"][
            "id"
        ]

    async def test_origin_lookup_returns_the_current_phase(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _kind(c)
        origin = str(uuid.uuid4())
        body = {
            "title": "minted",
            "origin_kind": "project",
            "origin_project_id": origin,
            "project_type_id": k["type"]["id"],
            "brief": MIN_BRIEF,
        }
        p = await _post(c, "/pm/projects", body)
        await _project(c, "unrelated", k["type"]["id"])
        assert (await _set(c, p["id"], phase="preliminary")).status_code == 200

        found = (await c.get("/pm/projects", params={"origin_project_id": origin})).json()
        assert [x["id"] for x in found] == [p["id"]]
        assert found[0]["current_phase"] == "preliminary"
        assert found[0]["current_phase_id"] == k["phases"]["preliminary"]["id"]
        assert found[0]["phase_updated_at"] is not None

        # The get-or-create re-POST (no brief) keeps the type and the phase; it lands on the
        # existing row, so the brief is not required. POST always requires the type (v4
        # ruling 2), the re-POST included.
        body.pop("brief")
        again = await _post(c, "/pm/projects", body)
        assert again["id"] == p["id"]
        assert again["project_type_id"] == k["type"]["id"]
        assert again["current_phase"] == "preliminary"


# ---------------------------------------------------------------------------
# PUT /pm/projects/{eid}/phase
# ---------------------------------------------------------------------------


class TestSetPhase:
    async def test_set_by_id_and_by_name(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        req = k["phases"]["request"]

        by_id = await _set(c, p["id"], phase_id=req["id"])
        assert by_id.status_code == 200, by_id.text
        out = by_id.json()
        assert out["project_id"] == p["id"]
        assert out["phase"] == {"id": req["id"], "name": "request"}
        assert out["previous_phase"] == {"id": k["phases"][SETUP]["id"], "name": SETUP}
        assert out["changed"] is True and out["phase_updated_at"]

        by_name = await _set(c, p["id"], phase="preliminary")
        assert by_name.status_code == 200, by_name.text
        out = by_name.json()
        assert out["phase"]["name"] == "preliminary"
        assert out["previous_phase"] == {"id": req["id"], "name": "request"}

        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert got["current_phase"] == "preliminary"
        assert got["current_phase_id"] == k["phases"]["preliminary"]["id"]

    @pytest.mark.parametrize("case", ["neither", "explicit_nulls", "both"])
    async def test_exactly_one_target_or_422(self, authenticated_client: AsyncClient, case: str):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        body = {
            "neither": {},
            "explicit_nulls": {"phase": None, "phase_id": None},
            "both": {"phase": "request", "phase_id": k["phases"]["request"]["id"]},
        }[case]
        resp = await c.put(f"/pm/projects/{p['id']}/phase", json=body)
        assert resp.status_code == 422, resp.text
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["current_phase"] == SETUP

    async def test_phase_not_in_the_type_is_422_naming_the_allowed(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c, "bug_hunt", ("request", "execution"))
        stray = await _phase(c, "closed")  # in the tenant's catalogue, not in the type
        p = await _project(c, "p1", k["type"]["id"])
        for body in ({"phase_id": stray["id"]}, {"phase": "closed"}, {"phase": "nope"}):
            resp = await _set(c, p["id"], **body)
            assert resp.status_code == 422, resp.text
            assert "['setup', 'request', 'execution']" in _msg(resp)
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["current_phase"] == SETUP
        assert len(await _events(c, p["id"])) == 1, "only the create's null → setup"

    async def test_no_type_is_409(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        """A LEGACY row (created before v4 ruling 2: no type, no phase) cannot be phased."""
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "legacy", k["type"]["id"])
        await test_session.execute(
            text(
                "UPDATE pm_projects SET current_phase_id = NULL, project_type_id = NULL "
                "WHERE id = :id"
            ),
            {"id": p["id"]},
        )
        await test_session.commit()
        resp = await _set(c, p["id"], phase="request")
        assert resp.status_code == 409, resp.text
        assert "project has no project_type_id" in _msg(resp)

    async def test_gate_skip_forward_and_backwards_move_both_succeed(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        gate = await _post(
            c,
            "/pm/gates",
            {
                "name": "contract_signed",
                "phase_id": k["phases"]["contract"]["id"],
                "approver_role": "project_owner",
            },
        )
        assert gate["phase_id"] == k["phases"]["contract"]["id"]
        p = await _project(c, "p1", k["type"]["id"])
        await _set(c, p["id"], phase="request")
        skip = await _set(c, p["id"], phase="release")  # past the gated contract phase
        assert skip.status_code == 200 and skip.json()["changed"] is True, skip.text
        back = await _set(c, p["id"], phase="preliminary")
        assert back.status_code == 200 and back.json()["changed"] is True, back.text
        assert back.json()["previous_phase"]["name"] == "release"

    async def test_restamp_is_idempotent(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        first = (await _set(c, p["id"], phase="request")).json()
        again = await _set(c, p["id"], phase_id=k["phases"]["request"]["id"])
        assert again.status_code == 200, again.text
        out = again.json()
        assert out["changed"] is False
        assert out["phase"]["name"] == "request"
        assert out["previous_phase"]["name"] == "request"
        assert out["phase_updated_at"] == first["phase_updated_at"]
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert (
            got["phase_updated_at"].replace("Z", "+00:00")[:26]
            == first["phase_updated_at"].replace("Z", "+00:00")[:26]
        )
        assert len(await _events(c, p["id"])) == 2, "create + request; a re-stamp writes none"

    async def test_restart_on_the_current_phase_starts_a_new_visit(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        first = (await _set(c, p["id"], phase="request")).json()
        restart = await _set(c, p["id"], phase="request", restart=True)
        assert restart.status_code == 200, restart.text
        out = restart.json()
        assert out["changed"] is True
        assert (
            out["phase"]
            == out["previous_phase"]
            == {
                "id": k["phases"]["request"]["id"],
                "name": "request",
            }
        )
        assert datetime.fromisoformat(out["phase_updated_at"]) > datetime.fromisoformat(
            first["phase_updated_at"]
        )
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert got["current_phase"] == "request"
        assert (
            got["phase_updated_at"].replace("Z", "+00:00")[:26]
            == out["phase_updated_at"].replace("Z", "+00:00")[:26]
        )
        events = await _events(c, p["id"])
        assert len(events) == 3, "create + request + restart"
        newest = events[0]
        assert (newest["from_phase"], newest["to_phase"], newest["kind"]) == (
            "request",
            "request",
            "phase",
        )
        assert newest["from_phase_id"] == newest["to_phase_id"]

    async def test_restart_false_on_the_current_phase_is_still_a_no_op(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        first = (await _set(c, p["id"], phase="request")).json()
        again = await _set(c, p["id"], phase="request", restart=False)
        assert again.status_code == 200, again.text
        assert again.json()["changed"] is False
        assert again.json()["phase_updated_at"] == first["phase_updated_at"]
        assert len(await _events(c, p["id"])) == 2

    async def test_restart_with_a_stale_if_current_phase_is_409_and_writes_nothing(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        first = (await _set(c, p["id"], phase="request")).json()
        stale = await _set(c, p["id"], phase="request", restart=True, if_current_phase="contract")
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "phase_mismatch"
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert got["current_phase"] == "request"
        assert (
            got["phase_updated_at"].replace("Z", "+00:00")[:26]
            == first["phase_updated_at"].replace("Z", "+00:00")[:26]
        )
        assert len(await _events(c, p["id"])) == 2, "create + request; the refused restart none"

    async def test_restart_on_another_phase_is_an_ordinary_change(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        moved = await _set(c, p["id"], phase="request", restart=True)
        assert moved.status_code == 200 and moved.json()["changed"] is True, moved.text
        assert moved.json()["previous_phase"]["name"] == SETUP
        assert len(await _events(c, p["id"])) == 2

    async def test_if_current_phase(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        req = k["phases"]["request"]

        # A fresh project is in setup: an explicit null ("expect no phase") is refused ...
        fresh = await _set(c, p["id"], phase="request", if_current_phase=None)
        assert fresh.status_code == 409, fresh.text
        assert '"name": "setup"' in _msg(fresh)
        ok = await _set(c, p["id"], phase="request", if_current_phase=SETUP)
        assert ok.status_code == 200, ok.text
        # ... and so is any other stale expectation once the phase moved.
        stale = await _set(c, p["id"], phase="preliminary", if_current_phase=None)
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "phase_mismatch"
        assert f'"id": "{req["id"]}", "name": "request"' in _msg(stale)

        for expected in ("contract", k["phases"]["contract"]["id"]):
            miss = await _set(c, p["id"], phase="preliminary", if_current_phase=expected)
            assert miss.status_code == 409, miss.text
            assert '"name": "request"' in _msg(miss)
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["current_phase"] == "request"

        by_name = await _set(c, p["id"], phase="preliminary", if_current_phase="request")
        assert by_name.status_code == 200 and by_name.json()["changed"] is True
        by_id = await _set(
            c, p["id"], phase="contract", if_current_phase=k["phases"]["preliminary"]["id"]
        )
        assert by_id.status_code == 200 and by_id.json()["changed"] is True
        assert len(await _events(c, p["id"])) == 4

    async def test_an_event_per_real_change_newest_first(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "p1", k["type"]["id"])
        path = ["request", "preliminary", "preliminary", "execution", "request"]
        for name in path:
            assert (await _set(c, p["id"], phase=name)).status_code == 200

        resp = await c.get(f"/pm/projects/{p['id']}/phase-events")
        assert resp.headers["X-Total-Count"] == "5"
        events = resp.json()
        assert [(e["from_phase"], e["to_phase"]) for e in events] == [
            ("execution", "request"),
            ("preliminary", "execution"),
            ("request", "preliminary"),
            (SETUP, "request"),
            (None, SETUP),
        ]
        assert events[-1]["from_phase_id"] is None
        assert events[0]["to_phase_id"] == k["phases"]["request"]["id"]
        assert all(e["project_id"] == p["id"] and e["subject_id"] for e in events)
        page = await _events(c, p["id"], limit=2, offset=1)
        assert [e["id"] for e in page] == [e["id"] for e in events[1:3]]
        # The project's phase_updated_at is the newest event's time.
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert got["phase_updated_at"][:19] == events[0]["at"][:19]

    async def test_unknown_project_is_404(self, authenticated_client: AsyncClient):
        c = authenticated_client
        missing = str(uuid.uuid4())
        assert (await _set(c, missing, phase="request")).status_code == 404
        assert (await c.get(f"/pm/projects/{missing}/phase-events")).status_code == 404


# ---------------------------------------------------------------------------
# Catalogue changes under a project
# ---------------------------------------------------------------------------


class TestCatalogueGuards:
    async def test_set_phases_that_would_strand_a_project_is_409(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "stuck-project", k["type"]["id"])
        await _set(c, p["id"], phase="contract")
        keep = [k["phases"][n]["id"] for n in TYPE_SPINE if n != "contract"]
        resp = await c.put(
            f"/pm/project-types/{k['type']['id']}", json={"name": "feature", "phase_ids": keep}
        )
        assert resp.status_code == 409, resp.text
        assert "stuck-project" in _msg(resp)
        got = (await c.get(f"/pm/project-types/{k['type']['id']}")).json()
        assert [x["name"] for x in got["phases"]] == list(TYPE_SPINE)
        # Dropping a phase no project sits on, and reordering, are fine.
        drop = [k["phases"][n]["id"] for n in reversed(TYPE_SPINE) if n != "release"]
        ok = await c.put(
            f"/pm/project-types/{k['type']['id']}", json={"name": "feature", "phase_ids": drop}
        )
        assert ok.status_code == 200, ok.text
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["current_phase"] == "contract"

    async def test_deleting_a_type_in_use_is_409_naming_the_projects(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _kind(c)
        p = await _project(c, "user-of-type", k["type"]["id"])
        resp = await c.delete(f"/pm/project-types/{k['type']['id']}")
        assert resp.status_code == 409, resp.text
        assert "project user-of-type" in _msg(resp)
        assert (await c.get(f"/pm/project-types/{k['type']['id']}")).status_code == 200
        # Once no project uses it, it deletes. (A project can no longer drop its type —
        # its phase is never null — so the project goes instead.)
        assert (await c.delete(f"/pm/projects/{p['id']}")).status_code == 200
        assert (await c.delete(f"/pm/project-types/{k['type']['id']}")).status_code == 200


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    async def test_other_tenant_sees_touches_and_references_nothing(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        ka = await _kind(a)
        pa = await _project(a, "a-project", ka["type"]["id"])
        assert (await _set(a, pa["id"], phase="request")).status_code == 200

        # B cannot read, move or list the history of A's project.
        assert (await _set(b, pa["id"], phase="request")).status_code == 404
        assert (await b.get(f"/pm/projects/{pa['id']}/phase-events")).status_code == 404
        assert (await b.get(f"/pm/projects/{pa['id']}")).status_code == 404

        # B may not type its project with A's project type, on create or on update.
        foreign = await b.post(
            "/pm/projects", json=with_brief({"title": "b", "project_type_id": ka["type"]["id"]})
        )
        assert foreign.status_code == 422, foreign.text
        kb = await _kind(b)
        pb = await _project(b, "b-project", kb["type"]["id"])
        put = await b.put(
            f"/pm/projects/{pb['id']}", json={"title": "b", "project_type_id": ka["type"]["id"]}
        )
        assert put.status_code == 422, put.text
        # B may not move its project onto A's phase id (not one of B's type's phases).
        stray = await _set(b, pb["id"], phase_id=ka["phases"]["request"]["id"])
        assert stray.status_code == 422, stray.text

        # A's project and history are untouched.
        assert (await a.get(f"/pm/projects/{pa['id']}")).json()["current_phase"] == "request"
        assert len(await _events(a, pa["id"])) == 2
        assert len(await _events(b, pb["id"])) == 1, "only B's own null → setup"


# ---------------------------------------------------------------------------
# The database constraints behind the checks
# ---------------------------------------------------------------------------


class TestDatabaseBackstop:
    async def test_composite_fk_refuses_a_raw_stranded_update(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        c = authenticated_client
        k = await _kind(c, "bug_hunt", ("request", "execution"))
        stray = await _phase(c, "closed")
        p = await _project(c, "p1", k["type"]["id"])
        await _set(c, p["id"], phase="request")

        with pytest.raises(IntegrityError, match="fk_pm_projects_selected_phase"):
            await test_session.execute(
                text("UPDATE pm_projects SET current_phase_id = :ph WHERE id = :id"),
                {"ph": stray["id"], "id": p["id"]},
            )
            await test_session.commit()  # DEFERRED: checked here
        await test_session.rollback()

        with pytest.raises(IntegrityError, match="ck_pm_projects_phase_needs_type"):
            await test_session.execute(
                text("UPDATE pm_projects SET project_type_id = NULL WHERE id = :id"),
                {"id": p["id"]},
            )
            await test_session.commit()
        await test_session.rollback()

        with pytest.raises(IntegrityError, match="fk_pm_projects_selected_phase"):
            await test_session.execute(
                text(
                    "DELETE FROM pm_project_type_phases "
                    "WHERE project_type_id = :t AND phase_id = :ph"
                ),
                {"t": k["type"]["id"], "ph": k["phases"]["request"]["id"]},
            )
            await test_session.commit()
        await test_session.rollback()
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["current_phase"] == "request"

    @pytest.mark.parametrize("race", ["type_change", "set_phases", "set_phase"])
    async def test_constraint_behind_a_skipped_precheck_is_409(
        self, authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, race: str
    ):
        import services.pm_catalogue_service as cat_svc
        import services.pm_project_phase_service as phase_svc

        c = authenticated_client
        k = await _kind(c)
        other = await _kind(c, "bug_hunt", ("request", "execution"))
        stray = await _phase(c, "closed")
        p = await _project(c, "p1", k["type"]["id"])
        await _set(c, p["id"], phase="contract")

        if race == "type_change":
            monkeypatch.setattr(phase_svc, "assert_type_change_keeps_phase", AsyncMock())
            resp = await c.put(
                f"/pm/projects/{p['id']}",
                json={"title": "p1", "project_type_id": other["type"]["id"]},
            )
        elif race == "set_phases":
            monkeypatch.setattr(cat_svc, "_stranded_projects", AsyncMock(return_value=[]))
            keep = [k["phases"][n]["id"] for n in TYPE_SPINE if n != "contract"]
            resp = await c.put(
                f"/pm/project-types/{k['type']['id']}",
                json={"name": "feature", "phase_ids": keep},
            )
        else:
            # The phase left the type between the membership check and the write.
            everything = AsyncMock(
                return_value=[phase_svc.PhaseRef(id=uuid.UUID(stray["id"]), name="closed")]
            )
            monkeypatch.setattr(phase_svc, "type_phases", everything)
            resp = await _set(c, p["id"], phase="closed")
        assert resp.status_code == 409, resp.text
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert got["project_type_id"] == k["type"]["id"]
        assert got["current_phase"] == "contract"
        assert len(await _events(c, p["id"])) == 2
        pt = (await c.get(f"/pm/project-types/{k['type']['id']}")).json()
        assert [x["name"] for x in pt["phases"]] == list(TYPE_SPINE)
