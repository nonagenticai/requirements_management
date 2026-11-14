"""fr cockpit A4: acceptance criteria as items, the criterion check, the closure event,
``budget_currency``.

* Criteria are items ``{id, text, status, evidence, checked_at, checked_by}``; plain strings
  are accepted at the input edge and stored as ``unchecked`` items.
* The check is server-owned: only ``PATCH /pm/projects/{eid}/criteria/{criterion_id}`` sets
  it, and only for PM / n8n (a fr-delegated caller is 403, as for ``PUT .../phase``).
* status → ``closed`` writes one ``closure`` phase event (idempotent).
* Migration 2026_10_25_0064's data steps, run against real rows.

All tests hit real PostgreSQL through the FastAPI test client; only licence introspection is
faked (the ``licences`` fixture) for the delegated caller.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.project_types import typed
from tests.test_pm_project_intake import (  # noqa: F401 — `licences` is a fixture
    CUSTOMER_TENANT,
    DELEGATED,
    licences,
)

CONVEYOR = {"X-Tenant-Id": CUSTOMER_TENANT}
MIGRATION = next(
    (Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("2026_10_25_0064_*.py")
)


def _migration():
    spec = importlib.util.spec_from_file_location("m0064", MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _code(resp) -> str | None:
    body = resp.json()
    return (body.get("error") or {}).get("code") if isinstance(body, dict) else None


async def _ok(resp) -> dict:
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _project(c: AsyncClient, headers: dict | None = None, **body) -> dict:
    kw = {"headers": headers} if headers else {}
    payload = await typed(c, {"title": "p", "brief": {"notes": "n"}, **body}, headers)
    return await _ok(await c.post("/pm/projects", json=payload, **kw))


def _criteria(project: dict) -> list[dict]:
    return project["brief"]["acceptance_criteria"]


async def _patch(c: AsyncClient, pid: str, cid: str, headers: dict | None = None, **body):
    kw = {"headers": headers} if headers else {}
    return await c.patch(f"/pm/projects/{pid}/criteria/{cid}", json=body, **kw)


EVIDENCE = [{"kind": "test_run", "ref": "run-42"}, {"kind": "pr", "ref": "https://x/pr/1"}]


# ---------------------------------------------------------------------------
# Items and the input edge
# ---------------------------------------------------------------------------


class TestCriteriaItems:
    async def test_strings_become_unchecked_items_with_ids(self, authenticated_client):
        p = await _project(
            authenticated_client, brief={"acceptance_criteria": ["tests pass", "docs updated"]}
        )
        items = _criteria(p)
        assert [i["text"] for i in items] == ["tests pass", "docs updated"]
        assert {i["status"] for i in items} == {"unchecked"}
        assert all(uuid.UUID(i["id"]) for i in items)
        assert len({i["id"] for i in items}) == 2
        assert all(i["evidence"] == [] and i["checked_at"] is None for i in items)
        got = await _ok(await authenticated_client.get(f"/pm/projects/{p['id']}"))
        assert _criteria(got) == items

    async def test_a_brief_write_cannot_set_a_check(self, authenticated_client):
        forged = {
            "id": "c1",
            "text": "t",
            "status": "met",
            "evidence": EVIDENCE,
            "checked_by": "me",
        }
        p = await _project(authenticated_client, brief={"acceptance_criteria": [forged]})
        (item,) = _criteria(p)
        assert (item["id"], item["status"], item["evidence"], item["checked_by"]) == (
            "c1",
            "unchecked",
            [],
            None,
        )

    async def test_duplicate_ids_are_422(self, authenticated_client):
        dup = [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]
        resp = await authenticated_client.post(
            "/pm/projects",
            json=await typed(
                authenticated_client, {"title": "d", "brief": {"acceptance_criteria": dup}}
            ),
        )
        assert resp.status_code == 422, resp.text

    async def test_later_versions_keep_checks_by_id_or_text(self, authenticated_client):
        c = authenticated_client
        p = await _project(c, brief={"acceptance_criteria": ["a", "b", "c"]})
        ids = {i["text"]: i["id"] for i in _criteria(p)}
        for t in ("a", "b", "c"):
            await _ok(await _patch(c, p["id"], ids[t], status="met", evidence=EVIDENCE[:1]))
        # a: plain string again (text match); b: same id, new text; c: dropped; d: new.
        v = await _ok(
            await c.post(
                f"/pm/projects/{p['id']}/brief/versions",
                json={
                    "brief": {
                        "acceptance_criteria": ["a", {"id": ids["b"], "text": "b2"}, "d"],
                    }
                },
            )
        )
        a, b, d = v["brief"]["acceptance_criteria"]
        assert (a["id"], a["status"], a["evidence"]) == (ids["a"], "met", EVIDENCE[:1])
        assert (b["id"], b["status"]) == (ids["b"], "unchecked")
        assert d["status"] == "unchecked" and d["id"] not in ids.values()


# ---------------------------------------------------------------------------
# PATCH /pm/projects/{eid}/criteria/{criterion_id}
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("licences")
class TestCriterionCheck:
    async def test_service_marks_met_and_is_idempotent(self, test_client: AsyncClient):
        c = test_client
        p = await _project(c, CONVEYOR, brief={"acceptance_criteria": ["tests pass"]})
        cid = _criteria(p)[0]["id"]
        first = await _ok(await _patch(c, p["id"], cid, CONVEYOR, status="met", evidence=EVIDENCE))
        assert first["changed"] is True and first["brief_version"] == 2
        crit = first["criterion"]
        assert (crit["status"], crit["evidence"]) == ("met", EVIDENCE)
        assert crit["checked_at"] is not None and crit["checked_by"]
        again = await _ok(await _patch(c, p["id"], cid, CONVEYOR, status="met", evidence=EVIDENCE))
        assert (again["changed"], again["brief_version"]) == (False, 2)
        got = await _ok(await c.get(f"/pm/projects/{p['id']}", headers=CONVEYOR))
        assert got["brief_version"] == 2
        assert _criteria(got)[0]["status"] == "met"
        versions = await _ok(
            await c.get(f"/pm/projects/{p['id']}/brief/versions", headers=CONVEYOR)
        )
        assert versions[0]["note"] == f"acceptance criterion {cid}: met"
        assert versions[0]["author_kind"] == "conveyor"
        # Reset: unchecked clears the check.
        reset = await _ok(await _patch(c, p["id"], cid, CONVEYOR, status="unchecked"))
        assert reset["criterion"]["checked_at"] is None
        assert reset["criterion"]["evidence"] == []

    async def test_delegated_user_is_403_and_changes_nothing(self, test_client: AsyncClient):
        c = test_client
        p = await _project(c, DELEGATED, brief={"acceptance_criteria": ["x"]})
        cid = _criteria(p)[0]["id"]
        resp = await _patch(c, p["id"], cid, DELEGATED, status="met", evidence=EVIDENCE)
        assert resp.status_code == 403, resp.text
        assert _code(resp) == "project_writes_are_pm_only"
        got = await _ok(await c.get(f"/pm/projects/{p['id']}", headers=DELEGATED))
        assert (got["brief_version"], _criteria(got)[0]["status"]) == (1, "unchecked")
        # Control: the conveyor (non-delegated) in the same tenant may.
        ok = await _patch(c, p["id"], cid, CONVEYOR, status="not_met", evidence=EVIDENCE[:1])
        assert ok.status_code == 200, ok.text

    async def test_met_without_evidence_and_unknown_id(self, test_client: AsyncClient):
        c = test_client
        p = await _project(c, CONVEYOR, brief={"acceptance_criteria": ["x"]})
        cid = _criteria(p)[0]["id"]
        bad = await _patch(c, p["id"], cid, CONVEYOR, status="met")
        assert bad.status_code == 422, bad.text
        bad2 = await _patch(c, p["id"], cid, CONVEYOR, status="unchecked", evidence=EVIDENCE)
        assert bad2.status_code == 422, bad2.text
        missing = await _patch(c, p["id"], "nope", CONVEYOR, status="not_met")
        assert missing.status_code == 404, missing.text
        assert _code(missing) == "criterion_not_found"

    async def test_a_check_over_the_brief_cap_is_422_and_stores_nothing(
        self, test_client: AsyncClient
    ):
        """A check that would push the stored brief over 128 KiB is refused like an oversize
        brief in a request body, and the brief stays readable and writable (it used to be
        stored, and every later read and write of the brief then failed validation)."""
        c = test_client
        p = await _project(c, CONVEYOR, brief={"acceptance_criteria": ["a", "b"]})
        ids = [i["id"] for i in _criteria(p)]
        full = [{"kind": "test_run", "ref": f"{n:04d}" + "r" * 1996} for n in range(50)]
        first = await _ok(await _patch(c, p["id"], ids[0], CONVEYOR, status="met", evidence=full))
        assert first["brief_version"] == 2  # ~101 KB: under the cap
        over = await _patch(c, p["id"], ids[1], CONVEYOR, status="met", evidence=full)
        assert over.status_code == 422, over.text[:300]
        assert _code(over) == "VALIDATION_ERROR"
        assert "body.brief:brief_over_128_kib" in over.text
        # Nothing staged; the current brief and every version still read.
        got = await _ok(await c.get(f"/pm/projects/{p['id']}", headers=CONVEYOR))
        assert got["brief_version"] == 2
        assert [i["status"] for i in _criteria(got)] == ["met", "unchecked"]
        versions = await _ok(
            await c.get(f"/pm/projects/{p['id']}/brief/versions", headers=CONVEYOR)
        )
        assert [v["version"] for v in versions] == [2, 1]
        await _ok(await c.get(f"/pm/projects/{p['id']}/brief/versions/2", headers=CONVEYOR))
        # A brief write whose CARRIED-OVER check takes it over the cap is refused the same
        # way, though the body itself is small enough to pass request validation.
        grow = {"acceptance_criteria": ["a", "b"], "in_scope": ["x" * 1000] * 40}
        big = await c.post(
            f"/pm/projects/{p['id']}/brief/versions", json={"brief": grow}, headers=CONVEYOR
        )
        assert big.status_code == 422, big.text[:300]
        assert "body.brief:brief_over_128_kib" in big.text
        # Control: the same check within the cap is still accepted.
        ok = await _patch(c, p["id"], ids[1], CONVEYOR, status="met", evidence=full[:5])
        assert (await _ok(ok))["brief_version"] == 3


# ---------------------------------------------------------------------------
# Closure phase event
# ---------------------------------------------------------------------------


async def _closures(c: AsyncClient, pid: str) -> list[dict]:
    events = await _ok(await c.get(f"/pm/projects/{pid}/phase-events"))
    return [e for e in events if e["kind"] == "closure"]


class TestClosureEvent:
    async def test_closing_writes_one_event_idempotently(self, authenticated_client):
        c = authenticated_client
        p = await _project(c)
        await _ok(await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "status": "closed"}))
        (ev,) = await _closures(c, p["id"])
        assert ev["from_phase_id"] == ev["to_phase_id"] == p["current_phase_id"]
        assert ev["to_phase"] == "setup"
        await _ok(await c.put(f"/pm/projects/{p['id']}", json={"title": "p2", "status": "closed"}))
        await _ok(await c.put(f"/pm/projects/{p['id']}", json={"title": "p3"}))
        assert len(await _closures(c, p["id"])) == 1
        phase_events = await _ok(await c.get(f"/pm/projects/{p['id']}/phase-events"))
        assert [e["kind"] for e in phase_events] == ["closure", "phase"]
        # Reopened and closed again: a second, real closure.
        await _ok(
            await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "status": "executing"})
        )
        await _ok(await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "status": "closed"}))
        assert len(await _closures(c, p["id"])) == 2

    async def test_created_closed_has_a_closure(self, authenticated_client):
        p = await _project(authenticated_client, status="closed")
        assert len(await _closures(authenticated_client, p["id"])) == 1


# ---------------------------------------------------------------------------
# budget_currency
# ---------------------------------------------------------------------------


class TestBudgetCurrency:
    async def test_round_trip(self, authenticated_client):
        c = authenticated_client
        p = await _project(c, budget=1200.5, budget_currency="eur")
        assert (p["budget"], p["budget_currency"]) == (1200.5, "EUR")
        got = await _ok(await c.get(f"/pm/projects/{p['id']}"))
        assert got["budget_currency"] == "EUR"
        kept = await _ok(await c.put(f"/pm/projects/{p['id']}", json={"title": "p"}))
        assert kept["budget_currency"] == "EUR"
        usd = await _ok(
            await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "budget_currency": "USD"})
        )
        assert usd["budget_currency"] == "USD"
        bad = await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "budget_currency": "XYZ"})
        assert bad.status_code == 422, bad.text
        cleared = await _ok(
            await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "budget_currency": None})
        )
        assert cleared["budget_currency"] is None

    async def test_default_is_null(self, authenticated_client):
        p = await _project(authenticated_client)
        assert p["budget_currency"] is None


# ---------------------------------------------------------------------------
# Migration 2026_10_25_0064 data steps, against real rows
# ---------------------------------------------------------------------------


class TestMigrationData:
    async def test_pure_conversion_is_deterministic_and_idempotent(self):
        m = _migration()
        once = m.criteria_to_items("p1", ["a", "a", "b"])
        assert [i["text"] for i in once] == ["a", "a", "b"]
        assert {i["status"] for i in once} == {"unchecked"}
        assert len({i["id"] for i in once}) == 3
        assert m.criteria_to_items("p1", ["a", "a", "b"]) == once
        assert m.criteria_to_items("p1", once) == once
        assert m.criteria_to_items("p2", ["a"])[0]["id"] != once[0]["id"]
        assert m.items_to_criteria(once) == ["a", "a", "b"]

    async def test_legacy_rows_are_converted(self, authenticated_client, test_engine):
        c = authenticated_client
        m = _migration()
        p = await _project(c, brief={"acceptance_criteria": ["placeholder"]}, currency=" usd ")
        await _ok(await c.put(f"/pm/projects/{p['id']}", json={"title": "p", "status": "closed"}))
        # Rewind the rows to what production holds today: string criteria, no closure event,
        # no budget_currency.
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE pm_project_brief_versions SET brief = jsonb_set(brief, "
                    "'{acceptance_criteria}', '[\"tests pass\", \"no regressions\"]') "
                    "WHERE project_id = :p"
                ),
                {"p": p["id"]},
            )
            await conn.execute(text("DELETE FROM pm_project_phase_events WHERE kind = 'closure'"))
            await conn.execute(text("UPDATE pm_projects SET budget_currency = NULL"))
        async with test_engine.begin() as conn:
            changed = await conn.run_sync(m.convert_briefs)
            closures = await conn.run_sync(m.backfill_closure_events)
            currencies = await conn.run_sync(m.backfill_budget_currency)
        assert (changed, closures, currencies) == (1, 1, 1)
        got = await _ok(await c.get(f"/pm/projects/{p['id']}"))
        items = _criteria(got)
        assert [(i["text"], i["status"]) for i in items] == [
            ("tests pass", "unchecked"),
            ("no regressions", "unchecked"),
        ]
        assert items[0]["id"] == m.criteria_to_items(p["id"], ["tests pass"])[0]["id"]
        assert got["budget_currency"] == "USD"
        (ev,) = await _closures(c, p["id"])
        assert ev["subject_id"] == "migration:2026_10_25_0064"
        # Re-run: nothing left to do.
        async with test_engine.begin() as conn:
            again = (
                await conn.run_sync(m.convert_briefs),
                await conn.run_sync(m.backfill_closure_events),
                await conn.run_sync(m.backfill_budget_currency),
            )
        assert again == (0, 0, 0)
        # The converted criterion is checkable.
        ok = await _patch(c, p["id"], items[0]["id"], status="met", evidence=EVIDENCE[:1])
        assert ok.status_code == 200, ok.text
