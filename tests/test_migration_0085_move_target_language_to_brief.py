"""Migration 0085 (George 2026-11-07): a rewrite's target language leaves ``it_inputs``.

The language moved to the brief (``ProjectBrief.target_language``), so a stored
``it_inputs`` that still carries it would 422 every later PUT of that project — ``ItInputs``
is ``extra="forbid"`` and the stored value is re-parsed on update.

Runs the real alembic chain into a private schema (same machinery and advisory lock as
``test_rls_db_state_pinned.py``): upgrade to 0084, seed two projects — one whose stored
``it_inputs`` carries ``language``, one that never did — upgrade to 0085 and read the stored
JSONB again; downgrade that one step.

The other keys are untouched, the row without the key is byte-identical, and the downgrade is
a no-op (the value is not recoverable; pre-prod is forward-only). No brief backfill happens
by design: brief versions are append-only.
"""

from __future__ import annotations

import json
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

PREVIOUS = "2026_11_07_0084"
CURRENT = "2026_11_07_0085"
TENANT = T("migration-0085")

WITH_LANGUAGE = {"it_flow": "rewrite", "language": "rust", "test_command": ["pytest", "-q"]}
WITHOUT_LANGUAGE = {"it_flow": "rewrite", "test_command": ["pytest", "-q"]}


def _seed(conn: psycopg.Connection, schema: str) -> dict[str, uuid.UUID]:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    ids: dict[str, uuid.UUID] = {}
    for name, it_inputs in (("rewrite", WITH_LANGUAGE), ("plain", WITHOUT_LANGUAGE)):
        rid = uuid.uuid4()
        ids[name] = rid
        conn.execute(
            "INSERT INTO pm_projects (id, slug, title, tenant_id, tenant_name, subject_id, "
            "visibility, status, version, version_status, origin_kind, it_inputs) "
            "VALUES (%s, %s, %s, %s, 'n', 'alice', 'tenant', 'initiating', '1.0.0', "
            "'DRAFT', 'tenant', %s::jsonb)",
            (rid, name, name, TENANT, json.dumps(it_inputs)),
        )
    return ids


def _read(conn: psycopg.Connection, schema: str, ids: dict[str, uuid.UUID]) -> dict:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    rows = {
        name: conn.execute("SELECT it_inputs FROM pm_projects WHERE id = %s", (rid,)).fetchone()[0]
        for name, rid in ids.items()
    }
    version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version is not None
    return {"head": version[0], "it_inputs": rows}


@pytest.fixture(scope="module")
def states() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m85{tag}", f"rls_hide_m85{tag}"
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
                ids = _seed(conn, schema)
                out["before"] = _read(conn, schema, ids)
            _alembic(base, schema, "upgrade", CURRENT)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["after"] = _read(conn, schema, ids)
            _alembic(base, schema, "downgrade", PREVIOUS)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["after_downgrade"] = _read(conn, schema, ids)
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


def test_the_seed_is_the_pre_migration_state(states: dict) -> None:
    """Control: before 0085 the rewrite project really carries the key."""
    before = states["before"]
    assert before["head"] == PREVIOUS
    assert before["it_inputs"]["rewrite"] == WITH_LANGUAGE
    assert before["it_inputs"]["plain"] == WITHOUT_LANGUAGE


def test_upgrade_strips_the_key_and_nothing_else(states: dict) -> None:
    after = states["after"]
    assert after["head"] == CURRENT
    assert after["it_inputs"]["rewrite"] == {
        "it_flow": "rewrite",
        "test_command": ["pytest", "-q"],
    }
    assert "language" not in after["it_inputs"]["rewrite"]
    # A project that never carried the key is unchanged.
    assert after["it_inputs"]["plain"] == WITHOUT_LANGUAGE


def test_downgrade_is_a_noop(states: dict) -> None:
    after = states["after_downgrade"]
    assert after["head"] == PREVIOUS
    assert after["it_inputs"] == states["after"]["it_inputs"]
