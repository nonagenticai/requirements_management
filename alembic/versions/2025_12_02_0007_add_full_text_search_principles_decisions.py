"""Add full-text search and trigram indexes for principles and decisions tables.

The original draft of this migration tried to fold JSONB-array text
(``implications`` / ``tags`` / ``alternatives``) into the generated tsvector
expressions via ``SELECT … FROM jsonb_array_elements_text`` subqueries, and
also wrapped each text column in ``unaccent(...)``. PostgreSQL 18 strictly
rejects both of those inside ``GENERATED ALWAYS AS … STORED``: subqueries
are forbidden outright, and ``unaccent`` is STABLE rather than IMMUTABLE so
it cannot appear in a generated-column expression either. Older PostgreSQL
versions silently created the columns as plain (non-generated), so ``tsv``
was always ``NULL`` — which means the JSONB-array contributions were never
actually indexed in production anyway.

This migration now uses the same simple, IMMUTABLE-safe shape that
``2026_03_10_0010_fix_requirements_tsv_coalesce.py`` introduced for the
``requirements`` table: ``to_tsvector('english', coalesce(<col>, '') || ...)``
over the plain text columns only. JSONB-array text is dropped from FTS,
matching production reality. The pg_trgm and unaccent extensions are still
available (installed by 0002) for ad-hoc query-time use.

Revision ID: 2025_12_02_0007
Revises: 2025_11_28_0006
Create Date: 2025-12-02 00:00:00
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "2025_12_02_0007"
down_revision = "2025_11_28_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add full-text search support for principles and decisions tables.

    This migration adds:
    1. Generated tsvector columns for full-text search
    2. GIN indexes on tsvector columns for fast FTS queries
    3. Trigram GIN indexes on text fields for fuzzy/similarity matching
    """

    # ========================================================================
    # PRINCIPLES TABLE
    # ========================================================================

    # Add generated tsvector column for principles over title + statement +
    # rationale. PG18 rejects subqueries and STABLE functions (unaccent) in
    # generated column expressions; JSONB-array contributions were silently
    # missing from FTS anyway since the original 0007 created the column as
    # plain non-generated (always NULL).
    op.execute(
        """
        ALTER TABLE principles
        ADD COLUMN IF NOT EXISTS tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(title, '') || ' ' ||
                coalesce(statement, '') || ' ' ||
                coalesce(rationale, '')
            )
        ) STORED
        """
    )

    # Create GIN index on tsvector for full-text search
    op.execute("CREATE INDEX IF NOT EXISTS idx_principles_tsv ON principles USING GIN (tsv)")

    # Create trigram indexes for fuzzy matching on key text fields
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_principles_title_trgm ON principles USING GIN (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_principles_statement_trgm ON principles USING GIN (statement gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_principles_rationale_trgm ON principles USING GIN (rationale gin_trgm_ops)"
    )

    # Combined search index for multi-field queries
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_principles_search_trgm
        ON principles
        USING GIN ((title || ' ' || statement || ' ' || coalesce(rationale, '')) gin_trgm_ops)
        """
    )

    # ========================================================================
    # DECISIONS TABLE
    # ========================================================================

    # Add generated tsvector column for decisions over title + context +
    # decision + consequences. Same PG18 constraints as the principles
    # column above: no subqueries, no STABLE-function (unaccent) calls.
    op.execute(
        """
        ALTER TABLE decisions
        ADD COLUMN IF NOT EXISTS tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(title, '') || ' ' ||
                coalesce(context, '') || ' ' ||
                coalesce(decision, '') || ' ' ||
                coalesce(consequences, '')
            )
        ) STORED
        """
    )

    # Create GIN index on tsvector for full-text search
    op.execute("CREATE INDEX IF NOT EXISTS idx_decisions_tsv ON decisions USING GIN (tsv)")

    # Create trigram indexes for fuzzy matching on key text fields
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_title_trgm ON decisions USING GIN (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_context_trgm ON decisions USING GIN (context gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_decision_trgm ON decisions USING GIN (decision gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_consequences_trgm ON decisions USING GIN (consequences gin_trgm_ops)"
    )

    # Combined search index for multi-field queries
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decisions_search_trgm
        ON decisions
        USING GIN ((title || ' ' || context || ' ' || decision || ' ' || coalesce(consequences, '')) gin_trgm_ops)
        """
    )


def downgrade() -> None:
    """Remove full-text search support from principles and decisions tables."""

    # Remove decisions indexes and column
    op.execute("DROP INDEX IF EXISTS idx_decisions_search_trgm")
    op.execute("DROP INDEX IF EXISTS idx_decisions_consequences_trgm")
    op.execute("DROP INDEX IF EXISTS idx_decisions_decision_trgm")
    op.execute("DROP INDEX IF EXISTS idx_decisions_context_trgm")
    op.execute("DROP INDEX IF EXISTS idx_decisions_title_trgm")
    op.execute("DROP INDEX IF EXISTS idx_decisions_tsv")
    op.execute("ALTER TABLE decisions DROP COLUMN IF EXISTS tsv")

    # Remove principles indexes and column
    op.execute("DROP INDEX IF EXISTS idx_principles_search_trgm")
    op.execute("DROP INDEX IF EXISTS idx_principles_rationale_trgm")
    op.execute("DROP INDEX IF EXISTS idx_principles_statement_trgm")
    op.execute("DROP INDEX IF EXISTS idx_principles_title_trgm")
    op.execute("DROP INDEX IF EXISTS idx_principles_tsv")
    op.execute("ALTER TABLE principles DROP COLUMN IF EXISTS tsv")
