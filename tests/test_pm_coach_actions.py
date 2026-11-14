"""Kanban-coach ADMISSION (R311): ``/pm/projects/{eid}/coach-actions``.

kanban-coach-errors.md §3 (post-Admit choices), §4.3 (I1-I14: RM admission is the only
arbiter), §4.4 (caps 1/3/10 by effective remedy, the phase-visit count via the stored
``phase_visit_at``, the POST and PATCH route steps). All route tests hit real PostgreSQL
through the FastAPI test client; ``apply_caps`` is also tested as a pure function.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.pm_coach import CoachCaps
from services.pm_coach_action_service import apply_caps
from tests.test_pm_catalogue import _As
from tests.test_pm_project_intake import (  # noqa: F401 — `licences` is a fixture
    DELEGATED,
    licences,
)
from tests.test_pm_project_phase import _kind, _project, _set


class _Client(_As):
    """``_As`` with PATCH and an arbitrary header set."""

    def __init__(self, client: AsyncClient, headers: dict):
        self._c, self._h = client, headers

    async def patch(self, path: str, **kw):
        return await self._c.patch(path, headers=self._h, **kw)


def _body(**over) -> dict:
    """One admission request; every id unique unless the test pins it."""
    n = uuid.uuid4().hex[:10]
    body = {
        "card_id": "card-x",
        "target_card_id": "card-x",
        "exit_group": "own",
        "entry": "2026-10-19T10:00:00.000Z",
        "error_class": "card_action_failed",
        "requested_remedy": "rerun",
        "source_execution_id": f"src-{n}",
        "root_execution_id": f"root-{n}",
        "coach_execution_id": f"coach-{n}",
        "ask_allowed": True,
    }
    body.update(over)
    return body


async def _setup(c) -> dict:
    tag = uuid.uuid4().hex[:6]
    k = await _kind(c, f"it_{tag}")
    return await _project(c, f"p-{tag}", k["type"]["id"])


async def _admit(c, pid: str, **over) -> dict:
    resp = await c.post(f"/pm/projects/{pid}/coach-actions", json=_body(**over))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _patch(c, pid: str, row: dict, coach: str, applied: str, **extra):
    body = {"coach_execution_id": coach, "applied_remedy": applied, **extra}
    return await c.patch(f"/pm/projects/{pid}/coach-actions/{row['id']}", json=body)


async def _rows(c, pid: str, **params) -> list[dict]:
    resp = await c.get(f"/pm/projects/{pid}/coach-actions", params=params)
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Total-Count"] == str(len(resp.json()))
    return resp.json()


def _code(resp) -> str:
    return resp.json()["error"]["code"]


# ---------------------------------------------------------------------------
# The cap table as a pure function (§4.4)
# ---------------------------------------------------------------------------


def _caps(requested: str, **counts):
    args = {
        "ask_allowed": True,
        "caps": CoachCaps(),
        "incident_rank": 1,
        "card_count": 0,
        "entry_automatic": 0,
        "entry_asks": 0,
        **counts,
    }
    return apply_caps(requested, **args)


class TestApplyCaps:
    @pytest.mark.parametrize("remedy", ["rerun", "move", "repull", "ask", "block"])
    def test_under_every_cap_the_request_stands(self, remedy):
        assert _caps(remedy) == (remedy, None)

    @pytest.mark.parametrize("remedy", ["rerun", "move", "repull"])
    def test_second_automatic_remedy_in_the_entry_is_an_ask(self, remedy):
        assert _caps(remedy, entry_automatic=1) == ("ask", "entry_cap")

    def test_after_an_ask_in_the_entry_the_escalated_remedy_is_a_block(self):
        assert _caps("rerun", entry_automatic=1, entry_asks=1) == ("block", "ask_in_entry")
        assert _caps("ask", entry_asks=1) == ("block", "ask_in_entry")

    @pytest.mark.parametrize("remedy", ["rerun", "move", "repull", "ask"])
    def test_any_request_after_an_ask_in_the_entry_is_a_block(self, remedy):
        """§4.4: an admitted ask in the entry blocks every later request in it, even an
        automatic one under the automatic cap."""
        assert _caps(remedy, entry_asks=1) == ("block", "ask_in_entry")

    def test_an_ask_the_entry_does_not_allow_is_a_block(self):
        assert _caps("ask", ask_allowed=False) == ("block", "ask_not_allowed")
        assert _caps("rerun", entry_automatic=1, ask_allowed=False) == (
            "block",
            "ask_not_allowed",
        )
        # ask_allowed only matters for an ask: an automatic remedy under the cap stands.
        assert _caps("rerun", ask_allowed=False) == ("rerun", None)

    def test_card_cap_blocks(self):
        assert _caps("rerun", card_count=2) == ("rerun", None)
        assert _caps("rerun", card_count=3) == ("block", "card_cap")
        assert _caps("ask", card_count=3) == ("block", "card_cap")

    def test_project_cap_blocks_only_a_new_incident(self):
        assert _caps("rerun", incident_rank=10) == ("rerun", None)
        assert _caps("rerun", incident_rank=11) == ("block", "project_cap")

    def test_a_fan_out_sibling_inherits_its_incidents_place(self):
        """A sibling row (same root + exit group, another target) ranks as its incident:
        the 10th's sibling is within the cap, the 11th's (the 11th was blocked) is not."""
        assert _caps("rerun", incident_rank=10) == ("rerun", None)
        assert _caps("rerun", incident_rank=11) == ("block", "project_cap")
        assert _caps("repull", incident_rank=11) == ("block", "project_cap")

    def test_caps_only_move_up(self):
        assert _caps("block", entry_automatic=5, entry_asks=5) == ("block", None)

    def test_tuned_caps_are_honoured(self):
        caps = CoachCaps(entry=2, card=5, project=1)
        assert _caps("rerun", caps=caps, entry_automatic=1) == ("rerun", None)
        assert _caps("rerun", caps=caps, card_count=4) == ("rerun", None)
        assert _caps("rerun", caps=caps, incident_rank=2) == ("block", "project_cap")


# ---------------------------------------------------------------------------
# POST: admission, idempotency, the incident key (§4.3)
# ---------------------------------------------------------------------------


class TestAdmit:
    async def test_first_coach_is_admitted_with_the_visit_stamp(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _setup(c)
        got = await _admit(c, p["id"])
        assert got["admitted"] is True and got["remedy"] == "rerun" and got["reason"] is None
        assert (got["project_incidents"], got["card_count"], got["entry_count"]) == (1, 1, 1)
        assert got["replayed"] is False
        (row,) = await _rows(c, p["id"])
        assert row["id"] == got["id"]
        assert row["phase"] == "setup", "the phase at admission (trace)"
        project = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert project["phase_updated_at"] is not None
        assert row["phase_visit_at"] == project["phase_updated_at"]
        assert (row["applied_remedy"], row["applied_at"]) == (None, None)
        assert row["effective_remedy"] == "rerun"

    async def test_re_post_by_the_same_coach_is_answered_from_its_row(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _setup(c)
        body = _body()
        first = await c.post(f"/pm/projects/{p['id']}/coach-actions", json=body)
        again = await c.post(f"/pm/projects/{p['id']}/coach-actions", json=body)
        assert first.status_code == again.status_code == 200
        assert again.json()["id"] == first.json()["id"]
        assert again.json()["remedy"] == first.json()["remedy"]
        assert again.json()["replayed"] is True
        # A re-POST with a different request is still answered from the row: one coach run
        # is one row.
        other = await c.post(
            f"/pm/projects/{p['id']}/coach-actions", json={**body, "requested_remedy": "block"}
        )
        assert other.json()["id"] == first.json()["id"]
        assert other.json()["remedy"] == "rerun"
        assert len(await _rows(c, p["id"])) == 1

    async def test_second_coach_of_the_incident_loses_and_writes_nothing(
        self, authenticated_client: AsyncClient
    ):
        """I1/I7: same target, entry, root and exit group ⇒ the second loses."""
        c = authenticated_client
        p = await _setup(c)
        a = _body(error_class="card_action_failed")
        b = _body(
            root_execution_id=a["root_execution_id"],
            error_class="execution_failed",  # the child's backstop: another class, same key
            requested_remedy="ask",
        )
        won = await _admit(c, p["id"], **a)
        lost = await _admit(c, p["id"], **b)
        assert won["admitted"] is True
        assert lost == {
            "id": None,
            "admitted": False,
            "remedy": None,
            "reason": "same_incident",
            "winner": a["coach_execution_id"],
            "replayed": False,
            "project_incidents": None,
            "card_count": None,
            "entry_count": None,
        }
        assert [r["coach_execution_id"] for r in await _rows(c, p["id"])] == [
            a["coach_execution_id"]
        ]

    async def test_other_branches_and_targets_of_one_run_are_separate_incidents(
        self, authenticated_client: AsyncClient
    ):
        """I11: one run, E10 on X (rerun group) and E35 on Y1, Y2 (pull group)."""
        c = authenticated_client
        p = await _setup(c)
        root = "root-run-1"
        x = await _admit(c, p["id"], root_execution_id=root, exit_group="rerun")
        y1 = await _admit(
            c, p["id"], root_execution_id=root, exit_group="pull", target_card_id="y1"
        )
        y2 = await _admit(
            c, p["id"], root_execution_id=root, exit_group="pull", target_card_id="y2"
        )
        assert [g["admitted"] for g in (x, y1, y2)] == [True, True, True]
        assert [g["remedy"] for g in (x, y1, y2)] == ["rerun", "rerun", "rerun"]
        # Two incidents (root, rerun) and (root, pull): the fan-out is one incident.
        assert y2["project_incidents"] == 2

    async def test_note_is_never_admitted(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        resp = await c.post(
            f"/pm/projects/{p['id']}/coach-actions", json=_body(requested_remedy="note")
        )
        assert resp.status_code == 422, resp.text
        assert await _rows(c, p["id"]) == []

    async def test_a_coach_id_admitted_on_another_project_is_409(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p1, p2 = await _setup(c), await _setup(c)
        body = _body()
        await _admit(c, p1["id"], **body)
        resp = await c.post(f"/pm/projects/{p2['id']}/coach-actions", json=body)
        assert resp.status_code == 409, resp.text
        assert _code(resp) == "coach_execution_id_reused"

    async def test_unknown_project_is_404(self, authenticated_client: AsyncClient):
        c = authenticated_client
        resp = await c.post(f"/pm/projects/{uuid.uuid4()}/coach-actions", json=_body())
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# The cap table, through the route (§4.4)
# ---------------------------------------------------------------------------


class TestCaps:
    async def test_per_entry_second_automatic_is_ask_then_block(
        self, authenticated_client: AsyncClient
    ):
        """I6: A's rerun fails again in the same entry ⇒ new root ⇒ admitted as ask; one
        more ⇒ block."""
        c = authenticated_client
        p = await _setup(c)
        first = await _admit(c, p["id"])
        second = await _admit(c, p["id"], requested_remedy="move")
        third = await _admit(c, p["id"], requested_remedy="repull")
        assert [g["remedy"] for g in (first, second, third)] == ["rerun", "ask", "block"]
        assert [g["reason"] for g in (first, second, third)] == [
            None,
            "entry_cap",
            "ask_in_entry",
        ]
        # A new list entry is a new entry: the per-entry slot is free again.
        moved_on = await _admit(c, p["id"], entry="2026-10-19T11:00:00.000Z")
        assert (moved_on["remedy"], moved_on["entry_count"]) == ("block", 0), (
            "but the card's cap (3) for this phase visit was reached"
        )
        assert moved_on["reason"] == "card_cap"

    @pytest.mark.parametrize("requested", ["rerun", "move", "repull", "ask"])
    async def test_after_an_admitted_ask_in_the_entry_every_request_is_a_block(
        self, authenticated_client: AsyncClient, requested: str
    ):
        c = authenticated_client
        p = await _setup(c)
        asked = await _admit(c, p["id"], requested_remedy="ask")
        assert asked["remedy"] == "ask"
        got = await _admit(c, p["id"], requested_remedy=requested)
        assert (got["remedy"], got["reason"]) == ("block", "ask_in_entry")
        # Another entry of the same card is not affected.
        other = await _admit(c, p["id"], requested_remedy=requested, entry="other-entry")
        assert other["remedy"] == requested

    async def test_an_ask_the_entry_does_not_allow_is_a_block(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _setup(c)
        got = await _admit(c, p["id"], requested_remedy="ask", ask_allowed=False)
        assert (got["remedy"], got["reason"]) == ("block", "ask_not_allowed")

    async def test_per_card_cap(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        got = [await _admit(c, p["id"], entry=f"2026-10-19T1{i}:00:00.000Z") for i in range(4)]
        assert [g["remedy"] for g in got] == ["rerun", "rerun", "rerun", "block"]
        assert got[3]["reason"] == "card_cap"
        assert [g["card_count"] for g in got] == [1, 2, 3, 4]
        # Another card is not touched by X's cap.
        other = await _admit(c, p["id"], target_card_id="card-y")
        assert other["remedy"] == "rerun"

    async def test_per_project_incident_cap_counts_incidents_not_rows(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _setup(c)
        roots = [f"root-{i}" for i in range(10)]
        for i, root in enumerate(roots):
            got = await _admit(c, p["id"], target_card_id=f"card-{i}", root_execution_id=root)
            assert got["remedy"] == "rerun", (i, got)
        assert got["project_incidents"] == 10
        # A row of an incident already counted adds nothing: not blocked.
        fan_out = await _admit(c, p["id"], target_card_id="card-new", root_execution_id=roots[0])
        assert (fan_out["remedy"], fan_out["project_incidents"]) == ("rerun", 10)
        # The 11th incident is blocked, and so is any remedy, ask included.
        eleventh = await _admit(c, p["id"], target_card_id="card-11")
        assert (eleventh["remedy"], eleventh["reason"]) == ("block", "project_cap")
        ask = await _admit(c, p["id"], target_card_id="card-12", requested_remedy="ask")
        assert (ask["remedy"], ask["reason"]) == ("block", "project_cap")

    async def test_pull_fan_out_cannot_bypass_the_project_cap(
        self, authenticated_client: AsyncClient
    ):
        """Siblings (same root + exit group, other targets) rank as their incident: the 10th
        incident's sibling is allowed, the 11th's is blocked, an earlier one's is allowed."""
        c = authenticated_client
        p = await _setup(c)
        roots = [f"root-{i}" for i in range(11)]
        got = [
            await _admit(
                c, p["id"], target_card_id=f"card-{i}", root_execution_id=r, exit_group="pull"
            )
            for i, r in enumerate(roots)
        ]
        assert [g["remedy"] for g in got] == ["rerun"] * 10 + ["block"]
        tenth = await _admit(
            c, p["id"], target_card_id="sib-10", root_execution_id=roots[9], exit_group="pull"
        )
        assert (tenth["remedy"], tenth["reason"]) == ("rerun", None)
        eleventh = await _admit(
            c, p["id"], target_card_id="sib-11", root_execution_id=roots[10], exit_group="pull"
        )
        assert (eleventh["remedy"], eleventh["reason"]) == ("block", "project_cap")
        first = await _admit(
            c, p["id"], target_card_id="sib-1", root_execution_id=roots[0], exit_group="pull"
        )
        assert first["remedy"] == "rerun"

    async def test_tuned_caps_from_the_coach(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        caps = {"entry": 2, "card": 3, "project": 10}
        got = [await _admit(c, p["id"], caps=caps) for _ in range(3)]
        assert [g["remedy"] for g in got] == ["rerun", "rerun", "ask"]

    async def test_a_note_row_counts_for_no_cap(self, authenticated_client: AsyncClient):
        """A coach that dropped to note does not use up the entry's automatic slot, the
        card's count or the project's incident count."""
        c = authenticated_client
        p = await _setup(c)
        body = _body()
        row = await _admit(c, p["id"], **body)
        resp = await _patch(c, p["id"], row, body["coach_execution_id"], "note")
        assert resp.status_code == 200, resp.text
        again = await _admit(c, p["id"])
        assert (again["remedy"], again["card_count"], again["entry_count"]) == ("rerun", 1, 1)
        assert again["project_incidents"] == 1

    async def test_a_failed_patch_counts_as_the_admitted_remedy(
        self, authenticated_client: AsyncClient
    ):
        """No PATCH (the coach died) ⇒ the row counts as its admitted remedy (fail closed)."""
        c = authenticated_client
        p = await _setup(c)
        await _admit(c, p["id"])
        assert (await _admit(c, p["id"]))["remedy"] == "ask"


# ---------------------------------------------------------------------------
# The phase-visit count (§4.4)
# ---------------------------------------------------------------------------


class TestPhaseVisit:
    async def test_re_entering_a_phase_starts_the_count_at_zero(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _setup(c)
        caps = {"project": 1}
        first = await _admit(c, p["id"], caps=caps, target_card_id="a")
        capped = await _admit(c, p["id"], caps=caps, target_card_id="b")
        assert (first["remedy"], capped["remedy"]) == ("rerun", "block")

        assert (await _set(c, p["id"], phase="preliminary")).status_code == 200
        away = await _admit(c, p["id"], caps=caps, target_card_id="c")
        assert (away["remedy"], away["project_incidents"]) == ("rerun", 1)
        assert (await _set(c, p["id"], phase="setup")).status_code == 200
        back = await _admit(c, p["id"], caps=caps, target_card_id="d")
        assert (back["remedy"], back["project_incidents"]) == ("rerun", 1), (
            "the earlier setup visit's incidents are not this visit's"
        )
        phases = [r["phase"] for r in await _rows(c, p["id"])]
        assert phases == ["setup", "setup", "preliminary", "setup"]

    async def test_a_phase_change_resets_the_per_card_cap(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        caps = {"card": 1}
        first = await _admit(c, p["id"], caps=caps, entry="e1")
        capped = await _admit(c, p["id"], caps=caps, entry="e2")
        assert (first["remedy"], capped["remedy"], capped["reason"]) == (
            "rerun",
            "block",
            "card_cap",
        )
        assert (await _set(c, p["id"], phase="preliminary")).status_code == 200
        away = await _admit(c, p["id"], caps=caps, entry="e3")
        assert (away["remedy"], away["card_count"]) == ("rerun", 1)

    async def test_a_restart_resets_the_per_card_and_project_caps(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _setup(c)
        caps = {"card": 1, "project": 2}
        await _admit(c, p["id"], caps=caps, entry="e1")
        capped = await _admit(c, p["id"], caps=caps, entry="e2")
        assert (capped["remedy"], capped["reason"]) == ("block", "card_cap")
        other = await _admit(c, p["id"], caps=caps, target_card_id="b")
        assert (other["remedy"], other["reason"]) == ("block", "project_cap")

        restart = await _set(c, p["id"], phase="setup", restart=True)
        assert restart.status_code == 200 and restart.json()["changed"] is True, restart.text
        again = await _admit(c, p["id"], caps=caps, entry="e3")
        assert (again["remedy"], again["card_count"], again["project_incidents"]) == (
            "rerun",
            1,
            1,
        )

    async def test_a_same_phase_re_stamp_keeps_the_count(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        caps = {"project": 1}
        await _admit(c, p["id"], caps=caps, target_card_id="a")
        assert (await _set(c, p["id"], phase="setup")).status_code == 200  # no-op re-stamp
        assert (await _admit(c, p["id"], caps=caps, target_card_id="b"))["remedy"] == "block"
        # The per-card count is kept too.
        card = await _admit(c, p["id"], caps={"card": 1}, target_card_id="a", entry="e2")
        assert (card["remedy"], card["reason"]) == ("block", "card_cap")

    async def test_null_phase_updated_at_counts_all_rows(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        c = authenticated_client
        p = await _setup(c)
        await test_session.execute(
            text("UPDATE pm_projects SET phase_updated_at = NULL WHERE id = :id"), {"id": p["id"]}
        )
        await test_session.commit()
        caps = {"project": 2}
        got = [await _admit(c, p["id"], caps=caps, target_card_id=f"c{i}") for i in range(3)]
        assert [g["remedy"] for g in got] == ["rerun", "rerun", "block"]
        assert [r["phase_visit_at"] for r in await _rows(c, p["id"])] == [None, None, None]


# ---------------------------------------------------------------------------
# PATCH: keep / drop to note / upgrade to ask-block (§3, I13, I14)
# ---------------------------------------------------------------------------


class TestRecordApplied:
    async def test_keep_is_recorded_and_idempotent(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        body = _body()
        row = await _admit(c, p["id"], **body)
        coach = body["coach_execution_id"]
        resp = await _patch(c, p["id"], row, coach, "rerun")
        assert resp.status_code == 200, resp.text
        got = resp.json()
        assert (got["applied_remedy"], got["effective_remedy"]) == ("rerun", "rerun")
        assert got["applied_at"] is not None
        again = await _patch(c, p["id"], row, coach, "rerun")
        assert again.status_code == 200 and again.json()["applied_at"] == got["applied_at"]
        different = await _patch(c, p["id"], row, coach, "note")
        assert different.status_code == 409, different.text
        assert _code(different) == "applied_remedy_already_set"

    async def test_drop_to_note(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        body = _body(requested_remedy="ask")
        row = await _admit(c, p["id"], **body)
        resp = await _patch(c, p["id"], row, body["coach_execution_id"], "note")
        assert resp.status_code == 200, resp.text
        assert resp.json()["effective_remedy"] == "note"

    async def test_upgrade_needs_the_reason(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        body = _body()
        row = await _admit(c, p["id"], **body)
        coach = body["coach_execution_id"]
        resp = await _patch(c, p["id"], row, coach, "ask")
        assert resp.status_code == 422, resp.text
        assert _code(resp) == "reason_required"
        resp = await _patch(c, p["id"], row, coach, "ask", reason="post_admit_unverified")
        assert resp.status_code == 200, resp.text
        got = resp.json()
        assert (got["remedy"], got["applied_remedy"], got["reason"]) == (
            "rerun",
            "ask",
            "post_admit_unverified",
        )
        # The upgraded row counts as its ask: the next ask in this entry is a block.
        nxt = await _admit(c, p["id"], requested_remedy="ask")
        assert (nxt["remedy"], nxt["reason"]) == ("block", "ask_in_entry")

    async def test_upgrade_straight_to_block(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        body = _body(requested_remedy="repull")
        row = await _admit(c, p["id"], **body)
        resp = await _patch(
            c, p["id"], row, body["coach_execution_id"], "block", reason="post_admit_unverified"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["effective_remedy"] == "block"

    async def test_ask_upgrade_is_a_block_when_the_entry_already_has_an_ask(
        self, authenticated_client: AsyncClient
    ):
        """I12: the per-entry rule the coach cannot see after Admit."""
        c = authenticated_client
        p = await _setup(c)
        # A is admitted with a rerun; B (another incident, same target + entry) is then the
        # second automatic remedy ⇒ admitted as ask. A's post-Admit upgrade meets B's ask.
        body = _body(target_card_id="x", exit_group="pull")
        row = await _admit(c, p["id"], **body)
        assert row["remedy"] == "rerun"
        asked = await _admit(c, p["id"], target_card_id="x")
        assert asked["remedy"] == "ask"
        coach = body["coach_execution_id"]
        resp = await _patch(c, p["id"], row, coach, "ask", reason="post_admit_unverified")
        assert resp.status_code == 200, resp.text
        got = resp.json()
        assert (got["applied_remedy"], got["effective_remedy"]) == ("block", "block")
        # Idempotent for the value the coach sent; a different value is 409.
        again = await _patch(c, p["id"], row, coach, "ask", reason="post_admit_unverified")
        assert again.status_code == 200, again.text
        assert again.json()["effective_remedy"] == "block"
        block = await _patch(c, p["id"], row, coach, "block", reason="post_admit_unverified")
        assert block.status_code == 409, block.text

    async def test_a_note_row_ask_does_not_trigger_the_per_entry_rule(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _setup(c)
        ask_body = _body(requested_remedy="ask")
        asked = await _admit(c, p["id"], **ask_body)
        assert (
            await _patch(c, p["id"], asked, ask_body["coach_execution_id"], "note")
        ).status_code == 200
        body = _body(exit_group="pull")
        row = await _admit(c, p["id"], **body)
        resp = await _patch(
            c, p["id"], row, body["coach_execution_id"], "ask", reason="post_admit_unverified"
        )
        assert resp.json()["effective_remedy"] == "ask"

    @pytest.mark.parametrize(
        ("admitted", "applied"),
        [("ask", "block"), ("ask", "rerun"), ("block", "ask"), ("rerun", "move")],
    )
    async def test_nothing_else_is_allowed(
        self, authenticated_client: AsyncClient, admitted: str, applied: str
    ):
        c = authenticated_client
        p = await _setup(c)
        body = _body(requested_remedy=admitted)
        row = await _admit(c, p["id"], **body)
        assert row["remedy"] == admitted
        resp = await _patch(
            c, p["id"], row, body["coach_execution_id"], applied, reason="post_admit_unverified"
        )
        assert resp.status_code == 422, resp.text
        assert _code(resp) == "applied_remedy_not_allowed"
        assert (await _rows(c, p["id"]))[0]["applied_remedy"] is None

    async def test_only_the_rows_own_coach(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        row = await _admit(c, p["id"])
        resp = await _patch(c, p["id"], row, "someone-else", "rerun")
        assert resp.status_code == 409, resp.text
        assert _code(resp) == "coach_execution_mismatch"

    async def test_unknown_row_is_404(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        resp = await _patch(c, p["id"], {"id": str(uuid.uuid4())}, "coach", "rerun")
        assert resp.status_code == 404, resp.text

    async def test_a_row_of_another_project_is_404(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p1, p2 = await _setup(c), await _setup(c)
        body = _body()
        row = await _admit(c, p1["id"], **body)
        resp = await _patch(c, p2["id"], row, body["coach_execution_id"], "rerun")
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Concurrency (I2): the project row lock serialises admissions
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_parallel_coaches_of_one_incident_admit_exactly_one(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _setup(c)
        root = "root-race"
        bodies = [_body(root_execution_id=root) for _ in range(5)]
        resps = await asyncio.gather(
            *(c.post(f"/pm/projects/{p['id']}/coach-actions", json=b) for b in bodies)
        )
        assert [r.status_code for r in resps] == [200] * 5, [r.text for r in resps]
        got = [r.json() for r in resps]
        winners = [g for g in got if g["admitted"]]
        assert len(winners) == 1, got
        (winner,) = await _rows(c, p["id"])
        assert winner["id"] == winners[0]["id"]
        for g in got:
            if not g["admitted"]:
                assert (g["reason"], g["winner"]) == ("same_incident", winner["coach_execution_id"])

    async def test_parallel_incidents_on_one_entry_respect_the_entry_cap(
        self, authenticated_client: AsyncClient
    ):
        """Different incidents of one target + entry in parallel: exactly one automatic
        remedy, one ask, the rest block (the counts are taken under the lock)."""
        c = authenticated_client
        p = await _setup(c)
        caps = {"card": 100}
        resps = await asyncio.gather(
            *(
                c.post(f"/pm/projects/{p['id']}/coach-actions", json=_body(caps=caps))
                for _ in range(4)
            )
        )
        remedies = sorted(r.json()["remedy"] for r in resps)
        assert remedies == ["ask", "block", "block", "rerun"], [r.text for r in resps]

    async def test_parallel_re_posts_of_one_coach_write_one_row(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _setup(c)
        body = _body()
        resps = await asyncio.gather(
            *(c.post(f"/pm/projects/{p['id']}/coach-actions", json=body) for _ in range(3))
        )
        assert {r.json()["id"] for r in resps} != {None}
        assert len({r.json()["id"] for r in resps}) == 1
        assert sorted(r.json()["replayed"] for r in resps) == [False, True, True]
        assert len(await _rows(c, p["id"])) == 1


# ---------------------------------------------------------------------------
# Auth: PM only; tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("licences")
class TestPmOnly:
    async def test_fr_delegated_writes_are_403_reads_are_200(self, test_client: AsyncClient):
        d = _Client(test_client, DELEGATED)
        p = await _setup(d)
        resp = await d.post(f"/pm/projects/{p['id']}/coach-actions", json=_body())
        assert resp.status_code == 403, resp.text
        assert _code(resp) == "project_writes_are_pm_only"
        resp = await _patch(d, p["id"], {"id": str(uuid.uuid4())}, "coach", "rerun")
        assert resp.status_code == 403, resp.text
        assert await _rows(d, p["id"]) == []


class TestTenantIsolation:
    async def test_other_tenant_cannot_read_or_write(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = (
            _Client(test_client, {"X-Tenant-Id": tenant_a_id}),
            _Client(test_client, {"X-Tenant-Id": tenant_b_id}),
        )
        pa = await _setup(a)
        body = _body()
        row = await _admit(a, pa["id"], **body)
        assert (await b.get(f"/pm/projects/{pa['id']}/coach-actions")).status_code == 404
        resp = await b.post(f"/pm/projects/{pa['id']}/coach-actions", json=_body())
        assert resp.status_code == 404, resp.text
        resp = await _patch(b, pa["id"], row, body["coach_execution_id"], "rerun")
        assert resp.status_code == 404, resp.text
        # The same coach id in another tenant is that tenant's own row (n8n execution ids
        # are per instance, not global).
        pb = await _setup(b)
        other = await _admit(b, pb["id"], **body)
        assert other["admitted"] is True and other["id"] != row["id"]

    async def test_list_filters_by_target(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _setup(c)
        await _admit(c, p["id"], target_card_id="x")
        await _admit(c, p["id"], target_card_id="y")
        assert [r["target_card_id"] for r in await _rows(c, p["id"], target_card_id="y")] == ["y"]
        page = await c.get(f"/pm/projects/{p['id']}/coach-actions", params={"limit": 1})
        assert len(page.json()) == 1 and page.headers["X-Total-Count"] == "2"
