"""REG-1: register fields on risks / decisions / requirements and
``GET /pm/projects/{eid}/registers``.

All tests hit real PostgreSQL through the FastAPI test client (conveyor = non-delegated
licence, as the n8n requirements node is).
"""

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from models.version_history import EntityVersionHistory
from tests.project_types import typed
from tests.test_pm_catalogue import _As


async def _ok(resp) -> dict:
    # ``POST /pm/risks`` with a project_id is a get-or-create: 201 created, 200 existing.
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _project(c, **brief) -> dict:
    body = await typed(c, {"title": "registers project", "brief": {"notes": "n", **brief}})
    return await _ok(await c.post("/pm/projects", json=body))


def _risk(project_id: str, **over) -> dict:
    return {
        "title": "Migration may lose data",
        "project_id": project_id,
        "probability": 0.3,
        "impact_score": 3.0,
        "category": "technical",
        "response_strategy": "mitigate",
        "response_plan": "Rehearse in staging",
        "owner": "release manager",
        **over,
    }


def _decision(project_id: str, **over) -> dict:
    return {
        "title": "Use Postgres 18",
        "project_id": project_id,
        "context": "We need a relational store.",
        "decision": "Postgres 18 with psycopg 3.",
        "alternatives": ["MySQL", "SQLite"],
        **over,
    }


def _requirement(project_id: str, **over) -> dict:
    return {
        "title": "Users can reset their password",
        "project_id": project_id,
        "description": "Via an emailed link.",
        "effort": 3.0,
        **over,
    }


async def _registers(c, pid: str, **params) -> dict:
    return await _ok(await c.get(f"/pm/projects/{pid}/registers", params=params))


class TestWriteFields:
    async def test_risk_fields_round_trip_and_raised_at_defaults(self, authenticated_client):
        c = authenticated_client
        p = await _project(c)
        due = "2026-11-01T12:00:00Z"
        r = await _ok(
            await c.post(
                "/pm/risks",
                json=_risk(p["id"], phase=" Preliminary ", card_id="1234567890", due_at=due),
            )
        )
        assert (r["phase"], r["card_id"]) == ("preliminary", "1234567890")
        assert r["due_at"].startswith("2026-11-01T12:00:00")
        assert r["raised_at"] is not None, "raised_at must default to the insert time"
        raised = r["raised_at"]

        # PUT without raised_at keeps the stored value; due_at follows PUT semantics.
        reviewed = "2026-10-27T09:00:00Z"
        put = await _ok(
            await c.put(
                f"/pm/risks/{r['id']}",
                json=_risk(p["id"], phase="execution", last_reviewed_at=reviewed),
            )
        )
        assert put["raised_at"] == raised
        assert put["phase"] == "execution"
        assert put["due_at"] is None
        assert put["last_reviewed_at"].startswith("2026-10-27T09:00:00")

        # An explicit raised_at is honoured.
        put2 = await _ok(
            await c.put(
                f"/pm/risks/{r['id']}", json=_risk(p["id"], raised_at="2026-10-01T00:00:00Z")
            )
        )
        assert put2["raised_at"].startswith("2026-10-01T00:00:00")

    async def test_bad_phase_and_card_shapes_are_422(self, authenticated_client):
        c = authenticated_client
        p = await _project(c)
        assert (
            await c.post("/pm/risks", json=_risk(p["id"], phase="no spaces"))
        ).status_code == 422
        bad_card = _risk(p["id"], card_id="a/b")
        assert (await c.post("/pm/risks", json=bad_card)).status_code == 422

    async def test_decision_fields_and_decided_by_kind_is_closed(self, authenticated_client):
        c = authenticated_client
        p = await _project(c)
        d = await _ok(
            await c.post(
                "/decisions",
                json=_decision(
                    p["id"],
                    phase="setup",
                    card_id="c-1",
                    decided_by_kind="agent",
                    decided_at="2026-10-27T10:00:00Z",
                ),
            )
        )
        assert (d["phase"], d["card_id"], d["decided_by_kind"]) == ("setup", "c-1", "agent")
        assert d["decided_at"].startswith("2026-10-27T10:00:00")
        assert d["raised_at"] is not None
        bad = _decision(p["id"], title="other", decided_by_kind="committee")
        assert (await c.post("/decisions", json=bad)).status_code == 422

        patched = await _ok(
            await c.patch(f"/decisions/{d['id']}", json={"decided_by_kind": "human"})
        )
        assert patched["decided_by_kind"] == "human"
        assert patched["raised_at"] == d["raised_at"]
        assert patched["phase"] == "setup"

    async def test_requirement_fields(self, authenticated_client):
        c = authenticated_client
        p = await _project(c)
        r = await _ok(
            await c.post(
                "/requirements",
                json=_requirement(p["id"], status=" Proposed ", owner="po", phase="preliminary"),
            )
        )
        assert (r["status"], r["owner"], r["phase"]) == ("proposed", "po", "preliminary")
        patched = await _ok(await c.patch(f"/requirements/{r['id']}", json={"status": "agreed"}))
        assert patched["status"] == "agreed" and patched["owner"] == "po"
        assert patched["raised_at"] == r["raised_at"]

    async def test_identical_repost_is_not_a_change(self, authenticated_client):
        """A re-POST that omits ``raised_at`` must not read as a change (it is not written)."""
        c = authenticated_client
        p = await _project(c)
        first = await _ok(await c.post("/requirements", json=_requirement(p["id"])))
        again = await _ok(await c.post("/requirements", json=_requirement(p["id"])))
        assert again["id"] == first["id"]
        assert again["raised_at"] == first["raised_at"]
        versions = await _ok(await c.get(f"/requirements/{first['id']}/versions"))
        assert len(versions) == 1, versions


class TestRegistersRead:
    async def test_empty_project(self, authenticated_client):
        p = await _project(authenticated_client)
        got = await _registers(authenticated_client, p["id"])
        assert got == {
            "project_id": p["id"],
            "phase": None,
            "risks": [],
            "decisions": [],
            "requirements": [],
            "criteria": [],
            "counts": {
                "risks_open": 0,
                "risks_total": 0,
                "decisions": 0,
                "requirements": 0,
                "criteria_met": 0,
                "criteria_total": 0,
            },
        }

    async def test_all_registers_with_counts_order_and_rounding(self, authenticated_client):
        c = authenticated_client
        p = await _project(
            c,
            success_criteria=["Checkout works"],
            acceptance_criteria=[{"id": "ac1", "text": "tests pass"}, "docs updated"],
        )
        other = await _project(c)
        low = await _ok(await c.post("/pm/risks", json=_risk(p["id"], phase="setup")))
        high = await _ok(
            await c.post(
                "/pm/risks",
                json=_risk(
                    p["slug"], title="Vendor slips", probability=0.7, impact_score=5.0
                ),  # matched by the project's slug too
            )
        )
        await _ok(await c.post("/pm/risks", json=_risk(p["id"], title="Old risk", status="closed")))
        await _ok(await c.post("/pm/risks", json=_risk(other["id"], title="Not ours")))
        older = await _ok(
            await c.post(
                "/decisions",
                json=_decision(p["id"], decided_by_kind="rule", decided_at="2026-10-01T00:00:00Z"),
            )
        )
        newer = await _ok(
            await c.post(
                "/decisions",
                json=_decision(p["id"], title="Ship weekly", decided_at="2026-10-20T00:00:00Z"),
            )
        )
        await _ok(await c.post("/decisions", json=_decision(other["id"], title="Elsewhere")))
        req = await _ok(
            await c.post("/requirements", json=_requirement(p["id"], status="agreed", owner="po"))
        )
        await _ok(
            await c.patch(
                f"/pm/projects/{p['id']}/criteria/ac1",
                json={"status": "met", "evidence": [{"kind": "test_run", "ref": "run-1"}]},
            )
        )

        got = await _registers(c, p["id"])
        assert [r["id"] for r in got["risks"]][:2] == [high["id"], low["id"]]
        top = got["risks"][0]
        assert top["score"] == 3.5 and top["impact"] == 5.0 and top["probability"] == 0.7
        low_row = next(r for r in got["risks"] if r["id"] == low["id"])
        assert low_row["score"] == 0.9, "0.3 x 3.0 = 0.8999… must be rounded to 2 decimals"
        assert low_row["phase"] == "setup" and low_row["owner"] == "release manager"
        assert low_row["response_plan"] == "Rehearse in staging"
        assert low_row["raised_at"] is not None
        assert {"due_at", "last_reviewed_at", "card_id", "status"} <= set(low_row)
        assert "Not ours" not in {r["title"] for r in got["risks"]}

        assert [d["id"] for d in got["decisions"]] == [newer["id"], older["id"]]
        assert got["decisions"][1]["decided_by_kind"] == "rule"
        assert got["decisions"][1]["alternatives_count"] == 2

        assert [r["id"] for r in got["requirements"]] == [req["id"]]
        assert (got["requirements"][0]["status"], got["requirements"][0]["owner"]) == (
            "agreed",
            "po",
        )

        kinds = [(c_["kind"], c_["text"], c_["status"]) for c_ in got["criteria"]]
        assert kinds == [
            ("success", "Checkout works", None),
            ("acceptance", "tests pass", "met"),
            ("acceptance", "docs updated", "unchecked"),
        ]
        assert got["criteria"][1]["id"] == "ac1"
        assert got["criteria"][1]["evidence"] == [{"kind": "test_run", "ref": "run-1"}]
        assert got["counts"] == {
            "risks_open": 2,
            "risks_total": 3,
            "decisions": 2,
            "requirements": 1,
            "criteria_met": 1,
            "criteria_total": 3,
        }

    async def test_phase_filter(self, authenticated_client):
        c = authenticated_client
        p = await _project(c, success_criteria=["x"])
        await _ok(await c.post("/pm/risks", json=_risk(p["id"], phase="setup")))
        exe = await _ok(
            await c.post("/pm/risks", json=_risk(p["id"], title="Exec", phase="execution"))
        )
        await _ok(await c.post("/decisions", json=_decision(p["id"], phase="setup")))
        await _ok(await c.post("/requirements", json=_requirement(p["id"], phase="execution")))
        got = await _registers(c, p["id"], phase="Execution")
        assert got["phase"] == "execution"
        assert [r["id"] for r in got["risks"]] == [exe["id"]]
        assert got["decisions"] == []
        assert len(got["requirements"]) == 1
        assert len(got["criteria"]) == 1, "criteria have no phase and are never filtered"
        assert got["counts"]["risks_total"] == 1 and got["counts"]["decisions"] == 0

    async def test_project_copy_listed_once_and_raised_in_the_project(self, authenticated_client):
        c = authenticated_client
        p = await _project(c)
        original = await _ok(
            await c.post(
                "/requirements",
                json={**_requirement("", title="Tenant-wide"), "phase": "setup", "card_id": "9"},
            )
        )
        copy = await _ok(
            await c.post(
                f"/requirements/{original['id']}/project-copy", json={"project_id": p["id"]}
            )
        )
        assert copy["phase"] is None and copy["card_id"] is None
        got = await _registers(c, p["id"])
        assert [r["id"] for r in got["requirements"]] == [copy["id"]]


class TestTenantIsolation:
    async def test_cross_tenant_eid_is_404_and_rows_never_leak(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        pa = await _project(a)
        pb = await _project(b)
        await _ok(await a.post("/pm/risks", json=_risk(pa["id"])))
        # Tenant B files a risk under A's project id: it must not appear for A, and B's own
        # registers read of A's project is 404.
        await _ok(await b.post("/pm/risks", json=_risk(pa["id"], title="B's row")))

        resp = await b.get(f"/pm/projects/{pa['id']}/registers")
        assert resp.status_code == 404, resp.text
        got_a = await _ok(await a.get(f"/pm/projects/{pa['id']}/registers"))
        assert [r["title"] for r in got_a["risks"]] == ["Migration may lose data"]
        got_b = await _ok(await b.get(f"/pm/projects/{pb['id']}/registers"))
        assert got_b["risks"] == []

    async def test_unknown_project_is_404(self, authenticated_client):
        resp = await authenticated_client.get(
            "/pm/projects/0195f48b-d000-7000-8000-00000000dead/registers"
        )
        assert resp.status_code == 404


_ENTITIES = {
    "risk": ("/pm/risks", _risk),
    "decision": ("/decisions", _decision),
    "requirement": ("/requirements", _requirement),
}


class TestPutKeepsOmittedRegisterFields:
    """PUT that OMITS phase / card_id keeps them; an explicit null clears them."""

    @pytest.mark.parametrize("kind", sorted(_ENTITIES))
    async def test_omitted_is_kept(self, authenticated_client, kind):
        c = authenticated_client
        path, body = _ENTITIES[kind]
        p = await _project(c)
        row = await _ok(await c.post(path, json=body(p["id"], phase="setup", card_id="c-7")))
        put = await _ok(await c.put(f"{path}/{row['id']}", json=body(p["id"])))
        assert (put["phase"], put["card_id"]) == ("setup", "c-7")
        got = await _ok(await c.get(f"{path}/{row['id']}"))
        assert (got["phase"], got["card_id"]) == ("setup", "c-7")

    @pytest.mark.parametrize("kind", sorted(_ENTITIES))
    async def test_explicit_null_clears(self, authenticated_client, kind):
        c = authenticated_client
        path, body = _ENTITIES[kind]
        p = await _project(c)
        row = await _ok(await c.post(path, json=body(p["id"], phase="setup", card_id="c-7")))
        put = await _ok(
            await c.put(f"{path}/{row['id']}", json=body(p["id"], phase=None, card_id=None))
        )
        assert (put["phase"], put["card_id"]) == (None, None)
        got = await _ok(await c.get(f"{path}/{row['id']}"))
        assert (got["phase"], got["card_id"]) == (None, None)


class TestHistoryCarriesRegisterFields:
    async def test_risk_phase_change_is_in_the_version_history(
        self, authenticated_client, test_session
    ):
        c = authenticated_client
        p = await _project(c)
        r = await _ok(await c.post("/pm/risks", json=_risk(p["id"], phase="setup", card_id="c-1")))
        await _ok(await c.put(f"/pm/risks/{r['id']}", json=_risk(p["id"], phase="execution")))

        rows = (
            await test_session.execute(
                select(EntityVersionHistory.snapshot_data)
                .where(
                    EntityVersionHistory.entity_type == "risk",
                    EntityVersionHistory.entity_id == UUID(r["id"]),
                )
                .order_by(EntityVersionHistory.created_at, EntityVersionHistory.id)
            )
        ).scalars()
        phases = [(s["phase"], s["card_id"]) for s in rows]
        assert phases == [("setup", "c-1"), ("execution", "c-1")]
