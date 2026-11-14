"""Drop the never-used ``it_inputs.max_cards`` cap (George 2026-11-07).

``max_cards`` arrived with the rewrite fields (2026-10-26, #237) as an optional cap on the
number of cards a project may mint. George, 2026-11-07: *"card cap is new to me and i never
requested this field. so it can be removed."* Nothing reads it at runtime: the only n8n
references are sticky-note prose in nine ``cap.*.plan`` flows, and no executable node
mentions the key (measured on the cluster n8n DB).

The key lives inside ``pm_projects.it_inputs`` (JSONB, no column). ``ItInputs`` is
``extra="forbid"`` and ``services/pm_project_intake_service.py`` re-parses the STORED value on
every update, so a leftover key would turn every later PUT of that project into a 422. Strip
it before the field leaves the schema.

Downgrade is a no-op: the value is not recoverable and pre-prod is forward-only.

Revision ID: 2026_11_07_0084
Revises: 2026_11_07_0083
Create Date: 2026-11-07
"""

from alembic import op

revision = "2026_11_07_0084"
down_revision = "2026_11_07_0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE pm_projects SET it_inputs = it_inputs - 'max_cards' "
        "WHERE it_inputs IS NOT NULL AND it_inputs ? 'max_cards'"
    )


def downgrade() -> None:
    pass
