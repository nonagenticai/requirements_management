"""George 2026-11-07: the ``py2rust`` project type is RETIRED; the rewrite chain is an ``it`` flow.

Pinned here:

* the default dataset carries no ``py2rust`` type (nor its phases, nor its WIP limit) and ``it``
  carries the four rewrite placements: ``rewrite_plan`` in ``analysis`` and the three held
  ``rewrite_*`` types in ``execution``;
* migration 2026_11_07_0083 retires an existing tenant's type IN PLACE (``retired_at``; never a
  delete) and backfills the three placements into ``it``/``execution`` (its own test file);
* a retired type still lists and its existing project still reads, but starting a project on it
  (or moving one onto it) is refused 422 ``project_type_retired``;
* migration 2026_11_03_0079's historical behaviour (it ADDS the type to existing tenants) is
  still pinned, against the dataset of its day — see ``_pre_fix_defaults``.
"""

import copy
import importlib.util
import json
from pathlib import Path

from httpx import AsyncClient

import services.pm_catalogue_service as svc
from tests.briefs import MIN_BRIEF

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = json.loads((ROOT / "src/data/pm_catalogue_defaults.json").read_text())
MIGRATION = next((ROOT / "alembic" / "versions").glob("2026_11_03_0079_*.py"))
TYPE = "py2rust"
NEW_PHASES = ("plan", "port", "parity", "cutover")
PM = {"project_phase_start", "project_phase_closure"}
# 0079 (history) still places phase_coverage_check, retired by R347 (4) / 0082; its tests run
# against the dataset of its day, which held the type.
RETIRED = "phase_coverage_check"
PM_0079 = PM | {RETIRED}
RETIRED_ROW = {
    "name": RETIRED,
    "title": "Phase coverage check",
    "description": "retired by R347 (4)",
    "capability": "project_management",
    "inputs": [],
    "outputs": [],
    "acceptance": ["READY: every_phase_goal_served_by_a_planned_card"],
    "rules": [],
    "human_task": False,
    "definition_of_done": None,
}
# The four rewrite_* card types (RS-D4). 0083 places the three held ones on `it`/execution.
REWRITE = ("rewrite_plan", "rewrite_slice", "rewrite_parity_qa", "rewrite_cutover_readiness")
HELD = ("rewrite_slice", "rewrite_parity_qa", "rewrite_cutover_readiness")
# 0079's one-work-card-per-phase chain: plan -> slice -> parity -> cutover readiness.
WORK = dict(zip(NEW_PHASES, REWRITE, strict=True))


def _migration():
    spec = importlib.util.spec_from_file_location("m0079", MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pre_fix_defaults(drop_card_types: set[str] | frozenset[str] = frozenset()) -> dict:
    """The dataset of 0079's day: no py2rust type, none of its phases, and `it` has NOT yet
    gained the three rewrite_* placements 0083 backfills. The rewrite_* card types stay
    (unplaced), as the live conveyor tenant held them; ``drop_card_types`` removes some, as
    the other kata tenants lack them."""
    d = copy.deepcopy(DEFAULTS)
    d["project_types"] = [t for t in d["project_types"] if t["name"] != TYPE]
    d["wip_limits"] = [w for w in d["wip_limits"] if w["project_type"] != TYPE]
    d["project_phases"] = [p for p in d["project_phases"] if p["name"] not in NEW_PHASES]
    d["card_types"] = [
        c for c in [*d["card_types"], dict(RETIRED_ROW)] if c["name"] not in drop_card_types
    ]
    for pt in d["project_types"]:
        for phase, names in pt["card_types"].items():
            pt["card_types"][phase] = [
                n
                for n in names
                if n not in drop_card_types
                # 0083 places these on `it`; 0079's day did not.
                and not (pt["name"] == "it" and n in HELD)
            ]
    return d


def _code(resp) -> str | None:
    body = resp.json()
    return (body.get("error") or {}).get("code") if isinstance(body, dict) else None


class TestDefaults:
    def test_the_type_is_gone(self):
        names = [t["name"] for t in DEFAULTS["project_types"]]
        assert TYPE not in names
        assert sorted(names) == sorted(
            ["it", "kata", "program", "meta_prompt", "meta_procedure", "kaizen"]
        )
        assert not [w for w in DEFAULTS["wip_limits"] if w["project_type"] == TYPE]
        assert [p["name"] for p in DEFAULTS["project_phases"]] == [
            "setup",
            "preliminary",
            "analysis",
            "design",
            "execution",
            "test",
        ]
        assert not set(NEW_PHASES) & {p["name"] for p in DEFAULTS["project_phases"]}
        assert TYPE not in DEFAULTS["sources"]["v4_rulings"]["scope"]

    def test_it_carries_the_four_rewrite_placements(self):
        it = next(t for t in DEFAULTS["project_types"] if t["name"] == "it")
        assert "rewrite_plan" in it["card_types"]["analysis"], "R228/R333"
        assert {"rewrite_cutover_readiness", "rewrite_parity_qa", "rewrite_slice"} <= set(
            it["card_types"]["execution"]
        )
        # Nothing else claims them: `it` is their only home now.
        for pt in DEFAULTS["project_types"]:
            if pt["name"] == "it":
                continue
            placed = {n for v in pt["card_types"].values() for n in v}
            assert not placed & set(REWRITE), pt["name"]

    def test_the_four_rewrite_card_types_remain(self):
        """George 2026-11-07: the type is retired, the CARD TYPES are not — the chain lives on."""
        cts = {c["name"]: c for c in DEFAULTS["card_types"]}
        assert set(REWRITE) <= set(cts)
        # The three held definitions keep the live conveyor row's own values (RS-D4).
        assert (cts["rewrite_slice"]["capability"], cts["rewrite_slice"]["rules"]) == (
            "development",
            ["repo_write"],
        )
        assert (cts["rewrite_parity_qa"]["capability"], cts["rewrite_parity_qa"]["rules"]) == (
            "testing",
            ["container_read_exec"],
        )
        assert (
            cts["rewrite_cutover_readiness"]["capability"],
            cts["rewrite_cutover_readiness"]["rules"],
        ) == ("change_management", [])
        assert cts["rewrite_slice"]["definition_of_done"] == "pr_merged"
        assert cts["rewrite_parity_qa"]["definition_of_done"] == "asset_persisted"
        assert cts["rewrite_cutover_readiness"]["definition_of_done"] == "asset_persisted"

    def test_migration_0079_constants_are_the_historical_record(self):
        """0079's frozen copies are history: 0083 retired the type, so they no longer equal the
        default. They must still describe the type 0079 created (never a silent edit)."""
        m = _migration()
        assert [p["name"] for p in m.PHASES] == list(NEW_PHASES)
        assert (m.PROJECT_TYPE["name"], m.PROJECT_TYPE["title"]) == (
            TYPE,
            "Python → Rust rewrite",
        )
        assert m.PROJECT_TYPE["phases"] == ["setup", *NEW_PHASES]
        assert set(m.PROJECT_TYPE["card_types"]) == {"setup", *NEW_PHASES}
        for phase, work in WORK.items():
            assert set(m.PROJECT_TYPE["card_types"][phase]) == PM_0079 | {work}, phase
        assert m.PROJECT_TYPE["entry_card_types"] == {"setup": ["project_start_request_analysis"]}
        assert m.WIP_LIMIT == 1
        assert TYPE not in {t["name"] for t in DEFAULTS["project_types"]}, "0083 retired it"


async def _types(c: AsyncClient) -> dict[str, dict]:
    return {t["name"]: t for t in (await c.get("/pm/project-types")).json()}


async def _composition(c: AsyncClient, kind: str) -> dict[str, set[str]]:
    t = (await _types(c))[kind]
    comp = (await c.get(f"/pm/project-types/{t['id']}/card-types")).json()
    return {
        p["name"]: {ct["name"] for cap in p["capabilities"] for ct in cap["card_types"]}
        for p in comp["phases"]
    }


async def _phase_order(c: AsyncClient, kind: str) -> list[str]:
    t = (await _types(c))[kind]
    comp = (await c.get(f"/pm/project-types/{t['id']}/card-types")).json()
    return [p["name"] for p in comp["phases"]]


async def _import(
    c: AsyncClient, monkeypatch, drop: set[str] | frozenset[str] = frozenset()
) -> None:
    monkeypatch.setattr(svc, "load_defaults", lambda: _pre_fix_defaults(drop))
    r = await c.post("/pm/catalogue/import-defaults", json={})
    assert r.status_code == 200, r.text


async def _phase_names(c: AsyncClient) -> set[str]:
    return {p["name"] for p in (await c.get("/pm/project-phases")).json()}


class TestExistingTenants:
    """Migration 2026_11_03_0079 is HISTORY: it still adds the type to existing tenants."""

    async def test_upgrade_rerun_downgrade(self, authenticated_client, test_engine, monkeypatch):
        c = authenticated_client
        m = _migration()
        await _import(c, monkeypatch)
        assert TYPE not in await _types(c)
        it_before = await _composition(c, "it")
        phases_before = await _phase_names(c)
        it_project = await c.post(
            "/pm/projects",
            json={
                "project_type_id": (await _types(c))["it"]["id"],
                "title": "an it project",
                "brief": copy.deepcopy(MIN_BRIEF),
            },
        )
        assert it_project.status_code == 200, it_project.text

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_project_type) == {
                "types": 1,
                "phases": 4,
                "placed": 18,
                "skipped": 0,
            }
        types = await _types(c)
        tenant = types[TYPE]["tenant_id"]
        assert types[TYPE]["id"] == str(m.row_id(tenant, "project_type", TYPE))
        assert types[TYPE]["title"] == "Python → Rust rewrite"
        assert types[TYPE]["retired_at"] is None, "0079 creates it live; 0083 retires it"
        assert await _phase_order(c, TYPE) == ["setup", *NEW_PHASES]
        comp = await _composition(c, TYPE)
        assert comp["setup"] == {"project_setup", "project_start_request_analysis"}
        for phase, work in WORK.items():
            assert comp[phase] == PM_0079 | {work}, phase
        assert await _composition(c, "it") == it_before, "it unaffected"
        got = (await c.get(f"/pm/projects/{it_project.json()['id']}")).json()
        assert got["project_type_id"] == types["it"]["id"]
        wips = [
            w
            for w in (await c.get("/pm/wip-limits")).json()
            if w["project_type_id"] == types[TYPE]["id"]
        ]
        assert [w["wip_limit"] for w in wips] == [1]

        # A py2rust project can be created: it starts in setup.
        p = await c.post(
            "/pm/projects",
            json={
                "project_type_id": types[TYPE]["id"],
                "title": "rewrite x",
                "brief": copy.deepcopy(MIN_BRIEF),
            },
        )
        assert p.status_code == 200, p.text
        assert p.json()["current_phase"] == "setup"

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_project_type) == {
                "types": 0,
                "phases": 0,
                "placed": 0,
                "skipped": 0,
            }, "re-run changes nothing"

        # In use by a project: the downgrade keeps it.
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.remove_project_type) == {
                "types": 0,
                "phases": 0,
                "kept": 1,
            }
        assert TYPE in await _types(c)
        assert (await c.delete(f"/pm/projects/{p.json()['id']}")).status_code in (200, 204)
        async with test_engine.begin() as conn:
            await conn.exec_driver_sql(
                "DELETE FROM pm_project_phase_events WHERE project_id = %s", (p.json()["id"],)
            )

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.remove_project_type) == {
                "types": 1,
                "phases": 4,
                "kept": 0,
            }
        assert TYPE not in await _types(c)
        assert await _phase_names(c) == phases_before
        assert await _composition(c, "it") == it_before

    async def test_tenant_missing_a_card_type_is_skipped(
        self, authenticated_client, test_engine, monkeypatch, capsys
    ):
        c = authenticated_client
        m = _migration()
        await _import(c, monkeypatch, drop={"rewrite_slice"})
        phases_before = await _phase_names(c)
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_project_type) == {
                "types": 0,
                "phases": 0,
                "placed": 0,
                "skipped": 1,
            }
        assert "NOTICE 0079: skipped tenant" in capsys.readouterr().out
        assert TYPE not in await _types(c)
        assert await _phase_names(c) == phases_before

    async def test_tenant_without_a_kata_type_gets_nothing(self, authenticated_client, test_engine):
        c = authenticated_client
        m = _migration()
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_project_type) == {
                "types": 0,
                "phases": 0,
                "placed": 0,
                "skipped": 0,
            }
        assert TYPE not in await _types(c)

    async def test_tenant_with_its_own_py2rust_is_skipped_others_upgraded(
        self, test_client, tenant_a_id, tenant_b_id, test_engine, monkeypatch, capsys
    ):
        """A same-key ``py2rust`` with another id (as if the app created it between the check and
        the INSERT): the type INSERT's ON CONFLICT skips that tenant, writes none of its rows,
        and the migration still upgrades the next tenant."""
        m = _migration()
        monkeypatch.setattr(svc, "load_defaults", _pre_fix_defaults)
        a = {"X-Tenant-Id": tenant_a_id}
        b = {"X-Tenant-Id": tenant_b_id}
        for h in (a, b):
            r = await test_client.post("/pm/catalogue/import-defaults", json={}, headers=h)
            assert r.status_code == 200, r.text
        own = await test_client.post(
            "/pm/project-types", json={"name": TYPE, "title": "Ours"}, headers=a
        )
        assert own.status_code == 200, own.text
        assert own.json()["id"] != str(m.row_id(tenant_a_id, "project_type", TYPE))
        phases_a = {
            p["name"] for p in (await test_client.get("/pm/project-phases", headers=a)).json()
        }

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_project_type) == {
                "types": 1,
                "phases": 4,
                "placed": 18,
                "skipped": 1,
            }
        assert f"NOTICE 0079: skipped tenant {tenant_a_id}" in capsys.readouterr().out
        types_a = {
            t["name"]: t for t in (await test_client.get("/pm/project-types", headers=a)).json()
        }
        assert types_a[TYPE]["id"] == own.json()["id"]
        assert types_a[TYPE]["title"] == "Ours"
        assert types_a[TYPE]["phases"] == [], "nothing placed on the tenant's own type"
        assert {
            p["name"] for p in (await test_client.get("/pm/project-phases", headers=a)).json()
        } == phases_a, "no phase created for the skipped tenant"
        wips_a = (await test_client.get("/pm/wip-limits", headers=a)).json()
        assert not [w for w in wips_a if w["project_type_id"] == own.json()["id"]]
        types_b = {
            t["name"]: t for t in (await test_client.get("/pm/project-types", headers=b)).json()
        }
        assert types_b[TYPE]["id"] == str(m.row_id(tenant_b_id, "project_type", TYPE))
        assert [p["name"] for p in types_b[TYPE]["phases"]] == ["setup", *NEW_PHASES]

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.remove_project_type) == {
                "types": 1,
                "phases": 4,
                "kept": 0,
            }
        types_a = {
            t["name"]: t for t in (await test_client.get("/pm/project-types", headers=a)).json()
        }
        assert types_a[TYPE]["id"] == own.json()["id"], "downgrade never touches the tenant's own"

    async def test_tenant_own_type_and_phase_are_kept(
        self, authenticated_client, test_engine, monkeypatch
    ):
        c = authenticated_client
        m = _migration()
        await _import(c, monkeypatch)
        own_phase = await c.post("/pm/project-phases", json={"name": "plan", "title": "Ours"})
        assert own_phase.status_code == 200, own_phase.text
        async with test_engine.begin() as conn:
            out = await conn.run_sync(m.add_project_type)
        assert out == {"types": 1, "phases": 3, "placed": 18, "skipped": 0}
        assert await _phase_order(c, TYPE) == ["setup", *NEW_PHASES]
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.remove_project_type) == {
                "types": 1,
                "phases": 3,
                "kept": 0,
            }
        phases = {p["name"]: p for p in (await c.get("/pm/project-phases")).json()}
        assert phases["plan"]["id"] == own_phase.json()["id"], "the tenant's own phase stays"
        assert phases["plan"]["title"] == "Ours"


class TestRetirement:
    """0083: a retired type lists and its projects read, but it starts nothing new."""

    async def test_a_retired_type_refuses_new_projects_and_keeps_history(
        self, authenticated_client: AsyncClient, test_engine
    ):
        c = authenticated_client
        assert (await c.post("/pm/catalogue/import-defaults", json={})).status_code == 200
        phases = {p["name"]: p["id"] for p in (await c.get("/pm/project-phases")).json()}
        created = await c.post(
            "/pm/project-types", json={"name": TYPE, "title": "Python → Rust rewrite"}
        )
        assert created.status_code == 200, created.text
        tid = created.json()["id"]
        # A type needs a setup phase to start a project: select the one `it` uses.
        selected = await c.put(
            f"/pm/project-types/{tid}",
            json={
                "name": TYPE,
                "title": "Python → Rust rewrite",
                "phase_ids": [phases["setup"]],
            },
        )
        assert selected.status_code == 200, selected.text
        assert [p["name"] for p in selected.json()["phases"]] == ["setup"]

        # Live: a project on it is created and starts in setup.
        old = await c.post(
            "/pm/projects",
            json={
                "project_type_id": tid,
                "title": "rewrite x",
                "brief": copy.deepcopy(MIN_BRIEF),
            },
        )
        assert old.status_code == 200, old.text
        assert old.json()["current_phase"] == "setup"

        # The migration's one write: the type is retired, never deleted.
        async with test_engine.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE pm_project_types SET retired_at = now() WHERE id = %s", (tid,)
            )

        # Still listed (history), and the existing project still reads type + phase.
        assert (await _types(c))[TYPE]["retired_at"] is not None
        got = (await c.get(f"/pm/projects/{old.json()['id']}")).json()
        assert (got["project_type_id"], got["current_phase"]) == (tid, "setup")

        # NO new project on it.
        again = await c.post(
            "/pm/projects",
            json={
                "project_type_id": tid,
                "title": "rewrite y",
                "brief": copy.deepcopy(MIN_BRIEF),
            },
        )
        assert again.status_code == 422, again.text
        assert _code(again) == "project_type_retired"

        # And no existing project may move onto it.
        it = (await _types(c))["it"]
        it_project = await c.post(
            "/pm/projects",
            json={
                "project_type_id": it["id"],
                "title": "an it project",
                "brief": copy.deepcopy(MIN_BRIEF),
            },
        )
        assert it_project.status_code == 200, it_project.text
        moved = await c.put(
            f"/pm/projects/{it_project.json()['id']}",
            json={"title": "an it project", "project_type_id": tid},
        )
        assert moved.status_code == 422, moved.text
        assert _code(moved) == "project_type_retired"
        assert (await c.get(f"/pm/projects/{it_project.json()['id']}")).json()[
            "project_type_id"
        ] == it["id"], "the refused move changed nothing"

    async def test_import_defaults_gives_a_new_tenant_it_with_the_rewrite_cards(
        self, authenticated_client
    ):
        c = authenticated_client
        r = await c.post("/pm/catalogue/import-defaults", json={})
        assert r.status_code == 200, r.text
        assert TYPE not in await _types(c)
        assert await _phase_order(c, "it") == [
            "setup",
            "preliminary",
            "analysis",
            "design",
            "execution",
            "test",
        ]
        comp = await _composition(c, "it")
        assert "rewrite_plan" in comp["analysis"]
        assert {"rewrite_cutover_readiness", "rewrite_parity_qa", "rewrite_slice"} <= comp[
            "execution"
        ]
