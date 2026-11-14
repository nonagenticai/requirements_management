"""Retire the PM project type ``py2rust``; the rewrite chain becomes an ``it`` flow (George 2026-11-07).

The ``py2rust`` project type (``2026_11_03_0079``) is retired, not deleted. Measured on prod
2026-11-07: **2** ``pm_projects`` rows reference it, both in tenant
``5f868d42-03f8-51f0-a93c-3e83070dc196`` — one is in phase ``setup``, the other is ``closed``.
``pm_projects.project_type_id`` is ``ON DELETE RESTRICT``, so a delete is refused while they
exist, and deleting the type would destroy the only record of what those projects are. The type
therefore keeps every row (its phases, its placements, its WIP limit) and gains ``retired_at``:
``GET /pm/project-types`` still lists it, the existing projects still read their type and
current phase, and any NEW project (or type change) naming it is refused with 422
``project_type_retired``.

The rewrite chain lives on ``it`` from now on. ``it`` already places ``rewrite_plan`` in
``analysis`` (R228/R333); this migration backfills the three remaining ``rewrite_*`` card types
into ``it``'s ``execution`` phase — the placement the live conveyor tenant has — for every
tenant whose ``it`` type selects that phase and holds those card types. ``is_entry`` is false
(the phase does not start with them) and the row carries the owning project type's
``tenant_id``. ``ON CONFLICT DO NOTHING`` keeps it idempotent and safe where a tenant already
holds a placement.

No RLS change: the row-level tenant policy already covers ``pm_project_type_card_types``.

Downgrade: drops ``retired_at`` (the column has no other writer) and KEEPS the backfilled
placements -- pre-prod is forward-only and a placement may already be in use; it raises a
NOTICE saying so rather than deleting rows. The type itself is never touched either way.

Revision ID: 2026_11_07_0083
Revises: 2026_11_04_0082
Create Date: 2026-11-07
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_11_07_0083"
down_revision = "2026_11_04_0082"
branch_labels = None
depends_on = None

RETIRED_TYPE = "py2rust"
HOST_TYPE = "it"
PHASE = "execution"
CARD_TYPES = ("rewrite_cutover_readiness", "rewrite_parity_qa", "rewrite_slice")


def upgrade() -> None:
    op.add_column(
        "pm_project_types", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True)
    )
    # The type may not START a project any more; every row stays so history still reads.
    op.execute(
        "UPDATE pm_project_types SET retired_at = now() "
        f"WHERE name = '{RETIRED_TYPE}' AND retired_at IS NULL"
    )
    # The three held rewrite_* types join `it`'s execution phase (rewrite_plan is already in
    # analysis via the v4 design). tenant_id is the owning project type's, as on every row.
    names = ", ".join(f"'{n}'" for n in CARD_TYPES)
    op.execute(
        "INSERT INTO pm_project_type_card_types "
        "(project_type_id, phase_id, card_type_id, tenant_id, is_entry) "
        "SELECT t.id, tp.phase_id, ct.id, t.tenant_id, false "
        "FROM pm_project_types t "
        "JOIN pm_project_type_phases tp ON tp.project_type_id = t.id "
        "JOIN pm_project_phases ph ON ph.id = tp.phase_id "
        "JOIN pm_card_types ct ON ct.tenant_id = t.tenant_id "
        f"WHERE t.name = '{HOST_TYPE}' AND ph.name = '{PHASE}' "
        f"AND ct.name IN ({names}) "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.drop_column("pm_project_types", "retired_at")
    # Forward-only: the placements may already be in use. Say so rather than delete them.
    op.get_bind().exec_driver_sql(
        "DO $$ BEGIN RAISE NOTICE '2026_11_07_0083 downgrade: the three rewrite_* placements "
        "backfilled into it/execution were KEPT (pre-prod is forward-only; a placement may "
        "already be in use). Drop them by hand if the old catalogue is really needed.'; END $$"
    )
