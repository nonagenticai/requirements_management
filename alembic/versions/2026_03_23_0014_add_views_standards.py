"""Add architecture_views and architecture_standards tables.

Revision ID: 2026_03_23_0014
Revises: 2026_03_23_0013
Create Date: 2026-03-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision = "2026_03_23_0014"
down_revision = "2026_03_23_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # architecture_views
    # ------------------------------------------------------------------
    columns = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        sa.Column("viewpoint", sa.String(length=50), nullable=False),
        sa.Column(
            "element_ids",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="[]",
        ),
        sa.Column(
            "relationship_ids",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="[]",
        ),
        sa.Column(
            "layout_data",
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
    ]

    op.create_table("architecture_views", *columns)

    # Single-column indexes
    op.create_index(
        op.f("ix_architecture_views_slug"),
        "architecture_views",
        ["slug"],
    )
    op.create_index(
        op.f("ix_architecture_views_creator_id"),
        "architecture_views",
        ["creator_id"],
    )
    op.create_index(
        op.f("ix_architecture_views_project_id"),
        "architecture_views",
        ["project_id"],
    )

    # Composite indexes
    op.create_unique_constraint(
        "uq_arch_views_creator_slug",
        "architecture_views",
        ["creator_id", "slug"],
    )
    op.create_index(
        "ix_arch_views_creator_viewpoint",
        "architecture_views",
        ["creator_id", "viewpoint"],
    )
    op.create_index(
        "ix_arch_views_creator_project",
        "architecture_views",
        ["creator_id", "project_id"],
    )

    # Full-text search: tsvector column + GIN index (PostgreSQL only)
    if is_pg:
        op.execute("SELECT 1")  # verify PostgreSQL connectivity
        op.execute(
            """
            ALTER TABLE architecture_views
            ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('english',
                    coalesce(title, '') || ' ' || coalesce(description, '')
                )
            ) STORED
            """
        )
        op.execute("CREATE INDEX ix_arch_views_tsv ON architecture_views USING GIN (tsv)")

    # ------------------------------------------------------------------
    # architecture_standards
    # ------------------------------------------------------------------
    op.create_table(
        "architecture_standards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        sa.Column("standard_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="assess"),
        sa.Column(
            "compliance_level",
            sa.String(length=20),
            nullable=False,
            server_default="recommended",
        ),
        sa.Column(
            "related_principle_ids",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="[]",
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
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

    # Single-column indexes
    op.create_index(
        op.f("ix_architecture_standards_slug"),
        "architecture_standards",
        ["slug"],
    )
    op.create_index(
        op.f("ix_architecture_standards_creator_id"),
        "architecture_standards",
        ["creator_id"],
    )
    op.create_index(
        op.f("ix_architecture_standards_project_id"),
        "architecture_standards",
        ["project_id"],
    )

    # Composite indexes
    op.create_unique_constraint(
        "uq_arch_standards_creator_slug",
        "architecture_standards",
        ["creator_id", "slug"],
    )
    op.create_index(
        "ix_arch_standards_creator_type",
        "architecture_standards",
        ["creator_id", "standard_type"],
    )
    op.create_index(
        "ix_arch_standards_creator_status",
        "architecture_standards",
        ["creator_id", "status"],
    )
    op.create_index(
        "ix_arch_standards_creator_compliance",
        "architecture_standards",
        ["creator_id", "compliance_level"],
    )
    op.create_index(
        "ix_arch_standards_creator_project",
        "architecture_standards",
        ["creator_id", "project_id"],
    )

    # Full-text search: tsvector column + GIN index (PostgreSQL only)
    if is_pg:
        op.execute(
            """
            ALTER TABLE architecture_standards
            ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('english',
                    coalesce(title, '') || ' ' || coalesce(description, '')
                )
            ) STORED
            """
        )
        op.execute("CREATE INDEX ix_arch_standards_tsv ON architecture_standards USING GIN (tsv)")


def downgrade() -> None:
    # Drop tsv indexes first (PostgreSQL only)
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_arch_standards_tsv")

    op.drop_table("architecture_standards")

    if conn.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_arch_views_tsv")

    op.drop_table("architecture_views")
