"""Fix requirements tsv to be a proper generated column.

The original migration (0002) used a subquery (jsonb_array_elements_text)
in the GENERATED ALWAYS AS expression, which PostgreSQL does not support.
The column was created as a plain (non-generated) tsvector, always NULL.

This migration drops the broken column and recreates it as a proper
generated column matching the SQLAlchemy model definition (title + description).

Revision ID: 2026_03_10_0010
Revises: 2026_02_17_0009
Create Date: 2026-03-10
"""

from alembic import op

revision = "2026_03_10_0010"
down_revision = "2026_02_17_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop broken tsv column and recreate as proper generated column."""
    op.execute("DROP INDEX IF EXISTS idx_requirements_tsv")
    op.execute("DROP INDEX IF EXISTS ix_requirements_tsv")
    op.execute("ALTER TABLE requirements DROP COLUMN IF EXISTS tsv")
    op.execute(
        """
        ALTER TABLE requirements ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(title, '') || ' ' || coalesce(description, '')
            )
        ) STORED
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_requirements_tsv ON requirements USING GIN (tsv)")


def downgrade() -> None:
    """Remove tsv generated column."""
    op.execute("DROP INDEX IF EXISTS ix_requirements_tsv")
    op.execute("ALTER TABLE requirements DROP COLUMN IF EXISTS tsv")
