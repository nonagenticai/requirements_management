"""PM catalogue: project phases, capabilities, card types, project types + composition.

All tests hit real PostgreSQL through the FastAPI test client — no mocks.
"""

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.tenants import T

# RM's ``default`` board template is the MASTER (George 10-13 v4 ruling 12): group order →
# field count. Seeded from fr field-groups.ts @ de6c7763 (Metadata 22, Runtime State 58), plus
# project_type / phase / capability in Metadata (D4) and released in Runtime State (ruling 10;
# R66 dropped rework_count), plus source_artifact_id / source_item_id in Metadata (R188) and
# wait_for_phase in Runtime State (R190), plus spec_hash at the end of Metadata (R311: the
# card birth key of kanban-coach-errors.md §7 item 1), plus criterion_ids at the end of
# Relations (R-CK10: the acceptance-criterion ids a card serves, read by the closure flow).
TEMPLATE_FIELD_GROUPS = {
    "Metadata": 28,
    "Relations": 5,
    "Acceptance Criteria": 5,
    "Validation": 4,
    "Requirements": 5,
    "Principles": 5,
    "Decisions": 5,
    "Procedures": 4,
    "Runtime State": 60,
}

V3_KINDS = ("bug_hunt_fix", "feature_new", "feature_update", "integration_report")
# ⛔ KATA-ONLY (George 2026-11-01, Kata WP4): the workflow_engineering work a `kata` does. They
# are placed on `kata` alone - never on `it` (the explicit exception to R228).
KATA_CARD_TYPES = (
    "wf_observe",
    "wf_probe",
    "wf_root_cause",
    "wf_andon_scope",
    "wf_change",  # Kata WP5 (George 2026-11-03): the one execution-phase kata type
)
# Every card type a default project type places: `it`'s set plus the kata-only five (the three
# held rewrite_* types join `it` at the end of this list, 2026-11-07).
# R228 (George 2026-10-14): `it` places every card type the v4 design defines (66; R326
# George 2026-10-30 added change_principle_development + change_decision_development;
# R333 added rewrite_plan to the design, so the first regeneration emits it).
IT_CARD_TYPES = (
    "project_start_request_analysis",
    "project_setup",
    "project_closure",
    "stakeholder_comms",
    "release_planning",
    "future_state_analysis",
    "change_requirement_development",
    "change_principle_development",
    "change_decision_development",
    "research_spike",
    "prototype_experiment",
    "feasibility_study",
    "domain_analysis",
    "technology_landscape_survey",
    "rewrite_plan",
    "bug_analysis",
    "security_analysis",
    "code_quality_analysis",
    "structure_analysis",
    "consistency_analysis",
    "gap_analysis",
    "plateau_planning_analysis",
    "change_risk_analysis",
    "change_impact_analysis",
    "architecture_spike",
    "architecture_epic",
    "architecture_task",
    "architecture_subtask",
    "principle_development",
    "requirement_development",
    "choice_option_development",
    "decision_development",
    "architecture_baseline_harvest",
    "diagram_design",
    "narrative_design",
    "design_packaging",
    "testing_strategy",
    "testing_plan",
    "test_development",
    "testing_execution",
    "testing_evaluation",
    "test_process_evaluation",
    "bdd_test_development",
    "bdd_test_evaluation",
    "bdd_test_improvement",
    "tdd_test_development",
    "tdd_test_evaluation",
    "tdd_test_improvement",
    "development_poc",
    "development_bug_fix",
    "development_feature_new",
    "development_feature_update",
    "development_security_patch",
    "development_chore",
    "development_iac_security_patch",
    "development_source_security_patch",
    "re_engineering",
    "architecture_refactoring",
    "decoupling_refactoring",
    "library_refactoring",
    "code_smell_refactoring",
    "optimization_refactoring",
    "function_class_reallocation",
    "dependency_refactoring",
    "project_phase_start",
    "project_phase_closure",
)
# phase_coverage_check: retired by R347 (4).
RETIRED_CARD_TYPES = ("phase_coverage_check",)
# The rewrite chain's held types (RS-D4). George 2026-11-07: placed on `it` in execution (there
# is no py2rust project type); rewrite_plan is placed by its v4 design entry, in analysis.
REWRITE_CARD_TYPES = ("rewrite_slice", "rewrite_parity_qa", "rewrite_cutover_readiness")
ALL_CARD_TYPES = (*IT_CARD_TYPES, *KATA_CARD_TYPES, *REWRITE_CARD_TYPES)

DEFAULTS = json.loads(
    (Path(__file__).resolve().parents[1] / "src/data/pm_catalogue_defaults.json").read_text()
)


class _As:
    """One shared ASGI client, speaking as a fixed tenant on every call."""

    def __init__(self, client: AsyncClient, tenant_id: str):
        self._c, self._h = client, {"X-Tenant-Id": tenant_id}

    async def get(self, path: str, **kw):
        return await self._c.get(path, headers=self._h, **kw)

    async def post(self, path: str, **kw):
        return await self._c.post(path, headers=self._h, **kw)

    async def put(self, path: str, **kw):
        return await self._c.put(path, headers=self._h, **kw)

    async def delete(self, path: str, **kw):
        return await self._c.delete(path, headers=self._h, **kw)


async def _post(client: "AsyncClient | _As", path: str, body: dict) -> dict:
    resp = await client.post(path, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _phase(client: "AsyncClient | _As", name: str) -> dict:
    return await _post(client, "/pm/project-phases", {"name": name, "description": f"{name} d"})


async def _capability(client: "AsyncClient | _As", name: str) -> dict:
    return await _post(client, "/pm/capabilities", {"name": name})


async def _card_type(client: "AsyncClient | _As", name: str, capability_id: str) -> dict:
    return await _post(client, "/pm/card-types", {"name": name, "capability_id": capability_id})


# ---------------------------------------------------------------------------
# Catalogue CRUD
# ---------------------------------------------------------------------------


class TestCatalogueCrud:
    async def test_phase_crud_roundtrip(self, authenticated_client: AsyncClient):
        c = authenticated_client
        created = await _phase(c, "preliminary")
        assert created["name"] == "preliminary"
        assert created["title"] == "preliminary", "title defaults to the name"
        assert created["tenant_id"] == T("user-123")

        got = await c.get(f"/pm/project-phases/{created['id']}")
        assert got.status_code == 200 and got.json()["description"] == "preliminary d"

        upd = await c.put(
            f"/pm/project-phases/{created['id']}",
            json={"name": "preliminary", "title": "Preliminary", "description": "new"},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["title"] == "Preliminary"

        listed = await c.get("/pm/project-phases")
        assert listed.status_code == 200
        assert [p["name"] for p in listed.json()] == ["preliminary"]
        assert listed.headers["X-Total-Count"] == "1"

        deleted = await c.delete(f"/pm/project-phases/{created['id']}")
        assert deleted.status_code == 200
        assert (await c.get(f"/pm/project-phases/{created['id']}")).status_code == 404

    async def test_capability_crud_roundtrip(self, authenticated_client: AsyncClient):
        c = authenticated_client
        cap = await _capability(c, "testing")
        upd = await c.put(
            f"/pm/capabilities/{cap['id']}", json={"name": "testing", "description": "QA"}
        )
        assert upd.status_code == 200 and upd.json()["description"] == "QA"
        assert (await c.get(f"/pm/capabilities/{cap['id']}")).json()["name"] == "testing"
        assert [x["name"] for x in (await c.get("/pm/capabilities")).json()] == ["testing"]
        assert (await c.delete(f"/pm/capabilities/{cap['id']}")).status_code == 200

    async def test_card_type_crud_and_capability_filter(self, authenticated_client: AsyncClient):
        c = authenticated_client
        testing = await _capability(c, "testing")
        dev = await _capability(c, "development")
        bug = await _card_type(c, "bug_analysis", testing["id"])
        await _card_type(c, "development_bug_fix", dev["id"])

        only_testing = await c.get("/pm/card-types", params={"capability_id": testing["id"]})
        assert [x["name"] for x in only_testing.json()] == ["bug_analysis"]

        moved = await c.put(
            f"/pm/card-types/{bug['id']}",
            json={"name": "bug_analysis", "capability_id": dev["id"]},
        )
        assert moved.status_code == 200 and moved.json()["capability_id"] == dev["id"]
        assert (await c.get(f"/pm/card-types/{bug['id']}")).json()["capability_id"] == dev["id"]
        assert (await c.delete(f"/pm/card-types/{bug['id']}")).status_code == 200

    async def test_duplicate_name_is_409(self, authenticated_client: AsyncClient):
        c = authenticated_client
        await _phase(c, "request")
        dup = await c.post("/pm/project-phases", json={"name": "request"})
        assert dup.status_code == 409, dup.text

    async def test_rename_onto_existing_name_is_409(self, authenticated_client: AsyncClient):
        c = authenticated_client
        await _capability(c, "design")
        other = await _capability(c, "testing")
        resp = await c.put(f"/pm/capabilities/{other['id']}", json={"name": "design"})
        assert resp.status_code == 409, resp.text

    @pytest.mark.parametrize("bad", ["Bug Analysis", "bug-analysis", "1st", ""])
    async def test_name_must_be_snake_case(self, authenticated_client: AsyncClient, bad: str):
        resp = await authenticated_client.post("/pm/project-phases", json={"name": bad})
        assert resp.status_code == 422, resp.text

    async def test_card_type_needs_a_known_capability(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post(
            "/pm/card-types",
            json={"name": "bug_analysis", "capability_id": "0195f48b-0000-7000-8000-000000000000"},
        )
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Project types: phases and composition
# ---------------------------------------------------------------------------


async def _it_type(c: AsyncClient) -> dict:
    """A project type with phases request, preliminary, contract and two card types."""
    phases = {n: await _phase(c, n) for n in ("request", "preliminary", "contract")}
    pm = await _capability(c, "project_management")
    testing = await _capability(c, "testing")
    cts = {
        "project_setup": await _card_type(c, "project_setup", pm["id"]),
        "bug_analysis": await _card_type(c, "bug_analysis", testing["id"]),
        "decision_development": await _card_type(c, "decision_development", pm["id"]),
    }
    pt = await _post(
        c,
        "/pm/project-types",
        {
            "name": "it",
            "description": "repo url in, pull request out",
            "phase_ids": [phases[n]["id"] for n in ("request", "preliminary", "contract")],
        },
    )
    return {"type": pt, "phases": phases, "card_types": cts}


class TestProjectTypes:
    async def test_phases_are_ordered(self, authenticated_client: AsyncClient):
        c = authenticated_client
        it = await _it_type(c)
        assert [p["name"] for p in it["type"]["phases"]] == ["request", "preliminary", "contract"]
        assert [p["position"] for p in it["type"]["phases"]] == [0, 1, 2]

        got = await c.get(f"/pm/project-types/{it['type']['id']}")
        assert [p["name"] for p in got.json()["phases"]] == ["request", "preliminary", "contract"]
        listed = await c.get("/pm/project-types")
        assert [p["name"] for p in listed.json()[0]["phases"]] == [
            "request",
            "preliminary",
            "contract",
        ]

    async def test_reorder_keeps_card_types_and_drop_removes_them(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        it = await _it_type(c)
        tid, ph, ct = it["type"]["id"], it["phases"], it["card_types"]
        put = await c.put(
            f"/pm/project-types/{tid}/card-types",
            json={
                "phases": [
                    {
                        "phase_id": ph["preliminary"]["id"],
                        "card_type_ids": [ct["project_setup"]["id"], ct["bug_analysis"]["id"]],
                    },
                    {
                        "phase_id": ph["contract"]["id"],
                        "card_type_ids": [ct["decision_development"]["id"]],
                    },
                ]
            },
        )
        assert put.status_code == 200, put.text

        # Reorder: preliminary first, drop contract.
        upd = await c.put(
            f"/pm/project-types/{tid}",
            json={
                "name": "it",
                "phase_ids": [ph["preliminary"]["id"], ph["request"]["id"]],
            },
        )
        assert upd.status_code == 200, upd.text
        assert [p["name"] for p in upd.json()["phases"]] == ["preliminary", "request"]

        comp = (await c.get(f"/pm/project-types/{tid}/card-types")).json()
        assert [p["name"] for p in comp["phases"]] == ["preliminary", "request"]
        prelim = comp["phases"][0]
        assert {
            (cap["name"], ct2["name"])
            for cap in prelim["capabilities"]
            for ct2 in cap["card_types"]
        } == {("project_management", "project_setup"), ("testing", "bug_analysis")}
        assert comp["phases"][1]["capabilities"] == []

    async def test_composition_groups_by_capability(self, authenticated_client: AsyncClient):
        c = authenticated_client
        it = await _it_type(c)
        tid, ph, ct = it["type"]["id"], it["phases"], it["card_types"]
        resp = await c.put(
            f"/pm/project-types/{tid}/card-types",
            json={
                "phases": [
                    {
                        "phase_id": ph["preliminary"]["id"],
                        "card_type_ids": [ct["bug_analysis"]["id"], ct["project_setup"]["id"]],
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "it"
        assert [p["name"] for p in body["phases"]] == ["request", "preliminary", "contract"]
        prelim = body["phases"][1]
        assert [cap["name"] for cap in prelim["capabilities"]] == [
            "project_management",
            "testing",
        ]
        assert [x["name"] for x in prelim["capabilities"][1]["card_types"]] == ["bug_analysis"]

    async def test_card_types_only_in_selected_phases(self, authenticated_client: AsyncClient):
        c = authenticated_client
        it = await _it_type(c)
        execution = await _phase(c, "execution")
        resp = await c.put(
            f"/pm/project-types/{it['type']['id']}/card-types",
            json={
                "phases": [
                    {
                        "phase_id": execution["id"],
                        "card_type_ids": [it["card_types"]["bug_analysis"]["id"]],
                    }
                ]
            },
        )
        assert resp.status_code == 422, resp.text
        assert "not selected" in resp.text

    async def test_unknown_phase_in_selection_is_422(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post(
            "/pm/project-types",
            json={"name": "kaizen", "phase_ids": ["0195f48b-0000-7000-8000-000000000000"]},
        )
        assert resp.status_code == 422, resp.text
        assert (await authenticated_client.get("/pm/project-types")).json() == [], (
            "a refused create must not leave a half-built project type behind"
        )

    async def test_repeated_phase_is_422(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _phase(c, "request")
        resp = await c.post(
            "/pm/project-types", json={"name": "it", "phase_ids": [p["id"], p["id"]]}
        )
        assert resp.status_code == 422, resp.text

    async def test_in_use_deletes_are_409_and_name_the_user(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        it = await _it_type(c)
        tid, ph, ct = it["type"]["id"], it["phases"], it["card_types"]
        await c.put(
            f"/pm/project-types/{tid}/card-types",
            json={
                "phases": [
                    {"phase_id": ph["request"]["id"], "card_type_ids": [ct["project_setup"]["id"]]}
                ]
            },
        )
        phase_del = await c.delete(f"/pm/project-phases/{ph['request']['id']}")
        assert phase_del.status_code == 409 and "project type it" in phase_del.text
        ct_del = await c.delete(f"/pm/card-types/{ct['project_setup']['id']}")
        assert ct_del.status_code == 409 and "project type it" in ct_del.text
        cap_id = ct["project_setup"]["capability_id"]
        cap_del = await c.delete(f"/pm/capabilities/{cap_id}")
        assert cap_del.status_code == 409 and "card type project_setup" in cap_del.text

        # The project type itself deletes, taking its own composition with it ...
        assert (await c.delete(f"/pm/project-types/{tid}")).status_code == 200
        # ... after which the phase and card type are free.
        assert (await c.delete(f"/pm/project-phases/{ph['request']['id']}")).status_code == 200
        assert (await c.delete(f"/pm/card-types/{ct['project_setup']['id']}")).status_code == 200


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    async def test_other_tenant_sees_and_touches_nothing(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        phase = await _phase(a, "request")
        cap = await _capability(a, "testing")
        await _card_type(a, "bug_analysis", cap["id"])
        pt = await _post(a, "/pm/project-types", {"name": "it", "phase_ids": [phase["id"]]})

        for path in (
            "/pm/project-phases",
            "/pm/capabilities",
            "/pm/card-types",
            "/pm/project-types",
        ):
            assert (await b.get(path)).json() == [], path
        assert (await b.get(f"/pm/project-phases/{phase['id']}")).status_code == 404
        assert (await b.get(f"/pm/project-types/{pt['id']}/card-types")).status_code == 404
        assert (await b.delete(f"/pm/project-types/{pt['id']}")).status_code == 404

        # B may reuse A's names, and may not reference A's rows.
        assert (await b.post("/pm/project-phases", json={"name": "request"})).status_code == 200
        foreign_cap = await b.post(
            "/pm/card-types", json={"name": "bug_analysis", "capability_id": cap["id"]}
        )
        assert foreign_cap.status_code == 422
        foreign_phase = await b.post(
            "/pm/project-types", json={"name": "it", "phase_ids": [phase["id"]]}
        )
        assert foreign_phase.status_code == 422

    async def test_composition_refuses_another_tenants_card_types(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        """FKs alone would accept A's card_type_id in B's matrix; the service must not."""
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        a_cap = await _capability(a, "testing")
        a_ct = await _card_type(a, "bug_analysis", a_cap["id"])

        b_phase = await _phase(b, "request")
        b_cap = await _capability(b, "testing")
        b_ct = await _card_type(b, "bug_analysis", b_cap["id"])
        b_pt = await _post(b, "/pm/project-types", {"name": "it", "phase_ids": [b_phase["id"]]})

        resp = await b.put(
            f"/pm/project-types/{b_pt['id']}/card-types",
            json={"phases": [{"phase_id": b_phase["id"], "card_type_ids": [a_ct["id"]]}]},
        )
        assert resp.status_code == 422, resp.text
        # Nothing was stored, and B's own card type still composes normally.
        matrix = await b.get(f"/pm/project-types/{b_pt['id']}/card-types")
        assert a_ct["id"] not in matrix.text
        ok = await b.put(
            f"/pm/project-types/{b_pt['id']}/card-types",
            json={"phases": [{"phase_id": b_phase["id"], "card_type_ids": [b_ct["id"]]}]},
        )
        assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# Default catalogue
# ---------------------------------------------------------------------------


class TestImportDefaults:
    async def test_import_is_idempotent_and_complete(self, authenticated_client: AsyncClient):
        c = authenticated_client
        first = await _post(c, "/pm/catalogue/import-defaults", {})
        n = {
            k: len(DEFAULTS[k])
            for k in ("project_phases", "capabilities", "card_types", "project_types")
        }
        for k, count in n.items():
            assert first[k] == {"created": count, "existing": 0, "filled": 0}, k
        assert first["skipped_card_types"] == []

        second = await _post(c, "/pm/catalogue/import-defaults", {})
        for k, count in n.items():
            assert second[k] == {"created": 0, "existing": count, "filled": 0}, k

        listed = await c.get("/pm/card-types", params={"limit": 1000})
        assert listed.headers["X-Total-Count"] == str(n["card_types"])

    async def test_it_and_kata_types_match_the_dataset(self, authenticated_client: AsyncClient):
        c = authenticated_client
        await _post(c, "/pm/catalogue/import-defaults", {})
        types = {t["name"]: t for t in (await c.get("/pm/project-types")).json()}
        assert set(types) == {t["name"] for t in DEFAULTS["project_types"]}
        for gone in V3_KINDS:
            assert gone not in types, "R184: the v3 kinds are replaced by `it`"

        spine = [p["name"] for p in DEFAULTS["project_phases"]]
        for kind in ("it", "kata"):
            expected = next(t for t in DEFAULTS["project_types"] if t["name"] == kind)
            assert [p["name"] for p in types[kind]["phases"]] == expected["phases"], kind
            # kata keeps its first-build phases; it has every default phase.
            assert expected["phases"] == (
                spine if kind == "it" else ["setup", "preliminary", "execution"]
            ), kind
            comp = (await c.get(f"/pm/project-types/{types[kind]['id']}/card-types")).json()
            placed = {
                p["name"]: sorted(
                    ct["name"] for cap in p["capabilities"] for ct in cap["card_types"]
                )
                for p in comp["phases"]
            }
            assert placed == {k: sorted(v) for k, v in expected["card_types"].items()}, kind

    async def test_import_never_overwrites_tenant_edits(self, authenticated_client: AsyncClient):
        c = authenticated_client
        await _post(c, "/pm/catalogue/import-defaults", {})
        phases = {p["name"]: p for p in (await c.get("/pm/project-phases")).json()}
        req = phases["preliminary"]
        edited = await c.put(
            f"/pm/project-phases/{req['id']}",
            json={"name": "preliminary", "title": "Intake", "description": "ours"},
        )
        assert edited.status_code == 200
        await _post(c, "/pm/catalogue/import-defaults", {})
        again = (await c.get(f"/pm/project-phases/{req['id']}")).json()
        assert (again["title"], again["description"]) == ("Intake", "ours")

    async def test_import_into_one_tenant_only(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        await _post(a, "/pm/catalogue/import-defaults", {})
        assert (await a.get("/pm/project-types")).json() != []
        assert (await b.get("/pm/project-types")).json() == []


class TestDefaultsDataset:
    """The committed default dataset is internally consistent."""

    def test_card_type_rules(self):
        """Pins the default card-type rules fr's computed gating reads (exact membership).
        R228 places every v4-defined card type, so each rule covers every target the design
        defines; only targets the design does not define are reported as skipped."""
        by_rule: dict[str, set[str]] = {}
        for c in DEFAULTS["card_types"]:
            for r in c["rules"]:
                by_rule.setdefault(r, set()).add(c["name"])
        assert by_rule == {
            "repo_write": {
                "architecture_refactoring",
                "bdd_test_development",
                "bdd_test_improvement",
                "code_smell_refactoring",
                "decoupling_refactoring",
                "dependency_refactoring",
                "development_bug_fix",
                "development_chore",
                "development_feature_new",
                "development_feature_update",
                "development_iac_security_patch",
                "development_poc",
                "development_security_patch",
                "development_source_security_patch",
                "function_class_reallocation",
                "library_refactoring",
                "optimization_refactoring",
                "re_engineering",
                "tdd_test_development",
                "tdd_test_improvement",
                "test_development",
                "rewrite_slice",
            },
            "container_read_exec": {
                "rewrite_parity_qa",
                "bdd_test_evaluation",
                "bug_analysis",
                "security_analysis",
                "structure_analysis",
                "tdd_test_evaluation",
                "testing_execution",
            },
            "setup": {"project_setup"},
            "no_reclassify_target": {
                "bug_analysis",
                "development_chore",
                "security_analysis",
                "structure_analysis",
            },
            "dependency_upgrade": {"development_chore"},
        }
        assert DEFAULTS["sources"]["skipped_rule_targets"] == {
            "container_read_exec": ["verification_test"],
            "no_reclassify_target": ["bug_hunt_sweep", "verification_test"],
        }
        bug = next(c for c in DEFAULTS["card_types"] if c["name"] == "bug_analysis")
        assert bug["rules"] == ["container_read_exec", "no_reclassify_target"], "no v3 rules"

    def test_board_template_is_rms_own_master(self):
        """RM's ``default`` template is the master fr builds every board from (George 10-13
        v4 ruling 12): pinned to RM's OWN committed fixture, not to fr's field-groups.ts. A
        rename, reorder, addition or removal of a list, label or field goes red here."""
        (tpl,) = DEFAULTS["board_templates"]
        assert (tpl["name"], tpl["project_type"]) == ("default", None)
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "rm_board_template_default.json").read_text()
        )
        assert tpl["lists"] == fixture["lists"]
        assert tpl["labels"] == fixture["labels"]
        assert tpl["custom_field_groups"] == fixture["custom_field_groups"]
        counts = {g["name"]: len(g["fields"]) for g in tpl["custom_field_groups"]}
        assert counts == TEMPLATE_FIELD_GROUPS
        groups = {g["name"]: g["fields"] for g in tpl["custom_field_groups"]}
        meta = groups["Metadata"]
        i = meta.index("card_type")
        assert meta[i : i + 4] == ["card_type", "project_type", "phase", "capability"], "D4"
        assert groups["Runtime State"][-2:] == ["released", "wait_for_phase"], "ruling 10, R190"
        assert meta[-3:-1] == ["source_artifact_id", "source_item_id"], "R188"
        assert meta[-1] == "spec_hash", "R311"
        assert groups["Relations"] == [
            "parent_card",
            "child_cards",
            "requires",
            "required_by",
            "criterion_ids",
        ], "R-CK10"
        fields = [f for g in tpl["custom_field_groups"] for f in g["fields"]]
        assert "stopwatch_timeout" not in fields, "retired"
        assert "rework_count" not in fields, "R66: rework counting is not a concept"
        assert len(set(fields)) == len(fields)
        assert [x["name"] for x in tpl["labels"]] == ["TEMPLATE"]

    def test_counts(self):
        assert [p["name"] for p in DEFAULTS["project_phases"]] == [
            "setup",
            "preliminary",
            "analysis",
            "design",
            "execution",
            "test",
        ]
        # 15 registry + workflow_engineering (Kata WP4) + kanban_coach (ruling 9)
        assert len(DEFAULTS["capabilities"]) == 17
        # George 2026-11-07: `py2rust` is retired (migration 0083); the rewrite chain is an
        # `it` flow.
        assert {t["name"] for t in DEFAULTS["project_types"]} == {
            "program",
            "meta_prompt",
            "meta_procedure",
            "kaizen",
            "it",
            "kata",
        }
        assert len(DEFAULTS["wip_limits"]) == 6, "one per project type"
        assert {c["name"] for c in DEFAULTS["card_types"]} == set(ALL_CARD_TYPES)
        assert DEFAULTS["recipes"] == []
        assert DEFAULTS["gates"] == []
        assert [(m["name"], m["phase"]) for m in DEFAULTS["milestone_kinds"]] == [
            ("preliminary_done", "preliminary"),
            ("analysis_done", "analysis"),
            ("design_done", "design"),
            ("executed", "execution"),
            ("tested", "test"),
        ]

    def test_capabilities_come_from_the_registry_plus_ruling_9(self):
        # capabilities: trigger_registry.md is the source (George 2026-10-13), plus EXACTLY
        # the three v4 ruling 9 additions (George 10-13). The fixture is parsed from the
        # registry independently of the builder, so a builder that re-maps a card type or
        # adds anything else goes red here.
        reg = json.loads(
            (Path(__file__).parent / "fixtures" / "trigger_registry_capabilities.json").read_text()
        )
        # 84 in the archived blob + 4 py2rust rewrite_* (RS-D4) + 4 kata workflow_engineering (WP4)
        # + wf_change (Kata WP5)
        assert len(reg["card_types"]) == 93
        expected = {n: cap for n, cap in reg["card_types"].items() if n in ALL_CARD_TYPES}
        expected |= {
            "project_phase_start": "project_management",
            "project_phase_closure": "project_management",
        }
        got = {c["name"]: c["capability"] for c in DEFAULTS["card_types"]}
        assert got == expected
        assert set(got) == set(ALL_CARD_TYPES)
        assert not set(got) & set(RETIRED_CARD_TYPES)
        assert DEFAULTS["sources"]["unplaced_card_types"] == 93 + 2 - len(ALL_CARD_TYPES)
        assert [c["name"] for c in DEFAULTS["capabilities"]] == [
            *reg["capabilities"],
            "kanban_coach",
        ]
        names = {c["name"] for c in DEFAULTS["capabilities"]}
        for gone in ("solution_architecture", "requirements_management", "release_management"):
            assert gone not in names
            assert gone not in got.values()
        assert "v3_capabilities" not in DEFAULTS["sources"]

    def test_definition_of_done_from_vendored_predicates(self):
        # landing_predicates.json @ e21ee10f0 (scripts/sources/); the stale local checkout
        # had neither of these two.
        dod = {c["name"]: c["definition_of_done"] for c in DEFAULTS["card_types"]}
        assert dod["development_bug_fix"] == "pr_merged"
        assert dod["bug_analysis"] == "none"
        assert dod["project_phase_start"] is None, "not in the predicates: not specified"
        assert "e21ee10f0" in DEFAULTS["sources"]["definition_of_done"]
        # Every default value is one RM's model, schema and CHECK constraint accept.
        from typing import get_args

        from models.pm_catalogue import DEFINITION_OF_DONE
        from schemas.pm_catalogue import DefinitionOfDone

        used = {v for v in dod.values() if v is not None}
        assert used <= set(DEFINITION_OF_DONE)
        assert set(get_args(DefinitionOfDone)) == set(DEFINITION_OF_DONE)

    def test_references_resolve(self):
        caps = {c["name"] for c in DEFAULTS["capabilities"]}
        cts = {c["name"] for c in DEFAULTS["card_types"]}
        phases = {p["name"] for p in DEFAULTS["project_phases"]}
        assert len(cts) == len(DEFAULTS["card_types"]), "card type names repeat"
        assert {c["capability"] for c in DEFAULTS["card_types"]} <= caps
        for pt in DEFAULTS["project_types"]:
            assert set(pt["phases"]) <= phases, pt["name"]
            assert set(pt["card_types"]) <= set(pt["phases"]), pt["name"]
            for names in pt["card_types"].values():
                assert set(names) <= cts, pt["name"]
