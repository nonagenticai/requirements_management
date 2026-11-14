"""PM projects: project TYPE + current PHASE, and the phase history.

George 2026-10-13: RM owns a project's type and current phase; n8n is the only writer;
fr reads them.

``pm_projects`` gains three nullable columns (legacy rows stay NULL):

* ``project_type_id`` → ``pm_project_types``, ON DELETE RESTRICT (the API refuses a
  delete of a type in use with 409 naming the projects first);
* ``current_phase_id`` — with ``project_type_id`` a composite FK
  ``fk_pm_projects_selected_phase`` → ``pm_project_type_phases(project_type_id,
  phase_id)``, NO ACTION, DEFERRABLE INITIALLY DEFERRED (``set_phases`` deletes and
  re-inserts a type's selection inside one transaction), MATCH SIMPLE so a NULL passes;
* ``phase_updated_at`` — when the phase last really changed.

``ck_pm_projects_phase_needs_type``: a phase needs a type.

``pm_project_phase_events`` — one row per real phase change, NOT NULL ``uuid``
``tenant_id``, ``project_id`` CASCADE; phase ids are plain uuids (history outlives a
phase). It joins the RLS tenant-isolation backstop with the strict uuid predicate (same
as ``2026_10_12_0050``); it does not join ``system_published_read``.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_13_0053
Revises: 2026_10_12_0052
Create Date: 2026-10-13
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_13_0053"
down_revision = "2026_10_12_0052"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"


def upgrade() -> None:
    op.add_column("pm_projects", sa.Column("project_type_id", sa.Uuid(), nullable=True))
    op.add_column("pm_projects", sa.Column("current_phase_id", sa.Uuid(), nullable=True))
    op.add_column(
        "pm_projects", sa.Column("phase_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "pm_projects_project_type_id_fkey",
        "pm_projects",
        "pm_project_types",
        ["project_type_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_pm_projects_selected_phase",
        "pm_projects",
        "pm_project_type_phases",
        ["project_type_id", "current_phase_id"],
        ["project_type_id", "phase_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        "ck_pm_projects_phase_needs_type",
        "pm_projects",
        "current_phase_id IS NULL OR project_type_id IS NOT NULL",
    )
    op.create_index("ix_pm_projects_project_type_id", "pm_projects", ["project_type_id"])
    op.create_index(
        "ix_pm_projects_type_phase", "pm_projects", ["project_type_id", "current_phase_id"]
    )

    op.create_table(
        "pm_project_phase_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("pm_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_phase_id", sa.Uuid(), nullable=True),
        sa.Column("to_phase_id", sa.Uuid(), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
    )
    op.create_index(
        "ix_pm_project_phase_events_tenant_id", "pm_project_phase_events", ["tenant_id"]
    )
    op.create_index(
        "ix_pm_project_phase_events_project_at", "pm_project_phase_events", ["project_id", "at"]
    )
    op.execute("ALTER TABLE pm_project_phase_events ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON pm_project_phase_events")
    op.execute(
        "CREATE POLICY tenant_isolation ON pm_project_phase_events "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON pm_project_phase_events")
    op.drop_table("pm_project_phase_events")
    op.drop_index("ix_pm_projects_type_phase", table_name="pm_projects")
    op.drop_index("ix_pm_projects_project_type_id", table_name="pm_projects")
    op.drop_constraint("ck_pm_projects_phase_needs_type", "pm_projects", type_="check")
    op.drop_constraint("fk_pm_projects_selected_phase", "pm_projects", type_="foreignkey")
    op.drop_constraint("pm_projects_project_type_id_fkey", "pm_projects", type_="foreignkey")
    op.drop_column("pm_projects", "phase_updated_at")
    op.drop_column("pm_projects", "current_phase_id")
    op.drop_column("pm_projects", "project_type_id")
