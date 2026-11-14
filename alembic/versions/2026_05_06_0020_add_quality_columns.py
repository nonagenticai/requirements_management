"""Add quality columns to 9 entity tables (LLM enhancements Phase 1).

Adds five persisted quality columns + a (tenant_id, quality_score) covering
index to the nine entity tables that participate in LLM quality evaluation:

    requirements, principles, decisions,
    architecture_elements, architecture_views,
    pm_activities, pm_risks, pm_benefits, pm_buffers

Columns:
    quality_score             FLOAT      NULL
    quality_issues            JSONB      NULL
    quality_suggestions       JSONB      NULL
    last_evaluated_at         TIMESTAMPTZ NULL
    last_evaluated_by_agent   VARCHAR(128) NULL

The composite index lets the agent dashboard ask "show me low-score X for
tenant Y" without a full table scan. Index names use the same short alias
scheme as 0019 because ``architecture_elements`` would otherwise blow past
PostgreSQL's 63-char identifier limit.

Revision ID: 2026_05_06_0020
Revises: 2026_04_27_0019
Create Date: 2026-05-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "2026_05_06_0020"
down_revision = "2026_04_27_0019"
branch_labels = None
depends_on = None

TABLES = (
    "requirements",
    "principles",
    "decisions",
    "architecture_elements",
    "architecture_views",
    "pm_activities",
    "pm_risks",
    "pm_benefits",
    "pm_buffers",
)

# Short aliases so index names stay under the pg 63-char identifier limit.
# Mirror 0019's SHORT_ALIAS dict for the tables that need shortening.
SHORT_ALIAS: dict[str, str] = {
    "requirements": "requirements",
    "principles": "principles",
    "decisions": "decisions",
    "architecture_elements": "arch_elements",
    "architecture_views": "arch_views",
    "pm_activities": "pm_activities",
    "pm_risks": "pm_risks",
    "pm_benefits": "pm_benefits",
    "pm_buffers": "pm_buffers",
}


def upgrade() -> None:
    for table in TABLES:
        alias = SHORT_ALIAS[table]
        op.add_column(table, sa.Column("quality_score", sa.Float(), nullable=True))
        op.add_column(table, sa.Column("quality_issues", JSONB(), nullable=True))
        op.add_column(table, sa.Column("quality_suggestions", JSONB(), nullable=True))
        op.add_column(
            table,
            sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            table,
            sa.Column("last_evaluated_by_agent", sa.String(length=128), nullable=True),
        )
        op.create_index(
            f"ix_{alias}_quality_score",
            table,
            ["tenant_id", "quality_score"],
            if_not_exists=True,
        )


def downgrade() -> None:
    for table in TABLES:
        alias = SHORT_ALIAS[table]
        op.drop_index(f"ix_{alias}_quality_score", table_name=table)
        op.drop_column(table, "last_evaluated_by_agent")
        op.drop_column(table, "last_evaluated_at")
        op.drop_column(table, "quality_suggestions")
        op.drop_column(table, "quality_issues")
        op.drop_column(table, "quality_score")
