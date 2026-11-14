"""Add PM entity tables: programmes, projects, activities, resources, buffers, snapshots.

Revision ID: 2026_03_23_0015
Revises: 2026_03_23_0014
Create Date: 2026-03-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision = "2026_03_23_0015"
down_revision = "2026_03_23_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. pm_programmes
    # ------------------------------------------------------------------
    op.create_table(
        "pm_programmes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="identifying"),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "blueprint_data",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="{}",
        ),
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
        sa.Column(
            "version_status",
            sa.String(length=20),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("supersedes", sa.String(length=120), nullable=True),
        sa.Column("superseded_by", sa.String(length=120), nullable=True),
        sa.Column("version_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(op.f("ix_pm_programmes_slug"), "pm_programmes", ["slug"])
    op.create_index(op.f("ix_pm_programmes_creator_id"), "pm_programmes", ["creator_id"])
    op.create_unique_constraint(
        "uq_pm_programmes_creator_slug", "pm_programmes", ["creator_id", "slug"]
    )
    op.create_index("ix_pm_programmes_creator_status", "pm_programmes", ["creator_id", "status"])

    op.execute(
        """
        ALTER TABLE pm_programmes
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(title, '') || ' ' || coalesce(description, '')
            )
        ) STORED
        """
    )
    op.execute("CREATE INDEX ix_pm_programmes_tsv ON pm_programmes USING GIN (tsv)")

    # ------------------------------------------------------------------
    # 2. pm_projects
    # ------------------------------------------------------------------
    op.create_table(
        "pm_projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("programme_id", sa.String(length=100), nullable=True),
        sa.Column("tranche", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="initiating"),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
        sa.Column(
            "version_status",
            sa.String(length=20),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("supersedes", sa.String(length=120), nullable=True),
        sa.Column("superseded_by", sa.String(length=120), nullable=True),
        sa.Column("version_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(op.f("ix_pm_projects_slug"), "pm_projects", ["slug"])
    op.create_index(op.f("ix_pm_projects_creator_id"), "pm_projects", ["creator_id"])
    op.create_unique_constraint(
        "uq_pm_projects_creator_slug", "pm_projects", ["creator_id", "slug"]
    )
    op.create_index("ix_pm_projects_creator_status", "pm_projects", ["creator_id", "status"])
    op.create_index(
        "ix_pm_projects_creator_programme", "pm_projects", ["creator_id", "programme_id"]
    )

    op.execute(
        """
        ALTER TABLE pm_projects
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(title, '') || ' ' || coalesce(description, '')
            )
        ) STORED
        """
    )
    op.execute("CREATE INDEX ix_pm_projects_tsv ON pm_projects USING GIN (tsv)")

    # ------------------------------------------------------------------
    # 3. pm_activities
    # ------------------------------------------------------------------
    op.create_table(
        "pm_activities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=False),
        sa.Column("parent_activity_id", sa.Uuid(), nullable=True),
        sa.Column("wbs_code", sa.String(length=50), nullable=True),
        sa.Column("is_milestone", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_summary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("duration_optimistic", sa.Float(), nullable=True),
        sa.Column("duration_most_likely", sa.Float(), nullable=True),
        sa.Column("duration_pessimistic", sa.Float(), nullable=True),
        sa.Column("duration_planned", sa.Float(), nullable=True),
        sa.Column("duration_aggressive", sa.Float(), nullable=True),
        sa.Column("actual_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_finish", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_duration", sa.Float(), nullable=True),
        sa.Column("percent_complete", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column(
            "resource_ids",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="[]",
        ),
        sa.Column("cost_planned", sa.Float(), nullable=True),
        sa.Column("cost_actual", sa.Float(), nullable=True),
        sa.Column(
            "earned_value_method",
            sa.String(length=20),
            nullable=False,
            server_default="percent_complete",
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="not_started"),
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
        sa.Column(
            "version_status",
            sa.String(length=20),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("supersedes", sa.String(length=120), nullable=True),
        sa.Column("superseded_by", sa.String(length=120), nullable=True),
        sa.Column("version_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(op.f("ix_pm_activities_slug"), "pm_activities", ["slug"])
    op.create_index(op.f("ix_pm_activities_creator_id"), "pm_activities", ["creator_id"])
    op.create_unique_constraint(
        "uq_pm_activities_creator_slug", "pm_activities", ["creator_id", "slug"]
    )
    op.create_index(
        "ix_pm_activities_creator_project", "pm_activities", ["creator_id", "project_id"]
    )
    op.create_index("ix_pm_activities_creator_status", "pm_activities", ["creator_id", "status"])
    op.create_index(
        "ix_pm_activities_creator_parent", "pm_activities", ["creator_id", "parent_activity_id"]
    )

    op.execute(
        """
        ALTER TABLE pm_activities
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(title, '') || ' ' || coalesce(description, '')
            )
        ) STORED
        """
    )
    op.execute("CREATE INDEX ix_pm_activities_tsv ON pm_activities USING GIN (tsv)")

    # ------------------------------------------------------------------
    # 4. pm_resources (no tsv)
    # ------------------------------------------------------------------
    op.create_table(
        "pm_resources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("capacity_per_day", sa.Float(), nullable=False, server_default=sa.text("8.0")),
        sa.Column("cost_per_unit", sa.Float(), nullable=True),
        sa.Column(
            "calendar_data",
            JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "skills",
            JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
        sa.Column(
            "version_status",
            sa.String(length=20),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("supersedes", sa.String(length=120), nullable=True),
        sa.Column("superseded_by", sa.String(length=120), nullable=True),
        sa.Column("version_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(op.f("ix_pm_resources_slug"), "pm_resources", ["slug"])
    op.create_index(op.f("ix_pm_resources_creator_id"), "pm_resources", ["creator_id"])
    op.create_unique_constraint(
        "uq_pm_resources_creator_slug", "pm_resources", ["creator_id", "slug"]
    )
    op.create_index("ix_pm_resources_creator_type", "pm_resources", ["creator_id", "resource_type"])

    # ------------------------------------------------------------------
    # 5. pm_buffers (no version fields, no tsv)
    # ------------------------------------------------------------------
    op.create_table(
        "pm_buffers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=False),
        sa.Column("buffer_type", sa.String(length=20), nullable=False),
        sa.Column(
            "chain_activity_ids",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="[]",
        ),
        sa.Column("size_days", sa.Float(), nullable=False),
        sa.Column("consumed_days", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("penetration_pct", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="green"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(op.f("ix_pm_buffers_slug"), "pm_buffers", ["slug"])
    op.create_index(op.f("ix_pm_buffers_creator_id"), "pm_buffers", ["creator_id"])
    op.create_unique_constraint("uq_pm_buffers_creator_slug", "pm_buffers", ["creator_id", "slug"])
    op.create_index("ix_pm_buffers_creator_project", "pm_buffers", ["creator_id", "project_id"])
    op.create_index("ix_pm_buffers_creator_type", "pm_buffers", ["creator_id", "buffer_type"])

    # ------------------------------------------------------------------
    # 6. pm_schedule_snapshots (no slug, no version, no tsv)
    # ------------------------------------------------------------------
    op.create_table(
        "pm_schedule_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=False),
        sa.Column("snapshot_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_type", sa.String(length=20), nullable=False),
        sa.Column(
            "activity_data",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="{}",
        ),
        sa.Column(
            "evm_data",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_pm_snapshots_creator_project",
        "pm_schedule_snapshots",
        ["creator_id", "project_id"],
    )
    op.create_index(
        "ix_pm_snapshots_creator_type",
        "pm_schedule_snapshots",
        ["creator_id", "snapshot_type"],
    )


def downgrade() -> None:
    # Drop in reverse order. Drop GIN indexes first for tables that have them.
    op.drop_table("pm_schedule_snapshots")
    op.drop_table("pm_buffers")
    op.drop_table("pm_resources")

    op.execute("DROP INDEX IF EXISTS ix_pm_activities_tsv")
    op.drop_table("pm_activities")

    op.execute("DROP INDEX IF EXISTS ix_pm_projects_tsv")
    op.drop_table("pm_projects")

    op.execute("DROP INDEX IF EXISTS ix_pm_programmes_tsv")
    op.drop_table("pm_programmes")
