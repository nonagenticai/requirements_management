"""Add PM risk and benefit tables.

Revision ID: 2026_03_23_0016
Revises: 2026_03_23_0015
Create Date: 2026-03-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from alembic import op

# revision identifiers, used by Alembic.
revision = "2026_03_23_0016"
down_revision = "2026_03_23_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. pm_risks
    # ------------------------------------------------------------------
    op.create_table(
        "pm_risks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=False),
        sa.Column("probability", sa.Float(), nullable=False),
        sa.Column("impact_score", sa.Float(), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("response_strategy", sa.String(length=20), nullable=False),
        sa.Column("response_plan", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="identified"),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column("related_activity_ids", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
        sa.Column("version_status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("supersedes", sa.String(length=120), nullable=True),
        sa.Column("superseded_by", sa.String(length=120), nullable=True),
        sa.Column("version_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "tsv",
            TSVECTOR,
            sa.Computed(
                "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creator_id", "slug", name="uq_pm_risks_creator_slug"),
    )
    op.create_index("ix_pm_risks_id", "pm_risks", ["id"])
    op.create_index("ix_pm_risks_slug", "pm_risks", ["slug"])
    op.create_index("ix_pm_risks_creator_id", "pm_risks", ["creator_id"])
    op.create_index("ix_pm_risks_tsv", "pm_risks", ["tsv"], postgresql_using="gin")
    op.create_index("ix_pm_risks_creator_project", "pm_risks", ["creator_id", "project_id"])
    op.create_index("ix_pm_risks_creator_status", "pm_risks", ["creator_id", "status"])
    op.create_index("ix_pm_risks_creator_category", "pm_risks", ["creator_id", "category"])

    # ------------------------------------------------------------------
    # 2. pm_benefits
    # ------------------------------------------------------------------
    op.create_table(
        "pm_benefits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("programme_id", sa.String(length=100), nullable=True),
        sa.Column("benefit_type", sa.String(length=20), nullable=False),
        sa.Column("measurement", sa.Text(), nullable=False),
        sa.Column("baseline_value", sa.Float(), nullable=True),
        sa.Column("target_value", sa.Float(), nullable=True),
        sa.Column("actual_value", sa.Float(), nullable=True),
        sa.Column("realization_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="expected"),
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
        sa.Column("version_status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("supersedes", sa.String(length=120), nullable=True),
        sa.Column("superseded_by", sa.String(length=120), nullable=True),
        sa.Column("version_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "tsv",
            TSVECTOR,
            sa.Computed(
                "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creator_id", "slug", name="uq_pm_benefits_creator_slug"),
    )
    op.create_index("ix_pm_benefits_id", "pm_benefits", ["id"])
    op.create_index("ix_pm_benefits_slug", "pm_benefits", ["slug"])
    op.create_index("ix_pm_benefits_creator_id", "pm_benefits", ["creator_id"])
    op.create_index("ix_pm_benefits_tsv", "pm_benefits", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_pm_benefits_creator_programme", "pm_benefits", ["creator_id", "programme_id"]
    )
    op.create_index("ix_pm_benefits_creator_status", "pm_benefits", ["creator_id", "status"])


def downgrade() -> None:
    op.drop_table("pm_benefits")
    op.drop_table("pm_risks")
