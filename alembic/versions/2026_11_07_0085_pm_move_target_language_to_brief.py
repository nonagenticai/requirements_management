"""Move a rewrite's target language off ``it_inputs`` and onto the brief (George 2026-11-07).

George, 2026-11-07: *"target language should be extracted from the project request … and get
processed into the project brief and later project contract."* A rewrite's target language is a
fact about the REQUEST, so it belongs on the brief (``ProjectBrief.target_language``) next to the
problem statement and the requirements — not in ``it_inputs``, which holds the conveyor's
execution knobs. The four live ``rewrite_*.READY`` flows read it from the brief after this lane.

The key lives inside ``pm_projects.it_inputs`` (JSONB, no column). ``ItInputs`` is
``extra="forbid"`` and ``services/pm_project_intake_service.py`` re-parses the STORED value on
every update, so a leftover key would turn every later PUT of that project into a 422.

No brief backfill: ``pm_project_brief_versions`` is append-only and a version is authored, and
the table held 0 rows when this ran (measured on ``requirements_api``, 2026-11-07).

Downgrade is a no-op: the value is not recoverable and pre-prod is forward-only.

Revision ID: 2026_11_07_0085
Revises: 2026_11_07_0084
Create Date: 2026-11-07
"""

from alembic import op

revision = "2026_11_07_0085"
down_revision = "2026_11_07_0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE pm_projects SET it_inputs = it_inputs - 'language' "
        "WHERE it_inputs IS NOT NULL AND it_inputs ? 'language'"
    )


def downgrade() -> None:
    pass
