"""Retire the PM card type ``phase_coverage_check`` in every tenant (R347 (4)).

George 2026-11-04, R347 (4): ``project_phase_start`` judges phase coverage itself and the four
coverage flows are archived, so the card type ``phase_coverage_check`` (added by #264, placed by
``2026_11_03_0079`` on ``py2rust``) is retired. The code default
(``src/data/pm_catalogue_defaults.json``) no longer carries it; this migration removes the rows
existing tenants hold. Measured on prod before this shipped: 6 ``pm_card_types`` rows (one per
tenant), 34 ``pm_project_type_card_types`` placements, 0 recipe items, 0 cards of the type.

Upgrade, in order, across all tenants (the migration role is not subject to the tenant RLS
policy, as for ``2026_11_03_0079``): delete every row that references a ``pm_card_types`` row
named ``phase_coverage_check`` through a foreign key — ``pm_project_type_card_types`` (its
placements), ``pm_recipe_items`` and ``pm_wip_limits`` (all three ``ON DELETE RESTRICT``; on
head ``2026_11_04_0081`` no other table references ``pm_card_types.id``) — then the card type
rows themselves.

Forward-only: the downgrade raises. The rows are gone and nothing recreates them.

Revision ID: 2026_11_04_0082
Revises: 2026_11_04_0081
Create Date: 2026-11-04
"""

from alembic import op

revision = "2026_11_04_0082"
down_revision = "2026_11_04_0081"
branch_labels = None
depends_on = None

_RETIRED_IDS = "SELECT id FROM pm_card_types WHERE name = 'phase_coverage_check'"


def upgrade() -> None:
    # Every table holding a foreign key to pm_card_types.id (all ON DELETE RESTRICT), then it.
    op.execute(f"DELETE FROM pm_project_type_card_types WHERE card_type_id IN ({_RETIRED_IDS})")
    op.execute(f"DELETE FROM pm_recipe_items WHERE card_type_id IN ({_RETIRED_IDS})")
    op.execute(f"DELETE FROM pm_wip_limits WHERE card_type_id IN ({_RETIRED_IDS})")
    op.execute("DELETE FROM pm_card_types WHERE name = 'phase_coverage_check'")


def downgrade() -> None:
    raise NotImplementedError(
        "2026_11_04_0082 is forward-only: the card type phase_coverage_check and every row "
        "referencing it were deleted (R347 (4))."
    )
