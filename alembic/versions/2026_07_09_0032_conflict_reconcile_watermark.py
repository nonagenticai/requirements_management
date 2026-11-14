"""Per-tenant watermark for the procedure↔principle conflict reconciler (C3 PR2).

Operational cursor table — one row per tenant holding the max procedure
``updated_at`` the reconciler has processed, so restarts don't re-evaluate every
procedure. Intentionally NOT under the RLS backstop (cross-tenant operational
table swept by an owner-connected background loop; see the model docstring).

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_07_09_0032
Revises: 2026_07_09_0031
Create Date: 2026-07-09
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_07_09_0032"
down_revision = "2026_07_09_0031"
branch_labels = None
depends_on = None

_TABLE = "conflict_reconcile_watermark"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("tenant_id", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column("last_seen_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
