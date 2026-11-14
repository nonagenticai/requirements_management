"""PM brief versions: ``author_kind`` is derived, so ``author_kind_source`` goes (George 2026-10-13).

Anything not done through fr IS the conveyor: a brief version's ``author_kind`` is ``user``
if and only if the write was fr-delegated (``Principal.delegated``), else ``conveyor``. There
is no allowlist and no declared value any more, so how the kind "was established" is always
the same one rule and the column recording it carries no information.

Drops ``pm_project_brief_versions.author_kind_source`` and its CHECK
``ck_pm_project_brief_versions_author_kind_source``. ``author_kind`` and
``author_subject_id`` stay.

Safe: production holds 0 brief-version rows at this revision (measured before writing it).

Revision ID: 2026_10_13_0057
Revises: 2026_10_13_0056
Create Date: 2026-10-13
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_13_0057"
down_revision = "2026_10_13_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_pm_project_brief_versions_author_kind_source",
        "pm_project_brief_versions",
        type_="check",
    )
    op.drop_column("pm_project_brief_versions", "author_kind_source")


def downgrade() -> None:
    op.add_column(
        "pm_project_brief_versions",
        sa.Column(
            "author_kind_source",
            sa.String(length=16),
            nullable=False,
            server_default="declared",
        ),
    )
    op.create_check_constraint(
        "ck_pm_project_brief_versions_author_kind_source",
        "pm_project_brief_versions",
        "author_kind_source IN ('delegated', 'conveyor_subject', 'declared')",
    )
