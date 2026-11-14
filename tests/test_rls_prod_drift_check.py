"""scripts/rls_prod_drift_check.py against a real ``alembic upgrade head`` schema (#194).

Same fixture as ``test_rls_db_state_pinned.py`` (private schema, real migration
chain), but the schema is KEPT for the module so the script can read it. Pins:

* no drift -> exit 0;
* an extra permissive policy -> exit 1 with the diff on stdout;
* a BYPASSRLS role, a table-owning role and a missing role -> flagged;
* a non-alembic table is reported, and is drift only once the app role can read it
  with RLS off;
* the psql (prod) transport sends the identical report SQL, wrapped in
  ``BEGIN READ ONLY`` / ``ROLLBACK``, and never a SET.
"""

from __future__ import annotations

import re
import subprocess
import sys
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from scripts.rls_prod_drift_check import (
    REPORT_SQL,
    STATE_SQL,
    evaluate,
    fetch_report_dsn,
    render_psql_script,
)
from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    REPO,
    _base_url,
    _libpq,
    _run_and_read,
)

SCRIPT = REPO / "scripts" / "rls_prod_drift_check.py"


@pytest.fixture(scope="module")
def migrated_schema() -> Iterator[str]:
    schema = f"rls_drift_{uuid.uuid4().hex[:12]}"
    hide = f"rls_hide_{uuid.uuid4().hex[:12]}"
    base = _base_url()
    lock = psycopg.connect(_libpq(base), autocommit=True)
    lock.execute("SET lock_timeout = '15min'")  # test-only private connection
    lock.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        try:
            _run_and_read(base, schema, hide, keep=True)
        finally:
            lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        yield schema
    finally:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        lock.close()


def _exec(stmt: sql.Composable) -> None:
    with psycopg.connect(_libpq(_base_url()), autocommit=True) as conn:
        conn.execute(stmt)


def _connected_role() -> str:
    """The role the test DSN logs in as. It exists on ANY test DB by construction --
    unlike a hard-coded ``postgres`` (absent in CI, whose DSN user differs: rm#196)."""
    with psycopg.connect(_libpq(_base_url()), autocommit=True) as conn:
        row = conn.execute("SELECT current_user").fetchone()
    assert row is not None
    return str(row[0])


def _run(schema: str, *extra: str) -> subprocess.CompletedProcess[str]:
    """``--app-role <connected role>`` exists on any test DB (the default
    ``requirements_app`` need not); a later ``--app-role`` in ``extra`` overrides it
    (argparse: last wins)."""
    argv = ["--dsn", _libpq(_base_url()), "--schema", schema, "--app-role", _connected_role()]
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv, *extra],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_no_drift_exits_zero(migrated_schema: str) -> None:
    proc = _run(migrated_schema)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NO DRIFT" in proc.stdout
    assert "policies=0" not in proc.stdout  # control: it really read the migrated schema
    gone = _run(migrated_schema, "--app-role", f"nope_{uuid.uuid4().hex[:8]}")
    assert gone.returncode == 1 and "NOT FOUND" in gone.stdout  # a missing app role is drift


def test_extra_permissive_policy_exits_one_with_diff(migrated_schema: str) -> None:
    s = sql.Identifier(migrated_schema)
    _exec(sql.SQL("CREATE POLICY drift_probe ON {}.requirements FOR SELECT USING (true)").format(s))
    try:
        proc = _run(migrated_schema)
    finally:
        _exec(sql.SQL("DROP POLICY drift_probe ON {}.requirements").format(s))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "[policies] IN DB, NOT IN PIN" in proc.stdout
    assert '"policy": "drift_probe"' in proc.stdout and '"permissive": "PERMISSIVE"' in proc.stdout
    assert _run(migrated_schema).returncode == 0  # control: reverted -> clean again


def test_role_attributes_are_asserted(migrated_schema: str) -> None:
    sfx = uuid.uuid4().hex[:8]
    bypass, plain, owner = f"d194_byp_{sfx}", f"d194_ok_{sfx}", f"d194_own_{sfx}"
    s = sql.Identifier(migrated_schema)
    for r, attrs in ((bypass, "BYPASSRLS"), (plain, "NOBYPASSRLS"), (owner, "NOBYPASSRLS")):
        _exec(sql.SQL("CREATE ROLE {} {}").format(sql.Identifier(r), sql.SQL(attrs)))
    _exec(sql.SQL("CREATE TABLE {}.d194_owned (id int)").format(s))
    _exec(sql.SQL("ALTER TABLE {}.d194_owned OWNER TO {}").format(s, sql.Identifier(owner)))
    try:
        ok = _run(migrated_schema, "--roles-only", "--role", plain)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        assert f"role {plain}: rolsuper=False rolbypassrls=False owned_tables=0" in ok.stdout
        byp = _run(migrated_schema, "--roles-only", "--role", bypass)
        assert byp.returncode == 1 and f"role {bypass}: rolbypassrls = true" in byp.stdout
        own = _run(migrated_schema, "--roles-only", "--role", owner)
        assert own.returncode == 1 and "owns 1 table(s)" in own.stdout, own.stdout
        missing = _run(migrated_schema, "--roles-only", "--role", f"nope_{sfx}")
        assert missing.returncode == 1 and "NOT FOUND" in missing.stdout
    finally:
        _exec(sql.SQL("DROP TABLE {}.d194_owned").format(s))
        for r in (bypass, plain, owner):
            _exec(sql.SQL("DROP ROLE {}").format(sql.Identifier(r)))


def test_non_alembic_table_readable_with_rls_off_is_drift(migrated_schema: str) -> None:
    app = f"d194_app_{uuid.uuid4().hex[:8]}"
    s, a = sql.Identifier(migrated_schema), sql.Identifier(app)
    _exec(sql.SQL("CREATE ROLE {} NOBYPASSRLS").format(a))
    _exec(sql.SQL("CREATE TABLE {}.stray_repair_table (id int)").format(s))
    try:
        proc = _run(migrated_schema, "--app-role", app)  # no grant: reported, not drift
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert f"stray_repair_table: rls=False {app}_select=False" in proc.stdout
        _exec(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, a))
        _exec(sql.SQL("GRANT SELECT ON {}.stray_repair_table TO {}").format(s, a))
        proc = _run(migrated_schema, "--app-role", app)
        assert proc.returncode == 1 and "readable with RLS off" in proc.stdout, proc.stdout
    finally:
        _exec(sql.SQL("DROP TABLE {}.stray_repair_table").format(s))
        _exec(sql.SQL("DROP OWNED BY {}").format(a))
        _exec(sql.SQL("DROP ROLE {}").format(a))


def test_psql_transport_sends_the_same_report(migrated_schema: str) -> None:
    """The prod path pipes render_psql_script() into psql. Run that exact text and
    check it yields the same report as the psycopg path."""
    me = _connected_role()
    params = {"s": migrated_schema, "app": me, "roles": [me]}
    script = render_psql_script(params)
    stmts = [x.strip() for x in script.split(";\n") if x.strip()]
    assert len(stmts) == 3 and stmts[0] == "BEGIN READ ONLY" and stmts[2] == "ROLLBACK"
    with psycopg.connect(_libpq(_base_url()), autocommit=True) as conn:
        conn.execute("BEGIN READ ONLY")
        try:
            row = conn.execute(stmts[1].encode()).fetchone()  # no params: sent verbatim
        finally:
            conn.execute("ROLLBACK")
    assert row is not None
    assert row[0] == fetch_report_dsn(_libpq(_base_url()), params)
    assert row[0]["read_only"] == "on"


def test_read_only_guard_refuses_a_writable_transaction() -> None:
    with pytest.raises(RuntimeError, match="not read only"):
        evaluate({"read_only": "off"}, "public", "requirements_app", roles_only=True)


def test_render_refuses_non_identifiers() -> None:
    with pytest.raises(AssertionError):
        render_psql_script({"s": "public'; DROP TABLE x; --", "app": "a", "roles": []})


def test_script_never_issues_a_set() -> None:
    """No SET of any kind in the SQL it sends or in an execute() call."""
    src = SCRIPT.read_text()
    assert 'conn.execute("BEGIN READ ONLY")' in src and 'conn.execute("ROLLBACK")' in src
    assert not re.search(r"execute\(\s*[\"']\s*SET\b", src, re.I)
    for text in (STATE_SQL, REPORT_SQL):
        assert not re.search(r"\bSET\b", text, re.I)
