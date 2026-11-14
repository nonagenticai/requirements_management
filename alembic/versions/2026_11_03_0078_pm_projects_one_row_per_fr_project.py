"""One ``pm_projects`` row per fr project, enforced by the database (R345).

O-PROJECT-ID-AUTHORITY-BUILD part (1). fr's project id travels into RM as
``origin_project_id`` (``origin_kind = 'project'``). Until now the only backstop was
``uq_pm_projects_tenant_origin_project_title`` — keyed on the TITLE too and partial on
``visibility = 'tenant'`` — so a second POST with another title (or another visibility)
minted a second PM project for the same fr project.

Upgrade:

* ``ck_pm_projects_origin_kind``: ``origin_kind IN ('tenant', 'project', 'workflow')`` —
  exactly the domain ``ProjectIn.origin_kind`` accepts (and the sibling CHECKs on
  requirements/principles/decisions, migrations 0024/0025).
* ``ck_pm_projects_origin_project``: ``(origin_kind = 'project') = (origin_project_id IS
  NOT NULL)`` — a project-origin row names its fr project, and nothing else carries one.
* ``uq_pm_projects_tenant_origin_project``: UNIQUE ``(tenant_id, origin_project_id)``
  ``WHERE origin_kind = 'project'`` — lifetime unique: closed rows and every visibility
  included (``origin_project_id`` is immutable since #217; there is no unlink).
* Drops ``uq_pm_projects_tenant_origin_project_title``: strictly weaker than the new index.

Prod (2026-11-03, 22 rows): 21 ``project`` rows each with an ``origin_project_id`` and no
duplicate per ``(tenant_id, origin_project_id)``; 1 ``tenant`` row with NULL. No data fix.

Downgrade restores the title index and drops the new constraints.

Revision ID: 2026_11_03_0078
Revises: 2026_11_03_0077
Create Date: 2026-11-03
"""

from alembic import op

revision = "2026_11_03_0078"
down_revision = "2026_11_03_0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_pm_projects_origin_kind",
        "pm_projects",
        "origin_kind IN ('tenant', 'project', 'workflow')",
    )
    op.create_check_constraint(
        "ck_pm_projects_origin_project",
        "pm_projects",
        "(origin_kind = 'project') = (origin_project_id IS NOT NULL)",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_pm_projects_tenant_origin_project "
        "ON pm_projects (tenant_id, origin_project_id) "
        "WHERE origin_kind = 'project'"
    )
    op.execute("DROP INDEX IF EXISTS uq_pm_projects_tenant_origin_project_title")


def downgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_pm_projects_tenant_origin_project_title "
        "ON pm_projects (tenant_id, origin_project_id, title) "
        "WHERE origin_project_id IS NOT NULL AND visibility = 'tenant'"
    )
    op.execute("DROP INDEX IF EXISTS uq_pm_projects_tenant_origin_project")
    op.drop_constraint("ck_pm_projects_origin_project", "pm_projects", type_="check")
    op.drop_constraint("ck_pm_projects_origin_kind", "pm_projects", type_="check")
