"""Migration 2026_10_25_0065: ``criterion_ids`` added to EXISTING board templates (R-CK10).

Additive only: each row keeps its own current groups, lists and labels (the no-seeding rule —
a row is never replaced by the code default); the one change is ``criterion_ids`` in its own
Relations group, after ``required_by``. Run against real rows through the API.
"""

import importlib.util
from pathlib import Path

from httpx import AsyncClient

MIGRATION = next(
    (Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("2026_10_25_0065_*.py")
)


def _migration():
    spec = importlib.util.spec_from_file_location("m0065", MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The shape every production template row holds today: Relations without criterion_ids, and a
# tenant-local improvement (``my_field``) that the code default does not carry.
LEGACY = [
    {"name": "Metadata", "fields": ["priority", "my_field"]},
    {"name": "Relations", "fields": ["parent_card", "child_cards", "requires", "required_by"]},
    {"name": "Runtime State", "fields": ["released"]},
]


async def _post(c: AsyncClient, body: dict) -> dict:
    r = await c.post("/pm/board-templates", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _groups(c: AsyncClient, eid: str) -> list[dict]:
    r = await c.get(f"/pm/board-templates/{eid}")
    assert r.status_code == 200, r.text
    return r.json()["custom_field_groups"]


class TestPure:
    def test_inserted_after_required_by_and_idempotent(self):
        m = _migration()
        out = m.add_field(LEGACY)
        assert out[1]["fields"] == [
            "parent_card",
            "child_cards",
            "requires",
            "required_by",
            "criterion_ids",
        ]
        assert out[0] == LEGACY[0] and out[2] == LEGACY[2], "other groups untouched"
        assert LEGACY[1]["fields"][-1] == "required_by", "input not mutated"
        assert m.add_field(out) is None, "re-run changes nothing"
        assert m.remove_field(out) == LEGACY

    def test_appended_when_no_required_by(self):
        m = _migration()
        out = m.add_field([{"name": "Relations", "fields": ["parent_card"]}])
        assert out == [{"name": "Relations", "fields": ["parent_card", "criterion_ids"]}]

    def test_no_relations_group_or_already_present_is_left_alone(self):
        m = _migration()
        assert m.add_field([{"name": "Metadata", "fields": ["priority"]}]) is None
        assert m.add_field([{"name": "Metadata", "fields": ["criterion_ids"]}]) is None
        assert m.add_field(None) is None
        assert m.remove_field([{"name": "Metadata", "fields": ["criterion_ids"]}]) is None


class TestRows:
    async def test_existing_rows_gain_the_field_and_keep_their_own(
        self, authenticated_client, test_engine
    ):
        c = authenticated_client
        m = _migration()
        legacy = await _post(c, {"name": "default", "custom_field_groups": LEGACY})
        no_rel = await _post(
            c,
            {"name": "other", "custom_field_groups": [{"name": "Metadata", "fields": ["x"]}]},
        )
        unset = await _post(c, {"name": "bare"})
        async with test_engine.begin() as conn:
            changed = await conn.run_sync(m.rewrite_templates)
        assert changed == 1
        got = await _groups(c, legacy["id"])
        assert got[0] == LEGACY[0], "the tenant's own Metadata (my_field) survives"
        assert got[1]["fields"][-2:] == ["required_by", "criterion_ids"]
        assert got[2] == LEGACY[2]
        assert await _groups(c, no_rel["id"]) == [{"name": "Metadata", "fields": ["x"]}]
        assert await _groups(c, unset["id"]) is None
        async with test_engine.begin() as conn:
            assert await conn.run_sync(m.rewrite_templates) == 0, "re-run is a no-op"
        async with test_engine.begin() as conn:
            assert await conn.run_sync(lambda b: m.rewrite_templates(b, down=True)) == 1
        assert await _groups(c, legacy["id"]) == LEGACY
