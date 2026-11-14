"""Migration 0068 (A12): drop the title identity indexes, add ``idempotency_keys``; and back.

The suite builds its schema with ``Base.metadata.create_all``, so it never exercises the
migration. This runs the real ``alembic upgrade head`` chain into a private schema (same
machinery and advisory lock as ``test_rls_db_state_pinned.py``), reads what Postgres
reports, then runs ``alembic downgrade 2026_10_27_0067`` on the same schema and reads again.
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

TITLE_INDEXES = {
    "uq_arch_elements_tenant_project_title_type",
    "uq_arch_views_tenant_project_title",
    "uq_arch_standards_tenant_project_title",
}
KEPT_INDEXES = {
    "uq_arch_rels_tenant_edge",
    "uq_arch_elements_synthetic_binding_tenant",
    "uq_arch_elements_synthetic_binding_user",
}


def _read(conn: psycopg.Connection, schema: str) -> dict:
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s", (schema,)
        ).fetchall()
    }
    table = conn.execute(
        "SELECT c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = 'idempotency_keys'",
        (schema,),
    ).fetchone()
    policies = [
        r[0]
        for r in conn.execute(
            "SELECT policyname FROM pg_policies WHERE schemaname = %s AND tablename = %s",
            (schema, "idempotency_keys"),
        ).fetchall()
    ]
    unique = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s "
        "AND indexname = 'uq_idempotency_keys_tenant_subject_route_key'",
        (schema,),
    ).fetchone()
    head = conn.execute(
        sql.SQL("SELECT version_num FROM {}.alembic_version").format(sql.Identifier(schema))
    ).fetchone()
    return {
        "indexes": indexes,
        "table_rls": table[0] if table else None,
        "policies": policies,
        "unique": unique[0] if unique else None,
        "head": head[0] if head else None,
    }


def _downgrade(base: str, schema: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DATABASE_URL": _with_search_path(base, schema),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO / "src"), os.getenv("PYTHONPATH")])),
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "2026_10_27_0067"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture(scope="module")
def states() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m67{tag}", f"rls_hide_m67{tag}"
    base = _base_url()
    lock = psycopg.connect(_libpq(base), autocommit=True)
    lock.execute("SET lock_timeout = '15min'")  # test-only private connection
    lock.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            _reclaim_leftovers(conn)
        try:
            _run_and_read(
                base, schema, hide, keep=True, target=BEFORE_FORWARD_ONLY
            )  # public restored here
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                up = _read(conn, schema)
            proc = _downgrade(base, schema)
            assert proc.returncode == 0, (
                f"alembic downgrade failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
            )
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                down = _read(conn, schema)
        finally:
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                conn.execute("SET lock_timeout = '30s'")
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
    finally:
        lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        lock.close()
    yield {"up": up, "down": down}


def test_upgrade_reaches_0068(states: dict) -> None:
    assert states["up"]["head"] is not None and states["up"]["head"] >= "2026_10_27_0068"


def test_upgrade_drops_title_identity_and_keeps_edge_and_synthetic(states: dict) -> None:
    idx = states["up"]["indexes"]
    assert not (TITLE_INDEXES & idx), TITLE_INDEXES & idx
    assert KEPT_INDEXES <= idx, KEPT_INDEXES - idx


def test_upgrade_creates_idempotency_keys_under_rls(states: dict) -> None:
    up = states["up"]
    assert up["table_rls"] is True
    assert up["policies"] == ["tenant_isolation"]
    assert up["unique"] is not None
    assert "ix_idempotency_keys_tenant_created_at" in up["indexes"]
    assert "UNIQUE" in up["unique"] and "(tenant_id, subject_id, route, key)" in up["unique"], up[
        "unique"
    ]


def test_downgrade_restores_indexes_and_drops_table(states: dict) -> None:
    down = states["down"]
    assert down["head"] == "2026_10_27_0067"
    assert TITLE_INDEXES <= down["indexes"], TITLE_INDEXES - down["indexes"]
    assert down["table_rls"] is None  # table gone
    assert down["policies"] == []
