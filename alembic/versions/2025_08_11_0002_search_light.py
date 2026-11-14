"""Add full-text search and trigram indexes for lightweight semantic search.

The generated tsvector expression intentionally indexes only ``title`` and
``description``. The original draft of this migration tried to also fold in
``acceptance`` (a JSONB array) via a ``SELECT … FROM jsonb_array_elements_text``
subquery, but PostgreSQL 18 strictly rejects subqueries inside
``GENERATED ALWAYS AS … STORED`` expressions. Older PostgreSQL versions
silently created the column as plain (non-generated) and ``tsv`` was always
``NULL``; revision ``2026_03_10_0010`` later dropped + recreated the column
with the title+description-only expression. The expression here now matches
that post-0010 production reality so a fresh PG18 database can run the full
migration chain end-to-end. The pg_trgm and unaccent extensions are still
installed for ad-hoc query-time use, but unaccent is intentionally not used
inside the generated column (PG18 requires IMMUTABLE expressions; unaccent
is STABLE).

Revision ID: 2025_08_11_0002
Revises: 2025_08_11_0001
Create Date: 2025-08-11 00:10:00
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "2025_08_11_0002"
down_revision = "2025_08_11_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Enable extensions and add generated tsvector plus GIN/GIST indexes."""
    # Install extensions required for lightweight semantic search. pg_trgm provides
    # trigram similarity and the unaccent extension strips diacritics for better
    # matching. These commands are safe to run repeatedly.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    # Add a generated tsvector column that indexes title and description.
    # PG18 rejects subqueries in generated column expressions; acceptance was
    # effectively missing from FTS anyway since 0010 dropped it. unaccent() is
    # also dropped here because PG18 enforces IMMUTABLE on generated-column
    # expressions and unaccent is STABLE (matches 0010's known-good shape).
    op.execute(
        """
        ALTER TABLE requirements
        ADD COLUMN IF NOT EXISTS tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(title, '') || ' ' || coalesce(description, '')
            )
        ) STORED
        """
    )

    # Create a GIN index on the tsvector column for full‑text search
    op.execute("CREATE INDEX IF NOT EXISTS idx_requirements_tsv ON requirements USING GIN (tsv)")

    # Create trigram indexes on title and description to support similarity matching
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_requirements_title_trgm ON requirements USING GIN (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_requirements_desc_trgm ON requirements USING GIN (description gin_trgm_ops)"
    )


def downgrade() -> None:
    """Remove search extensions and indexes."""
    op.execute("DROP INDEX IF EXISTS idx_requirements_desc_trgm")
    op.execute("DROP INDEX IF EXISTS idx_requirements_title_trgm")
    op.execute("DROP INDEX IF EXISTS idx_requirements_tsv")
    op.execute("ALTER TABLE requirements DROP COLUMN IF EXISTS tsv")
    op.execute("DROP EXTENSION IF EXISTS unaccent")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
