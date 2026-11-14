"""Drop the legacy ``properties.code_binding`` JSONB key from architecture_elements.

After T3.1 (PR #38) all readers prefer the structured
``architecture_element_realisation_bindings`` table; after T3.2 (this PR) the
JSONB ``code_binding`` block is no longer written (the router strips it from
``properties`` before persistence) nor read (all reader fallbacks dropped).
This migration is the one-shot cleanup for historical rows seeded before T3.2.

Forward-only — pre-prod, no rollback shims. The downgrade reconstructs the
JSONB block from the structured table for the ``coding_component`` bindings
backfilled by ``2026_05_27_0026`` so the migration is reversible in test.

Revision ID: 2026_05_27_0027
Revises: 2026_05_27_0026
Create Date: 2026-05-28
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_05_27_0027"
down_revision = "2026_05_27_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE architecture_elements
               SET properties = properties - 'code_binding'
             WHERE properties ? 'code_binding'
            """
        )
    )


def downgrade() -> None:
    # Reconstruct the JSONB ``code_binding`` block from the structured
    # ``coding_component`` bindings backfilled by the previous migration.
    # Extra metadata (``path``/``kind``/``node_id``) is restored from the
    # binding row's own ``properties`` JSONB.
    op.execute(
        sa.text(
            """
            UPDATE architecture_elements e
               SET properties = COALESCE(e.properties, '{}'::jsonb)
                              || jsonb_build_object(
                                   'code_binding',
                                   jsonb_strip_nulls(
                                     jsonb_build_object(
                                       'container',   b.container,
                                       'component_id', b.component_id,
                                       'path',        b.properties->>'path',
                                       'kind',        b.properties->>'kind',
                                       'node_id',     b.properties->>'node_id'
                                     )
                                   )
                                 )
              FROM architecture_element_realisation_bindings b
             WHERE b.element_id = e.id
               AND b.binding_kind = 'coding_component'
               AND b.container IS NOT NULL
               AND b.component_id IS NOT NULL
            """
        )
    )
