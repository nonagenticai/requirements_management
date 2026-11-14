"""Append-only card ledger (``pm_card_ledger``): facts and claims per Planka card.

Design ruling R350 (1) (n8n-designs #117, ``design/choices/v4/README.md``): what a card's
flows learned is written to RM as ledger rows instead of living in Planka comments. A
``fact`` is recorded as-is; a ``claim`` may carry laya's score, verdict and reason (``laya_score``,
``accepted``, ``reason``), which a fact never does (CHECK ``ck_pm_card_ledger_laya_claim_only``).
The ledger is keyed by ``(tenant_id, planka_card_id)`` only and references no other table.

Append-only: no update or delete route. ``tenant_id`` is a NOT NULL ``uuid`` column, so the
table joins the RLS tenant-isolation backstop with the strict policy, copied from
``2026_09_23_0043``.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_11_04_0080
Revises: 2026_11_03_0079
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_11_04_0080"
down_revision = "2026_11_03_0079"
branch_labels = None
depends_on = None

_TABLE = "pm_card_ledger"
_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("planka_card_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("laya_score", sa.Double(), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('fact', 'claim')", name="ck_pm_card_ledger_kind"),
        sa.CheckConstraint(
            "kind = 'claim' OR (laya_score IS NULL AND accepted IS NULL AND reason IS NULL)",
            name="ck_pm_card_ledger_laya_claim_only",
        ),
    )
    op.create_index(
        "ix_pm_card_ledger_tenant_card_created",
        _TABLE,
        ["tenant_id", "planka_card_id", "created_at", "id"],
        unique=False,
    )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_index("ix_pm_card_ledger_tenant_card_created", table_name=_TABLE)
    op.drop_table(_TABLE)
