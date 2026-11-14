"""Migration 0049 (rm#191) ships the user-visibility identity indexes the model declares.

The rest of the suite builds its schema with ``Base.metadata.create_all``, so it
exercises the MODEL's indexes and never the migration — deleting both CREATE
INDEX statements from 0049 left every other test green. This module runs the
real ``alembic upgrade head`` chain into a private schema (same machinery and
advisory lock as ``test_rls_db_state_pinned.py``) and asserts, from what
Postgres itself reports:

* both ``uq_requirements_user_project_title`` and
  ``uq_requirements_user_project_stable_key`` exist, UNIQUE, on the exact key
  columns, with the exact partial predicate;
* each equals the model's declared index — the model's ``CreateIndex`` is
  compiled by SQLAlchemy, built on a column-identical scratch copy of the
  migrated table, and Postgres' canonical ``pg_get_indexdef`` of both compared.

It also pins 0049's duplicate pre-check to the indexes: for each case, the
pre-check raises IF AND ONLY IF building the index on the same rows fails.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from types import ModuleType

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from models.requirements import Requirement
from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    REPO,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
)

MIGRATION = REPO / "alembic" / "versions" / "2026_10_02_0049_requirements_user_identity_indexes.py"
INDEXES = ("uq_requirements_user_project_title", "uq_requirements_user_project_stable_key")

INDEX_SQL = """
SELECT i.indisunique,
       array(SELECT pg_get_indexdef(i.indexrelid, k, true)
             FROM generate_series(1, i.indnkeyatts) k ORDER BY k) AS cols,
       pg_get_expr(i.indpred, i.indrelid, true) AS pred,
       pg_get_indexdef(i.indexrelid) AS def
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0049", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _model_index(name: str):
    table = Requirement.metadata.tables[Requirement.__tablename__]
    matches = [ix for ix in table.indexes if ix.name == name]
    assert len(matches) == 1, f"model declares {len(matches)} indexes named {name}"
    return matches[0]


def _read_index(conn: psycopg.Connection, schema: str, name: str) -> dict | None:
    row = conn.execute(INDEX_SQL, (schema, name)).fetchone()
    if row is None:
        return None  # the tests below fail on it, naming the index
    unique, cols, pred, definition = row
    return {
        "unique": unique,
        "cols": list(cols),
        "pred": pred,
        "def": definition.replace(f"{schema}.", "<schema>."),
    }


def _drop_schema(base: str, schema: str) -> None:
    with psycopg.connect(_libpq(base), autocommit=True) as conn:
        conn.execute("SET lock_timeout = '30s'")
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _read_both(base: str, schema: str, scratch: str) -> dict:
    """Read both 0049 indexes from ``schema`` and the model's compiled twins."""
    out: dict = {"migrated": {}, "model": {}}
    with psycopg.connect(_libpq(base), autocommit=True) as conn:
        for name in INDEXES:
            out["migrated"][name] = _read_index(conn, schema, name)
        # Model side: a column-identical copy of the MIGRATED table (LIKE copies no
        # indexes), then the model's own compiled CreateIndex.
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(scratch)))
        conn.execute(
            sql.SQL("CREATE TABLE {}.requirements (LIKE {}.requirements)").format(
                sql.Identifier(scratch), sql.Identifier(schema)
            )
        )
        conn.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(scratch))
        )  # direct connection, not pgbouncer
        for name in INDEXES:
            ddl = str(CreateIndex(_model_index(name)).compile(dialect=postgresql.dialect()))
            conn.execute(ddl.encode())
            out["model"][name] = _read_index(conn, scratch, name)
    return out


@pytest.fixture(scope="module")
def migrated_indexes() -> Iterator[dict]:
    """Run the real chain (``_run_and_read(..., keep=True)``); read, drop, then release.

    Same advisory lock as the pinned / drift / 0048 fixtures, held until both
    schemas are dropped. ``rls_state_`` prefixes so ``_reclaim_leftovers`` cleans up
    after a killed run.
    """
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m49{tag}", f"rls_hide_m49{tag}"
    scratch = f"rls_state_m49{tag}_model"
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
            out = _read_both(base, schema, scratch)
            out["head"] = state["_head"]
        finally:
            _drop_schema(base, scratch)
            _drop_schema(base, schema)
    finally:
        lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        lock.close()
    yield out


def test_chain_reached_0049(migrated_indexes: dict) -> None:
    assert migrated_indexes["head"] is not None
    assert migrated_indexes["head"] >= "2026_10_02_0049", migrated_indexes["head"]


@pytest.mark.parametrize(
    ("name", "cols", "pred_terms"),
    [
        (
            "uq_requirements_user_project_title",
            ["tenant_id", "subject_id", "project_id", "title"],
            ["project_id IS NOT NULL", "stable_key IS NULL", "'user'"],
        ),
        (
            "uq_requirements_user_project_stable_key",
            ["tenant_id", "subject_id", "project_id", "stable_key"],
            ["stable_key IS NOT NULL", "'user'"],
        ),
    ],
)
def test_migrated_index_definition(
    migrated_indexes: dict, name: str, cols: list[str], pred_terms: list[str]
) -> None:
    got = migrated_indexes["migrated"][name]
    assert got is not None, f"alembic upgrade head did not create {name}"
    assert got["unique"] is True, got
    assert got["cols"] == cols, got
    assert got["pred"], f"{name} is not partial: {got}"
    for term in pred_terms:
        assert term in got["pred"], (term, got["pred"])
    assert "'tenant'" not in got["pred"], got["pred"]


@pytest.mark.parametrize("name", INDEXES)
def test_model_and_migration_agree(migrated_indexes: dict, name: str) -> None:
    """Parity: Postgres' canonical definition of the model's index == the migration's."""
    ix = _model_index(name)
    model = migrated_indexes["model"][name]
    migrated = migrated_indexes["migrated"][name]
    assert model is not None, f"model's {name} did not build"
    assert migrated is not None, f"alembic upgrade head did not create {name}"
    assert ix.unique is True
    assert [c.name for c in ix.columns] == migrated["cols"]
    assert model == migrated, f"model {model}\n!= migrated {migrated}"


# ---- pre-check ⇔ index -------------------------------------------------------

_ROW = {
    "tenant_id": "0195f48b-d000-7000-8000-0000000000aa",
    "subject_id": "alice",
    "project_id": "proj-1",
    "title": "Users can reset their password",
    "stable_key": None,
    "visibility": "user",
}

# (case id, overrides applied to BOTH rows, title index rejects?, stable index rejects?)
_CASES = [
    ("title-dup-in-predicate", {}, True, False),
    ("title-dup-null-project", {"project_id": None}, False, False),
    ("title-dup-tenant-visibility", {"visibility": "tenant"}, False, False),
    ("stable-dup-in-predicate", {"stable_key": "sk-1"}, False, True),
    ("stable-dup-null-project", {"stable_key": "sk-1", "project_id": None}, False, False),
    ("stable-dup-tenant-visibility", {"stable_key": "sk-1", "visibility": "tenant"}, False, False),
]


@pytest.fixture
def precheck_schema() -> Iterator[tuple[psycopg.Connection, str]]:
    """A scratch ``requirements`` with only the columns 0049 reads, no indexes."""
    base = _base_url()
    # NOT the rls_state_ prefix: this fixture does not hold the advisory lock, so a
    # concurrent _reclaim_leftovers must not see (and drop) it.
    schema = f"m0049_precheck_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(_libpq(base), autocommit=True)
    try:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(
            "CREATE TABLE requirements (tenant_id uuid NOT NULL, subject_id varchar(255) "
            "NOT NULL, project_id varchar(100), title varchar(300) NOT NULL, "
            "stable_key varchar(200), visibility varchar(16) NOT NULL)"
        )
        yield conn, schema
    finally:
        conn.close()
        _drop_schema(base, schema)


def _fails(conn: psycopg.Connection, statement: str) -> bool:
    try:
        with conn.transaction():
            conn.execute(statement.encode())
            raise _Rollback
    except _Rollback:
        return False
    except psycopg.Error:
        return True


class _Rollback(Exception):
    pass


@pytest.mark.parametrize(("case", "over", "title_rejects", "stable_rejects"), _CASES)
def test_precheck_raises_iff_an_index_would_reject(
    precheck_schema: tuple[psycopg.Connection, str],
    case: str,
    over: dict,
    title_rejects: bool,
    stable_rejects: bool,
) -> None:
    conn, _ = precheck_schema
    m = _load_migration()
    row = {**_ROW, **over}
    for _ in range(2):
        conn.execute(
            "INSERT INTO requirements (tenant_id, subject_id, project_id, title, stable_key, "
            "visibility) VALUES (%(tenant_id)s, %(subject_id)s, %(project_id)s, %(title)s, "
            "%(stable_key)s, %(visibility)s)",
            row,
        )
    title_fails = _fails(conn, m._index_sql(m._TITLE_NAME, m._TITLE_KEY, m._TITLE_PRED))
    stable_fails = _fails(conn, m._index_sql(m._STABLE_NAME, m._STABLE_KEY, m._STABLE_PRED))
    assert (title_fails, stable_fails) == (title_rejects, stable_rejects), case
    precheck_raises = _fails(conn, m._PRECHECK)
    assert precheck_raises == (title_rejects or stable_rejects), (
        f"{case}: pre-check raised={precheck_raises} but index rejects="
        f"{(title_fails, stable_fails)}"
    )
