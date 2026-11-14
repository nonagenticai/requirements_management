"""Add ``is_synthetic`` flag + composite index to architecture_elements.

Synthetic elements are scaffolding rows produced by tools (gap proposals,
view generators, etc.) that should be hidden from default CRUD listings
unless the caller explicitly opts in. The composite ``(tenant_id,
is_synthetic)`` index supports the default-filter list query without a
sequential scan.

Revision ID: 2026_05_06_0021
Revises: 2026_05_06_0020
Create Date: 2026-05-06
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_05_06_0021"
down_revision = "2026_05_06_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "architecture_elements",
        sa.Column(
            "is_synthetic",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_arch_elements_synthetic",
        "architecture_elements",
        ["tenant_id", "is_synthetic"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_arch_elements_synthetic", table_name="architecture_elements")
    op.drop_column("architecture_elements", "is_synthetic")
