"""Add tenant_name VARCHAR(255) to all 15 owned entities (UUIDv7 plan Phase 3).

Adds tenant_name as a human-readable display label alongside tenant_id (which
remains VARCHAR(255) for now — UUID type flip is Phase 4).

Phase 4 shipped on 2026-08-24 as ``2026_08_24_0042_tenant_id_is_a_uuid``. This
migration is what made it safe: it is the reason every affected row still has
its original label after tenant_id stopped being able to hold one. Backfills from
tenant_id and then tightens to NOT NULL.

Revision ID: 2026_04_27_0019
Revises: 2026_04_27_0018
Create Date: 2026-04-27
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_04_27_0019"
down_revision = "2026_04_27_0018"
branch_labels = None
depends_on = None

# All 15 owned tables.
ALL_TABLES = (
    "requirements",
    "principles",
    "decisions",
    "architecture_elements",
    "architecture_relationships",
    "architecture_views",
    "architecture_standards",
    "pm_programmes",
    "pm_projects",
    "pm_activities",
    "pm_resources",
    "pm_buffers",
    "pm_risks",
    "pm_benefits",
    "pm_schedule_snapshots",
)

# Short aliases so index names stay under the pg 63-char identifier limit.
SHORT_ALIAS: dict[str, str] = {
    "requirements": "requirements",
    "principles": "principles",
    "decisions": "decisions",
    "architecture_elements": "arch_elements",
    "architecture_relationships": "arch_rels",
    "architecture_views": "arch_views",
    "architecture_standards": "arch_standards",
    "pm_programmes": "pm_programmes",
    "pm_projects": "pm_projects",
    "pm_activities": "pm_activities",
    "pm_resources": "pm_resources",
    "pm_buffers": "pm_buffers",
    "pm_risks": "pm_risks",
    "pm_benefits": "pm_benefits",
    "pm_schedule_snapshots": "pm_snapshots",
}


def upgrade() -> None:
    for table in ALL_TABLES:
        alias = SHORT_ALIAS[table]

        # 1. Add nullable so existing rows survive.
        op.add_column(
            table,
            sa.Column("tenant_name", sa.String(length=255), nullable=True),
        )

        # 2. Backfill: tenant_name := tenant_id (strings like "n8n-dev" become
        #    the display name until Phase 4 mints real UUIDs).
        op.execute(f"UPDATE {table} SET tenant_name = tenant_id WHERE tenant_name IS NULL")

        # 3. Tighten to NOT NULL once every row has a value.
        op.alter_column(table, "tenant_name", nullable=False)

        # 4. Index for tenant_name queries.
        op.create_index(
            f"ix_{alias}_tenant_name",
            table,
            ["tenant_name"],
            if_not_exists=True,
        )


def downgrade() -> None:
    for table in ALL_TABLES:
        alias = SHORT_ALIAS[table]
        op.drop_index(f"ix_{alias}_tenant_name", table_name=table)
        op.drop_column(table, "tenant_name")
