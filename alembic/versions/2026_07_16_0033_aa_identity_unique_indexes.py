"""AA-intake identity partial unique indexes (WS1).

The AA-intake get-or-create gates on requirements / decisions / pm_activities /
principles / architecture_elements / architecture_relationships were best-effort
check-then-insert RACES: two concurrent re-POSTs both pass the SELECT, both get
a distinct auto-incremented slug (``EntityService._generate_unique_slug``:
title, title-1, …) so the per-tenant ``(tenant_id, slug)`` unique constraint
never fires, and both INSERT a duplicate.

These six partial unique indexes are the real DB-level backstop. The routers
route their project-scoped creates through ``EntityService.get_or_create`` /
``RelationshipService.get_or_create``, which optimistically insert and, on the
unique violation these indexes raise, roll back and re-SELECT the survivor
(mirrors ``services/realisation_bindings.attach_binding``).

FAIL-LOUD, NO RECONCILE: production has zero project-scoped duplicates on these
keys (verified before authoring). We deliberately do NOT dedup/delete rows in
``upgrade()``. If a duplicate ever exists, ``CREATE UNIQUE INDEX`` fails loud on
its own — that is the correct signal (a silent backfill/delete could destroy a
legitimately-distinct row or mask a real data-integrity problem). Repair is a
deliberate, human-reviewed step, never a migration side effect.

Each predicate is partial so rows outside the AA-intake project scope keep their
pre-existing behaviour:
  * ``project_id IS NOT NULL`` — tenant-wide (null-project) rows that
    legitimately share a title are unconstrained.
  * architecture_elements additionally excludes ``is_synthetic = true`` rows —
    synthetic artifacts are deduped by ``(container, component_id)`` code
    binding, NOT by title, and two synthetic rows may share a title.
  * principles use ``origin_project_id`` (Principle has no ``project_id``).

Indexes are also declared on the ORM models' ``__table_args__`` so
``Base.metadata.create_all()`` builds them for the Postgres-backed test suite;
this migration is the production/CI path.

Forward-only — pre-prod, no backwards-compat shims.

Revision ID: 2026_07_16_0033
Revises: 2026_07_09_0032
Create Date: 2026-07-16
"""

from alembic import op

revision = "2026_07_16_0033"
down_revision = "2026_07_09_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_requirements_tenant_project_title "
        "ON requirements (tenant_id, project_id, title) "
        "WHERE project_id IS NOT NULL AND visibility = 'tenant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_decisions_tenant_project_title "
        "ON decisions (tenant_id, project_id, title) "
        "WHERE project_id IS NOT NULL AND visibility = 'tenant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_pm_activities_tenant_project_title "
        "ON pm_activities (tenant_id, project_id, title) "
        "WHERE project_id IS NOT NULL AND visibility = 'tenant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_arch_elements_tenant_project_title_type "
        "ON architecture_elements (tenant_id, project_id, title, element_type) "
        "WHERE project_id IS NOT NULL AND is_synthetic = false AND visibility = 'tenant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_principles_tenant_origin_project_title "
        "ON principles (tenant_id, origin_project_id, title) "
        "WHERE origin_project_id IS NOT NULL AND visibility = 'tenant'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_arch_rels_tenant_edge "
        "ON architecture_relationships "
        "(tenant_id, project_id, source_entity_type, source_id, "
        "target_entity_type, target_id, relationship_type) "
        "WHERE project_id IS NOT NULL AND visibility = 'tenant'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_arch_rels_tenant_edge")
    op.execute("DROP INDEX IF EXISTS uq_principles_tenant_origin_project_title")
    op.execute("DROP INDEX IF EXISTS uq_arch_elements_tenant_project_title_type")
    op.execute("DROP INDEX IF EXISTS uq_pm_activities_tenant_project_title")
    op.execute("DROP INDEX IF EXISTS uq_decisions_tenant_project_title")
    op.execute("DROP INDEX IF EXISTS uq_requirements_tenant_project_title")
