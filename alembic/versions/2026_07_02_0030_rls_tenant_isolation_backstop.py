"""Postgres RLS tenant-isolation backstop on all 19 owned tenant tables.

Adds the provable DB-level second line of defense beneath this service's
application-level ``WHERE tenant_id`` predicates. Migration
``2026_04_27_0018_per_user_within_tenant_isolation`` gave every owned table a
NOT NULL ``tenant_id``; isolation has since been enforced purely in the
repository/query layer (``src/repositories/visibility.py``) with no DB backstop
— a single missed ``WHERE`` is a cross-tenant leak. This closes that gap.

Renders the DDL over the service's ``Base.metadata`` (same policy shape as
karma/hirag/kag). Every table carrying a ``tenant_id`` column is covered (19
tables); ``entity_version_history`` (no ``tenant_id``, reached only via
already-scoped parents) is auto-excluded by the table filter below.

**The DDL and the table selection are INLINED here (2026-11-01).** They used to
be imported from ``kag_common.database.rls``; a historical migration must not
depend on an external package, because that package's output changed across
releases and silently altered what this revision does (the frozen predicate
below is that same failure, once). The helpers at the bottom are the library's
0.37.0 behaviour, copied verbatim and frozen with this revision.

**This migration imports every model module explicitly** — the alembic ``env.py``
imports only a subset (e.g. it omits ``models.toc``), so relying on the ambient
metadata would silently skip the toc tables. Importing them here makes the DDL
generation complete regardless of env.py.

**Ships inert.** While the service connects as the table owner, the owner
bypasses ENABLE'd RLS (ENABLE-not-FORCE) → zero behaviour change. The policy
binds only at the atomic, reversible cutover: a separate non-owner restricted
role + ``TENANT_RLS_MODE=enforce`` (per-service gitops flip). The runtime GUC
(``app.current_tenant``) is bound per-request via ``register_tenant_guc`` in
``db.py`` (also inert until enforce).

Revision ID: 2026_07_02_0030
Revises: 2026_06_01_0029
Create Date: 2026-07-02
"""

import importlib
import pkgutil

from sqlalchemy import inspect

import models
from alembic import op
from db import Base

# Import every model module so Base.metadata is fully populated before the RLS
# DDL is generated (env.py imports only a subset — see module docstring).
for _m in pkgutil.iter_modules(models.__path__):
    importlib.import_module(f"models.{_m.name}")

revision = "2026_07_02_0030"
down_revision = "2026_06_01_0029"
branch_labels = None
depends_on = None


def _tables_not_yet_created() -> set[str]:
    """Metadata tenant-tables that don't yet exist in the DB at this revision.

    This migration renders RLS DDL from *live* ``Base.metadata``, which grows as
    later migrations add tables (e.g. ``principle_conflicts`` in
    ``2026_07_09_0031``). Those later tables don't exist when this runs, so on a
    from-scratch upgrade the ``ALTER TABLE … ENABLE ROW LEVEL SECURITY`` would
    fail. Each such table applies its own RLS in its own migration; exclude any
    not-yet-created table here so this backstop covers exactly the tables that
    exist at this point. (No-op on the already-applied prod DB.)
    """
    existing = set(inspect(op.get_bind()).get_table_names())
    return {t.name for t in Base.metadata.sorted_tables if t.name not in existing}


# ⛔ FROZEN PREDICATE — DO NOT RENDER THIS FROM ``Base.metadata``.
#
# This migration used to call ``rls.upgrade_statements(Base.metadata, …)``, which
# derives the predicate's cast from the CURRENT model's column type. That made a
# historical migration change meaning every time a model changed. When
# ``2026_08_24_0042`` flipped ``tenant_id`` to ``uuid``, this revision — twelve
# revisions earlier, where the column is still VARCHAR — started emitting
# ``… = NULLIF(…, '')::uuid`` and a from-scratch ``alembic upgrade head`` died
# with "operator does not exist: character varying = uuid", leaving CI and every
# new environment unable to build a schema at all. Existing databases never
# noticed, because they had already run this revision.
#
# At THIS point in history every ``tenant_id`` is a NOT NULL VARCHAR (made so by
# ``2026_04_27_0018``), so the predicate is text-to-text and strict for every
# table — exactly what production holds. Later revisions are free to change it;
# this one is now a fact about the past. Same shape as ``2026_07_09_0031:33-35``.
_GUC = "NULLIF(current_setting('app.current_tenant', true), '')"
_PREDICATE = f"tenant_id = {_GUC}"

# Frozen from ``kag_common.database.rls`` 0.37.0. Kept here so this revision
# means the same thing forever, regardless of what any package emits later.
_TENANT_COLUMN = "tenant_id"
_POLICY_NAME = "tenant_isolation"
_ALL_POLICY_NAMES = (
    _POLICY_NAME,
    f"{_POLICY_NAME}_read",
    f"{_POLICY_NAME}_ins",
    f"{_POLICY_NAME}_upd",
    f"{_POLICY_NAME}_del",
)


def _tenant_table_names() -> list[str]:
    """Tenant tables this revision covers — the LIST may come from metadata.

    Only the predicate had to be frozen. The table list is self-correcting:
    ``_tables_not_yet_created`` drops anything that does not exist yet, so a
    model added later is simply skipped here and applies its own RLS in its own
    migration.
    """
    exclude = _tables_not_yet_created()
    return [
        t.name
        for t in Base.metadata.sorted_tables
        if _TENANT_COLUMN in t.columns and t.name not in exclude
    ]


def _downgrade_statements() -> list[str]:
    """Reverse of the upgrade (DROP-only, so it carries no predicate and cannot drift)."""
    out: list[str] = []
    for name in _tenant_table_names():
        for policy in _ALL_POLICY_NAMES:
            out.append(f"DROP POLICY IF EXISTS {policy} ON {name}")
        out.append(f"ALTER TABLE {name} DISABLE ROW LEVEL SECURITY")
    return out


def upgrade() -> None:
    for table in _tenant_table_names():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}")
        op.execute(
            f"CREATE POLICY {_POLICY_NAME} ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    for stmt in _downgrade_statements():
        op.execute(stmt)
