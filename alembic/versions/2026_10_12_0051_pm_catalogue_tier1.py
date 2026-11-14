"""PM catalogue tier 1: phase / card-type attributes, entry flags, recipes.

Columns on existing tables (see ``src/models/pm_catalogue.py``):

* ``pm_project_phases`` — ``output``, ``exit_rule`` (conveyor v3 ``output`` / ``exit``);
* ``pm_project_type_phases`` — nullable per-project-type overrides of both;
* ``pm_project_type_card_types`` — ``is_entry`` (the phase starts with this card type);
* ``pm_card_types`` — ``inputs``, ``outputs``, ``acceptance``, ``rules`` (jsonb lists),
  ``human_task``, ``definition_of_done`` (CHECK against the ncn landing predicates).

The new attribute columns are NULL on existing rows — "never set" — so the default import
can fill them without overwriting a tenant's edit. Semantics, for every fillable column:
NULL = not set, and the default import fills it; ``[]`` / ``""`` = deliberately cleared,
and the import keeps it. The import never re-parents a row.

Two new tables, both with a NOT NULL ``uuid`` ``tenant_id`` and the RLS tenant-isolation
backstop with the strict uuid predicate (same as ``2026_10_12_0050``); neither joins
``system_published_read``:

* ``pm_recipes`` — one per (tenant, project type, phase, size), NULLS NOT DISTINCT; its
  phase must be one the project type selected: composite FK ``(project_type_id,
  phase_id)`` → ``pm_project_type_phases``, NO ACTION, DEFERRABLE INITIALLY DEFERRED (the
  phase selection is replaced by delete-and-reinsert in one transaction; the API's 409
  "phase still used by recipe(s)" runs first, the FK is the backstop);
* ``pm_recipe_items`` — its ordered lines; a CHECK ties ``count_mode`` to its fields.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_12_0051
Revises: 2026_10_12_0050
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_12_0051"
down_revision = "2026_10_12_0050"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"

_NEW_TABLES = ("pm_recipes", "pm_recipe_items")


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
    op.add_column("pm_project_phases", sa.Column("output", sa.Text(), nullable=True))
    op.add_column("pm_project_phases", sa.Column("exit_rule", sa.Text(), nullable=True))
    op.add_column("pm_project_type_phases", sa.Column("output", sa.Text(), nullable=True))
    op.add_column("pm_project_type_phases", sa.Column("exit_rule", sa.Text(), nullable=True))
    op.add_column(
        "pm_project_type_card_types",
        sa.Column("is_entry", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    for col in ("inputs", "outputs", "acceptance", "rules"):
        op.add_column(
            "pm_card_types",
            sa.Column(col, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )
    op.add_column("pm_card_types", sa.Column("human_task", sa.Boolean(), nullable=True))
    op.add_column(
        "pm_card_types", sa.Column("definition_of_done", sa.String(length=32), nullable=True)
    )
    op.create_check_constraint(
        "ck_pm_card_types_definition_of_done",
        "pm_card_types",
        "definition_of_done IS NULL OR definition_of_done IN "
        "('pr_merged', 'pr_opened', 'asset_promoted', 'none')",
    )

    op.create_table(
        "pm_recipes",
        *_catalogue_columns(),
        sa.Column(
            "project_type_id",
            sa.Uuid(),
            sa.ForeignKey("pm_project_types.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "phase_id",
            sa.Uuid(),
            sa.ForeignKey("pm_project_phases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("size", sa.String(length=1), nullable=True),
        *_catalogue_constraints("pm_recipes"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_type_id",
            "phase_id",
            "size",
            name="uq_pm_recipes_type_phase_size",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint("size IS NULL OR size IN ('S', 'M', 'L')", name="ck_pm_recipes_size"),
        sa.ForeignKeyConstraint(
            ["project_type_id", "phase_id"],
            ["pm_project_type_phases.project_type_id", "pm_project_type_phases.phase_id"],
            name="fk_pm_recipes_selected_phase",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index("ix_pm_recipes_project_type_id", "pm_recipes", ["project_type_id"])
    op.create_index("ix_pm_recipes_phase_id", "pm_recipes", ["phase_id"])

    op.create_table(
        "pm_recipe_items",
        sa.Column(
            "recipe_id",
            sa.Uuid(),
            sa.ForeignKey("pm_recipes.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "card_type_id",
            sa.Uuid(),
            sa.ForeignKey("pm_card_types.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("count_mode", sa.String(length=8), nullable=False),
        sa.Column("count", sa.Integer(), nullable=True),
        sa.Column("per_field", sa.Text(), nullable=True),
        sa.Column("cap", sa.Integer(), nullable=True),
        sa.Column(
            "inputs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "outputs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.CheckConstraint(
            "(count_mode = 'fixed' AND count IS NOT NULL AND count >= 1 AND per_field IS NULL"
            " AND cap IS NULL)"
            " OR (count_mode = 'per' AND count IS NULL AND per_field IS NOT NULL"
            " AND (cap IS NULL OR cap >= 1))",
            name="ck_pm_recipe_items_count_mode",
        ),
    )
    op.create_index("ix_pm_recipe_items_card_type", "pm_recipe_items", ["card_type_id"])

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
    op.drop_table("pm_recipe_items")
    op.drop_table("pm_recipes")
    op.drop_constraint("ck_pm_card_types_definition_of_done", "pm_card_types", type_="check")
    for col in ("definition_of_done", "human_task", "rules", "acceptance", "outputs", "inputs"):
        op.drop_column("pm_card_types", col)
    op.drop_column("pm_project_type_card_types", "is_entry")
    for table in ("pm_project_type_phases", "pm_project_phases"):
        op.drop_column(table, "exit_rule")
        op.drop_column(table, "output")
