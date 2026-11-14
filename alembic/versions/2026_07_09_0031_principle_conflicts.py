"""Persisted principle-conflict records (C3 — AA cockpit governance backstop).

Adds ``principle_conflicts``: one row per detected conflict where one side is a
principle and the counterparty is another principle or an agentic procedure.
Detection (``PrincipleConsistencyAgent``) previously threw its verdicts away;
this table captures them so the cockpit's ``principle_conflicts`` block shows
real data. See ``services.conflict_persistence`` for the hybrid lifecycle.

The table carries ``tenant_id`` (NOT NULL) so it must join the RLS tenant-
isolation backstop established in ``2026_07_02_0030``; the strict policy is
applied here for the new table (mirrors ``kag_common.database.rls``; String
tenant_id ⇒ no ``::uuid`` cast). Ships inert (ENABLE-not-FORCE; owner bypass).

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_07_09_0031
Revises: 2026_07_02_0030
Create Date: 2026-07-09
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_07_09_0031"
down_revision = "2026_07_02_0030"
branch_labels = None
depends_on = None

_TABLE = "principle_conflicts"

# Mirror kag_common.database.rls._strict_statements for the single new table.
# tenant_id is a String NOT NULL column → text-to-text predicate (no ::uuid cast).
_GUC = "NULLIF(current_setting('app.current_tenant', true), '')"
_PREDICATE = f"tenant_id = {_GUC}"
_RLS_UPGRADE = [
    f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY",
    f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}",
    f"CREATE POLICY tenant_isolation ON {_TABLE} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})",
]
_RLS_DOWNGRADE = [
    f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}",
    f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY",
]


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("subject_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="tenant"),
        sa.Column(
            "principle_id",
            sa.Uuid(),
            sa.ForeignKey("principles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("counterparty_kind", sa.String(length=16), nullable=False),
        sa.Column("counterparty_id", sa.String(length=255), nullable=False),
        sa.Column("counterparty_hash", sa.String(length=64), nullable=True),
        sa.Column("conflict_type", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column(
            "source",
            sa.String(length=40),
            nullable=False,
            server_default="consistency_agent",
        ),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "principle_id",
            "counterparty_kind",
            "counterparty_id",
            "conflict_type",
            name="uq_principle_conflicts_dedup",
        ),
    )
    op.create_index("ix_principle_conflicts_id", _TABLE, ["id"], unique=False)
    op.create_index("ix_principle_conflicts_tenant_id", _TABLE, ["tenant_id"], unique=False)
    op.create_index("ix_principle_conflicts_subject_id", _TABLE, ["subject_id"], unique=False)
    op.create_index("ix_principle_conflicts_principle_id", _TABLE, ["principle_id"], unique=False)
    op.create_index("ix_principle_conflicts_status", _TABLE, ["status"], unique=False)
    op.create_index(
        "ix_principle_conflicts_tenant_status", _TABLE, ["tenant_id", "status"], unique=False
    )
    op.create_index(
        "ix_principle_conflicts_counterparty",
        _TABLE,
        ["tenant_id", "counterparty_kind", "counterparty_id"],
        unique=False,
    )
    for stmt in _RLS_UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _RLS_DOWNGRADE:
        op.execute(stmt)
    op.drop_index("ix_principle_conflicts_counterparty", table_name=_TABLE)
    op.drop_index("ix_principle_conflicts_tenant_status", table_name=_TABLE)
    op.drop_index("ix_principle_conflicts_status", table_name=_TABLE)
    op.drop_index("ix_principle_conflicts_principle_id", table_name=_TABLE)
    op.drop_index("ix_principle_conflicts_subject_id", table_name=_TABLE)
    op.drop_index("ix_principle_conflicts_tenant_id", table_name=_TABLE)
    op.drop_index("ix_principle_conflicts_id", table_name=_TABLE)
    op.drop_table(_TABLE)
