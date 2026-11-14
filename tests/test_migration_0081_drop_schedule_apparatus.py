"""Migration 0081 (R352): the schedule apparatus is dropped, forward-only.

Runs the real alembic chain into a private schema (same machinery and advisory lock as
``test_rls_db_state_pinned.py``): upgrade to ``BEFORE_FORWARD_ONLY``, seed an activity, a
buffer, a schedule snapshot, a PM dependency edge, an edge with one activity end, and a
governance edge; upgrade to head; read what is left; then try to downgrade.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from tests.tenants import T
from tests.test_migration_0069_modeling_models import _alembic
from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    BEFORE_FORWARD_ONLY,
    REPO,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
    _with_search_path,
)

TENANT = T("migration-0081")
DROPPED = ("pm_activities", "pm_buffers", "pm_schedule_snapshots")
A1, A2 = uuid.uuid4(), uuid.uuid4()
R_DEP, R_HALF, R_GOV = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _seed(conn: psycopg.Connection, schema: str) -> None:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    for aid in (A1, A2):
        conn.execute(
            "INSERT INTO pm_activities (id, slug, title, tenant_id, tenant_name, subject_id, "
            "visibility, project_id, is_milestone, is_summary, percent_complete, "
            "earned_value_method, status, version, version_status) VALUES "
            "(%s, %s, %s, %s, 'n', 'alice', 'tenant', 'p1', false, false, 0, "
            "'percent_complete', 'not_started', '1.0.0', 'DRAFT')",
            (aid, f"a-{aid}", f"a-{aid}", TENANT),
        )
    conn.execute(
        "INSERT INTO pm_buffers (id, slug, tenant_id, tenant_name, subject_id, visibility, "
        "project_id, buffer_type, chain_activity_ids, size_days, consumed_days, "
        "penetration_pct, status) VALUES (%s, 'b', %s, 'n', 'alice', 'tenant', 'p1', "
        "'project_buffer', '[]', 1, 0, 0, 'green')",
        (uuid.uuid4(), TENANT),
    )
    conn.execute(
        "INSERT INTO pm_schedule_snapshots (id, tenant_id, tenant_name, subject_id, visibility, "
        "project_id, snapshot_date, snapshot_type, activity_data, evm_data) VALUES "
        "(%s, %s, 'n', 'alice', 'tenant', 'p1', now(), 'baseline', '{}', '{}')",
        (uuid.uuid4(), TENANT),
    )
    for rid, stype, sid, ttype, tid, rtype in (
        (R_DEP, "pm_activities", A1, "pm_activities", A2, "finish_to_start"),
        (R_HALF, "requirements", uuid.uuid4(), "pm_activities", A1, "traceability"),
        (R_GOV, "requirements", uuid.uuid4(), "requirements", uuid.uuid4(), "traceability"),
    ):
        conn.execute(
            "INSERT INTO architecture_relationships (id, slug, tenant_id, tenant_name, subject_id, "
            "visibility, project_id, source_entity_type, source_id, target_entity_type, target_id, "
            "relationship_type) VALUES (%s, %s, %s, 'n', 'alice', 'tenant', NULL, %s, %s, %s, %s, %s)",
            (rid, f"r-{rid}", TENANT, stype, sid, ttype, tid, rtype),
        )


def _read(conn: psycopg.Connection, schema: str) -> dict:
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s AND tablename = ANY(%s)",
            (schema, list(DROPPED)),
        ).fetchall()
    }
    policies = {
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_policies WHERE schemaname = %s AND tablename = ANY(%s)",
            (schema, list(DROPPED)),
        ).fetchall()
    }
    risk_cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'pm_risks'",
            (schema,),
        ).fetchall()
    }
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    rels = {
        r[0]
        for r in conn.execute(
            "SELECT id FROM architecture_relationships WHERE tenant_id = %s", (TENANT,)
        ).fetchall()
    }
    head = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return {
        "tables": tables,
        "policies": policies,
        "rels": rels,
        "risk_has_activity_ids": "related_activity_ids" in risk_cols,
        "head": head[0],
    }


def _downgrade(base: str, schema: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DATABASE_URL": _with_search_path(base, schema),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO / "src"), os.getenv("PYTHONPATH")])),
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", BEFORE_FORWARD_ONLY],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture(scope="module")
def states() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m81{tag}", f"rls_hide_m81{tag}"
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
            _run_and_read(base, schema, hide, keep=True, target=BEFORE_FORWARD_ONLY)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                _seed(conn, schema)
                out["before"] = _read(conn, schema)
            _alembic(base, schema, "upgrade", "2026_11_04_0081")
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


def test_the_seed_reached_every_dropped_table(states: dict) -> None:
    """Control: before 0081 the tables, the activity policies and all three edges exist.

    Only ``pm_activities`` is asserted to carry policies: ``2026_07_02_0030`` takes its
    TABLE LIST from live ``Base.metadata``, so on a fresh chain it no longer covers the
    buffer and snapshot tables once their models are gone (production has them, and the
    drop removes them either way).
    """
    assert states["before"]["tables"] == set(DROPPED)
    assert states["before"]["risk_has_activity_ids"]
    assert "pm_activities" in states["before"]["policies"]
    assert states["before"]["rels"] == {R_DEP, R_HALF, R_GOV}


def test_upgrade_drops_the_tables_and_their_policies(states: dict) -> None:
    assert states["after"]["head"] == "2026_11_04_0081"
    assert states["after"]["tables"] == set()
    assert states["after"]["policies"] == set()
    assert not states["after"]["risk_has_activity_ids"]


def test_upgrade_deletes_every_edge_with_an_activity_end_and_keeps_the_rest(states: dict) -> None:
    assert states["after"]["rels"] == {R_GOV}


def test_downgrade_is_refused_and_changes_nothing(states: dict) -> None:
    proc = states["downgrade"]
    assert proc.returncode != 0
    assert "forward-only" in proc.stderr
    assert states["after_downgrade"] == states["after"]
