"""Activity dedup on deterministic wbs_code — partitioned two-index design.

AA re-runs minted duplicate activities because ``create_activity`` keyed
get-or-create on ``(project_id, title)``; the LLM-authored title drifts across
BLOCKED→DOING re-runs. The n8n AA-intake node already forwards a STABLE
``wbs_code`` (e.g. ``1.2.3``) that the backend ignored. This migration keys the
activity dedup on ``(tenant_id, project_id, wbs_code)`` instead.

Design — PARTITIONED two-index (the load-bearing subtlety): the router runs
get-or-create through a SINGLE identity key per call, and its IntegrityError
recovery re-SELECTs on THAT one key. If a wbs-coded row could ALSO collide the
title index (or vice-versa), the recovery re-SELECT on the wrong key would miss
the survivor and re-raise → HTTP 500. So the two partial unique indexes are made
to cover DISJOINT row sets:

  * ``uq_pm_activities_tenant_project_title`` — narrowed with ``wbs_code IS
    NULL`` so it only covers rows WITHOUT a wbs_code (title-fallback dedup).
  * ``uq_pm_activities_tenant_project_wbs`` — new, covers rows WITH a wbs_code
    (``wbs_code IS NOT NULL``), the deterministic dedup key.

A row lands in exactly one index (partitioned on ``wbs_code IS NULL``), so no
insert can cross-collide under single-key recovery. Empty wbs_code is normalised
to NULL in the schema (``WbsCode`` type, mirrors ProjectScope #72) so the Python
truthiness gate and the SQL predicate agree.

FAIL-LOUD, NO RECONCILE: a prod-duplicate precheck for the new
``(tenant, project, wbs_code)`` key is run separately (by the orchestrator)
before merge. We deliberately do NOT dedup or delete rows in ``upgrade()`` — if a
duplicate ever exists, ``CREATE UNIQUE INDEX`` fails loud on its own, which is the
correct signal. Repair is a deliberate, human-reviewed step, never a migration
side effect.

The narrowed title index + the new wbs index are also declared on
``Activity.__table_args__`` so ``Base.metadata.create_all()`` builds them for the
Postgres-backed test suite; this migration is the production/CI path.

Forward-only — pre-prod, no backwards-compat shims.

Revision ID: 2026_07_18_0037
Revises: 2026_07_18_0036
Create Date: 2026-07-18
"""

from alembic import op

revision = "2026_07_18_0037"
down_revision = "2026_07_18_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the old title index (covered every project-scoped tenant row).
    op.execute("DROP INDEX IF EXISTS uq_pm_activities_tenant_project_title")
    # 2. Recreate it narrowed to wbs_code-less rows (original predicate from
    #    migration 2026_07_16_0033 where this index was first created,
    #    + AND wbs_code IS NULL).
    op.execute(
        "CREATE UNIQUE INDEX uq_pm_activities_tenant_project_title "
        "ON pm_activities (tenant_id, project_id, title) "
        "WHERE project_id IS NOT NULL AND wbs_code IS NULL AND visibility = 'tenant'"
    )
    # 3. New deterministic dedup key on wbs-coded rows.
    op.execute(
        "CREATE UNIQUE INDEX uq_pm_activities_tenant_project_wbs "
        "ON pm_activities (tenant_id, project_id, wbs_code) "
        "WHERE wbs_code IS NOT NULL AND visibility = 'tenant'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_pm_activities_tenant_project_wbs")
    op.execute("DROP INDEX IF EXISTS uq_pm_activities_tenant_project_title")
    # Restore the ORIGINAL (pre-partition) title predicate — no wbs_code IS NULL.
    op.execute(
        "CREATE UNIQUE INDEX uq_pm_activities_tenant_project_title "
        "ON pm_activities (tenant_id, project_id, title) "
        "WHERE project_id IS NOT NULL AND visibility = 'tenant'"
    )
