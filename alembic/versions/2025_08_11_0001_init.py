"""Initial schema creation.

Revision ID: 2025_08_11_0001
Revises: None
Create Date: 2025-08-11 00:00:00
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "2025_08_11_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create core tables for the application."""
    op.create_table(
        "requirements",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False, unique=True, index=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column(
            "links",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "acceptance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("effort", sa.Float, nullable=False),
        sa.Column("reach", sa.Float, server_default="0", nullable=False),
        sa.Column("impact", sa.Float, server_default="0", nullable=False),
        sa.Column("confidence", sa.Float, server_default="0.5", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    """Drop core tables."""
    op.drop_table("requirements")
