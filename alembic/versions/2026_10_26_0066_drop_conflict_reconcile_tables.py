"""Drop the conflict-reconciler's operational tables (LLM lane removed).

``conflict_reconcile_watermark`` (``0032``) and ``conflict_reconcile_failure``
(``0040``/``0041``; ``tenant_id`` flipped to ``uuid`` in ``0042``) were the
per-tenant cursor and per-procedure retry queue of the procedure↔principle
conflict reconciler — an LLM background loop. The reconciler, its models and
every reader/writer of these tables were removed with the rest of the LLM lane,
so nothing reads or writes them any more. Their rows are reconciler bookkeeping
only (a cursor and failure streaks), not domain data, so nothing is archived.

NOT dropped: ``principle_conflicts`` — the verdicts themselves stay readable via
``/principle-conflicts`` (fr reads them).

Neither table is under RLS (cross-tenant operational tables, see ``0032``), so
there is no policy to drop.

Downgrade recreates both tables EMPTY in their pre-drop (head ``0065``) shape.

Revision ID: 2026_10_26_0066
Revises: 2026_10_25_0065
Create Date: 2026-10-26
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_26_0066"
down_revision = "2026_10_25_0065"
branch_labels = None
depends_on = None

_WATERMARK = "conflict_reconcile_watermark"
_FAILURE = "conflict_reconcile_failure"


def upgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_FAILURE}")
    op.execute(f"DROP TABLE IF EXISTS {_WATERMARK}")


def downgrade() -> None:
    op.create_table(
        _WATERMARK,
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), primary_key=True, nullable=False),
        sa.Column("last_seen_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_table(
        _FAILURE,
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), primary_key=True, nullable=False),
        sa.Column("counterparty_id", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_failure_kind", sa.String(length=32), nullable=True),
        sa.Column("procedure_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "consecutive_transient_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
