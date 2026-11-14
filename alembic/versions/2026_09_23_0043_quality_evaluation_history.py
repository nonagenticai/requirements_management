"""Append-only quality evaluation history (``quality_evaluations``).

``QualityMixin`` stores only the latest score on each entity row; every
evaluation overwrote the one before it. This table records every evaluation —
heuristic and LLM judge alike — as its own row, with the model / provider /
prompt version worker_management reported for the LLM call when it reported
one. The entity's ``quality_*`` columns are unchanged and remain the "latest"
read shape.

``tenant_id`` is a NOT NULL ``uuid`` column, so the table joins the RLS
tenant-isolation backstop with the strict policy (the ``::uuid`` cast is what
``kag_common.database.rls._predicate`` derives for a uuid column since
``2026_08_24_0042``). New tables created by the migration owner inherit the
``requirements_app`` grants through the owner's default privileges (measured
in prod: ``requirements_user`` → ``requirements_app=arwd``).

No backfill: the only history that exists is the single latest score per row,
and copying it in would present a snapshot as a series.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_09_23_0043
Revises: 2026_08_24_0042
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_09_23_0043"
down_revision = "2026_08_24_0042"
branch_labels = None
depends_on = None

_TABLE = "quality_evaluations"
_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("issues", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("suggestions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evaluator_kind", sa.String(length=16), nullable=False),
        sa.Column("evaluator", sa.String(length=80), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=True),
        sa.Column("served_by", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=255), nullable=True),
        sa.Column("prompt_sha", sa.String(length=255), nullable=True),
        sa.Column("correlation_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_quality_evaluations_tenant_id", _TABLE, ["tenant_id"], unique=False)
    op.create_index(
        "ix_quality_evaluations_entity_created",
        _TABLE,
        ["tenant_id", "entity_type", "entity_id", "created_at"],
        unique=False,
    )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_index("ix_quality_evaluations_entity_created", table_name=_TABLE)
    op.drop_index("ix_quality_evaluations_tenant_id", table_name=_TABLE)
    op.drop_table(_TABLE)
