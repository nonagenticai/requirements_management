"""R326 (George 2026-10-30): ``change_principle_development`` + ``change_decision_development``.

fr mints both (revising an existing principle / decision) and stamps the card's ``card_group``
from the type's capability in the tenant's RM catalogue. With neither type in the catalogue the
group was ``''``. Pinned here: the default dataset carries both under ``change_management``,
placed like their sibling ``change_requirement_development``; and migration 2026_10_30_0076 adds
them to EXISTING tenants (whose catalogue an import will not re-place), additively.
"""

import copy
import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa
from httpx import AsyncClient

import services.pm_catalogue_service as svc

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = json.loads((ROOT / "src/data/pm_catalogue_defaults.json").read_text())
MIGRATION = next((ROOT / "alembic" / "versions").glob("2026_10_30_0076_*.py"))
NEW = ("change_principle_development", "change_decision_development")
SIBLING = "change_requirement_development"


def _migration():
    spec = importlib.util.spec_from_file_location("m0076", MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pre_fix_defaults() -> dict:
    """The dataset as it was before R326: neither type, nowhere."""
    d = copy.deepcopy(DEFAULTS)
    d["card_types"] = [c for c in d["card_types"] if c["name"] not in NEW]
    for pt in d["project_types"]:
        for phase, names in pt["card_types"].items():
            pt["card_types"][phase] = [n for n in names if n not in NEW]
    return d


class TestDefaults:
    def test_both_types_are_change_management(self):
        by_name = {c["name"]: c for c in DEFAULTS["card_types"]}
        for name in NEW:
            assert name in by_name, f"{name} missing: fr stamps card_group '' for it"
            assert by_name[name]["capability"] == "change_management"

    def test_placed_where_the_sibling_is(self):
        it = next(t for t in DEFAULTS["project_types"] if t["name"] == "it")
        for phase, names in it["card_types"].items():
            for name in NEW:
                assert (name in names) == (SIBLING in names), (phase, name)

    def test_migration_definitions_equal_the_defaults(self):
        m = _migration()
        by_name = {c["name"]: c for c in DEFAULTS["card_types"]}
        for ct in m.CARD_TYPES:
            want = {k: v for k, v in by_name[ct["name"]].items() if k != "capability"}
            assert ct == want, ct["name"]


async def _names_and_caps(c: AsyncClient) -> dict[str, str]:
    caps = {x["id"]: x["name"] for x in (await c.get("/pm/capabilities")).json()}
    cts = (await c.get("/pm/card-types", params={"limit": 1000})).json()
    return {x["name"]: caps[x["capability_id"]] for x in cts}


async def _placed(c: AsyncClient, phase: str) -> set[str]:
    types = {t["name"]: t for t in (await c.get("/pm/project-types")).json()}
    comp = (await c.get(f"/pm/project-types/{types['it']['id']}/card-types")).json()
    p = next(p for p in comp["phases"] if p["name"] == phase)
    return {ct["name"] for cap in p["capabilities"] for ct in cap["card_types"]}


class TestExistingTenants:
    async def test_migration_adds_and_places_them(
        self, authenticated_client, test_engine, monkeypatch
    ):
        c = authenticated_client
        m = _migration()
        # A tenant imported before R326.
        monkeypatch.setattr(svc, "load_defaults", _pre_fix_defaults)
        r = await c.post("/pm/catalogue/import-defaults", json={})
        assert r.status_code == 200, r.text
        before = await _names_and_caps(c)
        assert not set(NEW) & set(before)
        assert SIBLING in await _placed(c, "preliminary")

        async with test_engine.begin() as conn:
            out = await conn.run_sync(m.add_card_types)
        assert out == {"created": 2, "placed": 2}
        ids = {
            x["name"]: x["id"]
            for x in (await c.get("/pm/card-types", params={"limit": 1000})).json()
        }
        for name in NEW:
            tenant = (await c.get(f"/pm/card-types/{ids[name]}")).json()["tenant_id"]
            assert ids[name] == str(m.row_id(tenant, name)), "the deterministic id marks it ours"
        after = await _names_and_caps(c)
        for name in NEW:
            assert after[name] == "change_management", "fr's card_group for this type"
        assert set(NEW) <= await _placed(c, "preliminary")
        assert {k: v for k, v in after.items() if k not in NEW} == before, "nothing else changed"

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_card_types) == {"created": 0, "placed": 0}

        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.remove_card_types) == {
                "unplaced": 2,
                "deleted": 2,
                "kept": 0,
            }
        assert await _names_and_caps(c) == before

    async def test_tenant_own_row_is_kept(self, authenticated_client, test_engine, monkeypatch):
        c = authenticated_client
        m = _migration()
        monkeypatch.setattr(svc, "load_defaults", _pre_fix_defaults)
        assert (await c.post("/pm/catalogue/import-defaults", json={})).status_code == 200
        caps = {x["name"]: x["id"] for x in (await c.get("/pm/capabilities")).json()}
        own = await c.post(
            "/pm/card-types",
            json={
                "name": NEW[0],
                "title": "Ours",
                "description": "tenant text",
                "capability_id": caps["analysis"],
            },
        )
        assert own.status_code == 200, own.text
        async with test_engine.begin() as conn:
            out = await conn.run_sync(m.add_card_types)
        assert out == {"created": 1, "placed": 1}, "only the type the tenant lacks"
        assert NEW[0] not in await _placed(c, "preliminary"), "the tenant's own row is not placed"
        assert NEW[1] in await _placed(c, "preliminary")
        got = (await c.get(f"/pm/card-types/{own.json()['id']}")).json()
        assert (got["title"], got["capability_id"]) == ("Ours", caps["analysis"])

    async def test_tenant_without_the_sibling_gets_nothing(self, authenticated_client, test_engine):
        # Prod 2026-10-30: 5 tenants hold a change_management capability but no
        # change_requirement_development. The migration acts only where the sibling is.
        c = authenticated_client
        m = _migration()
        cap = await c.post("/pm/capabilities", json={"name": "change_management", "title": "CM"})
        assert cap.status_code == 200, cap.text
        async with test_engine.begin() as conn:
            out = await conn.run_sync(m.add_card_types)
        assert out == {"created": 0, "placed": 0}
        assert not set(NEW) & set(await _names_and_caps(c))

    async def test_downgrade_keeps_a_tenant_own_row_of_either_name(
        self, authenticated_client, test_engine, monkeypatch
    ):
        # The tenant's own change_management row of a NEW name, placed by the tenant: upgrade
        # leaves it alone, so downgrade must too.
        c = authenticated_client
        m = _migration()
        monkeypatch.setattr(svc, "load_defaults", _pre_fix_defaults)
        assert (await c.post("/pm/catalogue/import-defaults", json={})).status_code == 200
        caps = {x["name"]: x["id"] for x in (await c.get("/pm/capabilities")).json()}
        own = await c.post(
            "/pm/card-types",
            json={"name": NEW[1], "title": "Ours", "capability_id": caps["change_management"]},
        )
        assert own.status_code == 200, own.text
        async with test_engine.begin() as conn:
            pt, ph = (
                await conn.execute(
                    sa.text(
                        "SELECT p.project_type_id, p.phase_id FROM pm_project_type_card_types p "
                        "JOIN pm_card_types s ON s.id = p.card_type_id AND s.name = :sib "
                        "JOIN pm_project_types t ON t.id = p.project_type_id AND t.name = 'it' "
                        "JOIN pm_project_phases f ON f.id = p.phase_id "
                        "AND f.name = 'preliminary' WHERE s.tenant_id = CAST(:tenant AS uuid)"
                    ),
                    {"sib": SIBLING, "tenant": own.json()["tenant_id"]},
                )
            ).one()
            await conn.execute(
                sa.text(
                    "INSERT INTO pm_project_type_card_types "
                    "(project_type_id, phase_id, card_type_id, tenant_id, is_entry) "
                    "SELECT :pt, :ph, id, tenant_id, false FROM pm_card_types WHERE id = :id"
                ),
                {"pt": pt, "ph": ph, "id": own.json()["id"]},
            )
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_card_types) == {"created": 1, "placed": 1}
        async with test_engine.begin() as conn:
            out = await conn.run_sync(m.remove_card_types)
        assert out == {"unplaced": 1, "deleted": 1, "kept": 0}, "only the row it created"
        got = await c.get(f"/pm/card-types/{own.json()['id']}")
        assert got.status_code == 200 and got.json()["title"] == "Ours"
        placed = await _placed(c, "preliminary")
        assert NEW[1] in placed, "the tenant's own placement survives"
        assert NEW[0] not in placed

    async def test_downgrade_keeps_a_created_row_in_use_with_its_placement(
        self, authenticated_client, test_engine, monkeypatch, capsys
    ):
        c = authenticated_client
        m = _migration()
        monkeypatch.setattr(svc, "load_defaults", _pre_fix_defaults)
        assert (await c.post("/pm/catalogue/import-defaults", json={})).status_code == 200
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.add_card_types) == {"created": 2, "placed": 2}
        ids = {
            x["name"]: x["id"]
            for x in (await c.get("/pm/card-types", params={"limit": 1000})).json()
        }
        wip = await c.post("/pm/wip-limits", json={"wip_limit": 2, "card_type_id": ids[NEW[0]]})
        assert wip.status_code == 200, wip.text
        async with test_engine.begin() as conn:
            out = await conn.run_sync(m.remove_card_types)
        assert out == {"unplaced": 1, "deleted": 1, "kept": 1}
        assert f"NOTICE 0076 downgrade: kept {NEW[0]}" in capsys.readouterr().out
        assert (await c.get(f"/pm/card-types/{ids[NEW[0]]}")).status_code == 200
        placed = await _placed(c, "preliminary")
        assert NEW[0] in placed, "an in-use row keeps its placement (no half-removed state)"
        assert NEW[1] not in placed
