"""PM catalogue tier 2: WIP limits, priority levels, deliverable / milestone kinds, gates,
board templates.

Six new tables (see ``src/models/pm_catalogue.py``), each with a NOT NULL ``uuid``
``tenant_id`` and the RLS tenant-isolation backstop with the strict uuid predicate (same as
``2026_10_12_0050`` / ``0051``); none joins ``system_published_read``:

* ``pm_wip_limits`` — no name; one row per scope (tenant, project type, phase, capability,
  card type), NULLS NOT DISTINCT, NULL = any. The project type CASCADEs; phase,
  capability and card type are RESTRICT (the API refuses with 409 first).
* ``pm_priority_levels`` — ``rank`` unique per tenant; at most one ``is_default`` per
  tenant (partial unique index); ``conveyor_may_set``.
* ``pm_deliverable_kinds`` — ``counts_for_phase_exit``.
* ``pm_milestone_kinds`` — optional ``phase_id`` (RESTRICT).
* ``pm_gates`` — ``phase_id`` (RESTRICT), ``approver_role``, ``quorum`` ≥ 1,
  ``expires_after_hours`` NULL or > 0.
* ``pm_board_templates`` — optional ``project_type_id`` (SET NULL), ``lists``, ``labels``,
  ``custom_field_groups`` (nullable jsonb lists, no default).

Fillable attribute columns: NULL = not set, and the default import fills it; ``[]`` =
deliberately cleared, and the import keeps it. The import never re-parents a row, so a
milestone kind's ``phase_id`` is never filled.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_12_0052
Revises: 2026_10_12_0051
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_12_0052"
down_revision = "2026_10_12_0051"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"

_NEW_TABLES = (
    "pm_wip_limits",
    "pm_priority_levels",
    "pm_deliverable_kinds",
    "pm_milestone_kinds",
    "pm_gates",
    "pm_board_templates",
)


def _owned_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_name", sa.String(length=255), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("visibility", sa.String(length=16), server_default="tenant", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
        ),
    ]


def _catalogue_columns() -> list[sa.Column]:
    return [
        *_owned_columns(),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
    ]


def _catalogue_constraints(table: str) -> list:
    return [
        sa.UniqueConstraint("tenant_id", "name", name=f"uq_{table}_tenant_name"),
        sa.CheckConstraint("visibility IN ('tenant', 'user')", name=f"ck_{table}_visibility"),
    ]


def _fk(column: str, target: str, ondelete: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(
        column, sa.Uuid(), sa.ForeignKey(f"{target}.id", ondelete=ondelete), nullable=nullable
    )


def _jsonb_list(column: str) -> sa.Column:
    # Nullable with no default: NULL = not set (import fills), [] = cleared (import keeps).
    return sa.Column(column, postgresql.JSONB(astext_type=sa.Text()), nullable=True)


def upgrade() -> None:
    op.create_table(
        "pm_wip_limits",
        *_owned_columns(),
        _fk("project_type_id", "pm_project_types", "CASCADE"),
        _fk("phase_id", "pm_project_phases", "RESTRICT"),
        _fk("capability_id", "pm_capabilities", "RESTRICT"),
        _fk("card_type_id", "pm_card_types", "RESTRICT"),
        sa.Column("wip_limit", sa.Integer(), nullable=False),
        sa.CheckConstraint("visibility IN ('tenant', 'user')", name="ck_pm_wip_limits_visibility"),
        sa.CheckConstraint("wip_limit >= 0", name="ck_pm_wip_limits_wip_limit"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_type_id",
            "phase_id",
            "capability_id",
            "card_type_id",
            name="uq_pm_wip_limits_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )
    for col in ("project_type_id", "phase_id", "capability_id", "card_type_id"):
        op.create_index(f"ix_pm_wip_limits_{col}", "pm_wip_limits", [col])

    op.create_table(
        "pm_priority_levels",
        *_catalogue_columns(),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("conveyor_may_set", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_catalogue_constraints("pm_priority_levels"),
        sa.UniqueConstraint("tenant_id", "rank", name="uq_pm_priority_levels_tenant_rank"),
        sa.CheckConstraint("rank >= 0", name="ck_pm_priority_levels_rank"),
    )
    op.create_index(
        "uq_pm_priority_levels_one_default",
        "pm_priority_levels",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "pm_deliverable_kinds",
        *_catalogue_columns(),
        sa.Column(
            "counts_for_phase_exit", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        *_catalogue_constraints("pm_deliverable_kinds"),
    )

    op.create_table(
        "pm_milestone_kinds",
        *_catalogue_columns(),
        _fk("phase_id", "pm_project_phases", "RESTRICT"),
        *_catalogue_constraints("pm_milestone_kinds"),
    )
    op.create_index("ix_pm_milestone_kinds_phase_id", "pm_milestone_kinds", ["phase_id"])

    op.create_table(
        "pm_gates",
        *_catalogue_columns(),
        _fk("phase_id", "pm_project_phases", "RESTRICT", nullable=False),
        sa.Column("approver_role", sa.String(length=32), nullable=False),
        sa.Column("quorum", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("expires_after_hours", sa.Integer(), nullable=True),
        *_catalogue_constraints("pm_gates"),
        sa.CheckConstraint(
            "approver_role IN ('project_owner', 'tenant_admin', 'member', 'super_admin')",
            name="ck_pm_gates_approver_role",
        ),
        sa.CheckConstraint("quorum >= 1", name="ck_pm_gates_quorum"),
        sa.CheckConstraint(
            "expires_after_hours IS NULL OR expires_after_hours > 0",
            name="ck_pm_gates_expires_after_hours",
        ),
    )
    op.create_index("ix_pm_gates_phase_id", "pm_gates", ["phase_id"])

    op.create_table(
        "pm_board_templates",
        *_catalogue_columns(),
        _fk("project_type_id", "pm_project_types", "SET NULL"),
        _jsonb_list("lists"),
        _jsonb_list("labels"),
        _jsonb_list("custom_field_groups"),
        *_catalogue_constraints("pm_board_templates"),
    )
    op.create_index(
        "ix_pm_board_templates_project_type_id", "pm_board_templates", ["project_type_id"]
    )

    for table in _NEW_TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
