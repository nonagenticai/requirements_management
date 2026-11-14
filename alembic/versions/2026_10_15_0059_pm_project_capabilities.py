"""PM projects: per-project CAPABILITY selection, and its history (R260 v1).

George 2026-10-15: the project type's composition is the first filter; the PM then selects
which capabilities a project needs. Only PM / n8n writes the selection (v4 ruling 1), like
the project phase; fr reads it.

``pm_project_capabilities`` — one row per selected capability of a project:

* ``project_id`` → ``pm_projects`` ON DELETE CASCADE;
* ``capability_id`` → ``pm_capabilities`` (NO ACTION: the catalogue API refuses deleting a
  capability a project selected with 409 naming the project first);
* ``reason`` NOT NULL — why the PM selected it;
* ``approval_id`` — the fr sign-off that approved it; a plain uuid, no FK (sign-offs live
  in fr);
* UNIQUE ``(project_id, capability_id)``.

``pm_project_capability_events`` — one row per real change of the selected NAME set
(``added`` / ``removed`` capability names; names, not ids, so history outlives the
catalogue row). Same shape as ``pm_project_phase_events``.

Both join the RLS tenant-isolation backstop with the strict uuid predicate (same as
``2026_10_13_0053``); neither joins ``system_published_read``.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_15_0059
Revises: 2026_10_13_0058
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_15_0059"
down_revision = "2026_10_13_0058"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"
_TABLES = ("pm_project_capabilities", "pm_project_capability_events")


def upgrade() -> None:
    op.create_table(
        "pm_project_capabilities",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("pm_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "capability_id",
            sa.Uuid(),
            sa.ForeignKey("pm_capabilities.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "capability_id", name="uq_pm_project_capabilities_project_capability"
        ),
    )
    op.create_index(
        "ix_pm_project_capabilities_tenant_id", "pm_project_capabilities", ["tenant_id"]
    )
    op.create_index(
        "ix_pm_project_capabilities_capability_id", "pm_project_capabilities", ["capability_id"]
    )

    op.create_table(
        "pm_project_capability_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("pm_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "added",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "removed",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_pm_project_capability_events_tenant_id",
        "pm_project_capability_events",
        ["tenant_id"],
    )
    op.create_index(
        "ix_pm_project_capability_events_project_created",
        "pm_project_capability_events",
        ["project_id", "created_at"],
    )

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
