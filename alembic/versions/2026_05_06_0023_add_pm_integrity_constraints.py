"""Add real referential integrity to PM hierarchy.

Three structural fixes the original PM schema deferred:

1. ``pm_projects.programme_id`` was a free-form ``String(100)`` — clean it
   up, convert to ``UUID``, and add a ``FK → pm_programmes.id ON DELETE SET
   NULL``. Rows whose existing string value cannot cast to a UUID, or which
   point at a non-existent programme, are NULLed before the type change so
   the migration cannot fail on cast errors.

2. ``pm_benefits.programme_id`` — same treatment.

3. ``pm_activities.parent_activity_id`` already exists as a ``UUID`` column;
   add a self-FK with ``ON DELETE CASCADE`` so deleting a parent activity
   removes its descendants instead of orphaning them.

4. ``pm_activities.project_id`` is a ``String(100)`` slug-or-UUID; we keep
   the column shape but DON'T add a FK because callers route through the
   slug for legacy compatibility. Documented for future migration.

WARNING — destructive: rows in ``pm_projects.programme_id`` /
``pm_benefits.programme_id`` that don't reference a real programme are
silently NULLed. Run a pre-flight audit on production:

    SELECT id, programme_id FROM pm_projects
    WHERE programme_id IS NOT NULL
      AND programme_id NOT IN (SELECT id::text FROM pm_programmes);

Revision ID: 2026_05_06_0023
Revises: 2026_05_06_0022
Create Date: 2026-05-06
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_05_06_0023"
down_revision = "2026_05_06_0022"
branch_labels = None
depends_on = None


def _null_invalid_programme_refs(table: str) -> None:
    """NULL out programme_id values that aren't valid UUIDs OR don't reference a real programme."""
    # Step 1: NULL non-UUID-shaped strings.
    op.execute(
        f"""
        UPDATE {table}
        SET programme_id = NULL
        WHERE programme_id IS NOT NULL
          AND programme_id !~ '^[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}$'
        """
    )
    # Step 2: NULL UUID-shaped strings that don't match any pm_programmes.id.
    op.execute(
        f"""
        UPDATE {table}
        SET programme_id = NULL
        WHERE programme_id IS NOT NULL
          AND programme_id::uuid NOT IN (SELECT id FROM pm_programmes)
        """
    )


def upgrade() -> None:
    # ---- 1+2. pm_projects / pm_benefits programme_id → UUID + FK ----------
    for table, fk_name in (
        ("pm_projects", "fk_pm_projects_programme_id"),
        ("pm_benefits", "fk_pm_benefits_programme_id"),
    ):
        _null_invalid_programme_refs(table)
        op.alter_column(
            table,
            "programme_id",
            existing_type=sa.String(length=100),
            type_=sa.dialects.postgresql.UUID(as_uuid=True),
            postgresql_using="programme_id::uuid",
            existing_nullable=True,
        )
        op.create_foreign_key(
            fk_name,
            table,
            "pm_programmes",
            ["programme_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # ---- 3. pm_activities.parent_activity_id self-FK CASCADE --------------
    # NULL out rows whose parent_activity_id doesn't reference a real activity.
    op.execute(
        """
        UPDATE pm_activities
        SET parent_activity_id = NULL
        WHERE parent_activity_id IS NOT NULL
          AND parent_activity_id NOT IN (SELECT id FROM pm_activities)
        """
    )
    op.create_foreign_key(
        "fk_pm_activities_parent_activity_id",
        "pm_activities",
        "pm_activities",
        ["parent_activity_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_pm_activities_parent_activity_id",
        "pm_activities",
        type_="foreignkey",
    )

    for table, fk_name in (
        ("pm_benefits", "fk_pm_benefits_programme_id"),
        ("pm_projects", "fk_pm_projects_programme_id"),
    ):
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.alter_column(
            table,
            "programme_id",
            existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
            type_=sa.String(length=100),
            postgresql_using="programme_id::text",
            existing_nullable=True,
        )
