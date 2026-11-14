"""PM project capabilities: the PM's per-capability consult decision (R302).

George 2026-10-17: at project setup the PM consults every capability (``cap.<c>.consult``)
and stores, per selected capability, how much and for what it is involved. New columns on
``pm_project_capabilities``, all optional so a selection written before R302 stays valid:

* ``involvement`` — none | light | standard | heavy (CHECK);
* ``purpose`` — what the capability is involved for;
* ``rigor`` / ``complexity`` — low | medium | high (CHECK);
* ``consult`` — the raw consult answer (likely_card_types, operation_order, questions …);
* ``order_after`` — capability names whose work must precede this one (the PM's ordering);
  NOT NULL, default ``{}``.

No RLS change: the table already carries the strict-uuid ``tenant_isolation`` policy
(``2026_10_15_0059``) and policies are row-level, so new columns are covered.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_17_0060
Revises: 2026_10_15_0059
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_17_0060"
down_revision = "2026_10_15_0059"
branch_labels = None
depends_on = None

_TABLE = "pm_project_capabilities"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("involvement", sa.String(length=16), nullable=True))
    op.add_column(_TABLE, sa.Column("purpose", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("rigor", sa.String(length=16), nullable=True))
    op.add_column(_TABLE, sa.Column("complexity", sa.String(length=16), nullable=True))
    op.add_column(
        _TABLE, sa.Column("consult", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "order_after",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_pm_project_capabilities_involvement",
        _TABLE,
        "involvement IN ('none', 'light', 'standard', 'heavy')",
    )
    op.create_check_constraint(
        "ck_pm_project_capabilities_rigor", _TABLE, "rigor IN ('low', 'medium', 'high')"
    )
    op.create_check_constraint(
        "ck_pm_project_capabilities_complexity",
        _TABLE,
        "complexity IN ('low', 'medium', 'high')",
    )


def downgrade() -> None:
    for name in ("complexity", "rigor", "involvement"):
        op.drop_constraint(f"ck_pm_project_capabilities_{name}", _TABLE, type_="check")
    for col in ("order_after", "consult", "complexity", "rigor", "purpose", "involvement"):
        op.drop_column(_TABLE, col)
