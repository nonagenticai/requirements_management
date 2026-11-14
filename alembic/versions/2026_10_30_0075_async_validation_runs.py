"""Background validation of large sealed revisions: ``queued`` / ``running`` runs.

A sealed revision above ``MODEL_SYNC_MAX_CONCEPTS`` is validated in-process in the
background (up to ``MODEL_ASYNC_MAX_CONCEPTS``). Additive only:

* ``status`` also admits ``queued`` and ``running``;
* ``error`` — why a run ``failed`` (NULL otherwise);
* ``owner_boot_id`` — the process boot that owns a ``queued``/``running`` run. A run whose
  owner is not the current boot was orphaned by a restart and is marked ``failed``
  (tenant-scoped, so it needs no cross-tenant read past RLS). NULL for synchronous runs.

Revision ID: 2026_10_30_0075
Revises: 2026_10_30_0074
Create Date: 2026-10-30
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_30_0075"
down_revision = "2026_10_30_0074"
branch_labels = None
depends_on = None

_CK = "ck_modeling_validation_runs_status"


def upgrade() -> None:
    op.add_column("modeling_validation_runs", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("modeling_validation_runs", sa.Column("owner_boot_id", sa.Uuid(), nullable=True))
    op.drop_constraint(_CK, "modeling_validation_runs", type_="check")
    op.create_check_constraint(
        _CK,
        "modeling_validation_runs",
        "status IN ('queued', 'running', 'complete', 'incomplete', 'failed')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE modeling_validation_runs SET status = 'failed' "
        "WHERE status IN ('queued', 'running')"
    )
    op.drop_constraint(_CK, "modeling_validation_runs", type_="check")
    op.create_check_constraint(
        _CK, "modeling_validation_runs", "status IN ('complete', 'incomplete', 'failed')"
    )
    op.drop_column("modeling_validation_runs", "owner_boot_id")
    op.drop_column("modeling_validation_runs", "error")
