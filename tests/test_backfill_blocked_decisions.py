"""scripts/backfill_blocked_decisions.py — plan logic, and a real-DB apply/rerun/restore.

The plan tests are pure. The DB test seeds decisions the way blocked-escalate
minted them before n8n_custom_nodes#2126 and drives every phase against
Postgres, including restore and the skip-what-was-edited-since guard.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from models.decisions import Decision
from scripts.backfill_blocked_decisions import (
    Row,
    apply_phase,
    compute_plan,
    load,
    restore_phase,
)

TENANT = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"
PROJ = "38d69429-aa6d-40d6-a813-a250c29032ee"
SYSTEM = "00000000-0000-0000-0000-000000000001"
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def row(i: int, card: str, project: str | None = PROJ, minutes: int = 0, **kw) -> Row:
    ctx = f"card={card}; card_type=development_poc; project={project or ''}"
    return Row(
        id=f"00000000-0000-0000-0000-{i:012d}",
        tenant_id=kw.pop("tenant", TENANT),
        slug=f"blocked-{card}" + (f"-{i}" if i else ""),
        title=f"BLOCKED-{card}",
        project_id=None,
        stable_key=None,
        status=kw.pop("status", "proposed"),
        superseded_by=None,
        created_at=T0 + timedelta(minutes=minutes),
        context=kw.pop("context", ctx),
    )


def test_titles_use_card_name_and_fall_back_to_id_like_2126():
    plan = compute_plan([row(1, "111"), row(2, "222")], {}, {"111": "Fix add()"})
    titles = {i: d["title"][1] for i, d in plan.changes["titles"]}
    assert titles[row(1, "111").id] == "Blocked: Fix add()"
    assert titles[row(2, "222").id] == "Blocked: 222"


def test_title_is_sliced_to_200():
    plan = compute_plan([row(1, "111")], {}, {"111": "x" * 500})
    assert len(plan.changes["titles"][0][1]["title"][1]) == 200


def test_unparseable_context_is_reported_and_untouched():
    bad = row(1, "111", context="something else")
    plan = compute_plan([bad], {}, {})
    assert plan.unparseable == [bad.id]
    assert all(not plan.changes[p] for p in plan.changes)


def test_newest_row_survives_bound_and_older_ones_collapse_onto_it():
    old, new = row(1, "111", minutes=0), row(2, "111", minutes=5)
    plan = compute_plan([old, new], {}, {})
    assert plan.changes["bind"] == [
        (new.id, {"project_id": (None, PROJ), "stable_key": (None, "blocked:111")})
    ]
    assert plan.changes["collapse"] == [
        (old.id, {"status": ("proposed", "superseded"), "superseded_by": (None, new.slug)})
    ]


def test_survivor_takes_newest_valid_project_and_never_the_system_one():
    # newest row carries the system project; the older one a real project
    plan = compute_plan([row(1, "111", PROJ, 0), row(2, "111", SYSTEM, 5)], {}, {})
    assert plan.changes["bind"][0][1]["project_id"] == (None, PROJ)
    # only the system project ⇒ no binding at all (#2126: no projectId ⇒ no stableKey)
    plan = compute_plan([row(3, "333", SYSTEM)], {}, {})
    assert plan.changes["bind"] == []


def test_an_existing_2126_row_is_the_survivor():
    a, b = row(1, "111", minutes=0), row(2, "111", minutes=5)
    plan = compute_plan([a, b], {(TENANT, "blocked:111"): "blocked-111-new"}, {})
    assert plan.changes["bind"] == []
    assert {i for i, _ in plan.changes["collapse"]} == {a.id, b.id}
    assert all(d["superseded_by"][1] == "blocked-111-new" for _, d in plan.changes["collapse"])


def test_cards_are_grouped_per_tenant():
    other = "0195f48b-d000-7000-8000-000000000001"
    plan = compute_plan([row(1, "111"), row(2, "111", tenant=other)], {}, {})
    assert len(plan.changes["bind"]) == 2
    assert plan.changes["collapse"] == []


def test_non_proposed_rows_are_never_collapsed():
    plan = compute_plan([row(1, "111", status="accepted"), row(2, "111", minutes=5)], {}, {})
    assert plan.changes["collapse"] == []


# --- real DB ---------------------------------------------------------------


@pytest.fixture
async def backup_schema(test_engine):
    name = f"bkp_{uuid.uuid4().hex[:10]}"
    yield name
    async with test_engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))


def seed(card: str, minutes: int, project: str | None = PROJ) -> Decision:
    return Decision(
        slug=f"blocked-{card}-{minutes}",
        title=f"BLOCKED-{card}",
        context=f"card={card}; card_type=development_poc; project={project or ''}",
        decision="escalate: block reason: scope_too_large",
        tenant_id=TENANT,
        tenant_name="n8n-dev",
        subject_id="conveyor",
        status="proposed",
        created_at=T0 + timedelta(minutes=minutes),
    )


async def state(conn) -> dict[str, tuple]:
    async with conn.begin():
        res = await conn.execute(
            text(
                "SELECT slug, title, project_id, stable_key, status, superseded_by FROM decisions "
                "WHERE slug LIKE 'blocked-%' ORDER BY slug"
            )
        )
        return {r[0]: tuple(r[1:]) for r in res.fetchall()}


async def plan_now(conn, schema, names):
    async with conn.begin():
        rows, keyed = await load(conn, schema)
    return compute_plan(rows, keyed, names)


async def test_apply_all_phases_then_rerun_is_a_noop_then_restore(
    test_engine, test_session, backup_schema
):
    test_session.add_all([seed("111", 0), seed("111", 5), seed("222", 0, project=None)])
    await test_session.commit()
    names = {"111": "Fix add()"}

    async with test_engine.connect() as conn:
        before = await state(conn)
        for phase in ("titles", "bind", "collapse"):
            await apply_phase(
                conn, await plan_now(conn, backup_schema, names), phase, backup_schema
            )
        after = await state(conn)

        assert after["blocked-111-5"] == (
            "Blocked: Fix add()",
            PROJ,
            "blocked:111",
            "proposed",
            None,
        )
        assert after["blocked-111-0"] == (
            "Blocked: Fix add()",
            None,
            None,
            "superseded",
            "blocked-111-5",
        )
        # no project anywhere ⇒ retitled only, never bound
        assert after["blocked-222-0"] == ("Blocked: 222", None, None, "proposed", None)

        # rerun: the rows are still found (via the backup table) and nothing changes
        again = await plan_now(conn, backup_schema, names)
        assert again.candidates == 3
        assert all(not again.changes[p] for p in again.changes)

        # someone edits a retitled row after the backfill: restore must leave it alone
        async with conn.begin():
            await conn.execute(
                text("UPDATE decisions SET title = 'hand edited' WHERE slug = 'blocked-222-0'")
            )
        report = {}
        for phase in ("collapse", "bind", "titles"):
            report[phase] = await restore_phase(conn, phase, backup_schema)
        restored = await state(conn)

    assert report["titles"][0] == 2 and len(report["titles"][1]) == 1
    assert report["bind"][0] == 1 and report["collapse"][0] == 1
    assert restored["blocked-111-0"] == before["blocked-111-0"]
    assert restored["blocked-111-5"] == before["blocked-111-5"]
    assert restored["blocked-222-0"][0] == "hand edited"


# --- validator round 1 (phases run apart; #2126 not yet live) ----------------


def test_a_bound_survivor_stays_the_survivor_when_newer_blocked_rows_arrive():
    bound = row(1, "111", minutes=0)
    bound.project_id, bound.stable_key, bound.title = PROJ, "blocked:111", "Blocked: 111"
    newer = row(2, "111", minutes=30)
    plan = compute_plan([bound, newer], {}, {})
    # the newer row must NOT get the key (uq_decisions_tenant_project_stable_key)
    assert plan.changes["bind"] == []
    # and the bound row must not be superseded onto the unbound newcomer
    assert plan.changes["collapse"] == [
        (newer.id, {"status": ("proposed", "superseded"), "superseded_by": (None, bound.slug)})
    ]


def test_rows_of_an_unsafe_shape_are_reported_and_untouched():
    private = row(1, "111")
    private.visibility = "user"
    prebound = row(2, "222")
    prebound.project_id = PROJ  # bound but no stable_key: a retitle could collide
    plan = compute_plan([private, prebound], {}, {})
    assert sorted(plan.skipped_shape) == sorted([private.id, prebound.id])
    assert all(not plan.changes[p] for p in plan.changes)


def test_project_id_is_kept_verbatim_like_2126():
    upper = PROJ.upper()
    plan = compute_plan([row(1, "111", upper)], {}, {})
    assert plan.changes["bind"][0][1]["project_id"] == (None, upper)


async def test_cli_dry_run_writes_nothing_and_apply_titles_writes_only_titles(
    test_engine, test_session, backup_schema, tmp_path
):
    import asyncio
    import json as _json
    import os
    import subprocess
    import sys

    test_session.add_all([seed("111", 0), seed("111", 5)])
    await test_session.commit()
    names = tmp_path / "names.json"
    names.write_text(_json.dumps({"111": "Fix add()"}))
    script = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "backfill_blocked_decisions.py"
    )

    def cli(*extra: str) -> dict:
        out = subprocess.run(
            [
                sys.executable,
                script,
                "--card-names",
                str(names),
                "--backup-schema",
                backup_schema,
                *extra,
            ],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            check=True,
        )
        return _json.loads(out.stdout)

    async with test_engine.connect() as conn:
        before = await state(conn)
        dry = await asyncio.to_thread(cli)
        assert dry["dry_run"] is True and dry["titles"]["rows"] == 2
        assert await state(conn) == before
        async with conn.begin():
            exists = await conn.execute(
                text("SELECT to_regclass(:t) IS NOT NULL"),
                {"t": f"{backup_schema}.decisions_blocked_backfill"},
            )
            assert exists.scalar() is False

        applied = await asyncio.to_thread(cli, "--apply", "titles")
        assert applied["applied"] == {"titles": 2}
        after = await state(conn)
    for slug, (title, project_id, stable_key, status, superseded_by) in after.items():
        assert title == "Blocked: Fix add()"
        assert (project_id, stable_key, status, superseded_by) == before[slug][1:]
