"""Add origin columns to requirements (pilot for asset-bindings plan).

Plan ref: ~/.claude/plans/fr-asset-bindings-and-reuse.md (Phase B)

Origin captures provenance of an asset (where it was created) and is
orthogonal to bindings (which project(s) it is currently in scope for).
Three columns + one tracker:

  origin_kind            'tenant' | 'project' | 'workflow'
  origin_project_id      UUID, NULL unless origin_kind = 'project'
  origin_workflow_run_id text, NULL unless origin_kind = 'workflow'
  created_by_user_id     text   (subject_id; empty string default for backfill)

All existing rows are backfilled to origin_kind='tenant', preserving
the human-created provenance — they predate n8n-authored assets and
weren't tied to projects via the new bindings model.

The CHECK constraints pin each origin_kind to the corresponding
identifier column (project_id required for 'project'; workflow_run_id
required for 'workflow'; both NULL for 'tenant').

Pilot scope: requirements only. Principles + decisions get the same
shape in Phase G of the plan (one migration per kind).

Forward-only — pre-prod, no rollback shims required.

Revision ID: 2026_05_19_0024
Revises: 2026_05_06_0023
Create Date: 2026-05-19
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_05_19_0024"
down_revision = "2026_05_06_0023"
branch_labels = None
depends_on = None


ORIGIN_TABLES = ("requirements",)


def upgrade() -> None:
    for table in ORIGIN_TABLES:
        op.add_column(
            table,
            sa.Column(
                "origin_kind",
                sa.String(length=20),
                nullable=False,
                server_default="tenant",
            ),
        )
        op.add_column(
            table,
            sa.Column("origin_project_id", sa.Uuid(), nullable=True),
        )
        op.add_column(
            table,
            sa.Column("origin_workflow_run_id", sa.String(length=200), nullable=True),
        )
        op.add_column(
            table,
            sa.Column(
                "created_by_user_id",
                sa.String(length=255),
                nullable=False,
                server_default="",
            ),
        )
        op.create_check_constraint(
            f"ck_{table}_origin_kind",
            table,
            "origin_kind IN ('tenant', 'project', 'workflow')",
        )
        op.create_check_constraint(
            f"ck_{table}_origin_consistency",
            table,
            "(origin_kind = 'tenant'   AND origin_project_id IS NULL     AND origin_workflow_run_id IS NULL) OR "
            "(origin_kind = 'project'  AND origin_project_id IS NOT NULL AND origin_workflow_run_id IS NULL) OR "
            "(origin_kind = 'workflow' AND origin_workflow_run_id IS NOT NULL)",
        )


def downgrade() -> None:
    for table in ORIGIN_TABLES:
        op.drop_constraint(f"ck_{table}_origin_consistency", table, type_="check")
        op.drop_constraint(f"ck_{table}_origin_kind", table, type_="check")
        op.drop_column(table, "created_by_user_id")
        op.drop_column(table, "origin_workflow_run_id")
        op.drop_column(table, "origin_project_id")
        op.drop_column(table, "origin_kind")
