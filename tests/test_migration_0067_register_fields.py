"""Migration 0067 (REG-1) ships the register columns the models declare.

The rest of the suite builds its schema with ``Base.metadata.create_all``, so it exercises the
MODELS and never the migration. This module runs the real ``alembic upgrade head`` chain into a
private schema (same machinery and advisory lock as ``test_migration_0061_planka_card_id.py``)
and asserts, from what Postgres reports, that every register column exists with the model's
type and nullability, that ``raised_at`` defaults to ``now()``, and that
``ck_decisions_decided_by_kind`` is the model's CHECK.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import CheckConstraint, DateTime, String

from models.decisions import Decision
from models.pm import Risk
from models.requirements import Requirement
from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
)

COLUMNS = {
    "pm_risks": ("phase", "card_id", "raised_at", "due_at", "last_reviewed_at"),
    "decisions": ("phase", "card_id", "raised_at", "decided_by_kind", "decided_at"),
    "requirements": ("phase", "card_id", "raised_at", "status", "owner"),
}
MODELS = {"pm_risks": Risk, "decisions": Decision, "requirements": Requirement}

COLUMNS_SQL = """
SELECT table_name, column_name, data_type, character_maximum_length, is_nullable,
       column_default
FROM information_schema.columns
WHERE table_schema = %s AND table_name = ANY(%s)
"""
CHECK_SQL = """
SELECT pg_get_constraintdef(con.oid)
FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace
WHERE n.nspname = %s AND con.conname = 'ck_decisions_decided_by_kind'
"""


@pytest.fixture(scope="module")
def migrated() -> Iterator[dict]:
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
            state = _run_and_read(base, schema, hide, keep=True)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                rows = conn.execute(COLUMNS_SQL, (schema, list(COLUMNS))).fetchall()
                check = conn.execute(CHECK_SQL, (schema,)).fetchone()
            cols = {(r[0], r[1]): r[2:] for r in rows}
            yield {"head": state["_head"], "cols": cols, "check": check}
        finally:
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                conn.execute("SET lock_timeout = '30s'")
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
    finally:
        lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        lock.close()


def test_chain_reached_0067(migrated: dict) -> None:
    assert migrated["head"] is not None
    assert migrated["head"] >= "2026_10_27_0067", migrated["head"]


@pytest.mark.parametrize(("table", "column"), [(t, c) for t, cols in COLUMNS.items() for c in cols])
def test_column_matches_the_model(migrated: dict, table: str, column: str) -> None:
    got = migrated["cols"].get((table, column))
    assert got is not None, f"alembic upgrade head did not add {table}.{column}"
    data_type, length, nullable, default = got
    model_col = MODELS[table].__table__.c[column]
    assert nullable == "YES"
    assert model_col.nullable is True
    if isinstance(model_col.type, DateTime):
        assert data_type == "timestamp with time zone"
    else:
        assert isinstance(model_col.type, String)
        assert (data_type, length) == ("character varying", model_col.type.length)
    if column == "raised_at":
        assert default == "now()"


def test_decided_by_kind_check_matches_the_model(migrated: dict) -> None:
    assert migrated["check"] is not None, "ck_decisions_decided_by_kind missing"
    (definition,) = migrated["check"]
    for kind in ("human", "agent", "rule"):
        assert f"'{kind}'" in definition, definition
    names = {c.name for c in Decision.__table__.constraints if isinstance(c, CheckConstraint)}
    assert "ck_decisions_decided_by_kind" in names
