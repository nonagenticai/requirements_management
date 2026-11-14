"""PM card types: allow ``asset_persisted`` as a definition of done.

n8n_custom_nodes ``AA_IMPLEMENTATION/landing_predicates.json`` @ e21ee10f0 (vendored at
``scripts/sources/landing_predicates.json``) names the predicate ``asset_persisted`` — "the
card's asset_id field is bound to an element that was READ BACK from its owning service" —
and gives it to ``architecture_task``. The default catalogue carries that value, so
``ck_pm_card_types_definition_of_done`` must accept it or the import fails.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_13_0055
Revises: 2026_10_13_0054
Create Date: 2026-10-13
"""

from alembic import op

revision = "2026_10_13_0055"
down_revision = "2026_10_13_0054"
branch_labels = None
depends_on = None

_OLD = "('pr_merged', 'pr_opened', 'asset_promoted', 'none')"
_NEW = "('pr_merged', 'pr_opened', 'asset_promoted', 'asset_persisted', 'none')"


def _replace(values: str) -> None:
    op.drop_constraint("ck_pm_card_types_definition_of_done", "pm_card_types", type_="check")
    op.create_check_constraint(
        "ck_pm_card_types_definition_of_done",
        "pm_card_types",
        f"definition_of_done IS NULL OR definition_of_done IN {values}",
    )


def upgrade() -> None:
    _replace(_NEW)


def downgrade() -> None:
    _replace(_OLD)
