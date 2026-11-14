"""PM activities: the Planka card each activity mirrors (D1 / R309, George 2026-10-18).

R309: one pm_activity per board card. The flow goes from a Planka card id (webhook
data.item.id) to the activity, the same way pm_projects.planka_board_id (2026_10_13_0058)
goes from a board id to the project.

``pm_activities.planka_card_id varchar(64) NULL`` — UNIQUE per tenant
(``uq_pm_activities_tenant_planka_card_id`` on (tenant_id, planka_card_id); NULLs do not
collide, so summaries/milestones and every pre-D1 row stay valid). Per tenant, not global:
Planka is per tenant (BYO instance), ids from different instances are independent.
New nullable column on an existing table: no backfill, no duplicate pre-check needed
(every existing row is NULL). No RLS change: the policy is row-level.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_18_0061
Revises: 2026_10_17_0060
Create Date: 2026-10-18
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_18_0061"
down_revision = "2026_10_17_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pm_activities", sa.Column("planka_card_id", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_pm_activities_tenant_planka_card_id", "pm_activities", ["tenant_id", "planka_card_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_pm_activities_tenant_planka_card_id", "pm_activities", type_="unique")
    op.drop_column("pm_activities", "planka_card_id")
