"""Per-user-within-tenant isolation on every owned entity.

Adds (tenant_id, subject_id, visibility) to all 15 owned tables and backfills:
    tenant_id  := creator_id
    subject_id := creator_id
    visibility := 'tenant'

so today's behavior is preserved bit-for-bit. Then drops the old creator_id
column + its supporting indexes/unique constraints, replacing them with the
new (tenant_id, slug) unique constraint and (tenant_id, subject_id) +
(tenant_id, visibility) indexes plus a CHECK on visibility.

Mirrors procedure_management migration 004 (the pilot).

Revision ID: 2026_04_27_0018
Revises: 2026_03_24_0017
Create Date: 2026-04-27
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_04_27_0018"
down_revision = "2026_03_24_0017"
branch_labels = None
depends_on = None


# All 15 owned tables get the (tenant_id, subject_id, visibility) treatment.
# 14 of them have a `slug` column (so the unique key swap is uniform);
# `pm_schedule_snapshots` has no slug — it gets the columns + indexes only.
TABLES_WITH_SLUG = (
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
)

TABLES_NO_SLUG = ("pm_schedule_snapshots",)

ALL_TABLES = TABLES_WITH_SLUG + TABLES_NO_SLUG

# Map table → existing creator_id-based unique constraint name (for swap).
# These names come from the existing models' UniqueConstraint declarations.
EXISTING_UQ_NAMES: dict[str, str] = {
    "requirements": "uq_requirements_creator_slug",
    "principles": "uq_principles_creator_slug",
    "decisions": "uq_decisions_creator_slug",
    "architecture_elements": "uq_arch_elements_creator_slug",
    "architecture_relationships": "uq_arch_rels_creator_slug",
    "architecture_views": "uq_arch_views_creator_slug",
    "architecture_standards": "uq_arch_standards_creator_slug",
    "pm_programmes": "uq_pm_programmes_creator_slug",
    "pm_projects": "uq_pm_projects_creator_slug",
    "pm_activities": "uq_pm_activities_creator_slug",
    "pm_resources": "uq_pm_resources_creator_slug",
    "pm_buffers": "uq_pm_buffers_creator_slug",
    "pm_risks": "uq_pm_risks_creator_slug",
    "pm_benefits": "uq_pm_benefits_creator_slug",
}

# Map table → list of existing creator_id-prefixed indexes to drop. Models
# create various composite indexes like (creator_id, project_id),
# (creator_id, status) etc. We drop them all and rely on the new
# (tenant_id, subject_id) / (tenant_id, visibility) indexes plus pg's
# btree on tenant_id alone for the common queries. Specific app filters
# (e.g. project_id) can be re-indexed in a follow-up migration if needed.
EXISTING_CREATOR_INDEXES: dict[str, tuple[str, ...]] = {
    "requirements": (
        "ix_requirements_creator_project",
        "ix_requirements_creator_status",
        "ix_requirements_creator_created",
    ),
    "principles": (
        "ix_principles_creator_status",
        "ix_principles_creator_created",
        "ix_principles_creator_active",
    ),
    "decisions": (
        "ix_decisions_creator_project",
        "ix_decisions_creator_status",
        "ix_decisions_creator_created",
        "ix_decisions_creator_decision_status",
    ),
    "architecture_elements": (
        "ix_arch_elements_creator_layer",
        "ix_arch_elements_creator_type",
        "ix_arch_elements_creator_project",
        "ix_arch_elements_creator_state",
    ),
    "architecture_relationships": (
        "ix_arch_rels_creator_source",
        "ix_arch_rels_creator_target",
        "ix_arch_rels_creator_type",
        "ix_arch_rels_creator_project",
    ),
    "architecture_views": (
        "ix_arch_views_creator_viewpoint",
        "ix_arch_views_creator_project",
    ),
    "architecture_standards": (
        "ix_arch_standards_creator_type",
        "ix_arch_standards_creator_status",
        "ix_arch_standards_creator_compliance",
        "ix_arch_standards_creator_project",
    ),
    "pm_programmes": ("ix_pm_programmes_creator_status",),
    "pm_projects": (
        "ix_pm_projects_creator_status",
        "ix_pm_projects_creator_programme",
    ),
    "pm_activities": (
        "ix_pm_activities_creator_project",
        "ix_pm_activities_creator_status",
        "ix_pm_activities_creator_parent",
    ),
    "pm_resources": ("ix_pm_resources_creator_type",),
    "pm_buffers": (
        "ix_pm_buffers_creator_project",
        "ix_pm_buffers_creator_type",
    ),
    "pm_risks": (
        "ix_pm_risks_creator_project",
        "ix_pm_risks_creator_status",
        "ix_pm_risks_creator_category",
    ),
    "pm_benefits": (
        "ix_pm_benefits_creator_programme",
        "ix_pm_benefits_creator_status",
    ),
    "pm_schedule_snapshots": (
        "ix_pm_snapshots_creator_project",
        "ix_pm_snapshots_creator_type",
    ),
}

# Short aliases for new index names so we stay under the pg 63-char limit on
# the longer table names like architecture_relationships / architecture_standards.
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

        # 1. Add the three new columns as nullable so existing rows survive.
        op.add_column(table, sa.Column("tenant_id", sa.String(length=255), nullable=True))
        op.add_column(table, sa.Column("subject_id", sa.String(length=255), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "visibility",
                sa.String(length=16),
                server_default="tenant",
                nullable=True,
            ),
        )

        # 2. Backfill from creator_id (preserves today's behavior bit-for-bit).
        op.execute(
            f"UPDATE {table} SET "
            f"tenant_id = creator_id, "
            f"subject_id = creator_id, "
            f"visibility = 'tenant' "
            f"WHERE tenant_id IS NULL"
        )

        # 3. Tighten to NOT NULL now that every row has values.
        op.alter_column(table, "tenant_id", nullable=False)
        op.alter_column(table, "subject_id", nullable=False)
        op.alter_column(table, "visibility", nullable=False)

        # 4. Swap the unique constraint (creator_id, slug) → (tenant_id, slug).
        #    Use IF EXISTS — Base.metadata.create_all in tests may have created
        #    the constraint while alembic-deployed environments did so via
        #    earlier migrations. Be tolerant of either state.
        if table in TABLES_WITH_SLUG:
            existing_uq = EXISTING_UQ_NAMES[table]
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {existing_uq}")
            op.create_unique_constraint(
                f"uq_{alias}_tenant_slug",
                table,
                ["tenant_id", "slug"],
            )

        # 5. Drop old creator_id-prefixed composite indexes; create the new
        #    (tenant_id, subject_id) + (tenant_id, visibility) indexes.
        for idx in EXISTING_CREATOR_INDEXES.get(table, ()):
            op.execute(f"DROP INDEX IF EXISTS {idx}")
        op.create_index(f"ix_{alias}_tenant_subject", table, ["tenant_id", "subject_id"])
        op.create_index(f"ix_{alias}_tenant_visibility", table, ["tenant_id", "visibility"])

        # 6. CHECK constraint on visibility values.
        op.create_check_constraint(
            f"ck_{alias}_visibility",
            table,
            "visibility IN ('tenant', 'user')",
        )

        # 7. Drop the bare creator_id column index then the column itself.
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_creator_id")
        op.drop_column(table, "creator_id")


def downgrade() -> None:
    # Reverse: re-create creator_id, copy back from tenant_id, drop new
    # columns/indexes/constraints. visibility='user' rows lose their privacy
    # boundary on downgrade — there is no creator_id-only equivalent.
    for table in ALL_TABLES:
        alias = SHORT_ALIAS[table]

        op.add_column(table, sa.Column("creator_id", sa.String(length=100), nullable=True))
        op.execute(f"UPDATE {table} SET creator_id = tenant_id WHERE creator_id IS NULL")
        op.alter_column(table, "creator_id", nullable=False)
        op.create_index(f"ix_{table}_creator_id", table, ["creator_id"], unique=False)

        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{alias}_visibility")
        op.execute(f"DROP INDEX IF EXISTS ix_{alias}_tenant_visibility")
        op.execute(f"DROP INDEX IF EXISTS ix_{alias}_tenant_subject")

        if table in TABLES_WITH_SLUG:
            existing_uq = EXISTING_UQ_NAMES[table]
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS uq_{alias}_tenant_slug")
            op.create_unique_constraint(existing_uq, table, ["creator_id", "slug"])

        # We do NOT recreate the original creator_id-prefixed composite indexes
        # — they're considered legacy. A follow-up migration can add back any
        # specific indexes that turn out to be needed in production.

        op.drop_column(table, "visibility")
        op.drop_column(table, "subject_id")
        op.drop_column(table, "tenant_id")
