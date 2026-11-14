"""Fix slug uniqueness to be per-creator instead of global.

Revision ID: 2025_12_25_0008
Revises: 2025_12_02_0007
Create Date: 2025-12-25 00:00:00

This migration changes slug uniqueness from global to per-creator:
1. Drops the existing global unique constraints on slug columns
2. Creates composite unique constraints on (creator_id, slug) for each table

This allows different users to have items with the same slug,
fixing the multi-tenant violation where User A's slug blocked User B.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "2025_12_25_0008"
down_revision = "2025_12_02_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Change slug uniqueness from global to per-creator.

    The init migration (0001) declared each ``slug`` column as
    ``Column(..., unique=True, index=True)``. SQLAlchemy emits this as a
    UNIQUE INDEX named ``ix_<table>_slug`` rather than a UNIQUE CONSTRAINT
    named ``<table>_slug_key``. Both shapes existed historically depending
    on PG/SA versions, so we defensively try both: drop whichever exists.
    """

    # ========================================================================
    # REQUIREMENTS TABLE
    # ========================================================================
    op.execute("ALTER TABLE requirements DROP CONSTRAINT IF EXISTS requirements_slug_key")
    op.execute("DROP INDEX IF EXISTS ix_requirements_slug")
    op.create_unique_constraint(
        "uq_requirements_creator_slug", "requirements", ["creator_id", "slug"]
    )

    # ========================================================================
    # PRINCIPLES TABLE
    # ========================================================================
    op.execute("ALTER TABLE principles DROP CONSTRAINT IF EXISTS principles_slug_key")
    op.execute("DROP INDEX IF EXISTS ix_principles_slug")
    op.create_unique_constraint("uq_principles_creator_slug", "principles", ["creator_id", "slug"])

    # ========================================================================
    # DECISIONS TABLE
    # ========================================================================
    op.execute("ALTER TABLE decisions DROP CONSTRAINT IF EXISTS decisions_slug_key")
    op.execute("DROP INDEX IF EXISTS ix_decisions_slug")
    op.create_unique_constraint("uq_decisions_creator_slug", "decisions", ["creator_id", "slug"])


def downgrade() -> None:
    """Revert to global slug uniqueness via the original UNIQUE INDEX shape.

    WARNING: This will fail if there are duplicate slugs across creators!
    """

    # Requirements table
    op.drop_constraint("uq_requirements_creator_slug", "requirements", type_="unique")
    op.execute("CREATE UNIQUE INDEX ix_requirements_slug ON requirements (slug)")

    # Principles table
    op.drop_constraint("uq_principles_creator_slug", "principles", type_="unique")
    op.execute("CREATE UNIQUE INDEX ix_principles_slug ON principles (slug)")

    # Decisions table
    op.drop_constraint("uq_decisions_creator_slug", "decisions", type_="unique")
    op.execute("CREATE UNIQUE INDEX ix_decisions_slug ON decisions (slug)")
