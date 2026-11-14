"""George 10-13 v4 rulings on RM's PM surface.

* Ruling 1 (D15): fr may CREATE a project (the request, brief v1); every later write to the
  project's resources by a fr-delegated caller is 403 ``project_writes_are_pm_only``. Reads
  stay open, the conveyor is unaffected, the catalogue is not covered.
* Ruling 2 (D16): the IT type starts with the ``setup`` system phase; a created project is
  in it (the API-level create / 422 / 409 cases are in ``test_pm_project_phase.py``).
* Ruling 9: exactly three additions — capability ``kanban_coach``, card types
  ``project_phase_start`` / ``project_phase_closure`` (project_management) — in every WORK
  phase of ``it`` (R185: setup has no phase_start or closure of its own).
  ``phase_coverage_check``: retired by R347 (4).
* R184 / R185 / R88 / R188 / R190 / R193 (D22): ONE IT type ``it`` replaces the four v3 kinds;
  phases (George 2026-11-03) ``setup`` → ``preliminary`` → ``analysis`` → ``design`` →
  ``execution`` → ``test`` with the matrix placed by capability; entry card type
  ``project_start_request_analysis``; board fields ``source_artifact_id``,
  ``source_item_id``, ``wait_for_phase``; card definitions from the v4 capability YAML.
* Scope: program, meta_prompt, meta_procedure and kaizen are byte-identical to origin/main
  (no phases, no card types), so creating one is 409 ``project_type_missing_setup_phase``.

All tests hit real PostgreSQL through the FastAPI test client; only licence introspection is
faked (the ``licences`` fixture), as in ``test_pm_project_intake.py``.
"""

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient

from tests.briefs import MIN_BRIEF
from tests.project_types import setup_type_id, typed
from tests.test_pm_project_intake import (  # noqa: F401 — `licences` is a fixture
    CUSTOMER_TENANT,
    DELEGATED,
    licences,
)

DEFAULTS = json.loads(
    (Path(__file__).resolve().parents[1] / "src/data/pm_catalogue_defaults.json").read_text()
)
PM_ONLY = "project_writes_are_pm_only"
# R184: the one IT type; the four v3 kinds it replaces.
V4_KINDS = ("it",)
V3_KINDS = ("bug_hunt_fix", "feature_new", "feature_update", "integration_report")
# R185; George 2026-11-03: the phases of `it`, verbatim (closure is fr's, not RM's).
# George 2026-11-07: `py2rust` (and its plan/port/parity/cutover phases) is retired by 0083;
# the rewrite chain is an `it` flow.
IT_PHASES = ["setup", "preliminary", "analysis", "design", "execution", "test"]
# R228 (George 2026-10-14): `it` places every card type the v4 design defines (66; R326 added
# the two change_* asset-revision types, R333 added rewrite_plan). George 2026-11-03 "move by
# capability": analysis -> analysis; architecture, design, bdd_tdd -> design; testing -> test;
# research, change_management, development, refactoring keep their design `phase`. R331
# (George 2026-11-01): `choice_option_development` (ADM E) stays in `execution`;
# `decision_development` goes with it (its upstream is choice_option_development), and
# `tdd_test_development` too (paired with its development card, R250, P-W5-5).
# `development_bug_fix` is ALSO in `test` (testing_execution findings become fix cards). Every work
# phase has project_phase_start + project_phase_closure; project_closure is in the last (R88).
# Pinned literally so a design change is a visible diff.
_PM = {"project_phase_closure", "project_phase_start"}
IT_MATRIX = {
    "setup": {
        "project_setup",
        "project_start_request_analysis",
    },
    "preliminary": _PM
    | {
        "change_decision_development",
        "change_principle_development",
        "change_requirement_development",
        "development_poc",
        "domain_analysis",
        "feasibility_study",
        "future_state_analysis",
        "prototype_experiment",
        "release_planning",
        "research_spike",
        "stakeholder_comms",
        "technology_landscape_survey",
    },
    "analysis": _PM
    | {
        "bug_analysis",
        "change_impact_analysis",
        "change_risk_analysis",
        "code_quality_analysis",
        "consistency_analysis",
        "gap_analysis",
        "plateau_planning_analysis",
        "rewrite_plan",
        "security_analysis",
        "structure_analysis",
    },
    "design": _PM
    | {
        "architecture_baseline_harvest",
        "architecture_epic",
        "architecture_spike",
        "architecture_subtask",
        "architecture_task",
        "bdd_test_development",
        "bdd_test_evaluation",
        "bdd_test_improvement",
        "design_packaging",
        "diagram_design",
        "narrative_design",
        "principle_development",
        "requirement_development",
        "tdd_test_evaluation",
        "tdd_test_improvement",
    },
    "execution": _PM
    | {
        "architecture_refactoring",
        "choice_option_development",
        "code_smell_refactoring",
        "decision_development",
        "decoupling_refactoring",
        "dependency_refactoring",
        "development_bug_fix",
        "development_chore",
        "development_feature_new",
        "development_feature_update",
        "development_iac_security_patch",
        "development_security_patch",
        "development_source_security_patch",
        "function_class_reallocation",
        "library_refactoring",
        "optimization_refactoring",
        "re_engineering",
        # George 2026-11-07: the three held rewrite_* types (the chain is an `it` flow).
        "rewrite_cutover_readiness",
        "rewrite_parity_qa",
        "rewrite_slice",
        "tdd_test_development",
    },
    "test": _PM
    | {
        "development_bug_fix",
        "project_closure",
        "test_development",
        "test_process_evaluation",
        "testing_evaluation",
        "testing_execution",
        "testing_plan",
        "testing_strategy",
    },
}
# The four other default types, frozen as origin/main serialised them before v4 (one
# ``json.dumps(row, ensure_ascii=False)`` line each, key order included). v4 must not touch them.
NON_V4_ROWS = {
    "program": (
        '{"name": "program", "title": "Program", "description": "project of projects managing '
        'the dependencies between the projects and the overal direction", "phases": [], '
        '"card_types": {}, "entry_card_types": {}}'
    ),
    "meta_prompt": (
        '{"name": "meta_prompt", "title": "Meta prompt", "description": "project about the '
        'continuous and incremental improvement of prompts used in projects", "phases": [], '
        '"card_types": {}, "entry_card_types": {}}'
    ),
    "meta_procedure": (
        '{"name": "meta_procedure", "title": "Meta procedure", "description": "project about '
        'the continuous and incremental improvement of procedures used in projects", '
        '"phases": [], "card_types": {}, "entry_card_types": {}}'
    ),
    "kaizen": (
        '{"name": "kaizen", "title": "Kaizen", "description": "project about the continuous '
        'and incremental improvement of workflows used in the system", "phases": [], '
        '"card_types": {}, "entry_card_types": {}}'
    ),
}
# The conveyor in the customer's tenant: a plain (non-delegated) licence.
CONVEYOR = {"X-Tenant-Id": CUSTOMER_TENANT}


def _code(resp) -> str | None:
    body = resp.json()
    return (body.get("error") or {}).get("code") if isinstance(body, dict) else None


async def _delegated_project(c: AsyncClient, title: str = "requested") -> dict:
    resp = await c.post(
        "/pm/projects",
        json=await typed(c, {"title": title, "brief": MIN_BRIEF}, DELEGATED),
        headers=DELEGATED,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Ruling 1 (D15): after the request, only PM writes the project
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("licences")
class TestFrIsReadOnlyAfterTheRequest:
    async def test_delegated_create_works_and_starts_in_setup(self, test_client: AsyncClient):
        p = await _delegated_project(test_client)
        assert p["tenant_id"] == CUSTOMER_TENANT
        assert p["current_phase"] == "setup"
        assert p["brief_author_kind"] == "user"

    async def test_delegated_writes_are_403_and_change_nothing(self, test_client: AsyncClient):
        c = test_client
        p = await _delegated_project(c)
        pid = p["id"]
        writes = {
            "put": await c.put(f"/pm/projects/{pid}", json={"title": "renamed"}, headers=DELEGATED),
            "phase": await c.put(
                f"/pm/projects/{pid}/phase", json={"phase": "setup"}, headers=DELEGATED
            ),
            "brief_version": await c.post(
                f"/pm/projects/{pid}/brief/versions",
                json={"brief": {"notes": "v2"}},
                headers=DELEGATED,
            ),
            "delete": await c.delete(f"/pm/projects/{pid}", headers=DELEGATED),
        }
        for name, resp in writes.items():
            assert resp.status_code == 403, (name, resp.text)
            assert _code(resp) == PM_ONLY, name
        got = (await c.get(f"/pm/projects/{pid}", headers=DELEGATED)).json()
        assert got["title"] == "requested"
        assert got["brief_version"] == 1
        assert got["current_phase"] == "setup"

    async def test_fr_may_close_the_project_and_nothing_else(self, test_client: AsyncClient):
        """R345 (4): fr archiving its project closes the RM project — status ONLY."""
        c = test_client
        pid = (await _delegated_project(c))["id"]
        refused = {
            "other_status": {"title": "requested", "status": "executing"},
            "close_and_describe": {"title": "requested", "status": "closed", "description": "x"},
            "title_only": {"title": "requested"},
        }
        for name, body in refused.items():
            resp = await c.put(f"/pm/projects/{pid}", json=body, headers=DELEGATED)
            assert resp.status_code == 403, (name, resp.text)
            assert _code(resp) == PM_ONLY, name
        assert (await c.get(f"/pm/projects/{pid}", headers=DELEGATED)).json()["status"] != "closed"

        closed = await c.put(
            f"/pm/projects/{pid}",
            json={"title": "requested", "status": "closed"},
            headers=DELEGATED,
        )
        assert closed.status_code == 200, closed.text
        got = (await c.get(f"/pm/projects/{pid}", headers=DELEGATED)).json()
        assert got["status"] == "closed"
        assert got["title"] == "requested"
        assert got["current_phase"] == "setup"

    async def test_fr_close_never_writes_fr_s_title(self, test_client: AsyncClient):
        """A stale title in fr's body (PM renamed meanwhile) is ignored: no rename revert."""
        c = test_client
        pid = (await _delegated_project(c))["id"]
        renamed = await c.put(f"/pm/projects/{pid}", json={"title": "by pm"}, headers=CONVEYOR)
        assert renamed.status_code == 200, renamed.text
        closed = await c.put(
            f"/pm/projects/{pid}",
            json={"title": "requested", "status": "closed"},
            headers=DELEGATED,
        )
        assert closed.status_code == 200, closed.text
        assert (closed.json()["status"], closed.json()["title"]) == ("closed", "by pm")

    async def test_fr_closes_a_project_whose_stored_title_has_whitespace(
        self, test_client: AsyncClient, test_session
    ):
        """The schema strips fr's title; a stored title with edge whitespace still closes."""
        from sqlalchemy import select, update

        from models.pm import Project

        c = test_client
        pid = (await _delegated_project(c))["id"]
        await test_session.execute(
            update(Project).where(Project.id == pid).values(title="  padded  ")
        )
        await test_session.commit()
        closed = await c.put(
            f"/pm/projects/{pid}", json={"title": "padded", "status": "closed"}, headers=DELEGATED
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["status"] == "closed"
        # The stored row, not the (output-schema-stripped) response: title untouched.
        test_session.expire_all()
        row = (await test_session.execute(select(Project.title).where(Project.id == pid))).one()
        assert row.title == "  padded  "

    async def test_the_conveyor_may_still_set_any_status(self, test_client: AsyncClient):
        c = test_client
        pid = (await _delegated_project(c))["id"]
        put = await c.put(
            f"/pm/projects/{pid}",
            json={"title": "by pm", "status": "executing", "description": "d"},
            headers=CONVEYOR,
        )
        assert put.status_code == 200, put.text
        assert (put.json()["status"], put.json()["title"]) == ("executing", "by pm")

    async def test_delegated_reads_are_200(self, test_client: AsyncClient):
        c = test_client
        p = await _delegated_project(c)
        pid = p["id"]
        for path in (
            f"/pm/projects/{pid}",
            "/pm/projects",
            f"/pm/projects/{pid}/phase-events",
            f"/pm/projects/{pid}/wip-limit",
            f"/pm/projects/{pid}/brief/versions",
            f"/pm/projects/{pid}/brief/versions/1",
        ):
            resp = await c.get(path, headers=DELEGATED)
            assert resp.status_code == 200, (path, resp.text)

    async def test_the_conveyor_writes_the_same_project(self, test_client: AsyncClient):
        c = test_client
        p = await _delegated_project(c)
        pid = p["id"]
        put = await c.put(f"/pm/projects/{pid}", json={"title": "by pm"}, headers=CONVEYOR)
        assert put.status_code == 200, put.text
        phase = await c.put(f"/pm/projects/{pid}/phase", json={"phase": "setup"}, headers=CONVEYOR)
        assert phase.status_code == 200, phase.text
        v2 = await c.post(
            f"/pm/projects/{pid}/brief/versions",
            json={"brief": {"notes": "v2"}},
            headers=CONVEYOR,
        )
        assert v2.status_code == 200, v2.text
        assert v2.json()["author_kind"] == "conveyor"
        gone = await c.delete(f"/pm/projects/{pid}", headers=CONVEYOR)
        assert gone.status_code == 200, gone.text

    @pytest.mark.parametrize("with_brief", [False, True])
    async def test_delegated_repost_onto_an_existing_row_is_403(
        self, test_client: AsyncClient, with_brief: bool
    ):
        """The get-or-create re-POST that lands on an EXISTING project updates it in place:
        a write after the request. Brief-less it is found up front; with a brief it is the
        insert's unique violation that lands on the survivor. Both are refused."""
        c = test_client
        origin = {"origin_kind": "project", "origin_project_id": CUSTOMER_TENANT}
        first = await c.post(
            "/pm/projects",
            json=await typed(c, {"title": "minted", "brief": MIN_BRIEF, **origin}, DELEGATED),
            headers=DELEGATED,
        )
        assert first.status_code == 200, first.text
        body = {"title": "minted", "description": "changed", **origin}
        if with_brief:
            body["brief"] = {"notes": "again"}
        again = await c.post(
            "/pm/projects", json=await typed(c, body, DELEGATED), headers=DELEGATED
        )
        assert again.status_code == 403, again.text
        assert _code(again) == PM_ONLY
        got = (await c.get(f"/pm/projects/{first.json()['id']}", headers=DELEGATED)).json()
        assert got["description"] is None and got["brief_version"] == 1
        listed = await c.get(
            "/pm/projects", params={"origin_project_id": CUSTOMER_TENANT}, headers=DELEGATED
        )
        assert [x["id"] for x in listed.json()] == [first.json()["id"]]
        # The conveyor's re-POST keeps the idempotent update in place.
        conv = await c.post("/pm/projects", json=await typed(c, body, CONVEYOR), headers=CONVEYOR)
        assert conv.status_code == 200, conv.text
        assert conv.json()["id"] == first.json()["id"]

    async def test_delegated_catalogue_crud_is_allowed(self, test_client: AsyncClient):
        """fr helps tenants manage the catalogue: ruling 1 does not cover it."""
        c = test_client
        cap = await c.post("/pm/capabilities", json={"name": "fr_cap"}, headers=DELEGATED)
        assert cap.status_code == 200, cap.text
        ct = await c.post(
            "/pm/card-types",
            json={"name": "fr_card", "capability_id": cap.json()["id"]},
            headers=DELEGATED,
        )
        assert ct.status_code == 200, ct.text
        tid = await setup_type_id(c, DELEGATED, name="fr_type")
        upd = await c.put(
            f"/pm/capabilities/{cap.json()['id']}",
            json={"name": "fr_cap", "title": "edited"},
            headers=DELEGATED,
        )
        assert upd.status_code == 200, upd.text
        got = await c.get(f"/pm/project-types/{tid}", headers=DELEGATED)
        assert got.status_code == 200, got.text


def _project_routes() -> list[APIRoute]:
    from routers.pm_projects import router

    return [r for r in router.routes if isinstance(r, APIRoute)]


# Linking the Planka board completes the request (fr builds the board right after the
# create): fr may write it while the project has none; a DIFFERENT link afterwards is the
# same 403, raised in ``services.pm_project_board_service`` (``test_pm_project_board_link.py``).
REQUEST_COMPLETING_WRITES = {"/{eid}/planka-board"}
# fr may CLOSE a project (status only, R345 (4)); the route checks it, not the dependency.
FR_CLOSE_WRITE = ({"PUT"}, "/{eid}")


def test_every_mutating_project_route_carries_the_one_guard():
    """ONE dependency guards every mutating ``/pm/projects/{eid}...`` route: a new mutating
    route without it goes red here, naming it."""
    from auth.pm_only import require_pm_writer

    routes = _project_routes()
    assert len(routes) >= 10, "vacuity floor: the router was not walked"
    guarded, missing = [], []
    for r in routes:
        mutating = bool(r.methods - {"GET", "HEAD", "OPTIONS"})
        if not (mutating and r.path.startswith("/{eid}")) or r.path in REQUEST_COMPLETING_WRITES:
            continue
        if (r.methods, r.path) == FR_CLOSE_WRITE:
            continue
        calls = {d.call for d in r.dependant.dependencies}
        (guarded if require_pm_writer in calls else missing).append(f"{r.methods} {r.path}")
    assert missing == []
    # + PUT /{eid}/capabilities (R260); + POST/PATCH /{eid}/coach-actions (R311);
    # + PATCH /{eid}/criteria/{criterion_id} (fr cockpit A4, R-CK7)
    # - PUT /{eid}: route-checked fr close (FR_CLOSE_WRITE, R345 (4))
    assert len(guarded) == 7, guarded
    # Create is the request: never guarded. Reads are never guarded.
    for r in routes:
        calls = {d.call for d in r.dependant.dependencies}
        if r.path == "" or r.methods <= {"GET", "HEAD"}:
            assert require_pm_writer not in calls, f"{r.methods} {r.path}"


# ---------------------------------------------------------------------------
# Ruling 2 (D16): the setup system phase in the defaults
# ---------------------------------------------------------------------------


class TestV4Scope:
    def test_the_type_set_is_it_kata_and_the_four_untouched(self):
        names = [t["name"] for t in DEFAULTS["project_types"]]
        # + the kata type (George 2026-10-31, Kata K15): a v4 addition with only setup placed
        # (the py2rust type of 2026-11-03 is retired by migration 0083, George 2026-11-07)
        assert sorted(names) == sorted([*V4_KINDS, "kata", *NON_V4_ROWS])
        assert "R184" in DEFAULTS["sources"]["v4_rulings"]["scope"]
        assert "K15" in DEFAULTS["sources"]["v4_rulings"]["scope"]

    def test_kata_places_its_setup_and_the_kata_only_cards(self):
        # Kata K15 (George 2026-10-31, n8n-designs kata/wp3-entry.html) + Kata WP4 (George
        # 2026-11-01, kata/wp4-grasp.html): kata keeps the first-build phases of ``it`` (NOT
        # the 2026-11-03 analysis/design/test: phase_start's Plan gate BLOCKs a phase with zero
        # cards) and the setup card types of ``it``, and adds the four workflow_engineering
        # preliminary types plus (Kata WP5, George 2026-11-03) the ONE execution type wf_change —
        # which appear on NO other project type (the exception to R228).
        rows = {t["name"]: t for t in DEFAULTS["project_types"]}
        it, kata = rows["it"], rows["kata"]
        wf = ["wf_andon_scope", "wf_observe", "wf_probe", "wf_root_cause"]
        assert kata["phases"] == ["setup", "preliminary", "execution"]
        assert kata["card_types"] == {
            "setup": it["card_types"]["setup"],
            "preliminary": wf,
            "execution": ["wf_change"],
        }
        it_names = {n for v in it["card_types"].values() for n in v}
        assert not it_names & {*wf, "wf_change"}, "kata-only: `it` must never gain a wf_* card type"
        assert kata["entry_card_types"] == {"setup": ["project_start_request_analysis"]}

    def test_non_v4_types_are_byte_identical_to_origin_main(self):
        rows = {t["name"]: t for t in DEFAULTS["project_types"]}
        for name, frozen in NON_V4_ROWS.items():
            assert json.dumps(rows[name], ensure_ascii=False) == frozen, name

    async def test_a_non_v4_type_cannot_create_a_project(self, authenticated_client: AsyncClient):
        c = authenticated_client
        assert (await c.post("/pm/catalogue/import-defaults", json={})).status_code == 200
        types = {t["name"]: t for t in (await c.get("/pm/project-types")).json()}
        for name in NON_V4_ROWS:
            assert types[name]["phases"] == [], name
            resp = await c.post(
                "/pm/projects",
                json={
                    "title": f"p-{name}",
                    "brief": MIN_BRIEF,
                    "project_type_id": types[name]["id"],
                },
            )
            assert resp.status_code == 409, (name, resp.text)
            assert _code(resp) == "project_type_missing_setup_phase", name


class TestSetupPhaseDefaults:
    def test_setup_is_first_in_each_v4_kind(self):
        setup = DEFAULTS["project_phases"][0]
        assert (setup["name"], setup["title"]) == ("setup", "Setup")
        assert "George 10-13 v4 ruling 2" in setup["description"]
        assert [p["name"] for p in DEFAULTS["project_phases"]] == IT_PHASES
        spine = IT_PHASES[1:]
        kinds = [t for t in DEFAULTS["project_types"] if t["name"] in V4_KINDS]
        assert len(kinds) == 1
        for t in kinds:
            assert t["phases"] == ["setup", *spine], t["name"]
        assert DEFAULTS["sources"]["v4_rulings"]["setup_phase"].startswith("George 10-13")

    async def test_every_imported_v4_kind_creates_its_project_in_setup(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        assert (await c.post("/pm/catalogue/import-defaults", json={})).status_code == 200
        types = (await c.get("/pm/project-types")).json()
        assert len(types) == len(DEFAULTS["project_types"])
        types = [t for t in types if t["name"] in V4_KINDS]
        assert len(types) == 1
        for t in types:
            assert t["phases"][0]["name"] == "setup", t["name"]
            assert t["phases"][0]["position"] == 0, t["name"]
            resp = await c.post(
                "/pm/projects",
                json={"title": f"p-{t['name']}", "brief": MIN_BRIEF, "project_type_id": t["id"]},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["current_phase"] == "setup", t["name"]


# ---------------------------------------------------------------------------
# Ruling 9: exactly three additions
# ---------------------------------------------------------------------------

V4_CAPABILITY = {
    "name": "kanban_coach",
    "title": "Kanban coach",
    "description": (
        "Protector of flow. Any card interruption invokes a card for the kanban coach. Owns "
        "all flow-related decisions and fr/human interaction during a phase: blocked cards, "
        "comments, human input and answers, in-flow approvals, and rework limit reached (R34)."
    ),
}
V4_CARD_TYPES = {
    "project_phase_start": (
        "Project phase start",
        "each phase decisions of which capabilities and card types will be needed and "
        "creating the initial set of cards for that phase",
    ),
    "project_phase_closure": (
        "Project phase closure",
        "closing out the phase which also triggers the start of the next phase (or better "
        "asks the user to approve the phase)",
    ),
}


class TestRuling9Additions:
    def test_exactly_the_three_rows(self):
        caps = {c["name"]: c for c in DEFAULTS["capabilities"]}
        assert caps["kanban_coach"] == V4_CAPABILITY
        cts = {c["name"]: c for c in DEFAULTS["card_types"]}
        for name, (title, desc) in V4_CARD_TYPES.items():
            row = cts[name]
            assert (row["title"], row["description"]) == (title, desc), name
            assert row["capability"] == "project_management", name
        assert "phase_coverage_check" not in cts, "retired by R347 (4)"
        src = DEFAULTS["sources"]["v4_rulings"]
        assert src["capabilities"] == ["kanban_coach"]
        assert src["card_types"] == list(V4_CARD_TYPES)
        assert "George 10-13 v4 ruling 9" in src["additions"]
        # Nothing else is placed on kanban_coach (its card types are OPEN, O-COACH).
        assert not [c for c in DEFAULTS["card_types"] if c["capability"] == "kanban_coach"]

    def test_phase_cards_are_in_every_work_phase_of_each_v4_kind(self):
        kinds = [t for t in DEFAULTS["project_types"] if t["name"] in V4_KINDS]
        assert len(kinds) == 1
        for t in kinds:
            assert set(t["card_types"]) == set(t["phases"]), t["name"]
            assert not set(V4_CARD_TYPES) & set(t["card_types"]["setup"]), "R185"
            for phase in t["phases"][1:]:
                assert set(V4_CARD_TYPES) <= set(t["card_types"][phase]), (t["name"], phase)

    async def test_import_places_them_in_every_work_phase(self, authenticated_client: AsyncClient):
        c = authenticated_client
        assert (await c.post("/pm/catalogue/import-defaults", json={})).status_code == 200
        caps = {x["name"]: x for x in (await c.get("/pm/capabilities")).json()}
        assert caps["kanban_coach"]["title"] == "Kanban coach"
        kinds = [t for t in (await c.get("/pm/project-types")).json() if t["name"] in V4_KINDS]
        assert len(kinds) == 1
        for t in kinds:
            comp = (await c.get(f"/pm/project-types/{t['id']}/card-types")).json()
            assert [p["name"] for p in comp["phases"]] == [p["name"] for p in t["phases"]]
            for p in comp["phases"][1:]:
                placed = {
                    (cap["name"], ct["name"])
                    for cap in p["capabilities"]
                    for ct in cap["card_types"]
                }
                for name in V4_CARD_TYPES:
                    assert ("project_management", name) in placed, (t["name"], p["name"], name)


# ---------------------------------------------------------------------------
# D22: R184 / R185 / R88 / R188 / R190 / R193 — the one IT type
# ---------------------------------------------------------------------------


def _it() -> dict:
    (row,) = [t for t in DEFAULTS["project_types"] if t["name"] == "it"]
    return row


class TestD22ItType:
    def test_no_v3_it_kinds(self):
        names = {t["name"] for t in DEFAULTS["project_types"]}
        assert not names & set(V3_KINDS)
        assert not [r for r in DEFAULTS["recipes"] if r["project_type"] in V3_KINDS]
        assert DEFAULTS["recipes"] == [], "the v3 recipes went with the v3 kinds"
        assert "v3" not in json.dumps(DEFAULTS["sources"]["v4_rulings"])

    def test_it_is_the_registry_row(self):
        it = _it()
        assert it["description"].startswith("project that starts with receipt of a github repo")

    def test_it_has_three_phases_in_order(self):
        assert _it()["phases"] == IT_PHASES
        assert [p["name"] for p in DEFAULTS["project_phases"]] == IT_PHASES

    def test_matrix_and_entry(self):
        it = _it()
        assert {p: set(v) for p, v in it["card_types"].items()} == IT_MATRIX
        assert it["entry_card_types"] == {"setup": ["project_start_request_analysis"]}, "R88"
        placed = {n for v in IT_MATRIX.values() for n in v}
        # + the KATA-ONLY five (Kata WP4 2026-11-01 + WP5 2026-11-03): defaults `it` never places.
        kata_only = {"wf_observe", "wf_probe", "wf_root_cause", "wf_andon_scope", "wf_change"}
        # George 2026-11-07: the three held rewrite_* types are IN `placed` — `it` execution.
        assert {"rewrite_slice", "rewrite_parity_qa", "rewrite_cutover_readiness"} <= placed
        assert {c["name"] for c in DEFAULTS["card_types"]} == placed | kata_only
        assert not placed & kata_only

    def test_template_has_the_three_fields(self):
        (tpl,) = DEFAULTS["board_templates"]
        fields = [f for g in tpl["custom_field_groups"] for f in g["fields"]]
        for name in ("source_artifact_id", "source_item_id", "wait_for_phase"):
            assert fields.count(name) == 1, name

    def test_required_inputs_are_not_empty(self):
        """R193: a type's required inputs = the union of its entry card types' inputs."""
        it = _it()
        cts = {c["name"]: c for c in DEFAULTS["card_types"]}
        required = {
            i for names in it["entry_card_types"].values() for n in names for i in cts[n]["inputs"]
        }
        assert required, "the type would have no required inputs"
        assert required == {"problem_statement", "desired_outcome", "success_criteria", "in_scope"}
        # R194: READY.readying output keys are not inputs.
        all_inputs = {i for c in DEFAULTS["card_types"] for i in c["inputs"]}
        for not_an_input in ("brief_version", "project_type", "required_inputs"):
            assert not_an_input not in all_inputs, not_an_input
        # Every name is a field of RM's ProjectBrief.
        from schemas.pm_intake import ProjectBrief

        assert required <= set(ProjectBrief.model_fields)

    def test_only_declared_card_types_have_inputs(self):
        """R194: inputs come only from the YAML ``inputs:`` section; absent = []."""
        got = {c["name"]: c["inputs"] for c in DEFAULTS["card_types"]}
        assert got.pop("project_start_request_analysis") == [
            "problem_statement",
            "desired_outcome",
            "success_criteria",
            "in_scope",
        ]
        assert all(v == [] for v in got.values()), got

    def test_every_card_definition_comes_from_v4(self):
        """R193: inputs / outputs / acceptance are filled for every card type, and nothing
        v3-shaped survives (v3 acceptance was prose; v4 acceptance is ``<LIST>: <check>``)."""
        # R213: the flow, not the agent, writes psra's one product (the project_setup card),
        # so its releasing slots name no output; every other type has at least one.
        flow_only_outputs = {"project_start_request_analysis"}
        for c in DEFAULTS["card_types"]:
            assert c["acceptance"], c["name"]
            assert bool(c["outputs"]) is (c["name"] not in flow_only_outputs), c["name"]
            for a in c["acceptance"]:
                lst, _, check = a.partition(": ")
                assert lst in ("READY", "DOING", "QA", "DONE"), (c["name"], a)
                assert check.isidentifier(), (c["name"], a)
            assert "handoff_comment_id" not in c["outputs"], c["name"]
        cts = {c["name"]: c for c in DEFAULTS["card_types"]}
        assert "workspace" not in cts["project_setup"]["outputs"], "v3 definition"
        assert (
            # checklist_covers_required_inputs became a computed gate (R204/R212), not an
            # agent check, so it left the design's inspecting checks.
            "READY: brief_unchanged" in (cts["project_start_request_analysis"]["acceptance"])
        )
        assert cts["development_bug_fix"]["outputs"][-1] == "merge_sha"

    async def test_import_gives_it_three_phases_and_the_entry(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        assert (await c.post("/pm/catalogue/import-defaults", json={})).status_code == 200
        types = {t["name"]: t for t in (await c.get("/pm/project-types")).json()}
        assert not set(types) & set(V3_KINDS)
        it = types["it"]
        assert [p["name"] for p in it["phases"]] == IT_PHASES
        comp = (await c.get(f"/pm/project-types/{it['id']}/card-types")).json()
        entries = {
            (p["name"], ct["name"])
            for p in comp["phases"]
            for cap in p["capabilities"]
            for ct in cap["card_types"]
            if ct["is_entry"]
        }
        assert entries == {("setup", "project_start_request_analysis")}
