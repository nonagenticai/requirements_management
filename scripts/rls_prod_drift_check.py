#!/usr/bin/env python3
"""Read-only RLS drift check: does a LIVE database still match the reviewed CI pin?

Manual runbook script (issue #194; runbook: docs/DEPLOYMENT.md, "RLS drift check").

ONE instrument: ``STATE_SQL`` below is the query that ``tests/test_rls_db_state_pinned.py``
runs (via ``collect_state``) against ``alembic upgrade head`` in CI, and that this
script embeds in ``REPORT_SQL`` for the live database. Both are compared with
``tests/fixtures/rls_state_pin.json``.

Checks (exit 0 = no drift, 1 = drift, 2 = could not run):

1. RLS state: policies, RLS/FORCE flags, definer (prosecdef) functions and views in
   ``--schema`` (default ``public``) equal the pin. Extension-owned functions and
   views are excluded. ACLs are printed as INFO only (prod default privileges
   grant to the app role, so they legitimately differ from CI).
2. Role attributes, per ``--role`` (repeatable): ``rolsuper`` and ``rolbypassrls``
   both false, and the role owns zero tables in this database. FORCE RLS is off
   everywhere, so isolation relies on the app role being a non-owner without
   rolbypassrls. A role that does not exist is drift.
3. Non-alembic tables in the schema (tables the pin does not list, i.e. not
   produced by ``alembic upgrade head``): listed with their RLS flag and whether
   ``--app-role`` has SELECT. Drift ONLY when the app role can SELECT one with RLS
   off; merely existing is reported (their drop is a reviewed migration, #193).

Safety: everything runs inside ``BEGIN READ ONLY`` ... ``ROLLBACK`` and the script
asserts ``transaction_read_only = on`` from inside that transaction. It NEVER issues
a SET of any kind (a session-level SET through pgbouncer's transaction pooling is
handed to the next client). Connect DIRECT to Postgres, never through pgbouncer.

Two transports, same SQL:

* ``--dsn`` / ``RLS_DRIFT_DSN``: psycopg (CI tests, or any DSN you have).
* ``--ssh admin@db-host.example.com --db requirements_api``: pipes the SQL into
  ``sudo -u postgres psql`` on the database host (peer auth over the local
  socket; no password leaves the host). This is the prod path.

``--roles-only`` skips checks 1 and 3 (for procedure_management, artifact_registry).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import LiteralString

import psycopg

REPO = Path(__file__).resolve().parents[1]
PIN = REPO / "tests" / "fixtures" / "rls_state_pin.json"
SCHEMA_TOKEN = "<schema>"
PIN_KEYS = ("policies", "rls_tables", "security_definer_functions", "views")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The pinned instrument. Parameter: %(s)s = schema name.
STATE_SQL: LiteralString = """
SELECT json_build_object(
 'policies', (SELECT coalesce(json_agg(json_build_object(
        'table', tablename, 'policy', policyname, 'permissive', permissive, 'cmd', cmd,
        'roles', roles::text, 'qual', qual, 'with_check', with_check)
      ORDER BY tablename, policyname), '[]') FROM pg_policies WHERE schemaname = %(s)s),
 'rls_tables', (SELECT coalesce(json_agg(json_build_object(
        'table', c.relname, 'rls', c.relrowsecurity, 'force', c.relforcerowsecurity,
        'acl', coalesce(c.relacl::text, ''))
      ORDER BY c.relname), '[]')
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = %(s)s AND c.relkind IN ('r', 'p')),
 'security_definer_functions', (SELECT coalesce(json_agg(p.proname ORDER BY p.proname), '[]')
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname = %(s)s AND p.prosecdef
        AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')),
 'views', (SELECT coalesce(json_agg(c.relname ORDER BY c.relname), '[]')
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = %(s)s AND c.relkind IN ('v', 'm')
        AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e'))
)
"""

# Everything the drift check reads, as ONE statement (so both transports run the
# identical text). Parameters: %(s)s schema, %(app)s app role, %(roles)s text[].
REPORT_SQL: LiteralString = (
    """
SELECT json_build_object(
 'read_only', current_setting('transaction_read_only'),
 'database', current_database(),
 'state', ("""
    + STATE_SQL
    + """),
 'app_role_exists', EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %(app)s),
 'app_select', (SELECT coalesce(json_object_agg(c.relname,
        CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %(app)s)
             THEN has_table_privilege(%(app)s, c.oid, 'SELECT') END), '{}')
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = %(s)s AND c.relkind IN ('r', 'p')),
 'roles', (SELECT coalesce(json_agg(json_build_object(
        'role', want.rolname,
        'exists', r.oid IS NOT NULL,
        'rolsuper', r.rolsuper,
        'rolbypassrls', r.rolbypassrls,
        'owned_tables', (SELECT coalesce(json_agg(n.nspname || '.' || c.relname
                                                  ORDER BY n.nspname, c.relname), '[]')
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relowner = r.oid AND c.relkind IN ('r', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')))
      ORDER BY want.rolname), '[]')
      FROM unnest(%(roles)s::text[]) AS want(rolname)
      LEFT JOIN pg_roles r ON r.rolname = want.rolname)
)
"""
)


def normalise(state: dict, schema: str) -> dict:
    """Replace a private schema's qualifier with ``<schema>`` (no-op for ``public``,
    whose objects are unqualified on the default search_path, exactly as in the pin)."""
    if schema == "public":
        return state
    text = json.dumps(state, sort_keys=True).replace(f'"{schema}.', f'"{SCHEMA_TOKEN}.')
    text = text.replace(f"{schema}.", f"{SCHEMA_TOKEN}.")
    return json.loads(text)


def collect_state(conn: psycopg.Connection, schema: str) -> dict:
    """Run STATE_SQL for ``schema`` and normalise it (the CI test's collection path)."""
    row = conn.execute(STATE_SQL, {"s": schema}).fetchone()
    assert row is not None, "state query returned no row"
    return normalise(row[0], schema)


def _jset(items: list) -> dict[str, object]:
    return {json.dumps(x, sort_keys=True): x for x in items}


def diff_state(got: dict, pin: dict) -> tuple[list[str], list[str], list[dict]]:
    """Return (drift_lines, acl_info_lines, non_alembic_tables)."""
    drift: list[str] = []
    info: list[str] = []
    owned = {t["table"] for t in pin["rls_tables"]}  # what alembic upgrade head creates
    extra = [t for t in got["rls_tables"] if t["table"] not in owned]
    for key in PIN_KEYS:
        g = got[key]
        w = pin[key]
        if key == "rls_tables":  # ACLs are info, never drift; extras reported separately
            g = [t for t in g if t["table"] in owned]
            info = [f"{t['table']}: {t['acl']}" for t in g if t.get("acl")]
            g = [{k: v for k, v in t.items() if k != "acl"} for t in g]
            w = [{k: v for k, v in t.items() if k != "acl"} for t in w]
        gj, wj = _jset(g), _jset(w)
        drift += [f"[{key}] IN DB, NOT IN PIN: {k}" for k in sorted(set(gj) - set(wj))]
        drift += [f"[{key}] IN PIN, NOT IN DB: {k}" for k in sorted(set(wj) - set(gj))]
    return drift, info, extra


def check_roles(roles: list[dict]) -> tuple[list[str], list[str]]:
    """Return (drift_lines, report_lines) for the ``roles`` part of the report."""
    drift: list[str] = []
    out: list[str] = []
    for r in roles:
        name = r["role"]
        if not r["exists"]:
            drift.append(f"role {name}: NOT FOUND in this cluster")
            out.append(f"== role {name}: NOT FOUND")
            continue
        owned = r["owned_tables"]
        out.append(
            f"== role {name}: rolsuper={r['rolsuper']} rolbypassrls={r['rolbypassrls']} "
            f"owned_tables={len(owned)}"
        )
        if r["rolsuper"]:
            drift.append(f"role {name}: rolsuper = true")
        if r["rolbypassrls"]:
            drift.append(f"role {name}: rolbypassrls = true")
        if owned:
            drift.append(f"role {name}: owns {len(owned)} table(s): {owned[:10]}")
    return drift, out


def evaluate(
    report: dict, schema: str, app_role: str, roles_only: bool
) -> tuple[list[str], list[str]]:
    """Turn the raw REPORT_SQL result into (drift_lines, report_lines)."""
    if report.get("read_only") != "on":
        raise RuntimeError(f"transaction is not read only: {report.get('read_only')!r}")
    drift: list[str] = []
    out: list[str] = [f"== database {report['database']}, schema {schema}"]
    if not roles_only:
        pin = json.loads(PIN.read_text())
        got = normalise(report["state"], schema)
        d, info, extra = diff_state(got, pin)
        drift += d
        out.append(
            f"== RLS state: policies={len(got['policies'])} tables={len(got['rls_tables'])} "
            f"definer_fns={len(got['security_definer_functions'])} views={len(got['views'])} "
            f"| pin: policies={len(pin['policies'])} tables={len(pin['rls_tables'])}"
        )
        out.append("== ACLs (INFO only, never drift):")
        out += [f"   {i}" for i in info] or ["   (none)"]
        out.append(f"== Non-alembic tables in {schema}: {len(extra)}")
        if not report["app_role_exists"]:
            drift.append(f"app role {app_role}: NOT FOUND (cannot judge non-alembic tables)")
        for t in extra:
            sel = report["app_select"].get(t["table"])
            bad = bool(sel) and not t["rls"]
            out.append(
                f"   {t['table']}: rls={t['rls']} {app_role}_select={sel}"
                + ("  <-- DRIFT: readable with RLS off" if bad else "")
            )
            if bad:
                drift.append(f"non-alembic table {t['table']} readable by {app_role}, RLS off")
    d, lines = check_roles(report["roles"])
    drift += d
    out += lines
    return drift, out


def _params(schema: str, app_role: str, roles: list[str]) -> dict[str, object]:
    return {"s": schema, "app": app_role, "roles": roles}


def fetch_report_dsn(dsn: str, params: Mapping[str, object]) -> dict:
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("BEGIN READ ONLY")  # transaction-scoped; never a SET
        try:
            row = conn.execute(REPORT_SQL, params).fetchone()
        finally:
            conn.execute("ROLLBACK")
    assert row is not None, "report query returned no row"
    return row[0]


def render_psql_script(params: Mapping[str, object]) -> str:
    """The exact text piped into psql. Values are validated identifiers, so a plain
    quoted literal is safe; the statements are BEGIN READ ONLY, the report, ROLLBACK."""

    def lit(v: object) -> str:
        if isinstance(v, list):
            return "ARRAY[" + ",".join(lit(x) for x in v) + "]::text[]" if v else "'{}'::text[]"
        assert isinstance(v, str) and _IDENT.match(v), f"refusing non-identifier {v!r}"
        return f"'{v}'"

    body = re.sub(r"%\((\w+)\)s", lambda m: lit(params[m.group(1)]), REPORT_SQL)
    return f"BEGIN READ ONLY;\n{body.strip()};\nROLLBACK;\n"


def fetch_report_ssh(target: str, key: str | None, db: str, params: Mapping[str, object]) -> dict:
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if key:
        ssh += ["-i", os.path.expanduser(key)]
    remote = "sudo -u postgres psql -X -q -At -v ON_ERROR_STOP=1 -d " + shlex.quote(db)
    proc = subprocess.run(
        [*ssh, target, remote],
        input=render_psql_script(params),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql over ssh failed ({proc.returncode}): {proc.stderr.strip()}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("psql over ssh returned no output")
    return json.loads(lines[-1])


def _ident(v: str) -> str:
    if not _IDENT.match(v):
        raise argparse.ArgumentTypeError(f"not a plain identifier: {v!r}")
    return v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dsn", default=os.environ.get("RLS_DRIFT_DSN"), help="or env RLS_DRIFT_DSN")
    ap.add_argument("--ssh", help="ssh target of the DB host, e.g. admin@db-host.example.com")
    ap.add_argument("--ssh-key", default=None, help="ssh identity file (e.g. ~/.ssh/db-key)")
    ap.add_argument("--db", type=_ident, help="database name (with --ssh)")
    ap.add_argument("--schema", type=_ident, default="public")
    ap.add_argument("--role", type=_ident, action="append", default=[], help="repeatable")
    ap.add_argument("--app-role", type=_ident, default="requirements_app")
    ap.add_argument("--roles-only", action="store_true", help="skip pin + non-alembic checks")
    args = ap.parse_args(argv)
    params = _params(args.schema, args.app_role, args.role)
    try:
        if args.ssh:
            if not args.db:
                ap.error("--ssh needs --db")
            report = fetch_report_ssh(args.ssh, args.ssh_key, args.db, params)
        elif args.dsn:
            report = fetch_report_dsn(args.dsn, params)
        else:
            ap.error("need --dsn (or RLS_DRIFT_DSN) or --ssh + --db")
        drift, out = evaluate(report, args.schema, args.app_role, args.roles_only)
    except (psycopg.Error, RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"error: could not run the drift check: {exc}", file=sys.stderr)
        return 2
    print("\n".join(out))
    if drift:
        print(f"\nDRIFT ({len(drift)}):")
        print("\n".join(f"  {d}" for d in drift))
        return 1
    print("\nNO DRIFT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
