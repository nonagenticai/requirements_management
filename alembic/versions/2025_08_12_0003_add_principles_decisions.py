"""Add principles and decisions tables and project_id to requirements

Revision ID: 2025_08_12_0003
Revises: 2025_08_11_0002
Create Date: 2025-08-12 06:28:00

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "2025_08_12_0003"
down_revision = "2025_08_11_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create principles table
    op.create_table(
        "principles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("implications", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_principles_id"), "principles", ["id"], unique=False)
    op.create_index(op.f("ix_principles_slug"), "principles", ["slug"], unique=True)

    # Create decisions table
    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("consequences", sa.Text(), nullable=True),
        sa.Column("alternatives", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "related_principles",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_decisions_id"), "decisions", ["id"], unique=False)
    op.create_index(op.f("ix_decisions_slug"), "decisions", ["slug"], unique=True)
    op.create_index(op.f("ix_decisions_project_id"), "decisions", ["project_id"], unique=False)

    # Add project_id to requirements table
    op.add_column("requirements", sa.Column("project_id", sa.String(length=100), nullable=True))
    op.create_index(
        op.f("ix_requirements_project_id"), "requirements", ["project_id"], unique=False
    )


def downgrade() -> None:
    # Remove project_id from requirements
    op.drop_index(op.f("ix_requirements_project_id"), table_name="requirements")
    op.drop_column("requirements", "project_id")

    # Drop decisions table
    op.drop_index(op.f("ix_decisions_project_id"), table_name="decisions")
    op.drop_index(op.f("ix_decisions_slug"), table_name="decisions")
    op.drop_index(op.f("ix_decisions_id"), table_name="decisions")
    op.drop_table("decisions")

    # Drop principles table
    op.drop_index(op.f("ix_principles_slug"), table_name="principles")
    op.drop_index(op.f("ix_principles_id"), table_name="principles")
    op.drop_table("principles")
