"""Migration 0062 (R311) ships the ``pm_coach_actions`` table the model declares.

The rest of the suite builds its schema with ``Base.metadata.create_all``, so it exercises
the MODEL and never the migration. This module runs the real ``alembic upgrade head`` chain
into a private schema (same machinery and advisory lock as ``test_migration_0061_*``) and
asserts, from what Postgres itself reports, that the four indexes of kanban-coach-errors.md
§4.4 exist with exactly the model's columns and uniqueness, that RLS is enabled, and that
the column set equals the model's.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import Index

from models.pm import CoachAction
from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
)

TABLE = "pm_coach_actions"

INDEX_SQL = """
SELECT i.relname, ix.indisunique,
       array(SELECT a.attname FROM unnest(ix.indkey) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = k.attnum
             ORDER BY k.ord) AS cols
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = %s AND t.relname = %s AND NOT ix.indisprimary
"""

COLUMNS_SQL = """
SELECT column_name, is_nullable FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s
"""

RLS_SQL = """
SELECT c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""


def _drop_schema(base: str, schema: str) -> None:
    with psycopg.connect(_libpq(base), autocommit=True) as conn:
        conn.execute("SET lock_timeout = '30s'")
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(scope="module")
def migrated() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m62{tag}", f"rls_hide_m62{tag}"
    base = _base_url()
    lock = psycopg.connect(_libpq(base), autocommit=True)
    lock.execute("SET lock_timeout = '15min'")  # test-only private connection
    lock.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            _reclaim_leftovers(conn)
        try:
            state = _run_and_read(base, schema, hide, keep=True)  # public restored here
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                indexes = {
                    name: (unique, list(cols))
                    for name, unique, cols in conn.execute(INDEX_SQL, (schema, TABLE)).fetchall()
                }
                columns = dict(conn.execute(COLUMNS_SQL, (schema, TABLE)).fetchall())
                rls = conn.execute(RLS_SQL, (schema, TABLE)).fetchone()
            out = {"head": state["_head"], "indexes": indexes, "columns": columns, "rls": rls}
        finally:
            _drop_schema(base, schema)
    finally:
        lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        lock.close()
    yield out


def test_chain_reached_0062(migrated: dict) -> None:
    assert migrated["head"] is not None
    assert migrated["head"] >= "2026_10_19_0062", migrated["head"]


def test_the_four_indexes_match_the_model(migrated: dict) -> None:
    table = CoachAction.metadata.tables[TABLE]
    model = {
        ix.name: (bool(ix.unique), [c.name for c in ix.columns])
        for ix in table.indexes
        if isinstance(ix, Index)
    }
    assert model == {
        "uq_pm_coach_actions_coach_execution": (True, ["tenant_id", "coach_execution_id"]),
        "uq_pm_coach_actions_incident": (
            True,
            [
                "tenant_id",
                "project_id",
                "target_card_id",
                "entry",
                "root_execution_id",
                "exit_group",
            ],
        ),
        "ix_pm_coach_actions_project_visit": (
            False,
            ["tenant_id", "project_id", "phase_visit_at"],
        ),
        "ix_pm_coach_actions_target_entry": (
            False,
            ["tenant_id", "project_id", "target_card_id", "entry"],
        ),
    }
    assert migrated["indexes"] == model


def test_columns_match_the_model(migrated: dict) -> None:
    table = CoachAction.metadata.tables[TABLE]
    model = {c.name: ("YES" if c.nullable else "NO") for c in table.columns}
    assert migrated["columns"] == model
    for name in ("phase", "phase_visit_at", "applied_remedy", "applied_at"):
        assert migrated["columns"][name] == "YES", name


def test_rls_is_enabled(migrated: dict) -> None:
    assert migrated["rls"] == (True,)
