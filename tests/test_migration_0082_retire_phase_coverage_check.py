"""Migration 0082 (R347 (4)): ``phase_coverage_check`` is deleted in every tenant, forward-only.

Runs the real alembic chain into a private schema (same machinery and advisory lock as
``test_rls_db_state_pinned.py``): upgrade to 0081, seed two tenants that each hold the retired
card type and a kept one, each placed on a project type, in a recipe and under a WIP limit;
upgrade to head; read what is left; then try to downgrade.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from tests.tenants import T
from tests.test_migration_0069_modeling_models import _alembic
from tests.test_migration_0081_drop_schedule_apparatus import _downgrade
from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
)

PREVIOUS = "2026_11_04_0081"
TENANTS = (T("migration-0082-a"), T("migration-0082-b"))
RETIRED, KEPT = "phase_coverage_check", "project_phase_start"


def _owned(conn: psycopg.Connection, table: str, tenant: str, **cols) -> uuid.UUID:
    rid = uuid.uuid4()
    names = ["id", "tenant_id", "tenant_name", "subject_id", *cols]
    conn.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, names)),
            sql.SQL(", ").join(sql.Placeholder() * len(names)),
        ),
        (rid, tenant, "n", "alice", *cols.values()),
    )
    return rid


def _seed(conn: psycopg.Connection, schema: str) -> None:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    for t in TENANTS:
        cap = _owned(conn, "pm_capabilities", t, name="project_management", title="PM")
        cts = {
            n: _owned(conn, "pm_card_types", t, name=n, title=n, capability_id=cap)
            for n in (RETIRED, KEPT)
        }
        pt = _owned(conn, "pm_project_types", t, name="it", title="IT")
        ph = _owned(conn, "pm_project_phases", t, name="analysis", title="Analysis")
        conn.execute(
            "INSERT INTO pm_project_type_phases (project_type_id, phase_id, position, tenant_id) "
            "VALUES (%s, %s, 0, %s)",
            (pt, ph, t),
        )
        recipe = _owned(conn, "pm_recipes", t, name="r", title="r", project_type_id=pt, phase_id=ph)
        for pos, ct in enumerate(cts.values()):
            conn.execute(
                "INSERT INTO pm_project_type_card_types (project_type_id, phase_id, card_type_id, "
                "tenant_id) VALUES (%s, %s, %s, %s)",
                (pt, ph, ct, t),
            )
            conn.execute(
                "INSERT INTO pm_recipe_items (recipe_id, position, card_type_id, count_mode, "
                "count, tenant_id) VALUES (%s, %s, %s, 'fixed', 1, %s)",
                (recipe, pos, ct, t),
            )
            _owned(conn, "pm_wip_limits", t, card_type_id=ct, wip_limit=1)


def _read(conn: psycopg.Connection, schema: str) -> dict:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))

    def names(table: str) -> list[tuple[str, str]]:
        return sorted(
            (str(r[0]), r[1])
            for r in conn.execute(
                sql.SQL(
                    "SELECT x.tenant_id, c.name FROM {} x JOIN pm_card_types c "
                    "ON c.id = x.card_type_id WHERE x.tenant_id = ANY(%s)"
                ).format(sql.Identifier(table)),
                (list(TENANTS),),
            ).fetchall()
        )

    card_types = sorted(
        (str(r[0]), r[1])
        for r in conn.execute(
            "SELECT tenant_id, name FROM pm_card_types WHERE tenant_id = ANY(%s)", (list(TENANTS),)
        ).fetchall()
    )
    head = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return {
        "card_types": card_types,
        "placements": names("pm_project_type_card_types"),
        "recipe_items": names("pm_recipe_items"),
        "wip_limits": names("pm_wip_limits"),
        "head": head[0],
    }


@pytest.fixture(scope="module")
def states() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m82{tag}", f"rls_hide_m82{tag}"
    base = _base_url()
    lock = psycopg.connect(_libpq(base), autocommit=True)
    lock.execute("SET lock_timeout = '15min'")  # test-only private connection
    lock.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    out: dict = {}
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            _reclaim_leftovers(conn)
        try:
            _run_and_read(base, schema, hide, keep=True, target=PREVIOUS)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                _seed(conn, schema)
                out["before"] = _read(conn, schema)
            _alembic(base, schema, "upgrade", "2026_11_04_0082")
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["after"] = _read(conn, schema)
            out["downgrade"] = _downgrade(base, schema)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["after_downgrade"] = _read(conn, schema)
        finally:
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                conn.execute("SET lock_timeout = '30s'")
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
    finally:
        lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        lock.close()
    yield out


def _both(*names: str) -> list[tuple[str, str]]:
    return sorted((t, n) for t in TENANTS for n in names)


def test_the_seed_reached_every_referencing_table(states: dict) -> None:
    """Control: before 0082 both tenants hold the retired type in every referencing table."""
    before = states["before"]
    assert before["head"] == PREVIOUS
    for key in ("card_types", "placements", "recipe_items", "wip_limits"):
        assert before[key] == _both(RETIRED, KEPT), key


def test_upgrade_deletes_the_retired_type_and_every_reference_in_every_tenant(
    states: dict,
) -> None:
    after = states["after"]
    assert after["head"] == "2026_11_04_0082"
    for key in ("card_types", "placements", "recipe_items", "wip_limits"):
        assert after[key] == _both(KEPT), key


def test_downgrade_is_refused_and_changes_nothing(states: dict) -> None:
    proc: subprocess.CompletedProcess[str] = states["downgrade"]
    assert proc.returncode != 0
    assert "forward-only" in proc.stderr
    assert states["after_downgrade"] == states["after"]
