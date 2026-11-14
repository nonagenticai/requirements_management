"""Migrate all entity PKs from Integer to UUID v7.

Changes:
- requirements.id: Integer → UUID
- principles.id: Integer → UUID
- decisions.id: Integer → UUID
- entity_version_history.id: Integer → UUID
- entity_version_history.entity_id: Integer → UUID

Existing rows get gen_random_uuid() values. New rows use Python uuid7().

Revision ID: 2026_03_23_0011
Revises: 2026_03_10_0010
Create Date: 2026-03-23
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_03_23_0011"
down_revision = "2026_03_10_0010"
branch_labels = None
depends_on = None

# Main entity tables and the history table that references them
ENTITY_TABLES = ["requirements", "principles", "decisions"]


def upgrade() -> None:
    conn = op.get_bind()

    # --- Phase 1: Add UUID columns to entity tables ---
    for table in ENTITY_TABLES:
        op.add_column(table, sa.Column("new_id", sa.Uuid(), nullable=True))
        conn.execute(
            sa.text(f"UPDATE {table} SET new_id = gen_random_uuid()")  # noqa: S608
        )
        op.alter_column(table, "new_id", nullable=False)

    # --- Phase 2: Add UUID columns to version history ---
    op.add_column("entity_version_history", sa.Column("new_id", sa.Uuid(), nullable=True))
    op.add_column("entity_version_history", sa.Column("new_entity_id", sa.Uuid(), nullable=True))

    conn.execute(sa.text("UPDATE entity_version_history SET new_id = gen_random_uuid()"))

    # Map old integer entity_id → new UUID via parent tables
    for table in ENTITY_TABLES:
        entity_type = table.rstrip("s")  # requirements→requirement, etc.
        conn.execute(
            sa.text(
                f"""
                UPDATE entity_version_history evh
                SET new_entity_id = t.new_id
                FROM {table} t
                WHERE evh.entity_type = :etype AND evh.entity_id = t.id
                """  # noqa: S608
            ),
            {"etype": entity_type},
        )

    # Any orphan rows (entity deleted but history remains) get a random UUID
    conn.execute(
        sa.text(
            "UPDATE entity_version_history SET new_entity_id = gen_random_uuid() "
            "WHERE new_entity_id IS NULL"
        )
    )
    op.alter_column("entity_version_history", "new_id", nullable=False)
    op.alter_column("entity_version_history", "new_entity_id", nullable=False)

    # --- Phase 3: Swap columns on entity tables ---
    for table in ENTITY_TABLES:
        # Drop indexes that reference the old id column
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.drop_column(table, "id")
        op.alter_column(table, "new_id", new_column_name="id")
        op.create_primary_key(f"{table}_pkey", table, ["id"])
        op.create_index(f"ix_{table}_id", table, ["id"])

    # --- Phase 4: Swap columns on version history ---
    op.drop_constraint("entity_version_history_pkey", "entity_version_history", type_="primary")
    op.drop_index("ix_version_history_entity", table_name="entity_version_history")
    op.drop_index("ix_version_history_entity_version", table_name="entity_version_history")

    op.drop_column("entity_version_history", "id")
    op.drop_column("entity_version_history", "entity_id")

    op.alter_column("entity_version_history", "new_id", new_column_name="id")
    op.alter_column("entity_version_history", "new_entity_id", new_column_name="entity_id")

    op.create_primary_key("entity_version_history_pkey", "entity_version_history", ["id"])
    op.create_index(
        "ix_version_history_entity",
        "entity_version_history",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_version_history_entity_version",
        "entity_version_history",
        ["entity_type", "entity_id", "version"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "Downgrade from UUID to integer PKs is lossy and not supported. "
        "Restore from backup if needed."
    )
