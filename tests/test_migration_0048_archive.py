"""Migration 0048 archives the stray tenant ad976f5b… and the hand-made repair tables (#193).

Runs the real alembic chain into a private schema (same parking + advisory-lock
discipline as ``test_rls_db_state_pinned.py``, whose helpers it reuses), seeds the
stray tenant's rows plus another tenant's rows and the three repair tables at
0047, then upgrades to 0048 with a private archive schema and downgrades again.

Asserted:
* every stray row is copied to ``<archive>.<table>_ad976f5b`` and deleted, counts equal;
* other tenants' rows are untouched;
* the repair tables moved, data intact;
* a NOBYPASSRLS non-owner role standing in for ``requirements_app`` — holding
  USAGE + SELECT on the source schema exactly like prod's default privileges —
  is refused on every archive table (positive control: it CAN read the source);
* downgrade restores every row and table byte-for-byte and drops the schema;
* a FK child row of ANOTHER tenant that points at a stray parent makes the
  upgrade refuse, changing nothing (negative control for the cascade guard).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    _libpq,
    _reclaim_leftovers,
    _restore_parked,
    _with_search_path,
)

REPO = Path(__file__).resolve().parents[1]
STRAY = "ad976f5b-f3a7-4441-ae87-c2090d038f9c"
OTHER = "0195f48b-d000-7000-8000-0000000000aa"
APP_ROLE = "requirements_app"  # the name 0048 revokes from; NOLOGIN stand-in here
REPAIR_TABLES = (
    "_conveyor_reset_20260922_assets",
    "outage_misclassification_repair_20260824",
    "quality_fabrication_repair_20260817",
)
SEEDED = {  # table -> stray rows seeded
    "principles": 2,
    "principle_conflicts": 3,
    "requirements": 2,
    "decisions": 1,
    "conflict_reconcile_watermark": 1,
}
TABLES = (*SEEDED, *REPAIR_TABLES)


def _alembic(base: str, schema: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DATABASE_URL": _with_search_path(base, schema),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO / "src"), os.getenv("PYTHONPATH")])),
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _ok(proc: subprocess.CompletedProcess[str]) -> str:
    assert proc.returncode == 0, f"alembic failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-4000:]}"
    return proc.stdout + proc.stderr


def _snapshot(conn: psycopg.Connection, schema: str, table: str) -> list[str]:
    """Every row of ``schema.table`` as sorted JSON text (empty if the table is absent)."""
    exists = conn.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s", (schema, table)
    ).fetchone()
    if not exists:
        return []
    rows = conn.execute(
        sql.SQL("SELECT to_jsonb(t)::text FROM {}.{} t ORDER BY 1").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
    ).fetchall()
    return [r[0] for r in rows]


def _count(conn: psycopg.Connection, schema: str, table: str, tenant: str | None = None) -> int:
    q = sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    if tenant is None:
        row = conn.execute(q).fetchone()
    else:
        row = conn.execute(q + sql.SQL(" WHERE tenant_id::text = %s"), (tenant,)).fetchone()
    assert row is not None
    return int(row[0])


def _seed(conn: psycopg.Connection, s: str) -> None:
    ident = sql.Identifier(s)
    pids: dict[str, list[str]] = {STRAY: [], OTHER: []}
    for tenant, n in ((STRAY, SEEDED["principles"]), (OTHER, 1)):
        for i in range(n):
            pid = str(uuid.uuid4())
            pids[tenant].append(pid)
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.principles (id, slug, title, statement, implications, tags, "
                    "active, tenant_id, subject_id, tenant_name) "
                    "VALUES (%s, %s, %s, 'S', '[]', '[]', true, %s, 'subj', 'm48')"
                ).format(ident),
                (pid, f"m48-{tenant[:8]}-{i}", f"P {i} words for tsv", tenant),
            )
    conflicts = [(STRAY, pids[STRAY][i % 2], f"cp-{i}") for i in range(3)] + [
        (OTHER, pids[OTHER][0], "cp-o")
    ]
    for tenant, pid, cp in conflicts:
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.principle_conflicts (id, tenant_id, principle_id, "
                "counterparty_kind, counterparty_id, conflict_type) "
                "VALUES (%s, %s, %s, 'principle', %s, 'contradiction')"
            ).format(ident),
            (str(uuid.uuid4()), tenant, pid, cp),
        )
    for tenant, n in ((STRAY, SEEDED["requirements"]), (OTHER, 1)):
        for i in range(n):
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.requirements (id, slug, title, description, effort, "
                    "tenant_id, subject_id, tenant_name) "
                    "VALUES (%s, %s, 'R', 'D', 1.5, %s, 'subj', 'm48')"
                ).format(ident),
                (str(uuid.uuid4()), f"m48-r-{tenant[:8]}-{i}", tenant),
            )
    for tenant in (STRAY, OTHER):
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.decisions (id, slug, title, status, context, decision, "
                "alternatives, related_principles, tenant_id, subject_id, tenant_name) "
                "VALUES (%s, %s, 'D', 'accepted', 'c', 'd', '[]', '[]', %s, 'subj', 'm48')"
            ).format(ident),
            (str(uuid.uuid4()), f"m48-d-{tenant[:8]}", tenant),
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.conflict_reconcile_watermark (tenant_id, last_seen_updated_at) "
                "VALUES (%s, now())"
            ).format(ident),
            (tenant,),
        )
    # the three hand-made repair tables (shapes are irrelevant; data must survive)
    conn.execute(
        sql.SQL("CREATE TABLE {}.{} (id serial PRIMARY KEY, asset text, payload jsonb)").format(
            ident, sql.Identifier(REPAIR_TABLES[0])
        )
    )
    conn.execute(
        sql.SQL(
            "INSERT INTO {}.{} (asset, payload) SELECT 'a' || g, '{{}}' FROM generate_series(1, 4) g"
        ).format(ident, sql.Identifier(REPAIR_TABLES[0]))
    )
    conn.execute(
        sql.SQL(
            "CREATE TABLE {}.{} (tenant_id uuid, counterparty_id text, last_error text)"
        ).format(ident, sql.Identifier(REPAIR_TABLES[1]))
    )
    conn.execute(
        sql.SQL(
            "INSERT INTO {}.{} SELECT t::uuid, 'cp' || g, 'e' FROM generate_series(1, 3) g, "
            "unnest(ARRAY[%s, %s]) t"
        ).format(ident, sql.Identifier(REPAIR_TABLES[1])),
        (STRAY, OTHER),
    )
    conn.execute(
        sql.SQL("CREATE TABLE {}.{} (id int, note text)").format(
            ident, sql.Identifier(REPAIR_TABLES[2])
        )
    )
    conn.execute(
        sql.SQL("INSERT INTO {}.{} VALUES (1, 'x'), (2, 'y')").format(
            ident, sql.Identifier(REPAIR_TABLES[2])
        )
    )


def _app_role_read(base: str, schema: str, table: str) -> str:
    """Try ``SELECT`` as the NOBYPASSRLS non-owner app stand-in; return 'ok' or the SQLSTATE."""
    with psycopg.connect(_libpq(base)) as conn:
        try:
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(APP_ROLE)))
            conn.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
            )
            return "ok"
        except psycopg.Error as exc:
            return exc.sqlstate or repr(exc)
        finally:
            conn.rollback()


def _run(base: str, schema: str, arch: str, hide: str) -> dict[str, Any]:
    out: dict[str, Any] = {"arch": arch, "schema": schema}
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            for ext in ("vector", "pg_trgm", "unaccent", "pgcrypto"):
                conn.execute(
                    sql.SQL("CREATE EXTENSION IF NOT EXISTS {} SCHEMA public").format(
                        sql.Identifier(ext)
                    )
                )
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

        _ok(_alembic(base, schema, "upgrade", "2026_10_02_0047"))

        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            _seed(conn, schema)
            conn.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                f"'{APP_ROLE}') THEN CREATE ROLE {APP_ROLE} NOLOGIN NOBYPASSRLS; END IF; END $$"
            )
            # what prod's default privileges give requirements_app in the source schema
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(APP_ROLE)
                )
            )
            conn.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(APP_ROLE)
                )
            )
            out["before"] = {t: _snapshot(conn, schema, t) for t in TABLES}
            out["before_stray"] = {t: _count(conn, schema, t, STRAY) for t in SEEDED}

        out["up_log"] = _ok(
            _alembic(base, schema, "-x", f"archive_schema={arch}", "upgrade", "2026_10_02_0048")
        )

        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            out["mid_src"] = {t: _snapshot(conn, schema, t) for t in TABLES}
            out["mid_stray_left"] = {t: _count(conn, schema, t, STRAY) for t in SEEDED}
            out["mid_arch"] = {t: _snapshot(conn, arch, t + "_ad976f5b") for t in SEEDED}
            out["mid_arch_repair"] = {t: _snapshot(conn, arch, t) for t in REPAIR_TABLES}
            out["arch_relations"] = sorted(
                r[0]
                for r in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = %s", (arch,)
                ).fetchall()
            )
            row = conn.execute(
                "SELECT coalesce(nspacl::text, ''), has_schema_privilege(%s, nspname, 'USAGE') "
                "FROM pg_namespace WHERE nspname = %s",
                (APP_ROLE, arch),
            ).fetchone()
            out["arch_acl"], out["arch_usage"] = row if row else (None, None)
        out["app_reads_src"] = _app_role_read(base, schema, "principles")
        out["app_reads_arch"] = {t: _app_role_read(base, arch, t) for t in out["arch_relations"]}

        out["down_log"] = _ok(
            _alembic(base, schema, "-x", f"archive_schema={arch}", "downgrade", "2026_10_02_0047")
        )
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            out["after"] = {t: _snapshot(conn, schema, t) for t in TABLES}
            out["arch_exists_after"] = bool(
                conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (arch,)).fetchone()
            )

            # negative control: another tenant's FK child of a stray principle
            pid = conn.execute(
                sql.SQL(
                    "SELECT id::text FROM {}.principles WHERE tenant_id::text = %s LIMIT 1"
                ).format(sql.Identifier(schema)),
                (STRAY,),
            ).fetchone()
            assert pid is not None
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.principle_conflicts (id, tenant_id, principle_id, "
                    "counterparty_kind, counterparty_id, conflict_type) "
                    "VALUES (%s, %s, %s, 'principle', 'xt', 'contradiction')"
                ).format(sql.Identifier(schema)),
                (str(uuid.uuid4()), OTHER, pid[0]),
            )
            out["guard_before"] = {t: _snapshot(conn, schema, t) for t in TABLES}
        refused = _alembic(
            base, schema, "-x", f"archive_schema={arch}", "upgrade", "2026_10_02_0048"
        )
        out["guard_rc"] = refused.returncode
        out["guard_log"] = refused.stdout + refused.stderr
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            out["guard_after"] = {t: _snapshot(conn, schema, t) for t in TABLES}
            out["guard_arch_exists"] = bool(
                conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (arch,)).fetchone()
            )
        return out
    finally:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            errors: list[str] = []
            for s in (arch, schema):
                try:
                    conn.execute(
                        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(s))
                    )
                except psycopg.Error as exc:
                    errors.append(f"drop {s}: {exc}")
            if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (hide,)).fetchone():
                errors += _restore_parked(conn, hide)
            assert not errors, f"0048 fixture cleanup failed: {errors}"


@pytest.fixture(scope="module")
def m48() -> Iterator[dict[str, Any]]:
    tag = uuid.uuid4().hex[:12]
    # ``rls_state_`` / ``rls_hide_`` prefixes so the pinned test's reclaim also
    # cleans up after a killed run of this module.
    schema, arch, hide = f"rls_state_m48{tag}", f"rls_state_m48{tag}_arch", f"rls_hide_m48{tag}"
    base = os.environ["DATABASE_URL"]
    lock = psycopg.connect(_libpq(base), autocommit=True)
    lock.execute("SET lock_timeout = '15min'")
    lock.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            _reclaim_leftovers(conn)
        yield _run(base, schema, arch, hide)
    finally:
        lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        lock.close()


def test_seed_is_real(m48: dict[str, Any]) -> None:
    """Control: the stray rows exist before the upgrade, and other tenants' rows too."""
    assert m48["before_stray"] == SEEDED
    for t in SEEDED:
        assert len(m48["before"][t]) > SEEDED[t], t


def test_stray_rows_archived_and_deleted_counts_equal(m48: dict[str, Any]) -> None:
    assert m48["mid_stray_left"] == dict.fromkeys(SEEDED, 0)
    for t, n in SEEDED.items():
        assert len(m48["mid_arch"][t]) == n, t
        stray_before = sorted(r for r in m48["before"][t] if STRAY in r)
        assert m48["mid_arch"][t] == stray_before, f"{t}: archive copy != the stray rows"
    assert "discovered stray rows" in m48["up_log"]


def test_other_tenants_rows_untouched(m48: dict[str, Any]) -> None:
    for t in SEEDED:
        others_before = sorted(r for r in m48["before"][t] if STRAY not in r)
        assert others_before, f"{t}: control needs another tenant's rows"
        assert m48["mid_src"][t] == others_before, t


def test_repair_tables_moved_intact(m48: dict[str, Any]) -> None:
    for t in REPAIR_TABLES:
        assert m48["mid_src"][t] == [], f"{t} still in the source schema"
        assert m48["mid_arch_repair"][t] == m48["before"][t], t
        assert m48["before"][t], f"{t}: control needs rows"


def test_app_role_cannot_read_archive(m48: dict[str, Any]) -> None:
    assert m48["app_reads_src"] == "ok", "control: the app stand-in must read the source"
    assert set(m48["arch_relations"]) == {f"{t}_ad976f5b" for t in SEEDED} | set(REPAIR_TABLES)
    assert m48["app_reads_arch"] == dict.fromkeys(m48["arch_relations"], "42501")
    assert m48["arch_usage"] is False
    assert APP_ROLE not in m48["arch_acl"]
    # nothing for PUBLIC: a PUBLIC aclitem has an empty grantee, i.e. starts with "="
    items = m48["arch_acl"].strip("{}").split(",") if m48["arch_acl"] else []
    assert not [i for i in items if i.startswith("=")], m48["arch_acl"]


def test_downgrade_restores_everything_exactly(m48: dict[str, Any]) -> None:
    assert m48["after"] == m48["before"]
    assert m48["arch_exists_after"] is False


def test_foreign_fk_child_makes_upgrade_refuse_and_change_nothing(m48: dict[str, Any]) -> None:
    assert m48["guard_rc"] != 0
    assert "reference stray rows of principles" in m48["guard_log"]
    assert m48["guard_after"] == m48["guard_before"]
    assert m48["guard_arch_exists"] is False
