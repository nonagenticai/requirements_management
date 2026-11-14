"""The RLS state that `alembic upgrade head` PRODUCES equals a reviewed pin.

This is the instrument that does not care HOW the SQL was written. The static
keep-out scan (``test_keepout_no_cross_tenant_read.py``) reads source text and is
the fast early warning; it cannot see SQL built at runtime, base64'd, assembled
in a helper, or issued by an ALTER in an oddly named file. This test runs the
real migration chain into a private schema on the CI Postgres and reads what
Postgres itself reports:

* ``pg_policies`` — every policy (table, name, permissive, cmd, roles, USING,
  WITH CHECK);
* ``pg_class.relrowsecurity`` / ``relforcerowsecurity`` per table;
* SECURITY DEFINER functions and views in the schema (both run as their owner
  and so read past RLS).

It must equal ``tests/fixtures/rls_state_pin.json`` EXACTLY. Any change — a new
policy, a widened predicate, a dropped one, RLS switched off, a definer function
or view — fails until a reviewer regenerates the pin. The pin was taken from the
0046 head and checked equal to production (``requirements_api.public``, read-only,
2026-10-02) for policies, definer functions and views.

User ruling 2026-10-02: the SYSTEM tenant never reads customer data; the only
cross-tenant read is ``system_published_read``.

No CREATE DATABASE (conftest rule): a uniquely named schema on the test DB is
created, migrated into via ``search_path``, read, and dropped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

# ONE instrument: the same collection query runs against prod in the drift check (#194).
from scripts.rls_prod_drift_check import collect_state

REPO = Path(__file__).resolve().parents[1]
PIN = REPO / "tests" / "fixtures" / "rls_state_pin.json"
# The newest revision a migration test may migrate to and then downgrade from:
# ``2026_11_04_0081`` (R352, drop the schedule apparatus) is forward-only and its
# downgrade raises. Bump this when a reversible revision lands below the next
# forward-only one.
BEFORE_FORWARD_ONLY = "2026_11_04_0080"
# Arbitrary constant: pg advisory lock serialising concurrent copies of the fixture.
_ADVISORY_LOCK_KEY = 0x524C5350  # "RLSP"


def _base_url() -> str:
    """conftest exports DATABASE_URL (CI DSN, TEST_DATABASE_URL, or its local default)."""
    return os.environ["DATABASE_URL"]


def _with_search_path(url: str, schema: str) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
    q.append(("options", f"-csearch_path={schema},public"))
    return urlunsplit(parts._replace(query=urlencode(q)))


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _restore_parked(conn: psycopg.Connection, hide: str) -> list[str]:
    """Move every table in ``hide`` back to public, one at a time; return failures."""
    failed: list[str] = []
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename", (hide,)
    ).fetchall()
    for (t,) in rows:
        try:
            conn.execute(
                sql.SQL("ALTER TABLE {}.{} SET SCHEMA public").format(
                    sql.Identifier(hide), sql.Identifier(t)
                )
            )
        except psycopg.Error as exc:  # keep restoring the rest; report at the end
            failed.append(f"{t}: {exc}")
    if not failed:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {}").format(sql.Identifier(hide)))
    return failed


def _reclaim_leftovers(conn: psycopg.Connection) -> None:
    """A killed earlier run (SIGKILL / timeout) can leave ``rls_hide_*`` with public's
    tables parked in it, or a stray ``rls_state_*`` schema. Restore / drop them first;
    conftest only reclaims ``test_gw*``."""
    for (ns,) in conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'rls_hide_%'"
    ).fetchall():
        _restore_parked(conn, ns)
    for (ns,) in conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'rls_state_%'"
    ).fetchall():
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(ns)))


@pytest.fixture(scope="module")
def migrated_state() -> Iterator[dict]:
    """Run the full alembic chain into a private schema; return its RLS state.

    Every step that changes shared state happens inside ``try`` and is undone in
    ``finally`` table-by-table, so a failure part-way never leaves public's tables
    parked; a run killed outright is cleaned up by ``_reclaim_leftovers`` next time.
    """
    schema = f"rls_state_{uuid.uuid4().hex[:12]}"
    hide = f"rls_hide_{uuid.uuid4().hex[:12]}"
    base = _base_url()
    # Under xdist each worker that receives one of this module's tests builds its
    # own copy of this fixture. Serialise them with a session advisory lock held
    # for the whole run, so one copy's _reclaim_leftovers can never drop another
    # copy's live schema, nor park public's tables under it.
    lock = psycopg.connect(_libpq(base), autocommit=True)
    lock.execute("SET lock_timeout = '15min'")
    lock.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            _reclaim_leftovers(conn)
        state = _run_and_read(base, schema, hide)  # restores public BEFORE tests run
        yield state
    finally:
        lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        lock.close()


def _run_and_read(
    base: str, schema: str, hide: str, keep: bool = False, target: str = "head"
) -> dict:
    """Park public, migrate into ``schema`` (to ``target``), read its RLS state, restore.

    A migration test that downgrades must stop BELOW the first forward-only revision
    (``2026_11_04_0081`` raises on downgrade), so it passes its own revision as ``target``.
    """
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            # Extensions live in public (once per DB); create them there up front so
            # the migrations' IF NOT EXISTS never lands one in the private schema.
            for ext in ("vector", "pg_trgm", "unaccent", "pgcrypto"):
                conn.execute(
                    sql.SQL("CREATE EXTENSION IF NOT EXISTS {} SCHEMA public").format(
                        sql.Identifier(ext)
                    )
                )
            # A serial pytest run keeps the ORM tables in ``public`` (conftest
            # create_all). PG's inspector lists every table VISIBLE on search_path,
            # so migration 0030 would see them and police them. Park them for the
            # duration of the migration; ``finally`` restores them one by one.
            moved = [
                r[0]
                for r in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                ).fetchall()
            ]
            if moved:
                conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(hide)))
                for t in moved:
                    conn.execute(
                        sql.SQL("ALTER TABLE public.{} SET SCHEMA {}").format(
                            sql.Identifier(t), sql.Identifier(hide)
                        )
                    )
        env = {
            **os.environ,
            "DATABASE_URL": _with_search_path(base, schema),
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(REPO / "src"), os.getenv("PYTHONPATH")])
            ),
        }
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", target],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert proc.returncode == 0, (
            f"alembic upgrade {target} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
        )
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            head = conn.execute(
                sql.SQL("SELECT version_num FROM {}.alembic_version").format(sql.Identifier(schema))
            ).fetchone()
            state = collect_state(conn, schema)
        state["_head"] = head[0] if head else None
        return state
    finally:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            errors: list[str] = []
            try:
                if not keep:  # keep=True: the caller owns (and drops) the migrated schema
                    conn.execute(
                        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                    )
            except psycopg.Error as exc:
                errors.append(f"drop {schema}: {exc}")
            if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (hide,)).fetchone():
                errors += _restore_parked(conn, hide)
            assert not errors, f"rls_state fixture cleanup failed: {errors}"


def test_migrations_ran_into_the_private_schema(migrated_state: dict) -> None:
    """Control: the chain really ran (non-empty policy set, a head revision)."""
    assert migrated_state["_head"]
    assert len(migrated_state["policies"]) > 0
    assert any(p["policy"] == "system_published_read" for p in migrated_state["policies"])


@pytest.mark.parametrize("key", ["policies", "rls_tables", "security_definer_functions", "views"])
def test_rls_state_equals_the_reviewed_pin(migrated_state: dict, key: str) -> None:
    pin = json.loads(PIN.read_text())
    got = migrated_state[key]
    want = pin[key]
    g = {json.dumps(x, sort_keys=True) for x in got}
    w = {json.dumps(x, sort_keys=True) for x in want}
    assert got == want, (
        f"RLS {key} drifted from tests/fixtures/rls_state_pin.json "
        f"(review: no tenant may read another tenant's rows; system never reads customers).\n"
        f"  NEW/CHANGED: {sorted(g - w)[:10]}\n  MISSING: {sorted(w - g)[:10]}"
    )


def test_every_tenant_table_has_rls_enabled(migrated_state: dict) -> None:
    """Belt and braces: the pin's RLS-off set is exactly the reviewed non-tenant tables."""
    off = sorted(t["table"] for t in migrated_state["rls_tables"] if not t["rls"])
    assert off == [
        "alembic_version",
        "entity_version_history",
        "tenant_id_crosswalk",
    ], off
