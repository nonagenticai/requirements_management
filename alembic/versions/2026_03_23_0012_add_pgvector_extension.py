"""Enable pgvector extension for vector similarity search.

Must run before any migration that adds vector columns.
PostgreSQL 18 on mf1 already has the pgvector package installed;
this just enables the extension in the database.

Skips on non-PostgreSQL (SQLite tests).

Revision ID: 2026_03_23_0012
Revises: 2026_03_23_0011
Create Date: 2026-03-23
"""

from alembic import op

revision = "2026_03_23_0012"
down_revision = "2026_03_23_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    op.execute("DROP EXTENSION IF EXISTS vector")
