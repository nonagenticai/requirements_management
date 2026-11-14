"""Add creator_id field to requirements, principles, and decisions

Revision ID: 2025_11_12_0005
Revises: 2025_10_13_0004
Create Date: 2025-11-12 00:00:00
"""

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision = "2025_11_12_0005"
down_revision = "2025_10_13_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add creator_id field to all tables and populate existing records with UUIDv7."""

    # Create the gen_random_uuid() extension if it doesn't exist (for UUIDv7 fallback)
    # Note: PostgreSQL doesn't have built-in UUIDv7, so we'll use UUIDv4 as a reasonable substitute
    op.execute(text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))

    # Add creator_id to requirements table
    op.add_column(
        "requirements",
        sa.Column("creator_id", sa.String(length=100), nullable=True),
    )

    # Update existing requirements with random UUIDs
    op.execute(
        text(
            "UPDATE requirements SET creator_id = gen_random_uuid()::text WHERE creator_id IS NULL"
        )
    )

    # Make the column non-nullable after populating
    op.alter_column("requirements", "creator_id", nullable=False)

    # Create index for better query performance on bulk deletes
    op.create_index("ix_requirements_creator_id", "requirements", ["creator_id"])

    # Add creator_id to principles table
    op.add_column(
        "principles",
        sa.Column("creator_id", sa.String(length=100), nullable=True),
    )

    # Update existing principles with random UUIDs
    op.execute(
        text("UPDATE principles SET creator_id = gen_random_uuid()::text WHERE creator_id IS NULL")
    )

    # Make the column non-nullable after populating
    op.alter_column("principles", "creator_id", nullable=False)

    # Create index for better query performance on bulk deletes
    op.create_index("ix_principles_creator_id", "principles", ["creator_id"])

    # Add creator_id to decisions table
    op.add_column(
        "decisions",
        sa.Column("creator_id", sa.String(length=100), nullable=True),
    )

    # Update existing decisions with random UUIDs
    op.execute(
        text("UPDATE decisions SET creator_id = gen_random_uuid()::text WHERE creator_id IS NULL")
    )

    # Make the column non-nullable after populating
    op.alter_column("decisions", "creator_id", nullable=False)

    # Create index for better query performance on bulk deletes
    op.create_index("ix_decisions_creator_id", "decisions", ["creator_id"])


def downgrade() -> None:
    """Remove creator_id field from all tables."""

    # Drop indexes
    op.drop_index("ix_decisions_creator_id", table_name="decisions")
    op.drop_index("ix_principles_creator_id", table_name="principles")
    op.drop_index("ix_requirements_creator_id", table_name="requirements")

    # Remove creator_id columns
    op.drop_column("decisions", "creator_id")
    op.drop_column("principles", "creator_id")
    op.drop_column("requirements", "creator_id")
