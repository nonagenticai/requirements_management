"""PM projects: kanban-coach ADMISSIONS (R311, kanban-coach-errors.md §4.4).

``pm_coach_actions`` — append-only, one row per admitted kanban-coach run:

* ``project_id`` → ``pm_projects`` ON DELETE CASCADE;
* ``card_id`` (the source card), ``target_card_id`` (the card the remedy acts on),
  ``exit_group`` (``rerun`` / ``pull`` / ``own``), ``entry`` (the target's list entry token);
* ``phase`` — the project's phase name at admission (trace only, never counted);
  ``phase_visit_at`` — the project's ``phase_updated_at`` read under the row lock at
  admission; the per-project incident cap counts rows with ``phase_visit_at IS NOT DISTINCT
  FROM pm_projects.phase_updated_at`` (the current phase VISIT, no clock comparison);
* ``requested_remedy`` / ``remedy`` (admitted, after the caps) / ``applied_remedy`` (recorded
  by the coach after its post-Admit re-read; null until then) + ``applied_at``;
* ``admitted``, ``reason``, ``error_class``, the source / root / coach execution ids.

Indexes: UNIQUE ``(tenant_id, coach_execution_id)`` (one coach run is one row; a re-POST is
answered from it); UNIQUE incident key ``(tenant_id, project_id, target_card_id, entry,
root_execution_id, exit_group)`` (two coaches of one incident collide on purpose); count
indexes ``(tenant_id, project_id, phase_visit_at)`` and ``(tenant_id, project_id,
target_card_id, entry)``.

Joins the RLS tenant-isolation backstop with the strict uuid predicate (same as
``2026_10_15_0059``); does not join ``system_published_read``.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_19_0062
Revises: 2026_10_18_0061
Create Date: 2026-10-19
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_19_0062"
down_revision = "2026_10_18_0061"
branch_labels = None
depends_on = None

_TABLE = "pm_coach_actions"
_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("pm_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("card_id", sa.String(length=64), nullable=False),
        sa.Column("target_card_id", sa.String(length=64), nullable=False),
        sa.Column("exit_group", sa.String(length=16), nullable=False),
        sa.Column("entry", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=120), nullable=True),
        sa.Column("phase_visit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_class", sa.String(length=64), nullable=False),
        sa.Column("requested_remedy", sa.String(length=16), nullable=False),
        sa.Column("remedy", sa.String(length=16), nullable=False),
        sa.Column("applied_remedy", sa.String(length=16), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admitted", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("source_execution_id", sa.String(length=64), nullable=False),
        sa.Column("root_execution_id", sa.String(length=64), nullable=False),
        sa.Column("coach_execution_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "exit_group IN ('own', 'pull', 'rerun')", name="ck_pm_coach_actions_exit_group"
        ),
        sa.CheckConstraint(
            "requested_remedy IN ('ask', 'block', 'move', 'repull', 'rerun')",
            name="ck_pm_coach_actions_requested_remedy",
        ),
        sa.CheckConstraint(
            "remedy IN ('ask', 'block', 'move', 'repull', 'rerun')",
            name="ck_pm_coach_actions_remedy",
        ),
        sa.CheckConstraint(
            "applied_remedy IN ('ask', 'block', 'move', 'note', 'repull', 'rerun')",
            name="ck_pm_coach_actions_applied_remedy",
        ),
        sa.CheckConstraint(
            "(applied_remedy IS NULL) = (applied_at IS NULL)",
            name="ck_pm_coach_actions_applied_at",
        ),
    )
    op.create_index(
        "uq_pm_coach_actions_coach_execution",
        _TABLE,
        ["tenant_id", "coach_execution_id"],
        unique=True,
    )
    op.create_index(
        "uq_pm_coach_actions_incident",
        _TABLE,
        ["tenant_id", "project_id", "target_card_id", "entry", "root_execution_id", "exit_group"],
        unique=True,
    )
    op.create_index(
        "ix_pm_coach_actions_project_visit", _TABLE, ["tenant_id", "project_id", "phase_visit_at"]
    )
    op.create_index(
        "ix_pm_coach_actions_target_entry",
        _TABLE,
        ["tenant_id", "project_id", "target_card_id", "entry"],
    )

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_table(_TABLE)
