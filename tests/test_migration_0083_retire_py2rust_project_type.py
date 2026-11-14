"""Migration 0083 (George 2026-11-07): ``py2rust`` is RETIRED in place, and the three held
``rewrite_*`` card types are backfilled into ``it``'s ``execution`` phase.

Runs the real alembic chain into a private schema (same machinery and advisory lock as
``test_rls_db_state_pinned.py``): upgrade to 0082, seed two tenants with an ``it`` type (setup +
execution, the three rewrite_* card types and a kept one), a live ``py2rust`` type used by a
project, and — for tenant A only — one of the three placements already present; upgrade to 0083;
read what changed; downgrade that one step; read again.

The type is retired, never deleted: its row, its project and every catalogue row survive.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from tests.tenants import T
from tests.test_migration_0069_modeling_models import _alembic
from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
)

PREVIOUS = "2026_11_04_0082"
CURRENT = "2026_11_07_0083"
TENANTS = (T("migration-0083-a"), T("migration-0083-b"))
RETIRED, HOST = "py2rust", "it"
BACKFILLED = ("rewrite_cutover_readiness", "rewrite_parity_qa", "rewrite_slice")
KEPT = "project_phase_start"  # a placement that must survive untouched


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


def _place(conn: psycopg.Connection, pt, ph, ct, tenant: str, entry: bool = False) -> None:
    conn.execute(
        "INSERT INTO pm_project_type_card_types (project_type_id, phase_id, card_type_id, "
        "tenant_id, is_entry) VALUES (%s, %s, %s, %s, %s)",
        (pt, ph, ct, tenant, entry),
    )


def _seed(conn: psycopg.Connection, schema: str) -> None:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    for index, t in enumerate(TENANTS):
        cap = _owned(conn, "pm_capabilities", t, name="development", title="Development")
        cts = {
            n: _owned(conn, "pm_card_types", t, name=n, title=n, capability_id=cap)
            for n in (*BACKFILLED, KEPT)
        }
        setup = _owned(conn, "pm_project_phases", t, name="setup", title="Setup")
        execution = _owned(conn, "pm_project_phases", t, name="execution", title="Execution")
        it = _owned(conn, "pm_project_types", t, name=HOST, title="IT")
        for position, phase in enumerate((setup, execution)):
            conn.execute(
                "INSERT INTO pm_project_type_phases (project_type_id, phase_id, position, "
                "tenant_id) VALUES (%s, %s, %s, %s)",
                (it, phase, position, t),
            )
        _place(conn, it, execution, cts[KEPT], t, entry=True)
        if index == 0:
            # Already placed: ON CONFLICT DO NOTHING must not duplicate it.
            _place(conn, it, execution, cts["rewrite_slice"], t)
        legacy = _owned(conn, "pm_project_types", t, name=RETIRED, title="Python -> Rust rewrite")
        conn.execute(
            "INSERT INTO pm_projects (id, slug, title, tenant_id, tenant_name, subject_id, "
            "visibility, status, version, version_status, origin_kind, project_type_id) "
            "VALUES (%s, 'p', 'p', %s, 'n', 'alice', 'tenant', 'initiating', '1.0.0', "
            "'DRAFT', 'tenant', %s)",
            (uuid.uuid4(), t, legacy),
        )


def _read(conn: psycopg.Connection, schema: str) -> dict:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    column = conn.execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = %s "
        "AND table_name = 'pm_project_types' AND column_name = 'retired_at'",
        (schema,),
    ).fetchone()
    assert column is not None
    has_column = bool(column[0])
    placements = sorted(
        (str(r[0]), r[1], r[2])
        for r in conn.execute(
            "SELECT x.tenant_id, c.name, x.is_entry FROM pm_project_type_card_types x "
            "JOIN pm_card_types c ON c.id = x.card_type_id "
            "JOIN pm_project_types t ON t.id = x.project_type_id "
            "WHERE t.name = %s AND x.tenant_id = ANY(%s)",
            (HOST, list(TENANTS)),
        ).fetchall()
    )
    projects = sorted(
        (str(r[0]), r[1], r[2])
        for r in conn.execute(
            "SELECT p.tenant_id, t.name, t.id FROM pm_projects p "
            "JOIN pm_project_types t ON t.id = p.project_type_id "
            "WHERE p.tenant_id = ANY(%s)",
            (list(TENANTS),),
        ).fetchall()
    )
    retired = None
    if has_column:
        retired = {
            (str(r[0]), r[1]): (r[2] is not None)
            for r in conn.execute(
                "SELECT tenant_id, name, retired_at FROM pm_project_types "
                "WHERE name = ANY(%s) AND tenant_id = ANY(%s)",
                ([RETIRED, HOST], list(TENANTS)),
            ).fetchall()
        }
    version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version is not None
    head = version[0]
    return {
        "head": head,
        "has_column": has_column,
        "retired": retired,
        "placements": placements,
        "projects": projects,
    }


@pytest.fixture(scope="module")
def states() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m83{tag}", f"rls_hide_m83{tag}"
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
            _alembic(base, schema, "upgrade", CURRENT)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["after"] = _read(conn, schema)
            # One step only: 0082 is itself forward-only.
            _alembic(base, schema, "downgrade", PREVIOUS)
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


def _placements(*extra: tuple[str, str, bool]) -> list[tuple[str, str, bool]]:
    """The expected placements, in the reader's sort order: KEPT is the only entry."""
    return sorted([*((t, KEPT, True) for t in TENANTS), *extra])


def test_the_seed_is_the_pre_migration_state(states: dict) -> None:
    """Control: before 0083 the column does not exist, the three are unplaced and the type is
    live and in use."""
    before = states["before"]
    assert before["head"] == PREVIOUS
    assert before["has_column"] is False
    assert before["retired"] is None
    # Tenant A already holds one of the three; the other is empty.
    assert before["placements"] == _placements((TENANTS[0], "rewrite_slice", False))
    assert [p[1] for p in before["projects"]] == [RETIRED, RETIRED]


def test_upgrade_retires_the_type_in_place_and_backfills_the_three(states: dict) -> None:
    after = states["after"]
    assert after["head"] == CURRENT
    assert after["has_column"] is True
    # Retired in place, never deleted: its project row still resolves to it. `it` stays live.
    assert after["retired"] == {
        **{(t, RETIRED): True for t in TENANTS},
        **{(t, HOST): False for t in TENANTS},
    }
    assert [p[1] for p in after["projects"]] == [RETIRED, RETIRED], "the type is never deleted"
    # The three, once each (tenant A's pre-existing rewrite_slice is not duplicated), is_entry
    # false; KEPT keeps its entry flag.
    assert after["placements"] == _placements(*((t, n, False) for t in TENANTS for n in BACKFILLED))


def test_downgrade_drops_the_column_and_keeps_the_placements(states: dict) -> None:
    """0083 is forward-only for the DATA: the column goes, the placements stay (a NOTICE says
    so). Deleting rows that may be in use is not a downgrade."""
    after = states["after_downgrade"]
    assert after["head"] == PREVIOUS
    assert after["has_column"] is False
    assert after["retired"] is None
    assert after["placements"] == states["after"]["placements"]
    assert after["projects"] == states["after"]["projects"]
