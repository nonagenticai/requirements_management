"""PM projects: a project's own WIP limit (George 2026-10-13).

"RM's default limit lets only one card per project be in DOING at a time. That is the
WIP limit, and we should define wip-limit in RM on the project fields of the API."

``pm_projects.wip_limit int NULL`` — the project TOTAL (cards in DOING across the whole
project). NULL = not set on the project: the tenant's catalogue policy applies
(``pm_wip_limits``, project-total row for the project's type, else the tenant-global
total). 0 = no card may be in DOING (a pause). ``ck_pm_projects_wip_limit_nonneg``
forbids negatives.

``pm_projects.rework_limit int NULL`` — how many times a card of the project may go back
for rework. NULL = the default of 2 (``schemas.pm.DEFAULT_REWORK_LIMIT``);
``ck_pm_projects_rework_limit_nonneg`` forbids negatives.

No new table, no RLS change: ``pm_projects`` keeps its existing policy.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_13_0054
Revises: 2026_10_13_0053
Create Date: 2026-10-13
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_13_0054"
down_revision = "2026_10_13_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pm_projects", sa.Column("wip_limit", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_pm_projects_wip_limit_nonneg",
        "pm_projects",
        "wip_limit >= 0",
    )
    op.add_column("pm_projects", sa.Column("rework_limit", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_pm_projects_rework_limit_nonneg",
        "pm_projects",
        "rework_limit >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pm_projects_rework_limit_nonneg", "pm_projects", type_="check")
    op.drop_column("pm_projects", "rework_limit")
    op.drop_constraint("ck_pm_projects_wip_limit_nonneg", "pm_projects", type_="check")
    op.drop_column("pm_projects", "wip_limit")
