"""Synthetic-identity dedup — code-binding identity columns + partial unique indexes.

The synthetic-Artifact upsert (``/architecture/elements`` with ``is_synthetic=true``
and ``properties.code_binding.{container, component_id}``) dedups only at the
application level (``find_synthetic_by_code_binding``). Under a concurrent double
re-accept, both POSTs miss the SELECT and both ``svc.create`` — minting TWO
synthetic elements bound to the SAME code unit. The realisation-binding dedup
index ``uq_realisation_binding_dedup`` is keyed per ``element_id`` so it does NOT
catch this cross-element collision (it is exactly the state PR #73 had to tolerate
with an ordered ``.first()`` to avoid a MultipleResultsFound → HTTP 500).

This migration makes "one synthetic per (tenant, project, code-unit)" a
DB-enforced invariant — the missing TWIN of the WS1 index
``uq_arch_elements_tenant_project_title_type`` (migration ``2026_07_16_0033``),
which deliberately EXCLUDES synthetic rows because they are deduped by code
binding, not title.

Design (Option B — identity on the ELEMENT, not the binding table): the synthetic's
identity ``(container, component_id)`` is denormalised onto ``architecture_elements``
itself. The element already carries every scope column (``tenant_id``,
``project_id``, ``visibility``, ``subject_id``, ``is_synthetic``), so the unique
index reads the row's OWN live columns — no cross-table staleness (a binding-less
PUT that changes ``project_id`` moves the uniqueness scope atomically) — and the
race backstop fires on the element INSERT, exactly where the double-mint happens.

``COALESCE(project_id, '')`` — unlike the WS1 title index's ``project_id IS NOT
NULL`` — so null-project (tenant-wide) synthetics dedup too: a synthetic's identity
is its code unit regardless of project scope. A per-``subject_id`` user-visibility
variant lets two distinct subjects each own a private synthetic on the same code
unit.

FAIL-LOUD, NO RECONCILE: a prod-duplicate precheck is run separately before merge.
We deliberately do NOT dedup/delete rows in ``upgrade()`` — if a duplicate exists,
``CREATE UNIQUE INDEX`` fails loud on its own, which is the correct signal. Repair
is a deliberate, human-reviewed step, never a migration side effect.

The columns + indexes are also declared on ``ArchitectureElement.__table_args__``
so ``Base.metadata.create_all()`` builds them for the Postgres-backed test suite;
this migration is the production/CI path.

Forward-only — pre-prod, no backwards-compat shims.

Revision ID: 2026_07_18_0036
Revises: 2026_07_16_0035
Create Date: 2026-07-18
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_07_18_0036"
down_revision = "2026_07_16_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Identity columns (nullable — only synthetics-with-binding populate them).
    op.add_column(
        "architecture_elements",
        sa.Column("synthetic_binding_container", sa.String(255), nullable=True),
    )
    op.add_column(
        "architecture_elements",
        sa.Column("synthetic_binding_component_id", sa.String(255), nullable=True),
    )

    # 2. Idempotent backfill from the canonical binding table. Pick the
    #    earliest-created coding_component binding per synthetic element
    #    (DISTINCT ON) as its identity. Re-runnable: only fills NULL rows.
    op.execute(
        sa.text(
            """
            UPDATE architecture_elements e
            SET synthetic_binding_container    = b.container,
                synthetic_binding_component_id = b.component_id
            FROM (
                SELECT DISTINCT ON (element_id)
                       element_id, container, component_id
                FROM architecture_element_realisation_bindings
                WHERE binding_kind = 'coding_component'
                  AND container IS NOT NULL
                  AND component_id IS NOT NULL
                ORDER BY element_id, created_at, id
            ) b
            WHERE e.id = b.element_id
              AND e.is_synthetic = true
              AND e.synthetic_binding_container IS NULL
            """
        )
    )

    # 3. Partial unique indexes — the DB-enforced invariant. FAIL-LOUD: a
    #    pre-existing cross-element synthetic duplicate makes these error, which
    #    is the correct signal (human repair, no blind dedup here).
    op.execute(
        "CREATE UNIQUE INDEX uq_arch_elements_synthetic_binding_tenant "
        "ON architecture_elements "
        "(tenant_id, COALESCE(project_id, ''), "
        "synthetic_binding_container, synthetic_binding_component_id) "
        "WHERE is_synthetic = true AND visibility = 'tenant' "
        "AND synthetic_binding_container IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_arch_elements_synthetic_binding_user "
        "ON architecture_elements "
        "(tenant_id, subject_id, COALESCE(project_id, ''), "
        "synthetic_binding_container, synthetic_binding_component_id) "
        "WHERE is_synthetic = true AND visibility = 'user' "
        "AND synthetic_binding_container IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_arch_elements_synthetic_binding_user")
    op.execute("DROP INDEX IF EXISTS uq_arch_elements_synthetic_binding_tenant")
    op.drop_column("architecture_elements", "synthetic_binding_component_id")
    op.drop_column("architecture_elements", "synthetic_binding_container")
