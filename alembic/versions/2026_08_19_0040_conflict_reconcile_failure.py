"""Per-procedure permanent-failure tracking for the conflict reconciler (issue #108).

This table is the reconciler's retry QUEUE. One row per procedure that failed
to evaluate, keyed ``(tenant_id, counterparty_id)``: while a row is OPEN the
procedure BYPASSES the per-tenant watermark gate and is re-asked on the next
cycle regardless of how far the watermark has advanced past it, and the row is
cleared once the procedure evaluates cleanly again. That is what stops a failed
procedure from being silently abandoned the moment the watermark moves on.

``consecutive_failures`` bounds how long that re-asking goes on: past the
quarantine threshold the row stops driving a retry every cycle and drops to a
periodic single re-probe, so a procedure that fails every time settles at a
trickle of LLM calls instead of one per 900s cycle forever. ``last_error`` / ``last_failure_kind`` / ``procedure_fingerprint``
record why, and the fingerprint lets an EDITED procedure re-enter the queue
instead of inheriting its predecessor's quarantine.

(There is no ``retry_floor``: the watermark is no longer clamped below the
earliest failure. An open row here is the entire retry mechanism.)

Operational cursor-style table, swept cross-tenant by an owner-connected
background loop. Intentionally NOT under the RLS backstop, exactly like
``conflict_reconcile_watermark`` (see the model docstring).

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_08_19_0040
Revises: 2026_08_11_0039
Create Date: 2026-08-19
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_08_19_0040"
down_revision = "2026_08_11_0039"
branch_labels = None
depends_on = None

_TABLE = "conflict_reconcile_failure"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("tenant_id", sa.String(length=255), primary_key=True, nullable=False),
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
    )
    # The reconciler loads a tenant's whole failure set once per cycle, keyed by
    # tenant alone; the composite PK's leading column already serves that, so no
    # extra index is created. A partial index on quarantined rows was considered
    # and rejected: the table is one row per *failing* procedure, which live
    # evidence puts at single digits per tenant.


def downgrade() -> None:
    op.drop_table(_TABLE)
