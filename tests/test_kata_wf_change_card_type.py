"""Kata WP5 (George 2026-11-03): ``wf_change``, the ONE execution-phase kata card type.

Pinned here: the default dataset carries it under ``workflow_engineering``, placed on ``kata``'s
``execution`` phase alone; and migration 2026_11_03_0077 adds it to EXISTING tenants that hold a
``kata`` project type (whose catalogue an import will not re-place), additively.
"""

import copy
import importlib.util
import json
from pathlib import Path

from httpx import AsyncClient

import services.pm_catalogue_service as svc

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = json.loads((ROOT / "src/data/pm_catalogue_defaults.json").read_text())
MIGRATION = next((ROOT / "alembic" / "versions").glob("2026_11_03_0077_*.py"))
NAME = "wf_change"


def _migration():
    spec = importlib.util.spec_from_file_location("m0077", MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pre_fix_defaults() -> dict:
    """The dataset as it was before Kata WP5: no wf_change, nowhere."""
    d = copy.deepcopy(DEFAULTS)
    d["card_types"] = [c for c in d["card_types"] if c["name"] != NAME]
    for pt in d["project_types"]:
        for phase, names in pt["card_types"].items():
            pt["card_types"][phase] = [n for n in names if n != NAME]
    return d


class TestDefaults:
    def test_wf_change_is_workflow_engineering(self):
        by_name = {c["name"]: c for c in DEFAULTS["card_types"]}
        assert by_name[NAME]["capability"] == "workflow_engineering"

    def test_placed_on_kata_execution_only(self):
        for pt in DEFAULTS["project_types"]:
            for phase, names in pt["card_types"].items():
                want = pt["name"] == "kata" and phase == "execution"
                assert (NAME in names) == want, (pt["name"], phase)

    def test_migration_definition_equals_the_default(self):
        m = _migration()
        by_name = {c["name"]: c for c in DEFAULTS["card_types"]}
        (ct,) = m.CARD_TYPES
        assert ct == {k: v for k, v in by_name[NAME].items() if k != "capability"}


async def _names_and_caps(c: AsyncClient) -> dict[str, str]:
    caps = {x["id"]: x["name"] for x in (await c.get("/pm/capabilities")).json()}
    cts = (await c.get("/pm/card-types", params={"limit": 1000})).json()
    return {x["name"]: caps[x["capability_id"]] for x in cts}


async def _placed(c: AsyncClient, kind: str, phase: str) -> set[str]:
    types = {t["name"]: t for t in (await c.get("/pm/project-types")).json()}
    comp = (await c.get(f"/pm/project-types/{types[kind]['id']}/card-types")).json()
    p = next(p for p in comp["phases"] if p["name"] == phase)
    return {ct["name"] for cap in p["capabilities"] for ct in cap["card_types"]}


async def _import_pre_fix(c: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(svc, "load_defaults", _pre_fix_defaults)
    r = await c.post("/pm/catalogue/import-defaults", json={})
    assert r.status_code == 200, r.text


class TestExistingTenants:
    async def test_migration_adds_and_places_it(
        self, authenticated_client, test_engine, monkeypatch
    ):
        c = authenticated_client
        m = _migration()
        await _import_pre_fix(c, monkeypatch)
        before = await _names_and_caps(c)
        assert NAME not in before
        assert await _placed(c, "kata", "execution") == set()

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_card_types) == {"created": 1, "placed": 1}
        ids = {
            x["name"]: x["id"]
            for x in (await c.get("/pm/card-types", params={"limit": 1000})).json()
        }
        tenant = (await c.get(f"/pm/card-types/{ids[NAME]}")).json()["tenant_id"]
        assert ids[NAME] == str(m.row_id(tenant, NAME)), "the deterministic id marks it ours"
        after = await _names_and_caps(c)
        assert after[NAME] == "workflow_engineering"
        assert await _placed(c, "kata", "execution") == {NAME}
        assert NAME not in await _placed(c, "it", "execution"), "kata-only"
        assert {k: v for k, v in after.items() if k != NAME} == before, "nothing else changed"

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_card_types) == {"created": 0, "placed": 0}

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.remove_card_types) == {
                "unplaced": 1,
                "deleted": 1,
                "kept": 0,
            }
        assert await _names_and_caps(c) == before
        assert await _placed(c, "kata", "execution") == set()

    async def test_tenant_own_row_is_kept_and_not_placed(
        self, authenticated_client, test_engine, monkeypatch
    ):
        c = authenticated_client
        m = _migration()
        await _import_pre_fix(c, monkeypatch)
        caps = {x["name"]: x["id"] for x in (await c.get("/pm/capabilities")).json()}
        own = await c.post(
            "/pm/card-types",
            json={"name": NAME, "title": "Ours", "capability_id": caps["analysis"]},
        )
        assert own.status_code == 200, own.text
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_card_types) == {"created": 0, "placed": 0}
        assert await _placed(c, "kata", "execution") == set()
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.remove_card_types) == {
                "unplaced": 0,
                "deleted": 0,
                "kept": 0,
            }
        got = (await c.get(f"/pm/card-types/{own.json()['id']}")).json()
        assert (got["title"], got["capability_id"]) == ("Ours", caps["analysis"])

    async def test_tenant_without_a_kata_type_gets_nothing(self, authenticated_client, test_engine):
        c = authenticated_client
        m = _migration()
        cap = await c.post("/pm/capabilities", json={"name": "workflow_engineering", "title": "WE"})
        assert cap.status_code == 200, cap.text
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_card_types) == {"created": 0, "placed": 0}
        assert NAME not in await _names_and_caps(c)

    async def test_downgrade_keeps_a_created_row_in_use_with_its_placement(
        self, authenticated_client, test_engine, monkeypatch, capsys
    ):
        c = authenticated_client
        m = _migration()
        await _import_pre_fix(c, monkeypatch)
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_card_types) == {"created": 1, "placed": 1}
        ids = {
            x["name"]: x["id"]
            for x in (await c.get("/pm/card-types", params={"limit": 1000})).json()
        }
        wip = await c.post("/pm/wip-limits", json={"wip_limit": 2, "card_type_id": ids[NAME]})
        assert wip.status_code == 200, wip.text
        async with test_engine.begin() as conn:
            out = await conn.run_sync(m.remove_card_types)
        assert out == {"unplaced": 0, "deleted": 0, "kept": 1}
        assert f"NOTICE 0077 downgrade: kept {NAME}" in capsys.readouterr().out
        assert await _placed(c, "kata", "execution") == {NAME}, "no half-removed state"
