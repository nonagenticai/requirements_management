"""Add Logical Thinking Process (Theory of Constraints) tables.

Three new tables backing the ``/toc`` resource family:

* ``toc_trees``    — one row per LTP structure (io_map / evaporating_cloud /
                     crt / frt / prt / trt). Versioned governance asset with
                     quality (CLR) columns + full-text tsv, mirroring the
                     requirements/architecture entity shape.
* ``toc_nodes``    — statement entities inside a tree (tree_id FK CASCADE).
* ``toc_settings`` — 3-level (global → tenant → project) config; unique on
                     (tenant_id, project_id) with '' as the tenant-level row.

Edges between nodes reuse the existing ``architecture_relationships`` table
(relationship_type ``toc_*``), so no edge table is created here.

Forward-only — pre-prod, no rollback shims required.

Revision ID: 2026_05_31_0028
Revises: 2026_05_27_0027
Create Date: 2026-05-31
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "2026_05_31_0028"
down_revision = "2026_05_27_0027"
branch_labels = None
depends_on = None


def _version_columns() -> list[sa.Column]:
    return [
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
        sa.Column("version_status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("supersedes", sa.String(length=120), nullable=True),
        sa.Column("superseded_by", sa.String(length=120), nullable=True),
        sa.Column("version_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
    ]


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # toc_trees
    # ------------------------------------------------------------------
    op.create_table(
        "toc_trees",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_name", sa.String(length=255), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="tenant"),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        sa.Column("tree_type", sa.String(length=30), nullable=False),
        *_version_columns(),
        sa.Column("origin_kind", sa.String(length=20), nullable=False, server_default="tenant"),
        sa.Column("origin_project_id", sa.Uuid(), nullable=True),
        sa.Column("origin_workflow_run_id", sa.String(length=200), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=255), nullable=False, server_default=""),
        *_timestamp_columns(),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("quality_issues", JSONB(), nullable=True),
        sa.Column("quality_suggestions", JSONB(), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_by_agent", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in ("id", "slug", "tenant_id", "tenant_name", "subject_id", "project_id", "tree_type"):
        op.create_index(op.f(f"ix_toc_trees_{col}"), "toc_trees", [col])
    op.create_unique_constraint("uq_toc_trees_tenant_slug", "toc_trees", ["tenant_id", "slug"])
    op.create_check_constraint(
        "ck_toc_trees_visibility", "toc_trees", "visibility IN ('tenant', 'user')"
    )
    op.create_check_constraint(
        "ck_toc_trees_tree_type",
        "toc_trees",
        "tree_type IN ('io_map', 'evaporating_cloud', 'crt', 'frt', 'prt', 'trt')",
    )
    op.create_index("ix_toc_trees_tenant_subject", "toc_trees", ["tenant_id", "subject_id"])
    op.create_index("ix_toc_trees_tenant_visibility", "toc_trees", ["tenant_id", "visibility"])
    op.create_index("ix_toc_trees_tenant_type", "toc_trees", ["tenant_id", "tree_type"])
    if is_pg:
        op.execute(
            """
            ALTER TABLE toc_trees
            ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('english',
                    coalesce(title, '') || ' ' || coalesce(description, '')
                )
            ) STORED
            """
        )
        op.execute("CREATE INDEX ix_toc_trees_tsv ON toc_trees USING GIN (tsv)")

    # ------------------------------------------------------------------
    # toc_nodes
    # ------------------------------------------------------------------
    op.create_table(
        "toc_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("tree_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("node_kind", sa.String(length=40), nullable=False),
        sa.Column("layout", JSONB(), nullable=True, server_default="{}"),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_name", sa.String(length=255), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="tenant"),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        *_version_columns(),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tree_id"], ["toc_trees.id"], name="fk_toc_nodes_tree_id_toc_trees", ondelete="CASCADE"
        ),
    )
    for col in (
        "id",
        "slug",
        "tree_id",
        "node_kind",
        "tenant_id",
        "tenant_name",
        "subject_id",
        "project_id",
    ):
        op.create_index(op.f(f"ix_toc_nodes_{col}"), "toc_nodes", [col])
    op.create_unique_constraint("uq_toc_nodes_tenant_slug", "toc_nodes", ["tenant_id", "slug"])
    op.create_check_constraint(
        "ck_toc_nodes_visibility", "toc_nodes", "visibility IN ('tenant', 'user')"
    )
    op.create_index("ix_toc_nodes_tenant_subject", "toc_nodes", ["tenant_id", "subject_id"])
    op.create_index("ix_toc_nodes_tenant_visibility", "toc_nodes", ["tenant_id", "visibility"])
    op.create_index("ix_toc_nodes_tree", "toc_nodes", ["tree_id"])

    # ------------------------------------------------------------------
    # toc_settings
    # ------------------------------------------------------------------
    op.create_table(
        "toc_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("settings", JSONB(), nullable=True, server_default="{}"),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_toc_settings_id"), "toc_settings", ["id"])
    op.create_index(op.f("ix_toc_settings_tenant_id"), "toc_settings", ["tenant_id"])
    op.create_unique_constraint(
        "uq_toc_settings_tenant_project", "toc_settings", ["tenant_id", "project_id"]
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_toc_trees_tsv")
    op.drop_table("toc_settings")
    op.drop_table("toc_nodes")
    op.drop_table("toc_trees")
