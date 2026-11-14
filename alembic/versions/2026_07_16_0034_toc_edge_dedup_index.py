"""TOC-node edge identity partial unique index (WS1 hardening — FIX 6).

``create_toc_edge`` builds an ``architecture_relationships`` row with
``source_entity_type='toc_nodes'`` and a NULL ``project_id``. The WS1
``uq_arch_rels_tenant_edge`` index is partial on ``project_id IS NOT NULL`` so it
cannot cover TOC edges — ``RelationshipService.create``'s auto-incrementing slug
let an identical re-POST silently INSERT a duplicate edge. This partial unique
index is the identity backstop for the null-project TOC edge case;
``create_toc_edge`` now routes through ``RelationshipService.get_or_create``.

FAIL-LOUD, NO RECONCILE: production has zero duplicate TOC-node edges on this key
(verified live before authoring — 249 tenant-visible toc edges, 0 dup groups). We
do NOT dedup/delete rows here. If a duplicate ever exists, ``CREATE UNIQUE INDEX``
fails loud on its own — the correct signal for a human-reviewed repair, never a
silent migration side effect.

Also declared on the ``ArchitectureRelationship`` model ``__table_args__`` so
``Base.metadata.create_all()`` builds it for the Postgres-backed test suite; this
migration is the production/CI path.

Forward-only — pre-prod, no backwards-compat shims.

Revision ID: 2026_07_16_0034
Revises: 2026_07_16_0033
Create Date: 2026-07-17
"""

from alembic import op

revision = "2026_07_16_0034"
down_revision = "2026_07_16_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_arch_rels_toc_edge "
        "ON architecture_relationships "
        "(tenant_id, source_entity_type, source_id, "
        "target_entity_type, target_id, relationship_type) "
        "WHERE source_entity_type = 'toc_nodes' AND visibility = 'tenant'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_arch_rels_toc_edge")
