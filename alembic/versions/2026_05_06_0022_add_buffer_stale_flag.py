"""Add ``is_stale`` flag to ``pm_buffers``.

CCPM buffers are only meaningful when their ``consumed_days`` and
``penetration_pct`` reflect the current state of the underlying chain
activities. Whenever an activity in the project mutates we mark every
buffer in the project stale; the buffer recalc service flips the flag
back to ``false`` after recomputing. The default is ``true`` so that
freshly-created buffers also surface as needing computation.

Revision ID: 2026_05_06_0022
Revises: 2026_05_06_0021
Create Date: 2026-05-06
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_05_06_0022"
down_revision = "2026_05_06_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pm_buffers",
        sa.Column(
            "is_stale",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("pm_buffers", "is_stale")
