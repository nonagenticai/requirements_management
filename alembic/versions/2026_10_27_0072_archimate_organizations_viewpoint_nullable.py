"""ArchiMate folder trees on the model; a view's viewpoint becomes optional.

* ``modeling_models.organizations`` JSONB NULL — the model's folder tree (Open Group
  ``organizations``; Archi folders) in the snapshot shape
  ``[{id, name, documentation, items[], children[]}]``. NULL = no tree supplied (an
  exchange export then writes Archi's default folders). It is model content: part of the
  revision manifest, written by ``PUT /modeling/models/{model_id}/organizations`` and the
  ``organization`` change-set op, and every write bumps the model head. Items that no
  longer resolve to a concept, relationship or view of the model are dropped whenever the
  tree is read. No RLS change: ``modeling_models`` already carries ``tenant_isolation``.
* ``architecture_views.viewpoint`` loses NOT NULL: NULL = no viewpoint (Archi's "None"),
  which every validator and the renderer treat as unfiltered. Existing rows keep their
  value.

Downgrade drops the folder trees and gives views without a viewpoint ``layered`` (the
viewpoint that admits every concept) before restoring NOT NULL.

Revision ID: 2026_10_27_0072
Revises: 2026_10_27_0071
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_27_0072"
down_revision = "2026_10_27_0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "modeling_models",
        sa.Column("organizations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.alter_column("architecture_views", "viewpoint", existing_type=sa.String(50), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE architecture_views SET viewpoint = 'layered' WHERE viewpoint IS NULL")
    op.alter_column("architecture_views", "viewpoint", existing_type=sa.String(50), nullable=False)
    op.drop_column("modeling_models", "organizations")
