"""Add composite indexes on creator_id for optimized queries

Revision ID: 2025_11_28_0006
Revises: 2025_11_12_0005
Create Date: 2025-11-28 00:00:00

This migration adds composite indexes to optimize common query patterns:
- (creator_id, project_id) for requirements: Optimize filtering by user and project
- (creator_id, project_id) for decisions: Optimize filtering by user and project
- (creator_id, version_status) for all tables: Optimize filtering by user and status
- (creator_id, created_at) for all tables: Optimize ordering user's items by date

These composite indexes improve performance for:
1. Fetching all items for a user in a specific project
2. Fetching drafts/published items for a user
3. Retrieving a user's recent items with ordering
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "2025_11_28_0006"
down_revision = "2025_11_12_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add composite indexes for better query performance."""

    # Requirements table composite indexes
    # Optimize: SELECT * FROM requirements WHERE creator_id = ? AND project_id = ?
    op.create_index(
        "ix_requirements_creator_project",
        "requirements",
        ["creator_id", "project_id"],
        postgresql_concurrently=False,
    )

    # Optimize: SELECT * FROM requirements WHERE creator_id = ? AND version_status = ? ORDER BY created_at
    op.create_index(
        "ix_requirements_creator_status",
        "requirements",
        ["creator_id", "version_status"],
        postgresql_concurrently=False,
    )

    # Optimize: SELECT * FROM requirements WHERE creator_id = ? ORDER BY created_at DESC
    op.create_index(
        "ix_requirements_creator_created",
        "requirements",
        ["creator_id", "created_at"],
        postgresql_concurrently=False,
    )

    # Principles table composite indexes
    # Optimize: SELECT * FROM principles WHERE creator_id = ? AND version_status = ?
    op.create_index(
        "ix_principles_creator_status",
        "principles",
        ["creator_id", "version_status"],
        postgresql_concurrently=False,
    )

    # Optimize: SELECT * FROM principles WHERE creator_id = ? ORDER BY created_at DESC
    op.create_index(
        "ix_principles_creator_created",
        "principles",
        ["creator_id", "created_at"],
        postgresql_concurrently=False,
    )

    # Optimize: SELECT * FROM principles WHERE creator_id = ? AND active = ?
    op.create_index(
        "ix_principles_creator_active",
        "principles",
        ["creator_id", "active"],
        postgresql_concurrently=False,
    )

    # Decisions table composite indexes
    # Optimize: SELECT * FROM decisions WHERE creator_id = ? AND project_id = ?
    op.create_index(
        "ix_decisions_creator_project",
        "decisions",
        ["creator_id", "project_id"],
        postgresql_concurrently=False,
    )

    # Optimize: SELECT * FROM decisions WHERE creator_id = ? AND version_status = ?
    op.create_index(
        "ix_decisions_creator_status",
        "decisions",
        ["creator_id", "version_status"],
        postgresql_concurrently=False,
    )

    # Optimize: SELECT * FROM decisions WHERE creator_id = ? ORDER BY created_at DESC
    op.create_index(
        "ix_decisions_creator_created",
        "decisions",
        ["creator_id", "created_at"],
        postgresql_concurrently=False,
    )

    # Optimize: SELECT * FROM decisions WHERE creator_id = ? AND status = ?
    op.create_index(
        "ix_decisions_creator_decision_status",
        "decisions",
        ["creator_id", "status"],
        postgresql_concurrently=False,
    )


def downgrade() -> None:
    """Remove composite indexes."""

    # Drop decisions indexes
    op.drop_index("ix_decisions_creator_decision_status", table_name="decisions")
    op.drop_index("ix_decisions_creator_created", table_name="decisions")
    op.drop_index("ix_decisions_creator_status", table_name="decisions")
    op.drop_index("ix_decisions_creator_project", table_name="decisions")

    # Drop principles indexes
    op.drop_index("ix_principles_creator_active", table_name="principles")
    op.drop_index("ix_principles_creator_created", table_name="principles")
    op.drop_index("ix_principles_creator_status", table_name="principles")

    # Drop requirements indexes
    op.drop_index("ix_requirements_creator_created", table_name="requirements")
    op.drop_index("ix_requirements_creator_status", table_name="requirements")
    op.drop_index("ix_requirements_creator_project", table_name="requirements")
