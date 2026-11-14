"""AA-intake identity partial unique indexes — Group A (WS1) + Project provenance.

Extends the WS1 get-or-create backstop (migration ``2026_07_16_0033``) to six
more entities and adds origin-provenance columns to ``pm_projects`` (WS2
groundwork). The routers route their project-/tree-scoped creates through
``EntityService.get_or_create``, which optimistically inserts and, on the unique
violation these partial indexes raise, rolls back and re-SELECTs the survivor.

Group A indexes:
  * ``uq_pm_risks_tenant_project_title`` — risks (project_id NOT NULL by schema,
    predicate keeps it symmetric with the other five).
  * ``uq_arch_standards_tenant_project_title`` — architecture standards.
  * ``uq_arch_views_tenant_project_title`` — architecture views.
  * ``uq_toc_trees_tenant_project_type_title`` — TOC trees; identity includes
    ``tree_type`` (a project may hold one tree of each of the six types).
  * ``uq_toc_nodes_tenant_tree_title`` — TOC nodes; ``tree_id`` is NOT NULL so
    only the ``visibility = 'tenant'`` predicate is needed.

Each predicate is partial so rows outside the AA-intake scope keep their
pre-existing behaviour (``project_id IS NOT NULL`` — tenant-wide null-project
rows that legitimately share a title are unconstrained; ``visibility = 'tenant'``
— per-subject user-visible rows are not deduped).

Project provenance: ``pm_projects`` gains ``origin_kind`` / ``origin_project_id``
/ ``origin_workflow_run_id`` (mirrors ``requirements``) plus
``uq_pm_projects_tenant_origin_project_title`` — a project has no self
``project_id`` scope, so its identity key is the origin provenance. Inert until
the n8n node sends ``originProjectId``.

FAIL-LOUD, NO RECONCILE: a prod-duplicate precheck for these keys is run
separately (by the orchestrator) before merge. We deliberately do NOT dedup or
delete rows in ``upgrade()`` — if a duplicate ever exists, ``CREATE UNIQUE
INDEX`` fails loud on its own, which is the correct signal. Repair is a
deliberate, human-reviewed step, never a migration side effect.

Indexes are also declared on the ORM models' ``__table_args__`` so
``Base.metadata.create_all()`` builds them for the Postgres-backed test suite;
this migration is the production/CI path.

Forward-only — pre-prod, no backwards-compat shims.

Revision ID: 2026_07_16_0035
Revises: 2026_07_16_0034
Create Date: 2026-07-16
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_07_16_0035"
down_revision = "2026_07_16_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_pm_risks_tenant_project_title "
        "ON pm_risks (tenant_id, project_id, title) "
        "WHERE project_id IS NOT NULL AND visibility = 'tenant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_arch_standards_tenant_project_title "
        "ON architecture_standards (tenant_id, project_id, title) "
        "WHERE project_id IS NOT NULL AND visibility = 'tenant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_arch_views_tenant_project_title "
        "ON architecture_views (tenant_id, project_id, title) "
        "WHERE project_id IS NOT NULL AND visibility = 'tenant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_toc_trees_tenant_project_type_title "
        "ON toc_trees (tenant_id, project_id, tree_type, title) "
        "WHERE project_id IS NOT NULL AND visibility = 'tenant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_toc_nodes_tenant_tree_title "
        "ON toc_nodes (tenant_id, tree_id, title) "
        "WHERE visibility = 'tenant'"
    )

    # Project provenance (WS2 groundwork).
    op.add_column(
        "pm_projects",
        sa.Column("origin_kind", sa.String(20), nullable=False, server_default="tenant"),
    )
    op.add_column("pm_projects", sa.Column("origin_project_id", sa.Uuid(), nullable=True))
    op.add_column("pm_projects", sa.Column("origin_workflow_run_id", sa.String(200), nullable=True))
    op.add_column(
        "pm_projects",
        sa.Column("created_by_user_id", sa.String(255), nullable=False, server_default=""),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_pm_projects_tenant_origin_project_title "
        "ON pm_projects (tenant_id, origin_project_id, title) "
        "WHERE origin_project_id IS NOT NULL AND visibility = 'tenant'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_pm_projects_tenant_origin_project_title")
    op.drop_column("pm_projects", "created_by_user_id")
    op.drop_column("pm_projects", "origin_workflow_run_id")
    op.drop_column("pm_projects", "origin_project_id")
    op.drop_column("pm_projects", "origin_kind")
    op.execute("DROP INDEX IF EXISTS uq_toc_nodes_tenant_tree_title")
    op.execute("DROP INDEX IF EXISTS uq_toc_trees_tenant_project_type_title")
    op.execute("DROP INDEX IF EXISTS uq_arch_views_tenant_project_title")
    op.execute("DROP INDEX IF EXISTS uq_arch_standards_tenant_project_title")
    op.execute("DROP INDEX IF EXISTS uq_pm_risks_tenant_project_title")
