"""Archive the unregistered tenant ad976f5b… and the hand-made repair tables (#193).

Tenant ``ad976f5b-f3a7-4441-ae87-c2090d038f9c`` is not in ``licenses.tenants``
(not customer, internal, service or test), yet owns rows here: an old seed/test
run from 2025-11-07/08. ``conflict_reconciler`` skips it every sweep as
``unknown_tenant``. Measured read-only on worker4 (2026-10-02): principle_conflicts
135, principles 5, requirements 5, decisions 5, conflict_reconcile_watermark 1.

Three tables in ``public`` were made by hand, outside alembic, during repairs:
``_conveyor_reset_20260922_assets``, ``outage_misclassification_repair_20260824``
(holds 20 rows of the stray tenant) and ``quality_fabrication_repair_20260817``.

Nothing is destroyed. Everything moves to a schema ``archive`` that the app role
cannot use:

1. ``CREATE SCHEMA IF NOT EXISTS archive``; ``REVOKE ALL ... FROM PUBLIC`` and,
   when the role exists, from ``requirements_app``. This migration gives the app
   role nothing in ``archive``. Measured in prod: the owner's default privileges
   are scoped ``IN SCHEMA public`` only, so tables created in ``archive`` carry
   no app-role ACL; and without USAGE on the schema the app cannot reach them
   anyway. The upgrade re-checks USAGE at the end and aborts if the app role has it.
2. ``ALTER TABLE ... SET SCHEMA archive`` for each repair table that exists.
   ⚠️ A table the migrating role does not own CANNOT be moved by it (Postgres:
   "must be owner"). Measured in prod: ``_conveyor_reset_20260922_assets`` and
   ``quality_fabrication_repair_20260817`` are owned by ``postgres``, while
   migrations run as ``requirements_user``. Those are SKIPPED with a loud
   WARNING rather than failing the deploy; they stay where they are (RLS off, no
   app-role grant, so the app cannot read them today either) until their owner
   is changed by a reviewed, separate step.
3. Every table in the migrated schema with a ``tenant_id`` column that holds rows
   of the stray tenant is DISCOVERED (not listed), copied to
   ``archive.<table>_ad976f5b`` and then deleted, FK children before parents.
   Fail-closed guards: a FK child row of a stray parent row that belongs to
   ANOTHER tenant (or to a table with no ``tenant_id``) aborts the migration
   instead of letting ON DELETE CASCADE / SET NULL touch someone else's data;
   copied count must equal deleted count; a table with forced RLS aborts (its
   count could be blind); an existing archive copy aborts.

``downgrade()`` re-inserts the archived rows (parents first, generated columns
recomputed), drops the copies, moves the repair tables back, and drops the
schema only if it is empty.

The source schema is ``current_schema()`` (``public`` in prod), so the migration
also runs correctly in the private schemas the tests migrate into. The archive
schema name may be overridden for tests with ``alembic -x archive_schema=<name>``;
prod passes nothing and gets ``archive``.

Revision ID: 2026_10_02_0048
Revises: 2026_10_02_0047
Create Date: 2026-10-02
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import context, op

revision = "2026_10_02_0048"
down_revision = "2026_10_02_0047"
branch_labels = None
depends_on = None

STRAY_TENANT = "ad976f5b-f3a7-4441-ae87-c2090d038f9c"
SUFFIX = "_" + STRAY_TENANT[:8]  # archive.<table>_ad976f5b
DEFAULT_ARCHIVE_SCHEMA = "archive"
APP_ROLE = "requirements_app"
REPAIR_TABLES = (
    "_conveyor_reset_20260922_assets",
    "outage_misclassification_repair_20260824",
    "quality_fabrication_repair_20260817",
)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _say(msg: str) -> None:
    print(f"NOTICE 0048: {msg}", flush=True)


def _q(ident: str) -> str:
    """Quote an identifier that already passed ``_IDENT`` (or came from the catalog)."""
    return '"' + ident.replace('"', '""') + '"'


def _archive_schema() -> str:
    name = context.get_x_argument(as_dictionary=True).get("archive_schema", DEFAULT_ARCHIVE_SCHEMA)
    if not _IDENT.match(name):
        raise RuntimeError(f"0048: invalid archive schema name {name!r}")
    return name


def _source_schema(bind: Connection) -> str:
    name = bind.execute(text("SELECT current_schema()")).scalar_one()
    if not name:
        raise RuntimeError("0048: no current_schema() (empty search_path?)")
    return str(name)


def _role_exists(bind: Connection, role: str) -> bool:
    return bool(
        bind.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
    )


def _table_exists(bind: Connection, schema: str, table: str) -> bool:
    return bool(
        bind.execute(
            text(
                "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :s AND c.relname = :t AND c.relkind IN ('r', 'p')"
            ),
            {"s": schema, "t": table},
        ).scalar()
    )


def _tenant_tables(bind: Connection, schema: str) -> list[str]:
    """Every ordinary table in ``schema`` with a ``tenant_id`` column, minus the repair tables."""
    rows = bind.execute(
        text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' "
            "  AND NOT a.attisdropped "
            "WHERE n.nspname = :s AND c.relkind IN ('r', 'p') ORDER BY c.relname"
        ),
        {"s": schema},
    ).scalars()
    return [t for t in rows if t not in REPAIR_TABLES]


def discover(bind: Connection, schema: str) -> dict[str, int]:
    """table -> row count of the stray tenant, for every tenant table that has any."""
    out: dict[str, int] = {}
    for t in _tenant_tables(bind, schema):
        n = bind.execute(
            text(f"SELECT count(*) FROM {_q(schema)}.{_q(t)} WHERE tenant_id::text = :id"),
            {"id": STRAY_TENANT},
        ).scalar_one()
        if n:
            out[t] = int(n)
    return out


def _fks(bind: Connection, schema: str) -> list[tuple[str, str, list[str], list[str]]]:
    """(child, parent, child_cols, parent_cols) for every FK whose parent is in ``schema``."""
    rows = bind.execute(
        text(
            "SELECT cc.relname, pc.relname, cn.nspname, "
            "  ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY u(n, i) "
            "        JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.n "
            "        ORDER BY u.i)::text[], "
            "  ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY u(n, i) "
            "        JOIN pg_attribute a ON a.attrelid = k.confrelid AND a.attnum = u.n "
            "        ORDER BY u.i)::text[] "
            "FROM pg_constraint k "
            "JOIN pg_class cc ON cc.oid = k.conrelid "
            "JOIN pg_namespace cn ON cn.oid = cc.relnamespace "
            "JOIN pg_class pc ON pc.oid = k.confrelid "
            "JOIN pg_namespace pn ON pn.oid = pc.relnamespace "
            "WHERE k.contype = 'f' AND pn.nspname = :s"
        ),
        {"s": schema},
    ).all()
    out = []
    for child, parent, child_ns, ccols, pcols in rows:
        if child_ns != schema:
            raise RuntimeError(
                f"0048: FK from {child_ns}.{child} into {schema}.{parent} crosses schemas; "
                "refusing to archive without reviewing it"
            )
        out.append((child, parent, list(ccols), list(pcols)))
    return out


def _children_first(
    tables: list[str], fks: list[tuple[str, str, list[str], list[str]]]
) -> list[str]:
    """Topological order over FK edges among ``tables``: children before parents."""
    edges = {(c, p) for c, p, _, _ in fks if c in tables and p in tables and c != p}
    remaining = list(tables)
    order: list[str] = []
    while remaining:
        # a table is ready when no other remaining table is its child
        ready = [t for t in remaining if not any(p == t and c in remaining for c, p in edges)]
        if not ready:
            raise RuntimeError(f"0048: FK cycle among {remaining}")
        for t in sorted(ready):
            order.append(t)
            remaining.remove(t)
    return order


def _guard_foreign_dependents(
    bind: Connection,
    schema: str,
    found: dict[str, int],
    fks: list[tuple[str, str, list[str], list[str]]],
) -> None:
    """Abort if any FK child row of a stray parent row is NOT itself a stray row."""
    for child, parent, ccols, pcols in fks:
        if parent not in found:
            continue
        has_tenant = bool(
            bind.execute(
                text(
                    "SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(:r) "
                    "AND attname = 'tenant_id' AND NOT attisdropped"
                ),
                {"r": f"{_q(schema)}.{_q(child)}"},
            ).scalar()
        )
        on = " AND ".join(f"c.{_q(a)} = p.{_q(b)}" for a, b in zip(ccols, pcols, strict=True))
        not_stray = "TRUE" if not has_tenant else "c.tenant_id::text IS DISTINCT FROM :id"
        n = bind.execute(
            text(
                f"SELECT count(*) FROM {_q(schema)}.{_q(child)} c "
                f"JOIN {_q(schema)}.{_q(parent)} p ON {on} "
                f"WHERE p.tenant_id::text = :id AND {not_stray}"
            ),
            {"id": STRAY_TENANT},
        ).scalar_one()
        if n:
            raise RuntimeError(
                f"0048: {n} row(s) of {child} (not the stray tenant) reference stray rows "
                f"of {parent}; deleting would cascade into / null them. Refusing."
            )


def _app_role_revoke(bind: Connection, arch: str) -> None:
    op.execute(f"REVOKE ALL ON SCHEMA {_q(arch)} FROM PUBLIC")
    if _role_exists(bind, APP_ROLE):
        op.execute(f"REVOKE ALL ON SCHEMA {_q(arch)} FROM {_q(APP_ROLE)}")
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {_q(arch)} FROM {_q(APP_ROLE)}")


def upgrade() -> None:
    bind = op.get_bind()
    src = _source_schema(bind)
    arch = _archive_schema()
    if arch == src:
        raise RuntimeError(f"0048: archive schema {arch!r} equals the source schema")
    _say(f"source schema {src!r}, archive schema {arch!r}, stray tenant {STRAY_TENANT}")

    # 1. the archive schema, closed to everyone but its owner
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(arch)}")
    _app_role_revoke(bind, arch)

    # 2. move the hand-made repair tables (whole tables, ACLs and all)
    for t in REPAIR_TABLES:
        if not _table_exists(bind, src, t):
            _say(f"repair table {src}.{t} absent; nothing to move")
            continue
        if _table_exists(bind, arch, t):
            raise RuntimeError(f"0048: {arch}.{t} already exists; refusing to overwrite")
        may_move = bind.execute(
            text(
                "SELECT pg_has_role(current_user, c.relowner, 'USAGE') FROM pg_class c "
                "WHERE c.oid = to_regclass(:r)"
            ),
            {"r": f"{_q(src)}.{_q(t)}"},
        ).scalar_one()
        if not may_move:
            owner = bind.execute(
                text("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = to_regclass(:r)"),
                {"r": f"{_q(src)}.{_q(t)}"},
            ).scalar_one()
            _say(
                f"WARNING: {src}.{t} is owned by {owner!r}, not by the migrating role; "
                "it cannot be moved here and is LEFT IN PLACE (see #193)"
            )
            continue
        n = bind.execute(text(f"SELECT count(*) FROM {_q(src)}.{_q(t)}")).scalar_one()
        op.execute(f"ALTER TABLE {_q(src)}.{_q(t)} SET SCHEMA {_q(arch)}")
        _say(f"moved {src}.{t} ({n} rows) -> {arch}.{t}")

    # 3. archive + delete the stray tenant's rows
    forced = (
        bind.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :s AND c.relforcerowsecurity "
                "AND NOT (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user)"
            ),
            {"s": src},
        )
        .scalars()
        .all()
    )
    if forced:
        raise RuntimeError(f"0048: forced RLS on {forced}; the stray-row counts would be blind")

    found = discover(bind, src)
    _say(f"discovered stray rows: {found or 'none'}")
    if not found:
        return
    fks = _fks(bind, src)
    _guard_foreign_dependents(bind, src, found, fks)

    for t, n in found.items():
        copy = t + SUFFIX
        if len(copy) > 63:
            raise RuntimeError(f"0048: archive name {copy!r} exceeds 63 bytes")
        if _table_exists(bind, arch, copy):
            raise RuntimeError(f"0048: {arch}.{copy} already exists; refusing to overwrite")
        # STRAY_TENANT is a module constant (a uuid literal), inlined because
        # CREATE TABLE AS is a utility statement and takes no bind parameters.
        op.execute(
            f"CREATE TABLE {_q(arch)}.{_q(copy)} AS "
            f"SELECT * FROM {_q(src)}.{_q(t)} WHERE tenant_id::text = '{STRAY_TENANT}'"
        )
        copied = bind.execute(text(f"SELECT count(*) FROM {_q(arch)}.{_q(copy)}")).scalar_one()
        if copied != n:
            raise RuntimeError(f"0048: {t}: discovered {n} rows but archived {copied}")

    for t in _children_first(list(found), fks):
        deleted = bind.execute(
            text(f"DELETE FROM {_q(src)}.{_q(t)} WHERE tenant_id::text = :id"),
            {"id": STRAY_TENANT},
        ).rowcount
        if deleted != found[t]:
            raise RuntimeError(f"0048: {t}: archived {found[t]} rows but deleted {deleted}")
        _say(f"archived+deleted {deleted} rows of {src}.{t} -> {arch}.{t}{SUFFIX}")

    left = discover(bind, src)
    if left:
        raise RuntimeError(f"0048: stray rows remain after delete: {left}")

    # The copies are new tables: make sure the app role holds nothing on them either.
    _app_role_revoke(bind, arch)
    if (
        _role_exists(bind, APP_ROLE)
        and bind.execute(
            text("SELECT has_schema_privilege(:r, :s, 'USAGE')"), {"r": APP_ROLE, "s": arch}
        ).scalar_one()
    ):
        raise RuntimeError(f"0048: {APP_ROLE} can still use schema {arch}; refusing")


def downgrade() -> None:
    bind = op.get_bind()
    src = _source_schema(bind)
    arch = _archive_schema()
    if not bind.execute(
        text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": arch}
    ).scalar():
        _say(f"archive schema {arch!r} absent; nothing to restore")
        return

    copies = [
        c
        for c in bind.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :s AND c.relkind = 'r' AND c.relname LIKE :p ORDER BY 1"
            ),
            {"s": arch, "p": "%" + SUFFIX},
        ).scalars()
        if c[: -len(SUFFIX)] not in REPAIR_TABLES
    ]
    bases = {c[: -len(SUFFIX)]: c for c in copies}
    for base in bases:
        if not _table_exists(bind, src, base):
            raise RuntimeError(f"0048 downgrade: {src}.{base} is gone; cannot restore into it")
    fks = _fks(bind, src)
    for base in reversed(_children_first(list(bases), fks)):  # parents first
        copy = bases[base]
        cols = (
            bind.execute(
                text(
                    "SELECT a.attname FROM pg_attribute a "
                    "WHERE a.attrelid = to_regclass(:r) AND a.attnum > 0 AND NOT a.attisdropped "
                    "AND a.attgenerated = '' ORDER BY a.attnum"
                ),
                {"r": f"{_q(src)}.{_q(base)}"},
            )
            .scalars()
            .all()
        )
        col_list = ", ".join(_q(c) for c in cols)
        n = bind.execute(
            text(
                f"INSERT INTO {_q(src)}.{_q(base)} ({col_list}) "
                f"SELECT {col_list} FROM {_q(arch)}.{_q(copy)}"
            )
        ).rowcount
        op.execute(f"DROP TABLE {_q(arch)}.{_q(copy)}")
        _say(f"restored {n} rows into {src}.{base}")

    for t in REPAIR_TABLES:
        if not _table_exists(bind, arch, t):
            continue
        if _table_exists(bind, src, t):
            raise RuntimeError(f"0048 downgrade: {src}.{t} exists again; refusing to clobber")
        op.execute(f"ALTER TABLE {_q(arch)}.{_q(t)} SET SCHEMA {_q(src)}")
        _say(f"moved {arch}.{t} back to {src}")

    remaining = bind.execute(
        text(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s"
        ),
        {"s": arch},
    ).scalar_one()
    if remaining:
        _say(f"archive schema {arch!r} still holds {remaining} relation(s); kept")
    else:
        op.execute(f"DROP SCHEMA {_q(arch)}")
        _say(f"dropped empty archive schema {arch!r}")
