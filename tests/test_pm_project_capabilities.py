"""Per-project capability selection (R260 v1): ``/pm/projects/{eid}/capabilities``.

George 2026-10-15: the project type's composition is the first filter; the PM then selects
which capabilities a project needs. Only PM / n8n writes it (v4 ruling 1); fr reads it.
All tests hit real PostgreSQL through the FastAPI test client — no mocks except the licence
introspection fake (``licences``) for the fr-delegated caller.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_pm_catalogue import _As, _capability, _card_type
from tests.test_pm_project_intake import (  # noqa: F401 — `licences` is a fixture
    DELEGATED,
    licences,
)
from tests.test_pm_project_phase import _kind, _project

PHASES = ("preliminary", "execution")
# capability → {phase: [card types]} placed in the test type's composition.
COMPOSITION = {
    "analysis": {"preliminary": ["bug_analysis", "gap_analysis"]},
    "development": {"execution": ["development_bug_fix"]},
    "testing": {"preliminary": ["testing_plan"], "execution": ["test_development"]},
    "project_management": {"preliminary": ["project_phase_start"]},
    "kanban_coach": {"execution": ["flow_check"]},
}


async def _composed(c: "AsyncClient | _As", type_name: str = "it") -> dict:
    """A project type over setup + PHASES whose composition places COMPOSITION, plus a
    capability (``design``) that has a card type in the catalogue but NOT in the type."""
    k = await _kind(c, type_name, PHASES)
    per_phase: dict[str, list[str]] = {p: [] for p in PHASES}
    for cap_name, phases in COMPOSITION.items():
        cap = await _capability(c, cap_name)
        for phase, card_types in phases.items():
            for ct in card_types:
                per_phase[phase].append((await _card_type(c, ct, cap["id"]))["id"])
    outside = await _capability(c, "design")
    await _card_type(c, "diagram_design", outside["id"])
    resp = await c.put(
        f"/pm/project-types/{k['type']['id']}/card-types",
        json={
            "phases": [
                {"phase_id": k["phases"][p]["id"], "card_type_ids": ids}
                for p, ids in per_phase.items()
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    return k


async def _put(c: "AsyncClient | _As", pid: str, caps: dict[str, str], **extra):
    body = {"capabilities": [{"name": n, "reason": r} for n, r in caps.items()], **extra}
    return await c.put(f"/pm/projects/{pid}/capabilities", json=body)


async def _get(c: "AsyncClient | _As", pid: str) -> dict:
    resp = await c.get(f"/pm/projects/{pid}/capabilities")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _events(c: "AsyncClient | _As", pid: str) -> list[dict]:
    resp = await c.get(f"/pm/projects/{pid}/capabilities/events")
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Total-Count"] == str(len(resp.json()))
    return resp.json()


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def _msg(resp) -> str:
    return resp.json()["error"]["message"]


class TestGet:
    async def test_fresh_project_lists_selectable_and_implicit(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        got = await _get(c, p["id"])
        assert got["project_id"] == p["id"]
        assert got["project_type"] == "it"
        assert got["selected"] == []
        assert got["implicit"] == ["project_management", "kanban_coach"]
        assert [s["name"] for s in got["selectable"]] == ["analysis", "development", "testing"]
        by_name = {s["name"]: s for s in got["selectable"]}
        assert by_name["analysis"]["phases"] == {"preliminary": ["bug_analysis", "gap_analysis"]}
        # Phases in the type's order (preliminary before execution).
        assert list(by_name["testing"]["phases"].items()) == [
            ("preliminary", ["testing_plan"]),
            ("execution", ["test_development"]),
        ]
        assert "design" not in by_name, "a card type outside the composition is not selectable"


class TestSet:
    async def test_put_then_get(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        approval = str(uuid.uuid4())
        resp = await _put(
            c,
            p["id"],
            {"analysis": "find the bug", "development": "fix it"},
            approval_id=approval,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["changed"] is True
        assert [(s["name"], s["reason"], s["approval_id"]) for s in body["selected"]] == [
            ("analysis", "find the bug", approval),
            ("development", "fix it", approval),
        ]
        assert all(s["selected_at"] for s in body["selected"])
        got = await _get(c, p["id"])
        assert got["selected"] == body["selected"]
        assert got["selectable"] == body["selectable"]
        events = await _events(c, p["id"])
        assert [(e["added"], e["removed"], e["approval_id"]) for e in events] == [
            (["analysis", "development"], [], approval)
        ]

    async def test_identical_resend_and_reason_only_change_write_no_event(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        first = await _put(c, p["id"], {"analysis": "a"})
        assert first.json()["changed"] is True
        again = await _put(c, p["id"], {"analysis": "a"})
        assert again.status_code == 200, again.text
        assert again.json()["changed"] is False
        reworded = await _put(c, p["id"], {"analysis": "a, reworded"})
        assert reworded.status_code == 200, reworded.text
        assert reworded.json()["changed"] is False
        sel = (await _get(c, p["id"]))["selected"]
        assert [(s["name"], s["reason"]) for s in sel] == [("analysis", "a, reworded")]
        assert sel[0]["selected_at"] == first.json()["selected"][0]["selected_at"]
        assert len(await _events(c, p["id"])) == 1

    async def test_change_writes_an_event_with_added_and_removed(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        await _put(c, p["id"], {"analysis": "a", "development": "d"})
        resp = await _put(c, p["id"], {"development": "d", "testing": "t"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True
        assert [s["name"] for s in resp.json()["selected"]] == ["development", "testing"]
        events = await _events(c, p["id"])
        assert [(e["added"], e["removed"]) for e in events] == [
            (["testing"], ["analysis"]),
            (["analysis", "development"], []),
        ], "newest first"
        assert all(e["project_id"] == p["id"] and e["subject_id"] for e in events)

    async def test_not_in_composition_is_422_naming_the_offenders(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        resp = await _put(c, p["id"], {"analysis": "a", "design": "x", "nonexistent": "y"})
        assert resp.status_code == 422, resp.text
        assert _code(resp) == "capability_not_in_type_composition"
        assert "['design', 'nonexistent']" in _msg(resp)
        assert (await _get(c, p["id"]))["selected"] == []
        assert await _events(c, p["id"]) == []

    @pytest.mark.parametrize("implicit", ["project_management", "kanban_coach"])
    async def test_implicit_capability_is_422(self, authenticated_client: AsyncClient, implicit):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        resp = await _put(c, p["id"], {"analysis": "a", implicit: "x"})
        assert resp.status_code == 422, resp.text
        assert _code(resp) == "implicit_capability"
        assert implicit in _msg(resp)
        assert (await _get(c, p["id"]))["selected"] == []

    @pytest.mark.parametrize(
        "body",
        [
            {"capabilities": []},
            {"capabilities": [{"name": "analysis", "reason": ""}]},
            {"capabilities": [{"name": "analysis", "reason": "   "}]},
            {"capabilities": [{"name": "analysis"}]},
            {
                "capabilities": [
                    {"name": "analysis", "reason": "a"},
                    {"name": "analysis", "reason": "b"},
                ]
            },
        ],
    )
    async def test_empty_list_or_reason_is_422(self, authenticated_client: AsyncClient, body):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        resp = await c.put(f"/pm/projects/{p['id']}/capabilities", json=body)
        assert resp.status_code == 422, resp.text
        assert (await _get(c, p["id"]))["selected"] == []

    async def test_if_current(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        ok = await _put(c, p["id"], {"analysis": "a"}, if_current=[])
        assert ok.status_code == 200, ok.text
        stale = await _put(c, p["id"], {"development": "d"}, if_current=[])
        assert stale.status_code == 409, stale.text
        assert _code(stale) == "if_current_mismatch"
        assert "['analysis']" in _msg(stale)
        moved = await _put(c, p["id"], {"development": "d"}, if_current=["analysis"])
        assert moved.status_code == 200, moved.text
        assert [s["name"] for s in moved.json()["selected"]] == ["development"]
        assert len(await _events(c, p["id"])) == 2

    async def test_no_project_type_is_409(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        """A LEGACY row (no type, no phase) has nothing selectable and cannot be set."""
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "legacy", k["type"]["id"])
        await test_session.execute(
            text(
                "UPDATE pm_projects SET current_phase_id = NULL, project_type_id = NULL "
                "WHERE id = :id"
            ),
            {"id": p["id"]},
        )
        await test_session.commit()
        resp = await _put(c, p["id"], {"analysis": "a"})
        assert resp.status_code == 409, resp.text
        assert "project has no project_type_id" in _msg(resp)
        got = await _get(c, p["id"])
        assert (got["project_type"], got["selectable"], got["selected"]) == (None, [], [])

    async def test_unknown_project_is_404(self, authenticated_client: AsyncClient):
        c = authenticated_client
        missing = str(uuid.uuid4())
        assert (await _put(c, missing, {"analysis": "a"})).status_code == 404
        assert (await c.get(f"/pm/projects/{missing}/capabilities")).status_code == 404
        assert (await c.get(f"/pm/projects/{missing}/capabilities/events")).status_code == 404

    async def test_deleting_a_selected_capability_is_409(self, authenticated_client: AsyncClient):
        """The catalogue refuses deleting a capability a project selected (NO ACTION FK)."""
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        await _put(c, p["id"], {"analysis": "a"})
        cap = next(x for x in (await c.get("/pm/capabilities")).json() if x["name"] == "analysis")
        resp = await c.delete(f"/pm/capabilities/{cap['id']}")
        assert resp.status_code == 409, resp.text
        assert f"project {p['slug']}" in _msg(resp)


@pytest.mark.usefixtures("licences")
class TestPmOnly:
    async def test_fr_delegated_put_is_403_and_get_is_200(self, test_client: AsyncClient):
        c = test_client
        d = _Headers(c, DELEGATED)
        k = await _composed(d)
        p = await _project(d, "requested", k["type"]["id"])
        resp = await _put(d, p["id"], {"analysis": "a"})
        assert resp.status_code == 403, resp.text
        assert _code(resp) == "project_writes_are_pm_only"
        got = await _get(d, p["id"])
        assert got["selected"] == []
        assert await _events(d, p["id"]) == []


class _Headers(_As):
    """``_As`` with an arbitrary header set (the fr-delegated caller)."""

    def __init__(self, client: AsyncClient, headers: dict):
        self._c, self._h = client, headers


class TestTenantIsolation:
    async def test_other_tenant_cannot_read_or_write(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        ka = await _composed(a)
        pa = await _project(a, "a-project", ka["type"]["id"])
        assert (await _put(a, pa["id"], {"analysis": "a"})).status_code == 200

        assert (await b.get(f"/pm/projects/{pa['id']}/capabilities")).status_code == 404
        assert (await b.get(f"/pm/projects/{pa['id']}/capabilities/events")).status_code == 404
        assert (await _put(b, pa["id"], {"analysis": "b"})).status_code == 404

        # B's own type composes nothing: A's capabilities are not selectable for B.
        kb = await _kind(b, "b_type", PHASES)
        pb = await _project(b, "b-project", kb["type"]["id"])
        assert (await _get(b, pb["id"]))["selectable"] == []
        stray = await _put(b, pb["id"], {"analysis": "b"})
        assert stray.status_code == 422, stray.text

        got = await _get(a, pa["id"])
        assert [(s["name"], s["reason"]) for s in got["selected"]] == [("analysis", "a")]
        assert len(await _events(a, pa["id"])) == 1


# ---------------------------------------------------------------------------
# R302 (George 2026-10-17): the PM's per-capability consult decision
# ---------------------------------------------------------------------------

CONSULT = {
    "likely_card_types": ["bug_analysis"],
    "operation_order": {"needs": [], "feeds": [{"capability": "development", "what": "cause"}]},
    "questions": ["Which release?"],
}


def _r302(name: str, **fields) -> dict:
    return {"name": name, "reason": f"{name} needed", **fields}


async def _put_raw(c: "AsyncClient | _As", pid: str, caps: list[dict], **extra):
    return await c.put(f"/pm/projects/{pid}/capabilities", json={"capabilities": caps, **extra})


R302_FIELDS = ("involvement", "purpose", "rigor", "complexity", "consult", "order_after")


def _fields(sel: dict) -> dict:
    return {f: sel[f] for f in R302_FIELDS}


class TestConsultFields:
    async def test_put_with_r302_fields_round_trips(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        caps = [
            _r302(
                "analysis",
                involvement="heavy",
                purpose="find the root cause",
                rigor="high",
                complexity="medium",
                consult=CONSULT,
            ),
            _r302(
                "development",
                involvement="Light ",  # normalised like every PM vocabulary
                purpose="fix it",
                rigor="medium",
                complexity="low",
                order_after=["analysis"],
            ),
        ]
        resp = await _put_raw(c, p["id"], caps)
        assert resp.status_code == 200, resp.text
        by_name = {s["name"]: _fields(s) for s in resp.json()["selected"]}
        assert by_name == {
            "analysis": {
                "involvement": "heavy",
                "purpose": "find the root cause",
                "rigor": "high",
                "complexity": "medium",
                "consult": CONSULT,
                "order_after": [],
            },
            "development": {
                "involvement": "light",
                "purpose": "fix it",
                "rigor": "medium",
                "complexity": "low",
                "consult": None,
                "order_after": ["analysis"],
            },
        }
        got = await _get(c, p["id"])
        assert {s["name"]: _fields(s) for s in got["selected"]} == by_name

    async def test_old_payload_still_works_and_reads_empty_fields(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        resp = await _put(c, p["id"], {"analysis": "a"})
        assert resp.status_code == 200, resp.text
        assert _fields(resp.json()["selected"][0]) == {
            "involvement": None,
            "purpose": None,
            "rigor": None,
            "complexity": None,
            "consult": None,
            "order_after": [],
        }

    async def test_field_only_change_updates_without_event_and_omitted_clears(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        first = await _put_raw(c, p["id"], [_r302("analysis", involvement="light", rigor="low")])
        assert first.json()["changed"] is True
        again = await _put_raw(c, p["id"], [_r302("analysis", involvement="light", rigor="low")])
        assert again.json()["changed"] is False
        bumped = await _put_raw(c, p["id"], [_r302("analysis", involvement="heavy", rigor="low")])
        assert bumped.status_code == 200, bumped.text
        assert bumped.json()["changed"] is False
        assert bumped.json()["selected"][0]["involvement"] == "heavy"
        # Replace-set: the old name + reason body clears the R302 fields.
        cleared = await _put(c, p["id"], {"analysis": "analysis needed"})
        assert cleared.status_code == 200, cleared.text
        sel = (await _get(c, p["id"]))["selected"][0]
        assert (sel["involvement"], sel["rigor"]) == (None, None)
        assert sel["selected_at"] == first.json()["selected"][0]["selected_at"]
        assert len(await _events(c, p["id"])) == 1

    @pytest.mark.parametrize(
        "cap",
        [
            _r302("analysis", involvement="massive"),
            _r302("analysis", rigor="extreme"),
            _r302("analysis", complexity="trivial"),
            _r302("analysis", involvement=3),
            _r302("analysis", purpose=""),
            _r302("analysis", purpose="x" * 2001),
            _r302("analysis", consult={"blob": "x" * 16_001}),
            _r302("analysis", consult=["not", "an", "object"]),
            _r302("analysis", order_after=["analysis"]),
            _r302("analysis", order_after=["development"]),  # not selected in the request
            _r302("analysis", unknown_field="x"),
            _r302("analysis", include=True),
        ],
    )
    async def test_invalid_r302_field_is_422(self, authenticated_client: AsyncClient, cap):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        resp = await _put_raw(c, p["id"], [cap])
        assert resp.status_code == 422, resp.text
        assert (await _get(c, p["id"]))["selected"] == []

    async def test_duplicate_order_after_is_422(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _composed(c)
        p = await _project(c, "p1", k["type"]["id"])
        caps = [_r302("analysis"), _r302("development", order_after=["analysis", "analysis"])]
        resp = await _put_raw(c, p["id"], caps)
        assert resp.status_code == 422, resp.text

    async def test_other_tenant_cannot_overwrite_r302_fields(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        ka = await _composed(a)
        pa = await _project(a, "a-project", ka["type"]["id"])
        ok = await _put_raw(a, pa["id"], [_r302("analysis", involvement="light", rigor="low")])
        assert ok.status_code == 200, ok.text
        stomp = await _put_raw(b, pa["id"], [_r302("analysis", involvement="heavy", rigor="high")])
        assert stomp.status_code == 404, stomp.text
        assert (await b.get(f"/pm/projects/{pa['id']}/capabilities")).status_code == 404
        sel = (await _get(a, pa["id"]))["selected"][0]
        assert (sel["involvement"], sel["rigor"]) == ("light", "low")


class TestConsultFieldsContract:
    """The vocabularies stay in sync with the model, and reach the OpenAPI as TOP-LEVEL enums
    (the n8n node generator reads ``enum`` only there)."""

    async def test_literals_match_model_constants(self):
        from typing import get_args

        from models.pm import CAPABILITY_INVOLVEMENTS, CAPABILITY_LEVELS
        from schemas.pm import CapabilityInvolvement, CapabilityLevel

        assert set(get_args(CapabilityInvolvement)) == CAPABILITY_INVOLVEMENTS
        assert set(get_args(CapabilityLevel)) == CAPABILITY_LEVELS

    async def test_openapi_exposes_top_level_enums(self, authenticated_client: AsyncClient):
        spec = (await authenticated_client.get("/openapi.json")).json()
        props = spec["components"]["schemas"]["ProjectCapabilityIn"]["properties"]
        assert props["involvement"]["enum"] == ["none", "light", "standard", "heavy"]
        assert props["rigor"]["enum"] == ["low", "medium", "high"]
        assert props["complexity"]["enum"] == ["low", "medium", "high"]
        assert props["purpose"]["type"] == "string"
        assert props["consult"]["type"] == "object"
        assert props["order_after"]["type"] == "array"
        assert spec["components"]["schemas"]["ProjectCapabilityIn"]["required"] == [
            "name",
            "reason",
        ]
