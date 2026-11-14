"""toc_settings.settings NOT NULL — align the DB with the model it already claims.

``TocSettings.settings`` is declared ``Mapped[dict[str, Any]]`` — NON-optional, so
SQLAlchemy's metadata says ``nullable=False``. The column was materialised nullable,
which made this the ONLY genuine (non-cosmetic) divergence in ``alembic check``:
the other ~97 operations are index-naming churn and unreflectable trigram/tsvector
indexes, all of which would be destructive to "fix".

Why the MODEL is the correct side, not the DB:

  * The read path (``services.toc_service.resolve_settings``) merges the column as a
    dict across the project → tenant → global chain. A NULL yields ``None`` and
    raises on the merge — there is no code path that tolerates it.
  * The write path never produces NULL: ``default=dict`` client-side, and the column
    already carries ``server_default '{}'::jsonb``. So NOT NULL is trivially
    satisfiable and every existing insert path already honours it.

Zero-risk in this fleet: production ``requirements_api.toc_settings`` holds **0 rows**
(verified on worker4 2026-08-11, ``count(*)=0, count(*) FILTER (WHERE settings IS NULL)=0``),
so the ALTER takes no meaningful lock and rewrites nothing.

The defensive backfill is kept anyway — it is a no-op on prod but makes the migration
correct for any environment where a NULL was written before the constraint existed.
It sets the same value the server default would have produced, so it cannot invent
semantics.

Forward-only (pre-prod), per repo convention.

Revision ID: 2026_08_11_0039
Revises: 2026_07_20_0038
Create Date: 2026-08-11
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_08_11_0039"
down_revision = "2026_07_20_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op on prod (0 NULL rows); correct for any env that predates the constraint.
    op.execute("UPDATE toc_settings SET settings = '{}'::jsonb WHERE settings IS NULL")
    op.alter_column(
        "toc_settings",
        "settings",
        existing_type=sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
        existing_server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "toc_settings",
        "settings",
        existing_type=sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
        existing_server_default=sa.text("'{}'::jsonb"),
        nullable=True,
    )
