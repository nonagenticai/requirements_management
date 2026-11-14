"""Migration 0080 on the real alembic chain: the append-only ``pm_card_ledger`` (R350(1)).

Upgrade creates the table with its two CHECKs and no FK (keyed by tenant + card only), the
``(tenant_id, planka_card_id, created_at, id)`` index and the strict ``tenant_isolation``
policy; downgrade to 0079 drops it; upgrade again restores it. Each state is read from the
catalogue, and the head state is exercised with real INSERTs. Same private-schema machinery as
``test_migration_0078``.
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
    BEFORE_FORWARD_ONLY,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
)

TENANT = T("migration-0080")
SOURCE = json.dumps({"list": "DOING", "node": "n", "execution_id": "1"})


def _state(conn: psycopg.Connection, schema: str) -> dict:
    cons = dict(
        conn.execute(
            "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = %s AND t.relname = 'pm_card_ledger' AND c.contype IN ('c', 'f')",
            (schema,),
        ).fetchall()
    )
    indexes = dict(
        conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s "
            "AND tablename = 'pm_card_ledger' AND indexname LIKE 'ix_%%'",
            (schema,),
        ).fetchall()
    )
    rls = conn.execute(
        "SELECT c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = 'pm_card_ledger'",
        (schema,),
    ).fetchone()
    policies = dict(
        conn.execute(
            "SELECT policyname, qual FROM pg_policies WHERE schemaname = %s "
            "AND tablename = 'pm_card_ledger'",
            (schema,),
        ).fetchall()
    )
    return {
        "constraints": cons,
        "indexes": indexes,
        "rls": None if rls is None else rls[0],
        "policies": policies,
    }


def _insert(conn: psycopg.Connection, kind: str, score, accepted, reason=None) -> str:
    """'' on success, else the violated constraint's name."""
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pm_card_ledger (id, tenant_id, planka_card_id, kind, key, value, "
                "source, laya_score, accepted, reason) "
                "VALUES (%s, %s, 'c1', %s, 'k', '1', %s, %s, %s, %s)",
                (uuid.uuid4(), TENANT, kind, SOURCE, score, accepted, reason),
            )
    except psycopg.errors.IntegrityError as e:
        return e.diag.constraint_name or "?"
    return ""


def _inserts(conn: psycopg.Connection, schema: str) -> dict:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    return {
        "fact": _insert(conn, "fact", None, None),
        "claim_scored": _insert(conn, "claim", 0.8, True),
        "claim_bare": _insert(conn, "claim", None, None),
        "fact_scored": _insert(conn, "fact", 0.8, None),
        "fact_accepted": _insert(conn, "fact", None, False),
        "fact_reason": _insert(conn, "fact", None, None, "because"),
        "claim_reasoned": _insert(conn, "claim", 0.4, False, "because"),
        "bogus_kind": _insert(conn, "rumour", None, None),
    }


@pytest.fixture(scope="module")
def states() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m80{tag}", f"rls_hide_m80{tag}"
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
                out["up"] = _state(conn, schema)
                out["inserts"] = _inserts(conn, schema)
            _alembic(base, schema, "downgrade", "2026_11_03_0079")
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
    cons = state["constraints"]
    assert {"ck_pm_card_ledger_kind", "ck_pm_card_ledger_laya_claim_only"} <= set(cons)
    assert "'fact'" in cons["ck_pm_card_ledger_kind"]
    assert "'claim'" in cons["ck_pm_card_ledger_kind"]
    fks = [d for d in cons.values() if d.startswith("FOREIGN KEY")]
    assert fks == [], cons
    idx = state["indexes"]["ix_pm_card_ledger_tenant_card_created"]
    assert "(tenant_id, planka_card_id, created_at, id)" in idx
    assert state["rls"] is True
    assert set(state["policies"]) == {"tenant_isolation"}
    assert "current_setting('app.current_tenant'" in state["policies"]["tenant_isolation"]


def test_upgrade_creates_the_ledger(states: dict) -> None:
    _assert_head(states["up"])


def test_head_rejects_laya_fields_on_a_fact_and_unknown_kinds(states: dict) -> None:
    assert states["inserts"] == {
        "fact": "",
        "claim_scored": "",
        "claim_bare": "",
        "fact_scored": "ck_pm_card_ledger_laya_claim_only",
        "fact_accepted": "ck_pm_card_ledger_laya_claim_only",
        "fact_reason": "ck_pm_card_ledger_laya_claim_only",
        "claim_reasoned": "",
        "bogus_kind": "ck_pm_card_ledger_kind",
    }


def test_downgrade_drops_the_table(states: dict) -> None:
    down = states["down"]
    assert down == {"constraints": {}, "indexes": {}, "rls": None, "policies": {}}


def test_upgrade_again_is_head_state(states: dict) -> None:
    _assert_head(states["again"])
