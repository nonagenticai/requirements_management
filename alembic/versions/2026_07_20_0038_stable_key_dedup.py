"""AA dedup on a deterministic stable_key for requirements / decisions / resources.

AA re-runs minted duplicate requirements, decisions and resources: get-or-create
keyed on ``(project_id, title)`` (requirements/decisions) or did a pure create
(resources), and the LLM-authored title drifts across re-runs. The n8n AA-intake
node now forwards a STABLE ``stable_key`` (cardId + kind + ordinal) that survives
re-runs — the exact mechanism already proven for ``pm_activities.wbs_code``
(migration 2026_07_18_0037).

Design — PARTITIONED two-index (the load-bearing subtlety, mirrors wbs): the
router runs get-or-create through a SINGLE identity key per call and its
IntegrityError recovery re-SELECTs on THAT one key. So the title and stable_key
partial unique indexes must cover DISJOINT row sets, else the recovery re-SELECT
on the wrong key misses the survivor and re-raises → HTTP 500:

  * requirements/decisions: ``uq_<t>_tenant_project_title`` narrowed with
    ``stable_key IS NULL``; new ``uq_<t>_tenant_project_stable_key`` on
    ``stable_key IS NOT NULL``.
  * pm_resources has no ``project_id`` (resources are tenant-scoped; the cardId
    embedded in stable_key keeps the key card-specific) and had NO title dedup at
    all, so only the new ``uq_pm_resources_tenant_stable_key`` is added.

Empty stable_key is normalised to NULL in the schema (``StableKey`` type) so the
Python truthiness gate and the SQL predicate agree.

FAIL-LOUD, NO RECONCILE: no dedup/delete in ``upgrade()`` — a duplicate would make
``CREATE UNIQUE INDEX`` fail loud, which is the correct signal; repair is a
deliberate human step, never a migration side effect. Forward-only (pre-prod).

Revision ID: 2026_07_20_0038
Revises: 2026_07_18_0037
Create Date: 2026-07-20
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_07_20_0038"
down_revision = "2026_07_18_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add the nullable stable_key columns.
    for table in ("requirements", "decisions", "pm_resources"):
        op.add_column(table, sa.Column("stable_key", sa.String(length=200), nullable=True))

    # 2. requirements / decisions: partition the title index on stable_key IS NULL
    #    and add the deterministic stable_key index (disjoint sets).
    for table in ("requirements", "decisions"):
        op.execute(f"DROP INDEX IF EXISTS uq_{table}_tenant_project_title")
        op.execute(
            f"CREATE UNIQUE INDEX uq_{table}_tenant_project_title "
            f"ON {table} (tenant_id, project_id, title) "
            f"WHERE project_id IS NOT NULL AND stable_key IS NULL AND visibility = 'tenant'"
        )
        op.execute(
            f"CREATE UNIQUE INDEX uq_{table}_tenant_project_stable_key "
            f"ON {table} (tenant_id, project_id, stable_key) "
            f"WHERE stable_key IS NOT NULL AND visibility = 'tenant'"
        )

    # 3. pm_resources: no project_id, no prior title dedup — just the tenant-scoped
    #    stable_key index.
    op.execute(
        "CREATE UNIQUE INDEX uq_pm_resources_tenant_stable_key "
        "ON pm_resources (tenant_id, stable_key) "
        "WHERE stable_key IS NOT NULL AND visibility = 'tenant'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_pm_resources_tenant_stable_key")
    for table in ("requirements", "decisions"):
        op.execute(f"DROP INDEX IF EXISTS uq_{table}_tenant_project_stable_key")
        op.execute(f"DROP INDEX IF EXISTS uq_{table}_tenant_project_title")
        # Restore the ORIGINAL (pre-partition) title predicate — no stable_key IS NULL.
        op.execute(
            f"CREATE UNIQUE INDEX uq_{table}_tenant_project_title "
            f"ON {table} (tenant_id, project_id, title) "
            f"WHERE project_id IS NOT NULL AND visibility = 'tenant'"
        )
    for table in ("requirements", "decisions", "pm_resources"):
        op.drop_column(table, "stable_key")
