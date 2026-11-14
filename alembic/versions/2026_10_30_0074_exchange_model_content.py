"""Open Group exchange content RM had no slot for.

* ``modeling_models.exchange`` (JSONB, NULL = none): model-level properties, metadata and
  custom viewpoint definitions (snapshot ``exchange``).
* ``architecture_views.viewpoint_ref``: the id of one of the model's custom viewpoints.
* ``architecture_view_objects.view_ref``: a node's diagram-model reference (drill-down
  view); a deleted view clears it.

Additive and nullable: no backfill, no RLS change (same tables, same policy).

Revision ID: 2026_10_30_0074
Revises: 2026_10_27_0073
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "2026_10_30_0074"
down_revision = "2026_10_27_0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("modeling_models", sa.Column("exchange", JSONB(), nullable=True))
    op.add_column("architecture_views", sa.Column("viewpoint_ref", sa.String(100), nullable=True))
    op.add_column(
        "architecture_view_objects",
        sa.Column(
            "view_ref",
            sa.Uuid(),
            sa.ForeignKey(
                "architecture_views.id",
                name="fk_architecture_view_objects_view_ref",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_architecture_view_objects_view_ref", "architecture_view_objects", ["view_ref"]
    )


def downgrade() -> None:
    op.drop_index("ix_architecture_view_objects_view_ref", "architecture_view_objects")
    op.drop_column("architecture_view_objects", "view_ref")
    op.drop_column("architecture_views", "viewpoint_ref")
    op.drop_column("modeling_models", "exchange")
