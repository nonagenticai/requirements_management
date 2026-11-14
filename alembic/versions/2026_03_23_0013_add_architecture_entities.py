"""Add architecture_elements and architecture_relationships tables.

Revision ID: 2026_03_23_0013
Revises: 2026_03_23_0012
Create Date: 2026-03-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision = "2026_03_23_0013"
down_revision = "2026_03_23_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # architecture_elements
    # ------------------------------------------------------------------
    columns = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        sa.Column("layer", sa.String(length=30), nullable=False),
        sa.Column("element_type", sa.String(length=50), nullable=False),
        sa.Column(
            "properties",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="{}",
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("architecture_state", sa.String(length=20), nullable=True),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
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

    op.create_table("architecture_elements", *columns)

    # Single-column indexes
    op.create_index(
        op.f("ix_architecture_elements_slug"),
        "architecture_elements",
        ["slug"],
    )
    op.create_index(
        op.f("ix_architecture_elements_creator_id"),
        "architecture_elements",
        ["creator_id"],
    )
    op.create_index(
        op.f("ix_architecture_elements_project_id"),
        "architecture_elements",
        ["project_id"],
    )

    # Composite indexes
    op.create_unique_constraint(
        "uq_arch_elements_creator_slug",
        "architecture_elements",
        ["creator_id", "slug"],
    )
    op.create_index(
        "ix_arch_elements_creator_layer",
        "architecture_elements",
        ["creator_id", "layer"],
    )
    op.create_index(
        "ix_arch_elements_creator_type",
        "architecture_elements",
        ["creator_id", "element_type"],
    )
    op.create_index(
        "ix_arch_elements_creator_project",
        "architecture_elements",
        ["creator_id", "project_id"],
    )
    op.create_index(
        "ix_arch_elements_creator_state",
        "architecture_elements",
        ["creator_id", "architecture_state"],
    )

    # Full-text search: tsvector column + GIN index (PostgreSQL only)
    if is_pg:
        op.execute("SELECT 1")  # verify PostgreSQL connectivity
        op.execute(
            """
            ALTER TABLE architecture_elements
            ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('english',
                    coalesce(title, '') || ' ' || coalesce(description, '')
                )
            ) STORED
            """
        )
        op.execute("CREATE INDEX ix_arch_elements_tsv ON architecture_elements USING GIN (tsv)")

    # ------------------------------------------------------------------
    # architecture_relationships
    # ------------------------------------------------------------------
    op.create_table(
        "architecture_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=True),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        sa.Column("source_entity_type", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_entity_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "properties",
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
        op.f("ix_architecture_relationships_slug"),
        "architecture_relationships",
        ["slug"],
    )
    op.create_index(
        op.f("ix_architecture_relationships_creator_id"),
        "architecture_relationships",
        ["creator_id"],
    )
    op.create_index(
        op.f("ix_architecture_relationships_project_id"),
        "architecture_relationships",
        ["project_id"],
    )

    # Composite indexes
    op.create_unique_constraint(
        "uq_arch_rels_creator_slug",
        "architecture_relationships",
        ["creator_id", "slug"],
    )
    op.create_index(
        "ix_arch_rels_creator_source",
        "architecture_relationships",
        ["creator_id", "source_entity_type", "source_id"],
    )
    op.create_index(
        "ix_arch_rels_creator_target",
        "architecture_relationships",
        ["creator_id", "target_entity_type", "target_id"],
    )
    op.create_index(
        "ix_arch_rels_creator_type",
        "architecture_relationships",
        ["creator_id", "relationship_type"],
    )
    op.create_index(
        "ix_arch_rels_creator_project",
        "architecture_relationships",
        ["creator_id", "project_id"],
    )


def downgrade() -> None:
    op.drop_table("architecture_relationships")

    # Drop tsv index/column first (PostgreSQL only)
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_arch_elements_tsv")

    op.drop_table("architecture_elements")
