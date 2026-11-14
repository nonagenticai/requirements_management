"""Add entity_version_history table for version snapshots.

Revision ID: 2026_02_17_0009
Revises: 2025_12_25_0008
Create Date: 2026-02-17 00:00:00

Adds a table to track version history snapshots for all entity types
(requirements, principles, decisions).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision = "2026_02_17_0009"
down_revision = "2025_12_25_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create entity_version_history table."""
    op.create_table(
        "entity_version_history",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("entity_type", sa.String(50), nullable=False, index=True),
        sa.Column("entity_id", sa.Integer(), nullable=False, index=True),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("version_status", sa.String(20), nullable=False),
        sa.Column("snapshot_data", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_version_history_entity",
        "entity_version_history",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_version_history_entity_version",
        "entity_version_history",
        ["entity_type", "entity_id", "version"],
    )


def downgrade() -> None:
    """Drop entity_version_history table."""
    op.drop_table("entity_version_history")
