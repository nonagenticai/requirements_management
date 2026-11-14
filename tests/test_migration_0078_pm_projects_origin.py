"""Migration 0078 on the real alembic chain: one ``pm_projects`` row per fr project (R345).

Upgrade adds ``ck_pm_projects_origin_kind``, ``ck_pm_projects_origin_project`` and the partial
unique ``uq_pm_projects_tenant_origin_project`` and drops the title-keyed index; downgrade to
0077 reverses it; upgrade again restores it. Each state is read from the catalogue with the
exact definitions, and the head state is exercised with real INSERTs. Same private-schema
machinery as ``test_migration_0072``.
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
    BEFORE_FORWARD_ONLY,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
)

TENANT = T("migration-0078")
ORIGIN = uuid.UUID("01900000-0000-7000-8000-000000000078")


def _state(conn: psycopg.Connection, schema: str) -> dict:
    checks = dict(
        conn.execute(
            "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = %s AND t.relname = 'pm_projects' AND c.contype = 'c' "
            "AND c.conname LIKE 'ck_pm_projects_origin%%'",
            (schema,),
        ).fetchall()
    )
    indexes = dict(
        conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s "
            "AND tablename = 'pm_projects' AND indexname LIKE 'uq_pm_projects_tenant_origin%%'",
            (schema,),
        ).fetchall()
    )
    return {"checks": checks, "indexes": indexes}


def _insert(conn: psycopg.Connection, slug: str, kind: str, origin, title: str = "p") -> str:
    """'' on success, else the violated constraint's name."""
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pm_projects (id, slug, title, tenant_id, tenant_name, subject_id, "
                "visibility, status, version, version_status, origin_kind, origin_project_id) "
                "VALUES (%s, %s, %s, %s, 'n', 'alice', 'tenant', 'initiating', '1.0.0', "
                "'DRAFT', %s, %s)",
                (uuid.uuid4(), slug, title, TENANT, kind, origin),
            )
    except psycopg.errors.IntegrityError as e:
        return e.diag.constraint_name or "?"
    return ""


def _inserts(conn: psycopg.Connection, schema: str) -> dict:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    return {
        "first": _insert(conn, "a", "project", ORIGIN),
        "dup_other_title": _insert(conn, "b", "project", ORIGIN, title="other"),
        "project_null": _insert(conn, "c", "project", None),
        "tenant_with_origin": _insert(conn, "d", "tenant", uuid.uuid4()),
        "bogus_kind": _insert(conn, "e", "bogus", None),
        "tenant_plain": _insert(conn, "f", "tenant", None),
    }


@pytest.fixture(scope="module")
def states() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m78{tag}", f"rls_hide_m78{tag}"
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
            _run_and_read(base, schema, hide, keep=True, target=BEFORE_FORWARD_ONLY)  # upgrade head
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["up"] = _state(conn, schema)
                out["inserts"] = _inserts(conn, schema)
            _alembic(base, schema, "downgrade", "2026_11_03_0077")
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["down"] = _state(conn, schema)
            _alembic(base, schema, "upgrade", BEFORE_FORWARD_ONLY)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["again"] = _state(conn, schema)
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


def _assert_head(state: dict) -> None:
    assert set(state["checks"]) == {"ck_pm_projects_origin_kind", "ck_pm_projects_origin_project"}
    for v in ("'tenant'", "'project'", "'workflow'"):
        assert v in state["checks"]["ck_pm_projects_origin_kind"]
    assert "origin_project_id IS NOT NULL" in state["checks"]["ck_pm_projects_origin_project"]
    assert set(state["indexes"]) == {"uq_pm_projects_tenant_origin_project"}
    idx = state["indexes"]["uq_pm_projects_tenant_origin_project"]
    assert "UNIQUE INDEX" in idx
    assert "(tenant_id, origin_project_id)" in idx
    assert "WHERE ((origin_kind)::text = 'project'::text)" in idx


def test_upgrade_adds_constraints_and_drops_title_index(states: dict) -> None:
    _assert_head(states["up"])


def test_head_rejects_every_second_or_inconsistent_row(states: dict) -> None:
    assert states["inserts"] == {
        "first": "",
        "dup_other_title": "uq_pm_projects_tenant_origin_project",
        "project_null": "ck_pm_projects_origin_project",
        "tenant_with_origin": "ck_pm_projects_origin_project",
        "bogus_kind": "ck_pm_projects_origin_kind",
        "tenant_plain": "",
    }


def test_downgrade_restores_title_index_and_drops_checks(states: dict) -> None:
    down = states["down"]
    assert down["checks"] == {}
    assert set(down["indexes"]) == {"uq_pm_projects_tenant_origin_project_title"}
    assert (
        "(tenant_id, origin_project_id, title)"
        in (down["indexes"]["uq_pm_projects_tenant_origin_project_title"])
    )


def test_upgrade_again_is_head_state(states: dict) -> None:
    _assert_head(states["again"])
