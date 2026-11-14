"""PM projects: the link to the project's Planka board (George 2026-10-13, v4 R73 / D20).

"We need to ensure that RM has sufficient information about the project and the board."
After fr creates a project in RM it builds the Planka board from RM's board template and
tells RM which board that is. Every later conveyor flow starts from a Planka webhook, whose
body names only Planka ids (``data.item.boardId`` / ``listId``), so n8n must be able to go
from a board id to the RM project and from a list id to the conveyor list.

``pm_projects``:

* ``planka_board_id varchar(64) NULL`` — the Planka board. UNIQUE per tenant,
  ``(tenant_id, planka_board_id)`` (one board, one project within a tenant; NULLs do not
  collide). Planka is per tenant (each tenant brings its own instance), so board ids from
  different tenants' instances are independent: a global UNIQUE would let tenant A lock
  tenant B out of B's own board id and leak whether an id is linked anywhere. Planka ids
  are 64-bit snowflakes serialised as strings.
* ``planka_project_id varchar(64) NULL`` — the Planka project holding the board.
* ``planka_list_ids jsonb NULL`` — ``{"TODO": id, "READY": id, "DOING": id, "QA": id,
  "DONE": id, "BLOCKED": id}`` (exactly those six keys; enforced by the API schema).
* ``planka_linked_at timestamptz NULL`` — when the link last really changed.

``ck_pm_projects_planka_link_complete``: a board id and its list map are set together.

The fr project id needs no column: it is ``origin_project_id`` (immutable since #217).
No new table, no RLS change: ``pm_projects`` keeps its existing policy.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_13_0058
Revises: 2026_10_13_0057
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_13_0058"
down_revision = "2026_10_13_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pm_projects", sa.Column("planka_board_id", sa.String(64), nullable=True))
    op.add_column("pm_projects", sa.Column("planka_project_id", sa.String(64), nullable=True))
    op.add_column(
        "pm_projects",
        sa.Column("planka_list_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "pm_projects", sa.Column("planka_linked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint(
        "uq_pm_projects_tenant_planka_board_id", "pm_projects", ["tenant_id", "planka_board_id"]
    )
    op.create_check_constraint(
        "ck_pm_projects_planka_link_complete",
        "pm_projects",
        "(planka_board_id IS NULL) = (planka_list_ids IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pm_projects_planka_link_complete", "pm_projects", type_="check")
    op.drop_constraint("uq_pm_projects_tenant_planka_board_id", "pm_projects", type_="unique")
    op.drop_column("pm_projects", "planka_linked_at")
    op.drop_column("pm_projects", "planka_list_ids")
    op.drop_column("pm_projects", "planka_project_id")
    op.drop_column("pm_projects", "planka_board_id")
