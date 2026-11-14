"""A12: architecture concepts are identified by id; ``Idempotency-Key`` replaces title identity.

Target spec 2026-10-26 §6 Identity (:3454-3456): the title-based get-or-create and its
unique indexes are replaced by ``Idempotency-Key``; same-name concepts are allowed and the
ArchiMate duplicate checker reports them as warnings (``AM-DUP-001``).

* DROP ``uq_arch_elements_tenant_project_title_type`` (``0033``),
  ``uq_arch_views_tenant_project_title`` and ``uq_arch_standards_tenant_project_title``
  (``0035``). KEPT: ``uq_arch_rels_tenant_edge`` (an edge's identity is its endpoints) and
  the synthetic code-binding indexes (``0036``).
* CREATE ``idempotency_keys`` — one row per ``(tenant_id, subject_id, route, key)`` (a key
  belongs to the calling subject, so it can never replay another subject's private create);
  ``ix_idempotency_keys_tenant_created_at`` serves the per-tenant expiry GC; ``status_code``
  NULL while the first request is in flight. Joins the RLS ``tenant_isolation`` backstop
  with the strict uuid predicate (same as ``0062``); does not join
  ``system_published_read``.

Production has 0 architecture elements/views/standards, so dropping the indexes needs no
dedup. Downgrade drops the table and recreates the three indexes exactly as ``0033`` /
``0035`` built them (it fails if same-title rows were written meanwhile — that is the
data the downgrade can no longer represent).

Revision ID: 2026_10_27_0068
Revises: 2026_10_27_0067
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_27_0068"
down_revision = "2026_10_27_0067"
branch_labels = None
depends_on = None

_TABLE = "idempotency_keys"
_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_arch_elements_tenant_project_title_type")
    op.execute("DROP INDEX IF EXISTS uq_arch_views_tenant_project_title")
    op.execute("DROP INDEX IF EXISTS uq_arch_standards_tenant_project_title")

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(key) BETWEEN 1 AND 255", name="ck_idempotency_keys_key_len"
        ),
    )
    op.create_index(
        "uq_idempotency_keys_tenant_subject_route_key",
        _TABLE,
        ["tenant_id", "subject_id", "route", "key"],
        unique=True,
    )
    op.create_index("ix_idempotency_keys_tenant_created_at", _TABLE, ["tenant_id", "created_at"])

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_table(_TABLE)
    op.execute(
        "CREATE UNIQUE INDEX uq_arch_elements_tenant_project_title_type "
        "ON architecture_elements (tenant_id, project_id, title, element_type) "
        "WHERE project_id IS NOT NULL AND is_synthetic = false AND visibility = 'tenant'"
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
