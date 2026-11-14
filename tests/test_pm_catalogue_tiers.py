"""PM catalogue tiers 1 and 2.

Tier 1: phase / card-type attributes, entry flags, overrides, recipes. Tier 2: WIP limits
(and their resolution), priority levels, deliverable / milestone kinds, gates (``has_gate``),
board templates, and their default import.

All tests hit real PostgreSQL through the FastAPI test client — no mocks, except the
race tests, which disable a pre-check to reach the database constraint behind it.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from schemas.pm_catalogue import BoardTemplateIn, CardTypeIn, ProjectPhaseIn
from tests.test_pm_catalogue import (
    TEMPLATE_FIELD_GROUPS,
    _As,
    _capability,
    _card_type,
    _phase,
    _post,
)

DEFAULTS = json.loads(
    (Path(__file__).resolve().parents[1] / "src/data/pm_catalogue_defaults.json").read_text()
)
# R184: the IT type; Kata K15: the kata type. George 2026-11-07: `py2rust` is retired
# (migration 0083) — the rewrite chain is an `it` flow, so these are the only two with phases.
KINDS = ("it", "kata")
# The v4 defaults carry no recipe (the v3 recipes went with the v3 kinds, D22), so the recipe
# import path is exercised with this one, injected into the loaded defaults.
INJECTED_RECIPE = {
    "name": "it_analysis_s",
    "title": "It — analysis (S)",
    "project_type": "it",
    "phase": "analysis",
    "size": "S",
    "items": [
        {
            "card_type": "bug_analysis",
            "count_mode": "fixed",
            "count": 1,
            "per_field": None,
            "cap": None,
            "inputs": [],
            "outputs": [],
        }
    ],
}


@pytest.fixture
def with_recipe(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The defaults as committed plus ``INJECTED_RECIPE``; returns the recipes."""
    import services.pm_catalogue_service as svc

    data = json.loads(json.dumps(DEFAULTS))
    data["recipes"] = [*data["recipes"], INJECTED_RECIPE]
    monkeypatch.setattr(svc, "load_defaults", lambda: json.loads(json.dumps(data)))
    return data["recipes"]


async def _bug_kind(c: "AsyncClient | _As") -> dict:
    """A small bug_hunt_fix: preliminary [project_setup*, bug_analysis], execution [fix]."""
    ph = {n: await _phase(c, n) for n in ("preliminary", "execution")}
    pm = await _capability(c, "project_management")
    dev = await _capability(c, "development")
    ct = {
        "project_setup": await _card_type(c, "project_setup", pm["id"]),
        "bug_analysis": await _card_type(c, "bug_analysis", pm["id"]),
        "development_bug_fix": await _card_type(c, "development_bug_fix", dev["id"]),
        "structure_analysis": await _card_type(c, "structure_analysis", pm["id"]),
    }
    pt = await _post(
        c,
        "/pm/project-types",
        {"name": "bug_hunt_fix", "phase_ids": [ph["preliminary"]["id"], ph["execution"]["id"]]},
    )
    resp = await c.put(
        f"/pm/project-types/{pt['id']}/card-types",
        json={
            "phases": [
                {
                    "phase_id": ph["preliminary"]["id"],
                    "card_type_ids": [ct["project_setup"]["id"], ct["bug_analysis"]["id"]],
                    "entry_card_type_ids": [ct["project_setup"]["id"]],
                },
                {
                    "phase_id": ph["execution"]["id"],
                    "card_type_ids": [ct["development_bug_fix"]["id"]],
                },
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    return {"type": pt, "phases": ph, "card_types": ct}


def _recipe(k: dict, name: str, phase: str, size: str | None, items: list[dict]) -> dict:
    return {
        "name": name,
        "project_type_id": k["type"]["id"],
        "phase_id": k["phases"][phase]["id"],
        "size": size,
        "items": items,
    }


def _item(k: dict, ct: str, **kw) -> dict:
    return {"card_type_id": k["card_types"][ct]["id"], **kw}


# ---------------------------------------------------------------------------
# New attribute columns
# ---------------------------------------------------------------------------


class TestAttributes:
    async def test_phase_output_and_exit_rule(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _post(
            c,
            "/pm/project-phases",
            {"name": "request", "output": "project_brief", "exit_rule": "brief DONE"},
        )
        assert (p["output"], p["exit_rule"]) == ("project_brief", "brief DONE")
        upd = await c.put(
            f"/pm/project-phases/{p['id']}", json={"name": "request", "output": "brief v2"}
        )
        assert upd.status_code == 200
        got = (await c.get(f"/pm/project-phases/{p['id']}")).json()
        assert (got["output"], got["exit_rule"]) == ("brief v2", "brief DONE"), "omitted = kept"
        upd = await c.put(
            f"/pm/project-phases/{p['id']}", json={"name": "request", "exit_rule": None}
        )
        assert upd.status_code == 200
        got = (await c.get(f"/pm/project-phases/{p['id']}")).json()
        assert (got["output"], got["exit_rule"]) == ("brief v2", None), "explicit null = NULL"

    async def test_card_type_attributes_roundtrip(self, authenticated_client: AsyncClient):
        c = authenticated_client
        cap = await _capability(c, "testing")
        body = {
            "name": "bug_analysis",
            "capability_id": cap["id"],
            "inputs": ["workspace", "baseline"],
            "outputs": ["bug_report"],
            "acceptance": ["every bug has a failing test"],
            "rules": ["R34: provable by a failing runtime test"],
            "human_task": True,
            "definition_of_done": "none",
        }
        ct = await _post(c, "/pm/card-types", body)
        for k in ("inputs", "outputs", "acceptance", "rules", "human_task", "definition_of_done"):
            assert ct[k] == body[k], k
        bare = await _card_type(c, "development_bug_fix", cap["id"])
        assert (bare["inputs"], bare["human_task"], bare["definition_of_done"]) == (
            None,
            None,
            None,
        ), "not set renders as null, not [] / false"
        bad = await c.post(
            "/pm/card-types",
            json={"name": "x", "capability_id": cap["id"], "definition_of_done": "shipped"},
        )
        assert bad.status_code == 422, bad.text
        listed = (await c.get("/pm/card-types")).json()
        assert {x["name"]: x["human_task"] for x in listed} == {
            "bug_analysis": True,
            "development_bug_fix": None,
        }


# ---------------------------------------------------------------------------
# Composition: is_entry + effective output / exit rule
# ---------------------------------------------------------------------------


class TestComposition:
    async def test_is_entry_and_effective_values(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _bug_kind(c)
        ph, ct, tid = k["phases"], k["card_types"], k["type"]["id"]
        await c.put(
            f"/pm/project-phases/{ph['preliminary']['id']}",
            json={"name": "preliminary", "output": "default out", "exit_rule": "default exit"},
        )
        resp = await c.put(
            f"/pm/project-types/{tid}/card-types",
            json={
                "phases": [
                    {
                        "phase_id": ph["preliminary"]["id"],
                        "card_type_ids": [ct["project_setup"]["id"], ct["bug_analysis"]["id"]],
                        "entry_card_type_ids": [ct["project_setup"]["id"]],
                        "output": "our out",
                    },
                    {
                        "phase_id": ph["execution"]["id"],
                        "card_type_ids": [ct["development_bug_fix"]["id"]],
                    },
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        prelim = resp.json()["phases"][0]
        assert (prelim["output"], prelim["output_overridden"]) == ("our out", True)
        assert (prelim["exit_rule"], prelim["exit_rule_overridden"]) == ("default exit", False)
        entry = {
            x["name"]: x["is_entry"] for cap in prelim["capabilities"] for x in cap["card_types"]
        }
        assert entry == {"project_setup": True, "bug_analysis": False}

        # A reorder of the project type's phases keeps overrides and entry flags.
        upd = await c.put(
            f"/pm/project-types/{tid}",
            json={
                "name": "bug_hunt_fix",
                "phase_ids": [ph["execution"]["id"], ph["preliminary"]["id"]],
            },
        )
        assert upd.status_code == 200, upd.text
        comp = (await c.get(f"/pm/project-types/{tid}/card-types")).json()
        prelim = next(p for p in comp["phases"] if p["name"] == "preliminary")
        assert (prelim["position"], prelim["output"], prelim["output_overridden"]) == (
            1,
            "our out",
            True,
        )
        assert any(x["is_entry"] for cap in prelim["capabilities"] for x in cap["card_types"])

    async def test_entry_must_be_a_placed_card_type(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _bug_kind(c)
        resp = await c.put(
            f"/pm/project-types/{k['type']['id']}/card-types",
            json={
                "phases": [
                    {
                        "phase_id": k["phases"]["preliminary"]["id"],
                        "card_type_ids": [k["card_types"]["bug_analysis"]["id"]],
                        "entry_card_type_ids": [k["card_types"]["project_setup"]["id"]],
                    }
                ]
            },
        )
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


class TestRecipes:
    async def test_crud_filters_and_item_replacement(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _bug_kind(c)
        s = await _post(
            c,
            "/pm/recipes",
            _recipe(
                k,
                "bug_prelim_s",
                "preliminary",
                "S",
                [
                    _item(k, "project_setup", inputs=["project_brief"], outputs=["workspace"]),
                    _item(k, "bug_analysis"),
                ],
            ),
        )
        assert (s["project_type"], s["phase"], s["size"]) == ("bug_hunt_fix", "preliminary", "S")
        assert [(i["position"], i["card_type"], i["count"]) for i in s["items"]] == [
            (0, "project_setup", 1),
            (1, "bug_analysis", 1),
        ], "fixed items default to count 1"
        assert s["items"][0]["inputs"] == ["project_brief"]

        ex = await _post(
            c,
            "/pm/recipes",
            _recipe(
                k,
                "bug_exec",
                "execution",
                None,
                [
                    _item(
                        k,
                        "development_bug_fix",
                        count_mode="per",
                        per_field="bug_report.confirmed_bugs",
                        cap=5,
                    )
                ],
            ),
        )
        assert (ex["items"][0]["count_mode"], ex["items"][0]["count"], ex["items"][0]["cap"]) == (
            "per",
            None,
            5,
        )

        got = await c.get(f"/pm/recipes/{s['id']}")
        assert got.status_code == 200 and got.json()["name"] == "bug_prelim_s"

        listed = await c.get("/pm/recipes")
        assert [r["name"] for r in listed.json()] == ["bug_exec", "bug_prelim_s"]
        assert listed.headers["X-Total-Count"] == "2"
        by_phase = await c.get("/pm/recipes", params={"phase_id": k["phases"]["execution"]["id"]})
        assert [r["name"] for r in by_phase.json()] == ["bug_exec"]
        by_size = await c.get("/pm/recipes", params={"size": "S"})
        assert [r["name"] for r in by_size.json()] == ["bug_prelim_s"]
        by_type = await c.get("/pm/recipes", params={"project_type_id": k["type"]["id"]})
        assert by_type.headers["X-Total-Count"] == "2"
        assert (await c.get("/pm/recipes", params={"size": "XL"})).status_code == 422

        # PUT replaces the whole item list.
        body = _recipe(k, "bug_prelim_s", "preliminary", "S", [_item(k, "bug_analysis", count=2)])
        upd = await c.put(f"/pm/recipes/{s['id']}", json=body)
        assert upd.status_code == 200, upd.text
        assert [(i["position"], i["card_type"], i["count"]) for i in upd.json()["items"]] == [
            (0, "bug_analysis", 2)
        ]

        assert (await c.delete(f"/pm/recipes/{s['id']}")).status_code == 200
        assert (await c.get(f"/pm/recipes/{s['id']}")).status_code == 404
        assert (await c.put(f"/pm/recipes/{s['id']}", json=body)).status_code == 404

    async def test_project_type_recipes_in_phase_then_size_order(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        k = await _bug_kind(c)
        fix = [_item(k, "development_bug_fix")]
        setup = [_item(k, "project_setup")]
        for name, phase, size, items in (
            ("r_exec", "execution", None, fix),
            ("r_l", "preliminary", "L", setup),
            ("r_s", "preliminary", "S", setup),
            ("r_m", "preliminary", "M", setup),
        ):
            await _post(c, "/pm/recipes", _recipe(k, name, phase, size, items))
        resp = await c.get(f"/pm/project-types/{k['type']['id']}/recipes")
        assert resp.status_code == 200
        assert [r["name"] for r in resp.json()] == ["r_s", "r_m", "r_l", "r_exec"]
        missing = "0195f48b-0000-7000-8000-000000000000"
        assert (await c.get(f"/pm/project-types/{missing}/recipes")).status_code == 404

    @pytest.mark.parametrize(
        "case",
        ["card_type_not_in_phase", "phase_not_selected", "unknown_project_type"],
    )
    async def test_recipe_must_fit_the_matrix(self, authenticated_client: AsyncClient, case: str):
        c = authenticated_client
        k = await _bug_kind(c)
        if case == "card_type_not_in_phase":
            # development_bug_fix is in the matrix, but for execution, not preliminary.
            body = _recipe(k, "r", "preliminary", None, [_item(k, "development_bug_fix")])
        elif case == "phase_not_selected":
            other = await _phase(c, "release")
            body = _recipe(k, "r", "preliminary", None, [])
            body["phase_id"] = other["id"]
        else:
            body = _recipe(k, "r", "preliminary", None, [])
            body["project_type_id"] = "0195f48b-0000-7000-8000-000000000000"
        resp = await c.post("/pm/recipes", json=body)
        assert resp.status_code == 422, resp.text
        assert (await c.get("/pm/recipes")).json() == []

    @pytest.mark.parametrize(
        "item",
        [
            {"count_mode": "per"},  # per needs per_field
            {"count_mode": "per", "per_field": "x", "count": 2},  # per takes no count
            {"count_mode": "fixed", "cap": 5},  # cap only for per
            {"count_mode": "fixed", "count": 0},
            {"count_mode": "sometimes"},
            {"bogus": 1},
        ],
    )
    async def test_item_shape_is_validated(self, authenticated_client: AsyncClient, item: dict):
        c = authenticated_client
        k = await _bug_kind(c)
        body = _recipe(k, "r", "preliminary", None, [_item(k, "bug_analysis", **item)])
        assert (await c.post("/pm/recipes", json=body)).status_code == 422

    async def test_one_recipe_per_type_phase_size(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _bug_kind(c)
        await _post(c, "/pm/recipes", _recipe(k, "a", "preliminary", None, []))
        same_slot = await c.post("/pm/recipes", json=_recipe(k, "b", "preliminary", None, []))
        assert same_slot.status_code == 409, same_slot.text
        sized = await c.post("/pm/recipes", json=_recipe(k, "b", "preliminary", "S", []))
        assert sized.status_code == 200, "a sized recipe is a different slot"
        dup_name = await c.post("/pm/recipes", json=_recipe(k, "a", "preliminary", "M", []))
        assert dup_name.status_code == 409
        # Moving b onto a's slot is a conflict too.
        move = await c.put(
            f"/pm/recipes/{sized.json()['id']}", json=_recipe(k, "b", "preliminary", None, [])
        )
        assert move.status_code == 409


# ---------------------------------------------------------------------------
# Delete guards and stranding
# ---------------------------------------------------------------------------


class TestGuards:
    async def test_deletes_in_use_by_a_recipe_are_409(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _bug_kind(c)
        r = await _post(
            c,
            "/pm/recipes",
            _recipe(k, "bug_exec", "execution", None, [_item(k, "development_bug_fix")]),
        )
        ct_del = await c.delete(f"/pm/card-types/{k['card_types']['development_bug_fix']['id']}")
        assert ct_del.status_code == 409 and "recipe bug_exec" in ct_del.text
        ph_del = await c.delete(f"/pm/project-phases/{k['phases']['execution']['id']}")
        assert ph_del.status_code == 409 and "recipe bug_exec" in ph_del.text

        # Deleting the project type cascades its recipes and their items.
        assert (await c.delete(f"/pm/project-types/{k['type']['id']}")).status_code == 200
        assert (await c.get(f"/pm/recipes/{r['id']}")).status_code == 404
        fix_id = k["card_types"]["development_bug_fix"]["id"]
        assert (await c.delete(f"/pm/card-types/{fix_id}")).status_code == 200

    async def test_matrix_change_may_not_strand_a_recipe(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _bug_kind(c)
        ph, ct, tid = k["phases"], k["card_types"], k["type"]["id"]
        await _post(
            c,
            "/pm/recipes",
            _recipe(k, "bug_exec", "execution", None, [_item(k, "development_bug_fix")]),
        )
        drop_ct = await c.put(
            f"/pm/project-types/{tid}/card-types",
            json={"phases": [{"phase_id": ph["execution"]["id"], "card_type_ids": []}]},
        )
        assert drop_ct.status_code == 409 and "bug_exec" in drop_ct.text
        drop_phase = await c.put(
            f"/pm/project-types/{tid}",
            json={"name": "bug_hunt_fix", "phase_ids": [ph["preliminary"]["id"]]},
        )
        assert drop_phase.status_code == 409 and "bug_exec" in drop_phase.text
        comp = (await c.get(f"/pm/project-types/{tid}/card-types")).json()
        execution = next(p for p in comp["phases"] if p["name"] == "execution")
        assert execution["capabilities"], "nothing changed"
        assert ct["development_bug_fix"]["id"] in json.dumps(comp)


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestRecipeTenantIsolation:
    async def test_other_tenant_sees_and_references_nothing(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        ka = await _bug_kind(a)
        ra = await _post(
            a,
            "/pm/recipes",
            _recipe(ka, "bug_exec", "execution", None, [_item(ka, "development_bug_fix")]),
        )
        assert (await b.get("/pm/recipes")).json() == []
        assert (await b.get(f"/pm/recipes/{ra['id']}")).status_code == 404
        assert (await b.delete(f"/pm/recipes/{ra['id']}")).status_code == 404
        assert (await b.get(f"/pm/project-types/{ka['type']['id']}/recipes")).status_code == 404

        kb = await _bug_kind(b)
        # B's recipe naming A's card type (same name, A's id) is refused ...
        foreign_ct = _recipe(kb, "bug_exec", "execution", None, [_item(ka, "development_bug_fix")])
        resp = await b.post("/pm/recipes", json=foreign_ct)
        assert resp.status_code == 422, resp.text
        # ... and so is a recipe on A's project type or phase.
        foreign_pt = _recipe(kb, "bug_exec", "execution", None, [])
        foreign_pt["project_type_id"] = ka["type"]["id"]
        assert (await b.post("/pm/recipes", json=foreign_pt)).status_code == 422
        foreign_phase = _recipe(kb, "bug_exec", "execution", None, [])
        foreign_phase["phase_id"] = ka["phases"]["execution"]["id"]
        assert (await b.post("/pm/recipes", json=foreign_phase)).status_code == 422
        # B's own works, under the same name A uses.
        own = _recipe(kb, "bug_exec", "execution", None, [_item(kb, "development_bug_fix")])
        assert (await b.post("/pm/recipes", json=own)).status_code == 200
        assert [r["id"] for r in (await a.get("/pm/recipes")).json()] == [ra["id"]]


# ---------------------------------------------------------------------------
# Default import: recipes, per-kind matrices, fill-NULL
# ---------------------------------------------------------------------------


class TestImportTiers:
    async def test_import_creates_recipes_and_is_idempotent(
        self, authenticated_client: AsyncClient, with_recipe: list[dict]
    ):
        c = authenticated_client
        assert DEFAULTS["recipes"] == [], "D22: no default recipes"
        first = await _post(c, "/pm/catalogue/import-defaults", {})
        assert first["recipes"] == {"created": len(with_recipe), "existing": 0, "filled": 0}
        assert first["skipped_recipes"] == []
        second = await _post(c, "/pm/catalogue/import-defaults", {})
        for k in ("project_phases", "capabilities", "card_types", "project_types", "recipes"):
            assert second[k]["created"] == 0, k
            assert second[k]["filled"] == 0, k
        assert second["recipes"]["existing"] == len(with_recipe)
        listed = await c.get("/pm/recipes", params={"limit": 1000})
        assert listed.headers["X-Total-Count"] == str(len(with_recipe))

    async def test_it_and_kata_matrices_match_the_defaults(self, authenticated_client: AsyncClient):
        c = authenticated_client
        await _post(c, "/pm/catalogue/import-defaults", {})
        types = {t["name"]: t for t in (await c.get("/pm/project-types")).json()}
        assert set(KINDS) <= set(types)
        spine = [p["name"] for p in DEFAULTS["project_phases"]]
        assert spine == [
            "setup",
            "preliminary",
            "analysis",
            "design",
            "execution",
            "test",
        ]
        # kata keeps its first-build phases (an empty phase BLOCKs at phase_start's Plan gate).
        kind_phases = {
            "it": spine,
            "kata": ["setup", "preliminary", "execution"],
        }
        for kind in KINDS:
            tid = types[kind]["id"]
            assert [p["name"] for p in types[kind]["phases"]] == kind_phases[kind], kind
            comp = (await c.get(f"/pm/project-types/{tid}/card-types")).json()
            matrix: dict[str, set[str]] = {}
            entries: set[tuple[str, str]] = set()
            for p in comp["phases"]:
                matrix[p["name"]] = set()
                for cap in p["capabilities"]:
                    for x in cap["card_types"]:
                        matrix[p["name"]].add(x["name"])
                        if x["is_entry"]:
                            entries.add((p["name"], x["name"]))
            assert entries == {("setup", "project_start_request_analysis")}, kind
            expected = next(t for t in DEFAULTS["project_types"] if t["name"] == kind)
            assert matrix == {p: set(v) for p, v in expected["card_types"].items()}, kind
            assert (await c.get(f"/pm/project-types/{tid}/recipes")).json() == []

    async def test_card_type_defaults(self, authenticated_client: AsyncClient):
        c = authenticated_client
        await _post(c, "/pm/catalogue/import-defaults", {})
        cts = {x["name"]: x for x in (await c.get("/pm/card-types", params={"limit": 1000})).json()}
        assert len(cts) == 74, (
            "R228: every v4-defined card type (R326: +2 change_*, R333: +rewrite_plan; "
            "Kata WP4: +4 kata-only wf_*; Kata WP5: +wf_change; "
            "+3 held rewrite_* on `it` execution (2026-11-07: no py2rust type); "
            "R347 (4): phase_coverage_check retired)"
        )
        assert "phase_coverage_check" not in cts
        assert [n for n, x in cts.items() if x["human_task"]] == []
        assert cts["bug_analysis"]["definition_of_done"] == "none"
        assert cts["development_bug_fix"]["definition_of_done"] == "pr_merged"
        assert cts["project_phase_start"]["definition_of_done"] is None
        assert cts["bug_analysis"]["outputs"] == [
            "draft_artifact_id",
            "report_artifact_id",
            "published_artifact_id",
        ]
        assert cts["bug_analysis"]["rules"] == ["container_read_exec", "no_reclassify_target"]
        caps = {x["id"]: x["name"] for x in (await c.get("/pm/capabilities")).json()}
        # Registry capabilities (trigger_registry.md is the only source, George 2026-10-13).
        assert caps[cts["bug_analysis"]["capability_id"]] == "analysis"
        assert caps[cts["development_bug_fix"]["capability_id"]] == "development"
        assert caps[cts["project_closure"]["capability_id"]] == "project_management"
        assert len(caps) == 17, "15 registry + workflow_engineering (Kata WP4) + kanban_coach"
        assert caps[cts["project_phase_start"]["capability_id"]] == "project_management"
        assert caps[cts["project_phase_closure"]["capability_id"]] == "project_management"
        assert "kanban_coach" in caps.values()
        phases = {p["name"]: p for p in (await c.get("/pm/project-phases")).json()}
        assert phases["preliminary"]["exit_rule"].startswith("project_phase_closure released")
        assert phases["setup"]["output"] is None

    async def test_fill_unset_never_overwrites_an_edit(self, authenticated_client: AsyncClient):
        c = authenticated_client
        # Rows the tenant had before tier 1 attributes existed: output/inputs unset.
        req = await _post(c, "/pm/project-phases", {"name": "execution"})
        prelim = await _post(
            c, "/pm/project-phases", {"name": "preliminary", "output": "our own output"}
        )
        mine = await _capability(c, "my_cap")
        dd = await _post(
            c,
            "/pm/card-types",
            {
                "name": "project_start_request_analysis",
                "capability_id": mine["id"],
                "human_task": True,
            },
        )
        out = await _post(c, "/pm/catalogue/import-defaults", {})
        # execution: output + exit_rule filled; preliminary: only its unset exit_rule.
        assert out["project_phases"]["filled"] == 2
        assert out["card_types"]["filled"] == 1

        default = {p["name"]: p for p in DEFAULTS["project_phases"]}
        got_req = (await c.get(f"/pm/project-phases/{req['id']}")).json()
        assert got_req["output"] == default["execution"]["output"]
        assert got_req["exit_rule"] == default["execution"]["exit_rule"]
        got_prelim = (await c.get(f"/pm/project-phases/{prelim['id']}")).json()
        assert got_prelim["output"] == "our own output", "an edited value is kept"
        assert got_prelim["exit_rule"] == default["preliminary"]["exit_rule"], "unset is filled"

        got_dd = (await c.get(f"/pm/card-types/{dd['id']}")).json()
        default_ct = {x["name"]: x for x in DEFAULTS["card_types"]}[
            "project_start_request_analysis"
        ]
        assert got_dd["inputs"] == default_ct["inputs"] != [], "NULL is filled"
        assert got_dd["human_task"] is True, "a set value is not overwritten"
        assert got_dd["capability_id"] == mine["id"], "never re-parented"

        again = await _post(c, "/pm/catalogue/import-defaults", {})
        assert again["project_phases"]["filled"] == 0
        assert again["card_types"]["filled"] == 0

    async def test_cleared_stays_cleared_and_null_is_filled(
        self, authenticated_client: AsyncClient
    ):
        """NULL = never set (import fills); ``[]`` / ``""`` = deliberately cleared (kept)."""
        c = authenticated_client
        req = await _post(c, "/pm/project-phases", {"name": "execution", "output": ""})
        mine = await _capability(c, "my_cap")
        dd = await _post(
            c,
            "/pm/card-types",
            {"name": "project_start_request_analysis", "capability_id": mine["id"], "inputs": []},
        )
        tpl = await _post(c, "/pm/board-templates", {"name": "default", "labels": []})
        # The explicit empties are stored as-is, not coerced to NULL.
        assert (req["output"], dd["inputs"], tpl["labels"]) == ("", [], [])

        await _post(c, "/pm/catalogue/import-defaults", {})

        default_phase = {p["name"]: p for p in DEFAULTS["project_phases"]}["execution"]
        default_ct = {x["name"]: x for x in DEFAULTS["card_types"]}[
            "project_start_request_analysis"
        ]
        got_req = (await c.get(f"/pm/project-phases/{req['id']}")).json()
        assert got_req["output"] == "", "a cleared output stays cleared"
        assert got_req["exit_rule"] == default_phase["exit_rule"], "NULL exit_rule is filled"
        got_dd = (await c.get(f"/pm/card-types/{dd['id']}")).json()
        assert got_dd["inputs"] == [], "cleared inputs stay cleared"
        # R213: psra's only product (the setup card) is created by the flow, so its default
        # outputs are []; acceptance carries the non-empty NULL → filled proof instead.
        assert got_dd["outputs"] == default_ct["outputs"] == [], "NULL outputs are filled"
        assert got_dd["acceptance"] == default_ct["acceptance"] != [], "NULL is filled"
        assert got_dd["human_task"] is False, "NULL human_task is filled"
        got_tpl = (await c.get(f"/pm/board-templates/{tpl['id']}")).json()
        assert got_tpl["labels"] == [], "cleared labels stay cleared"
        assert [x["name"] for x in got_tpl["lists"]][:2] == ["TODO", "READY"], "NULL is filled"

        # A PUT that clears a filled value is kept on the next import too.
        put = await c.put(
            f"/pm/card-types/{dd['id']}",
            json={
                "name": "project_start_request_analysis",
                "capability_id": mine["id"],
                "outputs": [],
            },
        )
        assert put.status_code == 200, put.text
        again = await _post(c, "/pm/catalogue/import-defaults", {})
        got_dd = (await c.get(f"/pm/card-types/{dd['id']}")).json()
        assert got_dd["outputs"] == [], "cleared by PUT stays cleared"
        assert got_dd["inputs"] == [], "omitted on PUT = unchanged (still cleared)"
        assert got_dd["human_task"] is False, "omitted on PUT = unchanged (the filled value)"
        assert again["card_types"]["filled"] == 0

    async def test_recipes_skipped_when_the_tenants_type_does_not_fit(
        self, authenticated_client: AsyncClient, with_recipe: list[dict]
    ):
        c = authenticated_client
        await _post(c, "/pm/project-types", {"name": "it"})  # no phases
        out = await _post(c, "/pm/catalogue/import-defaults", {})
        mine = sorted(r["name"] for r in with_recipe if r["project_type"] == "it")
        assert mine == ["it_analysis_s"]
        assert out["skipped_recipes"] == mine
        assert out["recipes"]["created"] == len(with_recipe) - len(mine)
        types = {t["name"]: t for t in (await c.get("/pm/project-types")).json()}
        assert types["it"]["phases"] == [], "an existing type is left as it is"


# ---------------------------------------------------------------------------
# Constraint races on the new tables map to 409, not 500
# ---------------------------------------------------------------------------


class TestTierRaceIs409:
    @pytest.mark.parametrize(
        "race",
        [
            "recipe_name",
            "recipe_slot",
            "card_type_in_recipe",
            "recipe_phase_not_selected",
            "phase_dropped_under_recipe",
        ],
    )
    async def test_constraint_behind_a_skipped_precheck_is_409(
        self, authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, race: str
    ):
        import services.pm_catalogue_service as svc

        c = authenticated_client
        k = await _bug_kind(c)
        fix = [_item(k, "development_bug_fix")]
        await _post(c, "/pm/recipes", _recipe(k, "a", "execution", None, fix))
        if race == "recipe_name":
            monkeypatch.setattr(svc, "_assert_name_free", AsyncMock())
            resp = await c.post("/pm/recipes", json=_recipe(k, "a", "execution", "S", fix))
        elif race == "recipe_slot":
            monkeypatch.setattr(svc, "_slot_taken", AsyncMock(return_value=None))
            resp = await c.post("/pm/recipes", json=_recipe(k, "b", "execution", None, fix))
        elif race == "card_type_in_recipe":
            monkeypatch.setattr(svc, "_users", AsyncMock(return_value=[]))
            fix_id = k["card_types"]["development_bug_fix"]["id"]
            resp = await c.delete(f"/pm/card-types/{fix_id}")
        elif race == "recipe_phase_not_selected":
            # fk_pm_recipes_selected_phase: a recipe's phase must be one its type selected.
            release = await _phase(c, "release")
            monkeypatch.setattr(svc, "_recipe_problems", AsyncMock(return_value=[]))
            body = _recipe(k, "b", "execution", None, fix) | {"phase_id": release["id"]}
            resp = await c.post("/pm/recipes", json=body)
        else:
            # Dropping a phase a recipe uses: the deferred composite FK refuses at commit.
            monkeypatch.setattr(svc, "_stranded_recipes", AsyncMock(return_value=[]))
            resp = await c.put(
                f"/pm/project-types/{k['type']['id']}",
                json={"name": "bug_hunt_fix", "phase_ids": [k["phases"]["preliminary"]["id"]]},
            )
        assert resp.status_code == 409, resp.text
        assert [r["name"] for r in (await c.get("/pm/recipes")).json()] == ["a"]
        got = (await c.get(f"/pm/project-types/{k['type']['id']}")).json()
        assert [p["name"] for p in got["phases"]] == ["preliminary", "execution"]


# ===========================================================================
# Tier 2
# ===========================================================================

MISSING = "0195f48b-0000-7000-8000-000000000000"


async def _dims(c: "AsyncClient | _As") -> dict:
    """A project type over two phases, two capabilities and a card type in each."""
    ph = {n: await _phase(c, n) for n in ("preliminary", "execution")}
    cap = {n: await _capability(c, n) for n in ("analysis", "development")}
    ct = {
        "bug_analysis": await _card_type(c, "bug_analysis", cap["analysis"]["id"]),
        "development_bug_fix": await _card_type(c, "development_bug_fix", cap["development"]["id"]),
    }
    pt = await _post(
        c,
        "/pm/project-types",
        {"name": "bug_hunt_fix", "phase_ids": [ph["preliminary"]["id"], ph["execution"]["id"]]},
    )
    return {"type": pt, "phases": ph, "caps": cap, "card_types": ct}


def _wip(d: dict, limit: int, **dims: str) -> dict:
    """A WIP-limit body; ``dims`` maps dimension → the key in ``d`` (e.g. phase="execution")."""
    body: dict = {"wip_limit": limit}
    if "project_type" in dims:
        body["project_type_id"] = d["type"]["id"]
    if "phase" in dims:
        body["phase_id"] = d["phases"][dims["phase"]]["id"]
    if "capability" in dims:
        body["capability_id"] = d["caps"][dims["capability"]]["id"]
    if "card_type" in dims:
        body["card_type_id"] = d["card_types"][dims["card_type"]]["id"]
    return body


async def _resolve(c: "AsyncClient | _As", d: dict, **dims: str):
    params = {k: v for k, v in _wip(d, 0, **dims).items() if k != "wip_limit"}
    return await c.get("/pm/wip-limits/resolve", params=params)


# ---------------------------------------------------------------------------
# WIP limits
# ---------------------------------------------------------------------------


class TestWipLimits:
    async def test_crud_filters_and_scope_uniqueness(self, authenticated_client: AsyncClient):
        c = authenticated_client
        d = await _dims(c)
        whole = await _post(c, "/pm/wip-limits", _wip(d, 1, project_type=""))
        assert (whole["project_type_id"], whole["phase_id"], whole["wip_limit"]) == (
            d["type"]["id"],
            None,
            1,
        )
        fix = await _post(
            c, "/pm/wip-limits", _wip(d, 3, project_type="", card_type="development_bug_fix")
        )
        assert (await c.get(f"/pm/wip-limits/{fix['id']}")).json()["wip_limit"] == 3

        listed = await c.get("/pm/wip-limits")
        assert [r["id"] for r in listed.json()] == [whole["id"], fix["id"]], "oldest first"
        assert listed.headers["X-Total-Count"] == "2"
        ct_id = d["card_types"]["development_bug_fix"]["id"]
        by_ct = await c.get("/pm/wip-limits", params={"card_type_id": ct_id})
        assert [r["id"] for r in by_ct.json()] == [fix["id"]]
        for dim in ("project_type_id", "phase_id", "capability_id"):
            assert (await c.get("/pm/wip-limits", params={dim: MISSING})).json() == [], dim

        dup = await c.post("/pm/wip-limits", json=_wip(d, 9, project_type=""))
        assert dup.status_code == 409, "one row per scope (NULLS NOT DISTINCT)"
        # Moving one row onto the other's scope is a conflict too.
        move = await c.put(
            f"/pm/wip-limits/{fix['id']}",
            json=_wip(d, 3, project_type="") | {"card_type_id": None},
        )
        assert move.status_code == 409

        upd = await c.put(
            f"/pm/wip-limits/{fix['id']}",
            json=_wip(d, 0, project_type="", card_type="development_bug_fix"),
        )
        assert upd.status_code == 200 and upd.json()["wip_limit"] == 0, upd.text

        assert (await c.delete(f"/pm/wip-limits/{fix['id']}")).status_code == 200
        assert (await c.get(f"/pm/wip-limits/{fix['id']}")).status_code == 404
        assert (await c.delete(f"/pm/wip-limits/{fix['id']}")).status_code == 404

    @pytest.mark.parametrize(
        "body",
        [{"wip_limit": -1}, {}, {"wip_limit": 1, "name": "x"}, {"wip_limit": 1, "phase_id": "x"}],
    )
    async def test_shape_is_validated(self, authenticated_client: AsyncClient, body: dict):
        assert (await authenticated_client.post("/pm/wip-limits", json=body)).status_code == 422

    @pytest.mark.parametrize(
        "dim", ["project_type_id", "phase_id", "capability_id", "card_type_id"]
    )
    async def test_unknown_reference_is_422(self, authenticated_client: AsyncClient, dim: str):
        resp = await authenticated_client.post(
            "/pm/wip-limits", json={dim: MISSING, "wip_limit": 1}
        )
        assert resp.status_code == 422 and dim in resp.text, resp.text


class TestWipResolve:
    async def test_most_specific_matching_row_wins(self, authenticated_client: AsyncClient):
        c = authenticated_client
        d = await _dims(c)
        await _post(c, "/pm/wip-limits", _wip(d, 10))  # every dimension any
        await _post(c, "/pm/wip-limits", _wip(d, 5, project_type=""))
        await _post(c, "/pm/wip-limits", _wip(d, 3, project_type="", phase="execution"))
        await _post(c, "/pm/wip-limits", _wip(d, 2, card_type="development_bug_fix"))

        full = await _resolve(
            c,
            d,
            project_type="",
            phase="execution",
            capability="development",
            card_type="development_bug_fix",
        )
        assert full.status_code == 200, full.text
        assert (full.json()["wip_limit"], full.json()["matched_on"]) == (
            3,
            ["project_type_id", "phase_id"],
        ), "two dimensions beat one"

        other_phase = (await _resolve(c, d, project_type="", phase="preliminary")).json()
        assert (other_phase["wip_limit"], other_phase["matched_on"]) == (5, ["project_type_id"])
        anything = (await _resolve(c, d)).json()
        assert (anything["wip_limit"], anything["matched_on"]) == (10, [])
        # A row's non-NULL dimension the query does not name does not match.
        only_ct = (await _resolve(c, d, card_type="development_bug_fix")).json()
        assert (only_ct["wip_limit"], only_ct["matched_on"]) == (2, ["card_type_id"])

    async def test_tie_break_card_type_capability_phase_project_type(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        d = await _dims(c)
        q = {
            "project_type": "",
            "phase": "execution",
            "capability": "development",
            "card_type": "development_bug_fix",
        }
        await _post(c, "/pm/wip-limits", _wip(d, 1, project_type=""))
        assert (await _resolve(c, d, **q)).json()["wip_limit"] == 1
        await _post(c, "/pm/wip-limits", _wip(d, 2, phase="execution"))
        assert (await _resolve(c, d, **q)).json()["wip_limit"] == 2, "phase > project type"
        await _post(c, "/pm/wip-limits", _wip(d, 3, capability="development"))
        assert (await _resolve(c, d, **q)).json()["wip_limit"] == 3, "capability > phase"
        await _post(c, "/pm/wip-limits", _wip(d, 4, card_type="development_bug_fix"))
        got = (await _resolve(c, d, **q)).json()
        assert (got["wip_limit"], got["matched_on"]) == (4, ["card_type_id"]), "card type wins"
        # Two-dimension tie: (project type, card type) beats (phase, capability).
        await _post(c, "/pm/wip-limits", _wip(d, 6, phase="execution", capability="development"))
        await _post(
            c, "/pm/wip-limits", _wip(d, 5, project_type="", card_type="development_bug_fix")
        )
        got = (await _resolve(c, d, **q)).json()
        assert (got["wip_limit"], got["matched_on"]) == (5, ["project_type_id", "card_type_id"])

    async def test_no_match_is_404(self, authenticated_client: AsyncClient):
        c = authenticated_client
        d = await _dims(c)
        assert (await _resolve(c, d, project_type="")).status_code == 404, "no rows at all"
        await _post(c, "/pm/wip-limits", _wip(d, 1, project_type=""))
        assert (await _resolve(c, d, phase="execution")).status_code == 404
        assert (await c.get("/pm/wip-limits/resolve", params={"phase_id": "x"})).status_code == 422


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------


class TestPriorityLevels:
    async def test_crud_and_rank_order(self, authenticated_client: AsyncClient):
        c = authenticated_client
        low = await _post(c, "/pm/priority-levels", {"name": "a_low", "rank": 3})
        top = await _post(
            c,
            "/pm/priority-levels",
            {"name": "z_top", "title": "P0", "rank": 0, "conveyor_may_set": False},
        )
        mid = await _post(
            c, "/pm/priority-levels", {"name": "m_mid", "rank": 2, "is_default": True}
        )
        assert (top["conveyor_may_set"], low["conveyor_may_set"], low["is_default"]) == (
            False,
            True,
            False,
        )
        listed = await c.get("/pm/priority-levels")
        assert [x["name"] for x in listed.json()] == ["z_top", "m_mid", "a_low"], "rank, not name"
        assert listed.headers["X-Total-Count"] == "3"
        assert (await c.get(f"/pm/priority-levels/{mid['id']}")).json()["is_default"] is True

        dup_rank = await c.post("/pm/priority-levels", json={"name": "b", "rank": 2})
        assert dup_rank.status_code == 409 and "m_mid" in dup_rank.text
        assert (
            await c.post("/pm/priority-levels", json={"name": "b", "rank": -1})
        ).status_code == 422

        upd = await c.put(f"/pm/priority-levels/{low['id']}", json={"name": "a_low", "rank": 1})
        assert upd.status_code == 200 and upd.json()["rank"] == 1, upd.text
        order = [x["name"] for x in (await c.get("/pm/priority-levels")).json()]
        assert order == ["z_top", "a_low", "m_mid"]

        assert (await c.delete(f"/pm/priority-levels/{low['id']}")).status_code == 200
        assert (await c.get(f"/pm/priority-levels/{low['id']}")).status_code == 404

    async def test_at_most_one_default(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p2 = await _post(c, "/pm/priority-levels", {"name": "p2", "rank": 2, "is_default": True})
        p1 = await _post(c, "/pm/priority-levels", {"name": "p1", "rank": 1})
        second = await c.post(
            "/pm/priority-levels", json={"name": "p3", "rank": 3, "is_default": True}
        )
        assert second.status_code == 409 and "p2" in second.text, second.text
        to_default = await c.put(
            f"/pm/priority-levels/{p1['id']}", json={"name": "p1", "rank": 1, "is_default": True}
        )
        assert to_default.status_code == 409
        # Re-saving the default itself is fine; unsetting it frees the slot.
        same = await c.put(
            f"/pm/priority-levels/{p2['id']}", json={"name": "p2", "rank": 2, "is_default": True}
        )
        assert same.status_code == 200
        await c.put(
            f"/pm/priority-levels/{p2['id']}", json={"name": "p2", "rank": 2, "is_default": False}
        )
        moved = await c.put(
            f"/pm/priority-levels/{p1['id']}", json={"name": "p1", "rank": 1, "is_default": True}
        )
        assert moved.status_code == 200, moved.text
        defaults = [
            x["name"] for x in (await c.get("/pm/priority-levels")).json() if x["is_default"]
        ]
        assert defaults == ["p1"]


# ---------------------------------------------------------------------------
# Deliverable kinds, milestone kinds, gates, board templates
# ---------------------------------------------------------------------------


class TestKindsAndGates:
    async def test_deliverable_kind_crud(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _post(c, "/pm/deliverable-kinds", {"name": "deliverable"})
        assert k["counts_for_phase_exit"] is True, "counts by default"
        upd = await c.put(
            f"/pm/deliverable-kinds/{k['id']}",
            json={"name": "deliverable", "counts_for_phase_exit": False},
        )
        assert upd.status_code == 200 and upd.json()["counts_for_phase_exit"] is False
        assert (await c.get(f"/pm/deliverable-kinds/{k['id']}")).json()["name"] == "deliverable"
        listed = await c.get("/pm/deliverable-kinds")
        assert [x["name"] for x in listed.json()] == ["deliverable"]
        assert (
            await c.post("/pm/deliverable-kinds", json={"name": "deliverable"})
        ).status_code == 409
        assert (await c.delete(f"/pm/deliverable-kinds/{k['id']}")).status_code == 200
        assert (await c.get(f"/pm/deliverable-kinds/{k['id']}")).status_code == 404

    async def test_milestone_kind_crud(self, authenticated_client: AsyncClient):
        c = authenticated_client
        ph = await _phase(c, "execution")
        m = await _post(c, "/pm/milestone-kinds", {"name": "executed", "phase_id": ph["id"]})
        free = await _post(c, "/pm/milestone-kinds", {"name": "kickoff"})
        assert (m["phase_id"], free["phase_id"]) == (ph["id"], None)
        by_phase = await c.get("/pm/milestone-kinds", params={"phase_id": ph["id"]})
        assert [x["name"] for x in by_phase.json()] == ["executed"]
        assert (await c.get("/pm/milestone-kinds")).headers["X-Total-Count"] == "2"
        unknown = await c.post("/pm/milestone-kinds", json={"name": "x", "phase_id": MISSING})
        assert unknown.status_code == 422
        kept = await c.put(f"/pm/milestone-kinds/{m['id']}", json={"name": "executed"})
        assert kept.status_code == 200 and kept.json()["phase_id"] == ph["id"], "omitted = kept"
        upd = await c.put(
            f"/pm/milestone-kinds/{m['id']}", json={"name": "executed", "phase_id": None}
        )
        assert upd.status_code == 200 and upd.json()["phase_id"] is None
        assert (await c.get(f"/pm/milestone-kinds/{m['id']}")).json()["phase_id"] is None
        assert (await c.delete(f"/pm/milestone-kinds/{m['id']}")).status_code == 200
        assert (await c.get(f"/pm/milestone-kinds/{m['id']}")).status_code == 404

    async def test_gate_crud_and_has_gate(self, authenticated_client: AsyncClient):
        c = authenticated_client
        contract = await _phase(c, "contract")
        execution = await _phase(c, "execution")
        pt = await _post(
            c, "/pm/project-types", {"name": "kind", "phase_ids": [contract["id"], execution["id"]]}
        )
        assert contract["has_gate"] is False
        g = await _post(c, "/pm/gates", {"name": "contract_signoff", "phase_id": contract["id"]})
        assert (g["approver_role"], g["quorum"], g["expires_after_hours"]) == (
            "project_owner",
            1,
            None,
        )

        phases = {p["name"]: p["has_gate"] for p in (await c.get("/pm/project-phases")).json()}
        assert phases == {"contract": True, "execution": False}
        assert (await c.get(f"/pm/project-phases/{contract['id']}")).json()["has_gate"] is True
        comp = (await c.get(f"/pm/project-types/{pt['id']}/card-types")).json()
        assert {p["name"]: p["has_gate"] for p in comp["phases"]} == phases
        typ = (await c.get(f"/pm/project-types/{pt['id']}")).json()
        assert {p["name"]: p["has_gate"] for p in typ["phases"]} == phases

        upd = await c.put(
            f"/pm/gates/{g['id']}",
            json={
                "name": "contract_signoff",
                "phase_id": contract["id"],
                "approver_role": "tenant_admin",
                "quorum": 2,
                "expires_after_hours": 48,
            },
        )
        assert upd.status_code == 200, upd.text
        assert (upd.json()["approver_role"], upd.json()["quorum"]) == ("tenant_admin", 2)
        assert (await c.get(f"/pm/gates/{g['id']}")).json()["expires_after_hours"] == 48
        by_phase = await c.get("/pm/gates", params={"phase_id": execution["id"]})
        assert by_phase.json() == []
        assert (await c.get("/pm/gates")).headers["X-Total-Count"] == "1"

        assert (await c.delete(f"/pm/gates/{g['id']}")).status_code == 200
        assert (await c.get(f"/pm/gates/{g['id']}")).status_code == 404
        assert (await c.get(f"/pm/project-phases/{contract['id']}")).json()["has_gate"] is False

    @pytest.mark.parametrize(
        "change",
        [
            {"approver_role": "anyone"},
            {"quorum": 0},
            {"expires_after_hours": 0},
            {"phase_id": None},
            {"phase_id": MISSING},
        ],
    )
    async def test_gate_shape_is_validated(self, authenticated_client: AsyncClient, change: dict):
        c = authenticated_client
        ph = await _phase(c, "contract")
        body = {"name": "g", "phase_id": ph["id"], **change}
        assert (await c.post("/pm/gates", json=body)).status_code == 422

    async def test_board_template_crud(self, authenticated_client: AsyncClient):
        c = authenticated_client
        pt = await _post(c, "/pm/project-types", {"name": "kind"})
        body = {
            "name": "default",
            "project_type_id": pt["id"],
            "lists": [
                {"name": "TODO", "position": 0},
                {"name": "DONE", "position": 1, "closed": True},
            ],
            "labels": [{"name": "TEMPLATE"}, {"name": "URGENT", "color": "berry-red"}],
            "custom_field_groups": [{"name": "Metadata", "fields": ["priority", "card_type"]}],
        }
        t = await _post(c, "/pm/board-templates", body)
        assert t["lists"] == [
            {"name": "TODO", "position": 0, "closed": False},
            {"name": "DONE", "position": 1, "closed": True},
        ]
        assert t["labels"][0] == {"name": "TEMPLATE", "color": None}
        assert t["custom_field_groups"] == body["custom_field_groups"]
        by_type = await c.get("/pm/board-templates", params={"project_type_id": pt["id"]})
        assert [x["name"] for x in by_type.json()] == ["default"]

        upd = await c.put(f"/pm/board-templates/{t['id']}", json={"name": "default"})
        assert upd.status_code == 200, upd.text
        got = (await c.get(f"/pm/board-templates/{t['id']}")).json()
        assert (got["project_type_id"], got["lists"], got["labels"]) == (
            pt["id"],
            t["lists"],
            t["labels"],
        ), "omitted = unchanged"
        upd = await c.put(
            f"/pm/board-templates/{t['id']}",
            json={"name": "default", "project_type_id": None, "lists": [], "labels": None},
        )
        assert upd.status_code == 200, upd.text
        got = (await c.get(f"/pm/board-templates/{t['id']}")).json()
        assert (got["project_type_id"], got["lists"], got["labels"]) == (None, [], None)
        assert (await c.delete(f"/pm/board-templates/{t['id']}")).status_code == 200
        assert (await c.get(f"/pm/board-templates/{t['id']}")).status_code == 404

    @pytest.mark.parametrize(
        "change",
        [
            {"lists": [{"name": "TODO", "position": 0}, {"name": "TODO", "position": 1}]},
            {"lists": [{"name": "A", "position": 0}, {"name": "B", "position": 0}]},
            {"lists": [{"name": "A", "position": -1}]},
            {"lists": [{"name": "A", "position": 0, "wip": 3}]},
            {"labels": [{"name": "X"}, {"name": "X"}]},
            {"custom_field_groups": [{"name": "G", "fields": ["a", "a"]}]},
            {"custom_field_groups": [{"name": "G", "fields": "a"}]},
            {"project_type_id": MISSING},
        ],
    )
    async def test_board_template_shape_is_validated(
        self, authenticated_client: AsyncClient, change: dict
    ):
        resp = await authenticated_client.post("/pm/board-templates", json={"name": "t", **change})
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Delete guards
# ---------------------------------------------------------------------------


class TestTier2Guards:
    async def test_phase_in_use_by_gate_milestone_or_wip_is_409(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        ph = await _phase(c, "contract")
        g = await _post(c, "/pm/gates", {"name": "contract_signoff", "phase_id": ph["id"]})
        m = await _post(c, "/pm/milestone-kinds", {"name": "contract_signed", "phase_id": ph["id"]})
        w = await _post(c, "/pm/wip-limits", {"phase_id": ph["id"], "wip_limit": 2})
        resp = await c.delete(f"/pm/project-phases/{ph['id']}")
        assert resp.status_code == 409, resp.text
        for user in (
            "gate contract_signoff",
            "milestone kind contract_signed",
            f"wip limit {w['id']}",
        ):
            assert user in resp.text, user
        for path in (
            f"/pm/gates/{g['id']}",
            f"/pm/milestone-kinds/{m['id']}",
            f"/pm/wip-limits/{w['id']}",
        ):
            assert (await c.delete(path)).status_code == 200
        assert (await c.delete(f"/pm/project-phases/{ph['id']}")).status_code == 200

    async def test_card_type_and_capability_in_use_by_wip_are_409(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        d = await _dims(c)
        ct = d["card_types"]["bug_analysis"]
        w_ct = await _post(c, "/pm/wip-limits", _wip(d, 1, card_type="bug_analysis"))
        resp = await c.delete(f"/pm/card-types/{ct['id']}")
        assert resp.status_code == 409 and f"wip limit {w_ct['id']}" in resp.text, resp.text

        dev = d["caps"]["development"]
        await c.delete(f"/pm/card-types/{d['card_types']['development_bug_fix']['id']}")
        w_cap = await _post(c, "/pm/wip-limits", _wip(d, 1, capability="development"))
        resp = await c.delete(f"/pm/capabilities/{dev['id']}")
        assert resp.status_code == 409 and f"wip limit {w_cap['id']}" in resp.text, resp.text
        await c.delete(f"/pm/wip-limits/{w_cap['id']}")
        assert (await c.delete(f"/pm/capabilities/{dev['id']}")).status_code == 200

    async def test_project_type_delete_cascades_wip_and_releases_templates(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        d = await _dims(c)
        w = await _post(c, "/pm/wip-limits", _wip(d, 1, project_type="", phase="execution"))
        t = await _post(
            c, "/pm/board-templates", {"name": "default", "project_type_id": d["type"]["id"]}
        )
        assert (await c.delete(f"/pm/project-types/{d['type']['id']}")).status_code == 200
        assert (await c.get(f"/pm/wip-limits/{w['id']}")).status_code == 404, "cascaded"
        got = await c.get(f"/pm/board-templates/{t['id']}")
        assert got.status_code == 200 and got.json()["project_type_id"] is None, "set NULL"
        # The phase is free again once the type's WIP limit went with it.
        assert (
            await c.delete(f"/pm/project-phases/{d['phases']['execution']['id']}")
        ).status_code == 200


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

_TIER2_LISTS = (
    "/pm/wip-limits",
    "/pm/priority-levels",
    "/pm/deliverable-kinds",
    "/pm/milestone-kinds",
    "/pm/gates",
    "/pm/board-templates",
)


class TestTier2TenantIsolation:
    async def test_other_tenant_sees_and_references_nothing(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        da = await _dims(a)
        ph = da["phases"]["execution"]["id"]
        rows = {
            "/pm/wip-limits": await _post(a, "/pm/wip-limits", _wip(da, 1, project_type="")),
            "/pm/priority-levels": await _post(a, "/pm/priority-levels", {"name": "p0", "rank": 0}),
            "/pm/deliverable-kinds": await _post(a, "/pm/deliverable-kinds", {"name": "d"}),
            "/pm/milestone-kinds": await _post(
                a, "/pm/milestone-kinds", {"name": "m", "phase_id": ph}
            ),
            "/pm/gates": await _post(a, "/pm/gates", {"name": "g", "phase_id": ph}),
            "/pm/board-templates": await _post(a, "/pm/board-templates", {"name": "t"}),
        }
        for path in _TIER2_LISTS:
            assert (await b.get(path)).json() == [], path
            assert (await b.get(f"{path}/{rows[path]['id']}")).status_code == 404, path
            assert (await b.delete(f"{path}/{rows[path]['id']}")).status_code == 404, path
        assert (await b.get("/pm/wip-limits/resolve")).status_code == 404

        # B naming A's rows in a WIP limit, gate, milestone kind or template → 422.
        for dim, value in (
            ("project_type_id", da["type"]["id"]),
            ("phase_id", ph),
            ("capability_id", da["caps"]["development"]["id"]),
            ("card_type_id", da["card_types"]["development_bug_fix"]["id"]),
        ):
            resp = await b.post("/pm/wip-limits", json={dim: value, "wip_limit": 1})
            assert resp.status_code == 422, (dim, resp.text)
        assert (await b.post("/pm/gates", json={"name": "g", "phase_id": ph})).status_code == 422
        foreign_m = await b.post("/pm/milestone-kinds", json={"name": "m", "phase_id": ph})
        assert foreign_m.status_code == 422
        foreign_t = await b.post(
            "/pm/board-templates", json={"name": "t", "project_type_id": da["type"]["id"]}
        )
        assert foreign_t.status_code == 422
        # B's own rows, under A's names and rank, are fine.
        assert (
            await b.post("/pm/priority-levels", json={"name": "p0", "rank": 0})
        ).status_code == 200
        assert (await b.post("/pm/wip-limits", json={"wip_limit": 1})).status_code == 200
        assert [r["id"] for r in (await a.get("/pm/wip-limits")).json()] == [
            rows["/pm/wip-limits"]["id"]
        ]


# ---------------------------------------------------------------------------
# Default import: tier 2
# ---------------------------------------------------------------------------

_TIER2_KEYS = (
    "wip_limits",
    "priority_levels",
    "deliverable_kinds",
    "milestone_kinds",
    "gates",
    "board_templates",
)


class TestImportTier2:
    async def test_import_creates_tier2_and_is_idempotent(self, authenticated_client: AsyncClient):
        c = authenticated_client
        first = await _post(c, "/pm/catalogue/import-defaults", {})
        for k in _TIER2_KEYS:
            assert first[k] == {"created": len(DEFAULTS[k]), "existing": 0, "filled": 0}, k
        assert first["skipped_priority_levels"] == []
        # 6 since 0083 retired py2rust (2026-11-07): one WIP limit per default project type.
        assert len(DEFAULTS["wip_limits"]) == len(DEFAULTS["project_types"]) == 6

        second = await _post(c, "/pm/catalogue/import-defaults", {})
        for k in (*_TIER2_KEYS, "project_phases", "capabilities", "card_types", "recipes"):
            assert (second[k]["created"], second[k]["filled"]) == (0, 0), k
        for k in _TIER2_KEYS:
            assert second[k]["existing"] == len(DEFAULTS[k]), k
            listed = await c.get(f"/pm/{k.replace('_', '-')}", params={"limit": 1000})
            assert listed.headers["X-Total-Count"] == str(len(DEFAULTS[k])), k

    async def test_default_values(self, authenticated_client: AsyncClient):
        c = authenticated_client
        await _post(c, "/pm/catalogue/import-defaults", {})
        levels = (await c.get("/pm/priority-levels")).json()
        assert [(x["title"], x["rank"]) for x in levels] == [
            ("P0", 0),
            ("P1", 1),
            ("P2", 2),
            ("P3", 3),
        ]
        assert [x["title"] for x in levels if x["is_default"]] == ["P2"]
        assert [x["title"] for x in levels if not x["conveyor_may_set"]] == ["P0"]

        kinds = (await c.get("/pm/deliverable-kinds")).json()
        assert {x["name"]: x["counts_for_phase_exit"] for x in kinds} == {
            "deliverable": True,
            "intermediate": True,
        }

        phases = {p["id"]: p for p in (await c.get("/pm/project-phases")).json()}
        milestones = {
            m["name"]: phases[m["phase_id"]]["name"]
            for m in (await c.get("/pm/milestone-kinds")).json()
        }
        assert milestones == {
            "preliminary_done": "preliminary",
            "analysis_done": "analysis",
            "design_done": "design",
            "executed": "execution",
            "tested": "test",
        }
        # v3's contract_signoff went with the contract phase (R185); v4 sign-offs are fr
        # approvals asked by the cards.
        assert (await c.get("/pm/gates")).json() == []
        assert [p["name"] for p in phases.values() if p["has_gate"]] == []

        (tpl,) = (await c.get("/pm/board-templates")).json()
        assert (tpl["name"], tpl["project_type_id"]) == ("default", None)
        assert [(x["name"], x["closed"]) for x in tpl["lists"]] == [
            ("TODO", False),
            ("READY", False),
            ("DOING", False),
            ("QA", False),
            ("BLOCKED", False),
            ("DONE", True),
            ("DROPPED", True),
        ]
        assert [x["name"] for x in tpl["labels"]] == ["TEMPLATE"]
        groups = {g["name"]: g["fields"] for g in tpl["custom_field_groups"]}
        assert list(groups) == list(TEMPLATE_FIELD_GROUPS)
        assert "priority" in groups["Metadata"] and "stopwatch_timeout" not in groups["Metadata"]

        types = {t["name"]: t["id"] for t in (await c.get("/pm/project-types")).json()}
        for name, tid in types.items():
            got = await c.get("/pm/wip-limits/resolve", params={"project_type_id": tid})
            assert (got.json()["wip_limit"], got.json()["matched_on"]) == (
                1,
                ["project_type_id"],
            ), name

    async def test_fill_unset_never_overwrites_an_edit(self, authenticated_client: AsyncClient):
        c = authenticated_client
        prelim = await _phase(c, "preliminary")
        executed = await _post(
            c, "/pm/milestone-kinds", {"name": "executed", "phase_id": prelim["id"]}
        )
        cleared = await c.put(
            f"/pm/milestone-kinds/{executed['id']}", json={"name": "executed", "phase_id": None}
        )
        assert cleared.status_code == 200 and cleared.json()["phase_id"] is None, cleared.text
        execution = await _phase(c, "execution")
        released = await _post(
            c, "/pm/milestone-kinds", {"name": "preliminary_done", "phase_id": execution["id"]}
        )
        own_lists = [{"name": "BACKLOG", "position": 0}]
        tpl = await _post(c, "/pm/board-templates", {"name": "default", "lists": own_lists})

        out = await _post(c, "/pm/catalogue/import-defaults", {})
        assert out["milestone_kinds"]["filled"] == 0, "import never re-parents a milestone"
        assert out["board_templates"]["filled"] == 1

        got_exec = (await c.get(f"/pm/milestone-kinds/{executed['id']}")).json()
        assert got_exec["phase_id"] is None, "a phase_id the tenant set to null stays null"
        got_rel = (await c.get(f"/pm/milestone-kinds/{released['id']}")).json()
        assert got_rel["phase_id"] == execution["id"], "an edited value is kept"

        got_tpl = (await c.get(f"/pm/board-templates/{tpl['id']}")).json()
        assert [x["name"] for x in got_tpl["lists"]] == ["BACKLOG"], "edited lists are kept"
        assert [x["name"] for x in got_tpl["labels"]] == ["TEMPLATE"], "NULL labels are filled"
        assert len(got_tpl["custom_field_groups"]) == len(TEMPLATE_FIELD_GROUPS)

        again = await _post(c, "/pm/catalogue/import-defaults", {})
        assert (again["milestone_kinds"]["filled"], again["board_templates"]["filled"]) == (0, 0)

    async def test_priority_conflicts_keep_the_tenants_levels(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        await _post(c, "/pm/priority-levels", {"name": "urgent", "rank": 0, "is_default": True})
        out = await _post(c, "/pm/catalogue/import-defaults", {})
        assert out["skipped_priority_levels"] == ["p0"]
        assert out["priority_levels"] == {"created": 3, "existing": 0, "filled": 0}
        levels = (await c.get("/pm/priority-levels")).json()
        assert [x["name"] for x in levels] == ["urgent", "p1", "p2", "p3"]
        assert [x["name"] for x in levels if x["is_default"]] == ["urgent"], "tenant's default kept"


# ---------------------------------------------------------------------------
# Constraint races on the tier 2 tables map to 409, not 500
# ---------------------------------------------------------------------------


class TestTier2RaceIs409:
    @pytest.mark.parametrize(
        "race",
        [
            "wip_scope",
            "priority_rank",
            "priority_default",
            "gate_name",
            "phase_in_gate",
            "card_type_in_wip",
        ],
    )
    async def test_constraint_behind_a_skipped_precheck_is_409(
        self, authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, race: str
    ):
        import services.pm_catalogue_service as svc

        c = authenticated_client
        d = await _dims(c)
        ph = await _phase(c, "contract")
        await _post(c, "/pm/wip-limits", _wip(d, 1, card_type="bug_analysis"))
        await _post(c, "/pm/priority-levels", {"name": "p2", "rank": 2, "is_default": True})
        await _post(c, "/pm/gates", {"name": "g", "phase_id": ph["id"]})
        if race == "wip_scope":
            monkeypatch.setattr(svc, "_assert_wip_scope_free", AsyncMock())
            resp = await c.post("/pm/wip-limits", json=_wip(d, 5, card_type="bug_analysis"))
        elif race == "priority_rank":
            monkeypatch.setattr(svc, "_assert_rank_free", AsyncMock())
            resp = await c.post("/pm/priority-levels", json={"name": "other", "rank": 2})
        elif race == "priority_default":
            monkeypatch.setattr(svc, "_assert_single_default", AsyncMock())
            resp = await c.post(
                "/pm/priority-levels", json={"name": "p1", "rank": 1, "is_default": True}
            )
        elif race == "gate_name":
            monkeypatch.setattr(svc, "_assert_name_free", AsyncMock())
            resp = await c.post("/pm/gates", json={"name": "g", "phase_id": ph["id"]})
        elif race == "phase_in_gate":
            monkeypatch.setattr(svc, "_users", AsyncMock(return_value=[]))
            resp = await c.delete(f"/pm/project-phases/{ph['id']}")
        else:
            monkeypatch.setattr(svc, "_users", AsyncMock(return_value=[]))
            resp = await c.delete(f"/pm/card-types/{d['card_types']['bug_analysis']['id']}")
        assert resp.status_code == 409, resp.text
        assert len((await c.get("/pm/wip-limits")).json()) == 1
        assert [x["name"] for x in (await c.get("/pm/priority-levels")).json()] == ["p2"]
        assert [x["name"] for x in (await c.get("/pm/gates")).json()] == ["g"]


# ---------------------------------------------------------------------------
# GET → PUT round trip keeps "not set" (NULL renders as null, never [] / false)
# ---------------------------------------------------------------------------


def _as_put(schema: type, got: dict, **changes) -> dict:
    """What a client PUTs back after a GET: the writable fields of the row, plus its edits."""
    return {k: v for k, v in got.items() if k in schema.model_fields} | changes


class TestGetPutRoundTrip:
    async def test_card_type(self, authenticated_client: AsyncClient):
        c = authenticated_client
        cap = await _capability(c, "my_cap")
        dd = await _card_type(c, "project_start_request_analysis", cap["id"])
        got = (await c.get(f"/pm/card-types/{dd['id']}")).json()
        for k in ("inputs", "outputs", "acceptance", "rules", "human_task"):
            assert got[k] is None, k
        put = await c.put(f"/pm/card-types/{dd['id']}", json=_as_put(CardTypeIn, got, title="DD"))
        assert put.status_code == 200, put.text

        out = await _post(c, "/pm/catalogue/import-defaults", {})
        assert out["card_types"]["filled"] == 1
        after = (await c.get(f"/pm/card-types/{dd['id']}")).json()
        default = {x["name"]: x for x in DEFAULTS["card_types"]}["project_start_request_analysis"]
        assert after["inputs"] == default["inputs"] != []
        assert after["human_task"] is False
        assert after["title"] == "DD"

    async def test_board_template(self, authenticated_client: AsyncClient):
        c = authenticated_client
        tpl = await _post(c, "/pm/board-templates", {"name": "default"})
        got = (await c.get(f"/pm/board-templates/{tpl['id']}")).json()
        assert (got["lists"], got["labels"], got["custom_field_groups"]) == (None, None, None)
        put = await c.put(
            f"/pm/board-templates/{tpl['id']}", json=_as_put(BoardTemplateIn, got, title="B")
        )
        assert put.status_code == 200, put.text

        out = await _post(c, "/pm/catalogue/import-defaults", {})
        assert out["board_templates"]["filled"] == 1
        after = (await c.get(f"/pm/board-templates/{tpl['id']}")).json()
        assert [x["name"] for x in after["lists"]][:2] == ["TODO", "READY"]
        assert after["labels"], "NULL labels are filled"

    async def test_phase(self, authenticated_client: AsyncClient):
        c = authenticated_client
        req = await _post(c, "/pm/project-phases", {"name": "execution"})
        got = (await c.get(f"/pm/project-phases/{req['id']}")).json()
        assert (got["output"], got["exit_rule"]) == (None, None)
        put = await c.put(
            f"/pm/project-phases/{req['id']}", json=_as_put(ProjectPhaseIn, got, title="Req")
        )
        assert put.status_code == 200, put.text

        out = await _post(c, "/pm/catalogue/import-defaults", {})
        assert out["project_phases"]["filled"] == 1
        after = (await c.get(f"/pm/project-phases/{req['id']}")).json()
        default = {p["name"]: p for p in DEFAULTS["project_phases"]}["execution"]
        assert default["output"] and default["exit_rule"]
        assert (after["output"], after["exit_rule"]) == (default["output"], default["exit_rule"])


# ---------------------------------------------------------------------------
# PUT: omitted = unchanged; explicit null = not set; explicit empty = cleared
# ---------------------------------------------------------------------------


class TestOmittedIsUnchanged:
    async def test_card_type(self, authenticated_client: AsyncClient):
        c = authenticated_client
        cap = await _capability(c, "testing")
        attrs = {
            "inputs": ["workspace"],
            "outputs": ["bug_report"],
            "acceptance": ["a"],
            "rules": ["R34"],
            "human_task": True,
            "definition_of_done": "none",
        }
        ct = await _post(
            c,
            "/pm/card-types",
            {"name": "bug_analysis", "title": "Nice", "capability_id": cap["id"], **attrs},
        )
        url = f"/pm/card-types/{ct['id']}"
        base = {"name": "bug_analysis", "capability_id": cap["id"]}

        assert (await c.put(url, json=base)).status_code == 200
        got = (await c.get(url)).json()
        assert {k: got[k] for k in attrs} == attrs, "an old client's PUT keeps the new fields"
        assert got["title"] == "Nice", "an omitted title is kept"

        assert (await c.put(url, json=base | {"inputs": None, "human_task": None})).status_code
        got = (await c.get(url)).json()
        assert (got["inputs"], got["human_task"]) == (None, None), "explicit null = not set"
        assert got["outputs"] == ["bug_report"]

        assert (await c.put(url, json=base | {"outputs": [], "rules": []})).status_code == 200
        got = (await c.get(url)).json()
        assert (got["outputs"], got["rules"]) == ([], []), "explicit [] = cleared"

    async def test_phase(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _post(
            c, "/pm/project-phases", {"name": "request", "output": "o", "exit_rule": "e"}
        )
        url = f"/pm/project-phases/{p['id']}"
        assert (await c.put(url, json={"name": "request", "title": "R"})).status_code == 200
        got = (await c.get(url)).json()
        assert (got["output"], got["exit_rule"], got["title"]) == ("o", "e", "R")
        assert (await c.put(url, json={"name": "request", "output": ""})).status_code == 200
        got = (await c.get(url)).json()
        assert (got["output"], got["exit_rule"]) == ("", "e"), "explicit '' = cleared"

    async def test_board_template_and_gate(self, authenticated_client: AsyncClient):
        c = authenticated_client
        labels = [{"name": "bug", "color": "red"}]
        tpl = await _post(c, "/pm/board-templates", {"name": "t", "labels": labels})
        url = f"/pm/board-templates/{tpl['id']}"
        assert (await c.put(url, json={"name": "t", "title": "T"})).status_code == 200
        assert (await c.get(url)).json()["labels"] == labels, "omitted = kept"
        assert (await c.put(url, json={"name": "t", "labels": None})).status_code == 200
        assert (await c.get(url)).json()["labels"] is None, "explicit null = not set"

        ph = await _phase(c, "contract")
        g = await _post(
            c,
            "/pm/gates",
            {
                "name": "signoff",
                "phase_id": ph["id"],
                "approver_role": "tenant_admin",
                "quorum": 2,
                "expires_after_hours": 24,
            },
        )
        gurl = f"/pm/gates/{g['id']}"
        assert (await c.put(gurl, json={"name": "signoff", "phase_id": ph["id"]})).status_code
        got = (await c.get(gurl)).json()
        assert (got["approver_role"], got["quorum"], got["expires_after_hours"]) == (
            "tenant_admin",
            2,
            24,
        )
        put = await c.put(
            gurl, json={"name": "signoff", "phase_id": ph["id"], "expires_after_hours": None}
        )
        assert put.status_code == 200 and put.json()["expires_after_hours"] is None

    async def test_wip_limit_keeps_scope(self, authenticated_client: AsyncClient):
        c = authenticated_client
        a, b = await _phase(c, "a_phase"), await _phase(c, "b_phase")
        wa = await _post(c, "/pm/wip-limits", {"phase_id": a["id"], "wip_limit": 3})
        await _post(c, "/pm/wip-limits", {"phase_id": b["id"], "wip_limit": 4})
        put = await c.put(f"/pm/wip-limits/{wa['id']}", json={"wip_limit": 5})
        assert put.status_code == 200, put.text
        assert (put.json()["phase_id"], put.json()["wip_limit"]) == (a["id"], 5)
        clash = await c.put(
            f"/pm/wip-limits/{wa['id']}", json={"phase_id": b["id"], "wip_limit": 1}
        )
        assert clash.status_code == 409, "the scope check sees the row as it will be stored"

    async def test_project_type_phases_and_recipe(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _bug_kind(c)
        tid = k["type"]["id"]
        put = await c.put(f"/pm/project-types/{tid}", json={"name": "bug_hunt_fix", "title": "B"})
        assert put.status_code == 200, put.text
        assert [p["name"] for p in put.json()["phases"]] == ["preliminary", "execution"]

        r = await _post(
            c,
            "/pm/recipes",
            _recipe(k, "bug_exec", "execution", "M", [_item(k, "development_bug_fix", count=2)]),
        )
        put = await c.put(
            f"/pm/recipes/{r['id']}",
            json={
                "name": "bug_exec",
                "title": "Exec",
                "project_type_id": tid,
                "phase_id": k["phases"]["execution"]["id"],
            },
        )
        assert put.status_code == 200, put.text
        got = put.json()
        assert (got["size"], got["title"]) == ("M", "Exec")
        assert [(i["card_type"], i["count"]) for i in got["items"]] == [("development_bug_fix", 2)]

    async def test_composition_keeps_overrides_and_entries(self, authenticated_client: AsyncClient):
        c = authenticated_client
        k = await _bug_kind(c)
        ph, ct, tid = k["phases"], k["card_types"], k["type"]["id"]
        url = f"/pm/project-types/{tid}/card-types"
        prelim_ids = [ct["project_setup"]["id"], ct["bug_analysis"]["id"]]
        exec_ids = [ct["development_bug_fix"]["id"]]
        seeded = await c.put(
            url,
            json={
                "phases": [
                    {
                        "phase_id": ph["preliminary"]["id"],
                        "card_type_ids": prelim_ids,
                        "entry_card_type_ids": [ct["project_setup"]["id"]],
                        "output": "our out",
                        "exit_rule": "our exit",
                    },
                    {
                        "phase_id": ph["execution"]["id"],
                        "card_type_ids": exec_ids,
                        "output": "exec out",
                    },
                ]
            },
        )
        assert seeded.status_code == 200, seeded.text

        def _state(comp: dict) -> dict:
            return {
                p["name"]: (
                    p["output_override"],
                    p["exit_rule_override"],
                    sorted(x["name"] for cap in p["capabilities"] for x in cap["card_types"]),
                    sorted(
                        x["name"]
                        for cap in p["capabilities"]
                        for x in cap["card_types"]
                        if x["is_entry"]
                    ),
                )
                for p in comp["phases"]
            }

        # An old client: card types only, no entry ids, no overrides.
        old = await c.put(
            url,
            json={
                "phases": [
                    {"phase_id": ph["preliminary"]["id"], "card_type_ids": prelim_ids},
                    {"phase_id": ph["execution"]["id"], "card_type_ids": exec_ids},
                ]
            },
        )
        assert old.status_code == 200, old.text
        assert _state(old.json())["preliminary"] == (
            "our out",
            "our exit",
            ["bug_analysis", "project_setup"],
            ["project_setup"],
        )

        # Explicit null / [] still clear.
        cleared = await c.put(
            url,
            json={
                "phases": [
                    {
                        "phase_id": ph["preliminary"]["id"],
                        "card_type_ids": prelim_ids,
                        "entry_card_type_ids": [],
                        "output": None,
                    },
                ]
            },
        )
        assert cleared.status_code == 200, cleared.text
        state = _state(cleared.json())
        assert state["preliminary"] == (
            None,
            "our exit",
            ["bug_analysis", "project_setup"],
            [],
        )
        assert state["execution"] == ("exec out", None, [], []), (
            "unlisted: no card types, overrides kept"
        )
