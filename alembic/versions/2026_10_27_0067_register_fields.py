"""Register fields on risks, decisions and requirements (REG-1, fr Registers page).

Spec: ``~/.claude/plans/program/20-designs/FR-PROJECT-HOME-REDESIGN-2026-10-26.md`` §7.4.
Every new column is NULLABLE.

* ``pm_risks``, ``decisions``, ``requirements``: ``phase varchar(64)`` (a phase NAME from the
  tenant's ``pm_project_phases`` catalogue — per tenant and extensible, so no CHECK),
  ``card_id varchar(64)`` (the Planka card; same width as ``pm_activities.planka_card_id``)
  and ``raised_at timestamptz DEFAULT now()``.
* ``pm_risks``: ``due_at``, ``last_reviewed_at`` (timestamptz).
* ``requirements``: ``status varchar(30)``, ``owner varchar(100)``.
* ``decisions``: ``decided_by_kind varchar(16)`` with CHECK ``ck_decisions_decided_by_kind``
  (human | agent | rule; NULL passes) and ``decided_at timestamptz``.

``raised_at`` is backfilled from ``created_at`` (rows with a NULL ``created_at`` stay NULL); the
column is added WITHOUT a default and the default is set after the backfill, so existing rows
never take the migration's own timestamp. No RLS change: the policy is row-level.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_27_0067
Revises: 2026_10_26_0066
Create Date: 2026-10-27
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_27_0067"
down_revision = "2026_10_26_0066"
branch_labels = None
depends_on = None

TABLES = ("pm_risks", "decisions", "requirements")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("phase", sa.String(64), nullable=True))
        op.add_column(table, sa.Column("card_id", sa.String(64), nullable=True))
        op.add_column(table, sa.Column("raised_at", TS, nullable=True))
        op.execute(
            f"UPDATE {table} SET raised_at = created_at "  # noqa: S608 — fixed table names
            "WHERE raised_at IS NULL AND created_at IS NOT NULL"
        )
        op.alter_column(table, "raised_at", server_default=sa.text("now()"))

    op.add_column("pm_risks", sa.Column("due_at", TS, nullable=True))
    op.add_column("pm_risks", sa.Column("last_reviewed_at", TS, nullable=True))

    op.add_column("requirements", sa.Column("status", sa.String(30), nullable=True))
    op.add_column("requirements", sa.Column("owner", sa.String(100), nullable=True))

    op.add_column("decisions", sa.Column("decided_by_kind", sa.String(16), nullable=True))
    op.add_column("decisions", sa.Column("decided_at", TS, nullable=True))
    op.create_check_constraint(
        "ck_decisions_decided_by_kind",
        "decisions",
        "decided_by_kind IN ('human', 'agent', 'rule')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_decisions_decided_by_kind", "decisions", type_="check")
    for column in ("decided_at", "decided_by_kind"):
        op.drop_column("decisions", column)
    for column in ("owner", "status"):
        op.drop_column("requirements", column)
    for column in ("last_reviewed_at", "due_at"):
        op.drop_column("pm_risks", column)
    for table in TABLES:
        for column in ("raised_at", "card_id", "phase"):
            op.drop_column(table, column)
