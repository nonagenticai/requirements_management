"""Add lens_metadata JSONB to requirements and decisions.

Persists the AA author subroutine's CLR reasoning lenses (Logical Thinking
Process) per asset so they are REST-queryable on ``RequirementOut`` /
``DecisionOut``. Shape (free-form):

    {
      "clr_assessment": {clarity, entity_existence, causality_existence,
                         cause_sufficiency, no_additional_cause,
                         no_cause_effect_reversal, predicted_effect_exists,
                         no_tautology, notes},
      "goal_tree_relevance": {relevant, traces_to}
    }

Additive, forward-only — pre-prod, no rollback shims required. Existing rows
backfill to ``'{}'`` via server_default.

Revision ID: 2026_06_01_0029
Revises: 2026_05_31_0028
Create Date: 2026-06-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "2026_06_01_0029"
down_revision = "2026_05_31_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("requirements", "decisions"):
        op.add_column(
            table,
            sa.Column(
                "lens_metadata",
                JSONB(),
                nullable=False,
                server_default="{}",
            ),
        )


def downgrade() -> None:
    for table in ("requirements", "decisions"):
        op.drop_column(table, "lens_metadata")
