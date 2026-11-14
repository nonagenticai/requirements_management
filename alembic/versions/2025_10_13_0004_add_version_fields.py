"""Add version management fields to requirements, principles, and decisions

Revision ID: 2025_10_13_0004
Revises: 2025_08_12_0003
Create Date: 2025-10-13 00:00:00
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "2025_10_13_0004"
down_revision = "2025_08_12_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add version management fields to all tables."""

    # Add version fields to requirements table
    op.add_column(
        "requirements",
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
    )
    op.add_column(
        "requirements",
        sa.Column(
            "version_status",
            sa.String(length=20),
            nullable=False,
            server_default="DRAFT",
        ),
    )
    op.add_column("requirements", sa.Column("supersedes", sa.String(length=120), nullable=True))
    op.add_column("requirements", sa.Column("superseded_by", sa.String(length=120), nullable=True))
    op.add_column("requirements", sa.Column("version_notes", sa.Text(), nullable=True))
    op.add_column("requirements", sa.Column("approved_by", sa.String(length=100), nullable=True))
    op.add_column(
        "requirements",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "requirements",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Add version fields to principles table
    op.add_column(
        "principles",
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
    )
    op.add_column(
        "principles",
        sa.Column(
            "version_status",
            sa.String(length=20),
            nullable=False,
            server_default="DRAFT",
        ),
    )
    op.add_column("principles", sa.Column("supersedes", sa.String(length=120), nullable=True))
    op.add_column("principles", sa.Column("superseded_by", sa.String(length=120), nullable=True))
    op.add_column("principles", sa.Column("version_notes", sa.Text(), nullable=True))
    op.add_column("principles", sa.Column("approved_by", sa.String(length=100), nullable=True))
    op.add_column(
        "principles",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "principles",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Add version fields to decisions table
    op.add_column(
        "decisions",
        sa.Column("version", sa.String(length=20), nullable=False, server_default="1.0.0"),
    )
    op.add_column(
        "decisions",
        sa.Column(
            "version_status",
            sa.String(length=20),
            nullable=False,
            server_default="DRAFT",
        ),
    )
    op.add_column("decisions", sa.Column("supersedes", sa.String(length=120), nullable=True))
    op.add_column("decisions", sa.Column("superseded_by", sa.String(length=120), nullable=True))
    op.add_column("decisions", sa.Column("version_notes", sa.Text(), nullable=True))
    op.add_column("decisions", sa.Column("approved_by", sa.String(length=100), nullable=True))
    op.add_column("decisions", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "decisions",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Create indexes for version fields for better query performance
    op.create_index("ix_requirements_version_status", "requirements", ["version_status"])
    op.create_index("ix_principles_version_status", "principles", ["version_status"])
    op.create_index("ix_decisions_version_status", "decisions", ["version_status"])


def downgrade() -> None:
    """Remove version management fields from all tables."""

    # Drop indexes
    op.drop_index("ix_decisions_version_status", table_name="decisions")
    op.drop_index("ix_principles_version_status", table_name="principles")
    op.drop_index("ix_requirements_version_status", table_name="requirements")

    # Remove version fields from decisions table
    op.drop_column("decisions", "published_at")
    op.drop_column("decisions", "approved_at")
    op.drop_column("decisions", "approved_by")
    op.drop_column("decisions", "version_notes")
    op.drop_column("decisions", "superseded_by")
    op.drop_column("decisions", "supersedes")
    op.drop_column("decisions", "version_status")
    op.drop_column("decisions", "version")

    # Remove version fields from principles table
    op.drop_column("principles", "published_at")
    op.drop_column("principles", "approved_at")
    op.drop_column("principles", "approved_by")
    op.drop_column("principles", "version_notes")
    op.drop_column("principles", "superseded_by")
    op.drop_column("principles", "supersedes")
    op.drop_column("principles", "version_status")
    op.drop_column("principles", "version")

    # Remove version fields from requirements table
    op.drop_column("requirements", "published_at")
    op.drop_column("requirements", "approved_at")
    op.drop_column("requirements", "approved_by")
    op.drop_column("requirements", "version_notes")
    op.drop_column("requirements", "superseded_by")
    op.drop_column("requirements", "supersedes")
    op.drop_column("requirements", "version_status")
    op.drop_column("requirements", "version")
