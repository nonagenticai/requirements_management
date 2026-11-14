"""PM catalogue: project phases, capabilities, card types, project types + composition.

Six tables (see ``src/models/pm_catalogue.py``):

* four tenant-owned catalogues — ``pm_project_phases``, ``pm_capabilities``,
  ``pm_card_types`` (each in exactly one capability) and ``pm_project_types``;
* ``pm_project_type_phases`` — a project type's ordered phase selection;
* ``pm_project_type_card_types`` — the card types a project type uses per selected phase,
  with a composite FK onto the selection so a card type cannot sit in an unselected phase.

Every table carries a NOT NULL ``uuid`` ``tenant_id`` and joins the RLS tenant-isolation
backstop with the strict uuid predicate (same as ``2026_09_23_0043``). None joins
``system_published_read``: customer tenants get the defaults by COPY
(``POST /pm/catalogue/import-defaults``), not by inheritance.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_12_0050
Revises: 2026_10_02_0049
Create Date: 2026-10-12
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_12_0050"
down_revision = "2026_10_02_0049"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"

_CATALOGUES = ("pm_project_phases", "pm_capabilities", "pm_card_types", "pm_project_types")
_COMPOSITION = ("pm_project_type_phases", "pm_project_type_card_types")


def _catalogue_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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


def _catalogue_constraints(table: str) -> list:
    return [
        sa.UniqueConstraint("tenant_id", "name", name=f"uq_{table}_tenant_name"),
        sa.CheckConstraint("visibility IN ('tenant', 'user')", name=f"ck_{table}_visibility"),
    ]


def upgrade() -> None:
    op.create_table(
        "pm_project_phases",
        *_catalogue_columns(),
        *_catalogue_constraints("pm_project_phases"),
    )
    op.create_table(
        "pm_capabilities",
        *_catalogue_columns(),
        *_catalogue_constraints("pm_capabilities"),
    )
    op.create_table(
        "pm_card_types",
        *_catalogue_columns(),
        sa.Column(
            "capability_id",
            sa.Uuid(),
            sa.ForeignKey("pm_capabilities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        *_catalogue_constraints("pm_card_types"),
    )
    op.create_index("ix_pm_card_types_capability_id", "pm_card_types", ["capability_id"])
    op.create_table(
        "pm_project_types",
        *_catalogue_columns(),
        *_catalogue_constraints("pm_project_types"),
    )
    for table in _CATALOGUES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    op.create_table(
        "pm_project_type_phases",
        sa.Column(
            "project_type_id",
            sa.Uuid(),
            sa.ForeignKey("pm_project_types.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "phase_id",
            sa.Uuid(),
            sa.ForeignKey("pm_project_phases.id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.UniqueConstraint(
            "project_type_id", "position", name="uq_pm_project_type_phases_type_position"
        ),
    )
    op.create_index("ix_pm_project_type_phases_phase", "pm_project_type_phases", ["phase_id"])

    op.create_table(
        "pm_project_type_card_types",
        sa.Column("project_type_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("phase_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "card_type_id",
            sa.Uuid(),
            sa.ForeignKey("pm_card_types.id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_type_id", "phase_id"],
            ["pm_project_type_phases.project_type_id", "pm_project_type_phases.phase_id"],
            ondelete="CASCADE",
            name="fk_pm_project_type_card_types_selected_phase",
        ),
    )
    op.create_index(
        "ix_pm_project_type_card_types_card_type", "pm_project_type_card_types", ["card_type_id"]
    )
    for table in _COMPOSITION:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    for table in _CATALOGUES + _COMPOSITION:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in reversed(_CATALOGUES + _COMPOSITION):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("pm_project_type_card_types")
    op.drop_table("pm_project_type_phases")
    op.drop_table("pm_project_types")
    op.drop_table("pm_card_types")
    op.drop_table("pm_capabilities")
    op.drop_table("pm_project_phases")
