"""Architecture element N-ary realisation bindings (task #25 scope B, PR 1/3).

Replaces the 1:1 ``architecture_elements.properties.code_binding`` JSONB
with a structured ``architecture_element_realisation_bindings`` table
supporting multiple bindings per element across three kinds
(``coding_component``, ``coding_container``, ``external_repo``).

The JSONB ``code_binding`` is kept under dual-write for one PR cycle so
existing readers (conformance, proposals) continue to work unmodified; a
future cleanup PR drops the JSONB key after readers migrate.

Backfill (idempotent): every existing element with a non-empty
``properties->'code_binding'->>'container'`` AND ``component_id`` produces
exactly one row of kind ``coding_component``. The dedup unique index makes
re-running the backfill a no-op.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_05_27_0026
Revises: 2026_05_19_0025
Create Date: 2026-05-27
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_05_27_0026"
down_revision = "2026_05_19_0025"
branch_labels = None
depends_on = None


_TABLE = "architecture_element_realisation_bindings"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "element_id",
            sa.Uuid(),
            sa.ForeignKey("architecture_elements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("binding_kind", sa.String(length=32), nullable=False),
        sa.Column("container", sa.String(length=255), nullable=True),
        sa.Column("component_id", sa.String(length=255), nullable=True),
        sa.Column("repository_url", sa.Text(), nullable=True),
        sa.Column("repository_branch", sa.String(length=255), nullable=True),
        sa.Column(
            "properties",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.CheckConstraint(
            "(binding_kind = 'coding_component' AND container IS NOT NULL "
            "AND component_id IS NOT NULL) "
            "OR (binding_kind = 'coding_container' AND container IS NOT NULL "
            "AND component_id IS NULL) "
            "OR (binding_kind = 'external_repo' AND repository_url IS NOT NULL)",
            name="ck_realisation_binding_kind_shape",
        ),
    )
    op.create_index(
        "ix_realisation_bindings_element_id",
        _TABLE,
        ["element_id"],
        unique=False,
    )
    op.create_index(
        "ix_realisation_bindings_tenant_id",
        _TABLE,
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_realisation_bindings_tenant_container",
        _TABLE,
        ["tenant_id", "container"],
        unique=False,
    )

    # Partial-uniqueness via COALESCE over nullable columns — alembic
    # ``create_index(unique=True)`` doesn't model this directly, so emit raw
    # DDL. The index enforces: at most one binding per
    # (element, kind, container?, component_id?, repository_url?) tuple.
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX uq_realisation_binding_dedup
              ON {_TABLE} (
                element_id,
                binding_kind,
                COALESCE(container, ''),
                COALESCE(component_id, ''),
                COALESCE(repository_url, '')
              )
            """
        )
    )

    # Idempotent backfill — one row of kind ``coding_component`` per element
    # whose JSONB carries both container and component_id. ON CONFLICT relies
    # on the dedup unique index, so re-running the migration is a no-op.
    # Uses ``gen_random_uuid()`` (pg18 built-in) for the row id — the model's
    # Python-level ``uuid7`` default is application-only; backfill rows don't
    # need monotonic-time ids, only uniqueness.
    op.execute(
        sa.text(
            f"""
            INSERT INTO {_TABLE} (
                id, element_id, tenant_id, binding_kind,
                container, component_id, properties
            )
            SELECT
                gen_random_uuid() AS id,
                e.id AS element_id,
                e.tenant_id,
                'coding_component' AS binding_kind,
                e.properties->'code_binding'->>'container'    AS container,
                e.properties->'code_binding'->>'component_id' AS component_id,
                jsonb_strip_nulls(
                    jsonb_build_object(
                        'path',    e.properties->'code_binding'->>'path',
                        'kind',    e.properties->'code_binding'->>'kind',
                        'node_id', e.properties->'code_binding'->>'node_id'
                    )
                ) AS properties
            FROM architecture_elements e
            WHERE e.properties->'code_binding'->>'container' IS NOT NULL
              AND e.properties->'code_binding'->>'container' <> ''
              AND e.properties->'code_binding'->>'component_id' IS NOT NULL
              AND e.properties->'code_binding'->>'component_id' <> ''
            ON CONFLICT DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index("uq_realisation_binding_dedup", table_name=_TABLE)
    op.drop_index("ix_realisation_bindings_tenant_container", table_name=_TABLE)
    op.drop_index("ix_realisation_bindings_tenant_id", table_name=_TABLE)
    op.drop_index("ix_realisation_bindings_element_id", table_name=_TABLE)
    op.drop_table(_TABLE)
