"""Fix ~190 nullable mismatches between models and migrations.

Many columns were created as nullable=True in migrations but the SQLAlchemy
models declare them as Mapped[str], Mapped[float], Mapped[bool], etc. (non-Optional),
meaning they should be NOT NULL in the database.

This migration:
1. Fills NULL values with sensible defaults
2. Sets columns to NOT NULL to match model declarations

Revision ID: 2026_03_24_0017
Revises: 2026_03_23_0016
Create Date: 2026-03-24
"""

from alembic import op

revision = "2026_03_24_0017"
down_revision = "2026_03_23_0016"
branch_labels = None
depends_on = None


def _fill_and_set_not_null(table: str, column: str, default_expr: str) -> None:
    """UPDATE nulls then ALTER to NOT NULL."""
    op.execute(f"UPDATE {table} SET {column} = {default_expr} WHERE {column} IS NULL")  # noqa: S608
    op.alter_column(table, column, nullable=False)


def _fill_slug_and_set_not_null(table: str) -> None:
    """Fill NULL slugs with 'auto-<id>' and set NOT NULL."""
    op.execute(  # noqa: S608
        f"UPDATE {table} SET slug = 'auto-' || id::text WHERE slug IS NULL"
    )
    op.alter_column(table, "slug", nullable=False)


def upgrade() -> None:
    # ==================================================================
    # requirements  (created in 0001, columns added in 0003-0005)
    # ==================================================================
    # slug, title, description, links, acceptance, effort were NOT NULL from 0001.
    # reach, impact, confidence had server_default + NOT NULL from 0001.
    # creator_id was made NOT NULL in 0005.
    # version, version_status were NOT NULL with server_default in 0004.

    # requirements: created_at, updated_at — model says Optional, no fix needed.

    # ==================================================================
    # principles  (created in 0003, columns added in 0004-0005)
    # ==================================================================
    # id, slug, title, statement, implications, tags, active were NOT NULL from 0003.
    # creator_id was made NOT NULL in 0005.
    # version, version_status were NOT NULL with server_default in 0004.

    # ==================================================================
    # decisions  (created in 0003, columns added in 0004-0005)
    # ==================================================================
    # id, slug, title, status, context, decision, alternatives, related_principles
    # were NOT NULL from 0003.
    # creator_id was made NOT NULL in 0005.
    # version, version_status were NOT NULL with server_default in 0004.

    # ==================================================================
    # entity_version_history  (created in 0009, migrated in 0011)
    # ==================================================================
    # All non-nullable fields (entity_type, entity_id, version, version_status,
    # snapshot_data) were explicitly nullable=False in 0009. OK.

    # ==================================================================
    # architecture_elements  (created in 0013)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: layer is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: element_type is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version_status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("architecture_elements")

    # ==================================================================
    # architecture_relationships  (created in 0013)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: source_entity_type is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: source_id is Mapped[UUID] (NOT NULL), migration: nullable=False — OK
    # Model: target_entity_type is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: target_id is Mapped[UUID] (NOT NULL), migration: nullable=False — OK
    # Model: relationship_type is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("architecture_relationships")

    # ==================================================================
    # architecture_views  (created in 0014)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: title is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: viewpoint is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version_status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("architecture_views")

    # ==================================================================
    # architecture_standards  (created in 0014)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: title is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: standard_type is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: compliance_level is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version_status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("architecture_standards")

    # ==================================================================
    # pm_programmes  (created in 0015)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: title is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version_status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("pm_programmes")

    # ==================================================================
    # pm_projects  (created in 0015)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: title is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version_status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("pm_projects")

    # ==================================================================
    # pm_activities  (created in 0015)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: title is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: project_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: is_milestone is Mapped[bool] (NOT NULL), migration: nullable=False — OK
    # Model: is_summary is Mapped[bool] (NOT NULL), migration: nullable=False — OK
    # Model: percent_complete is Mapped[float] (NOT NULL), migration: nullable=False — OK
    # Model: earned_value_method is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version_status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("pm_activities")

    # ==================================================================
    # pm_resources  (created in 0015)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: title is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: resource_type is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: capacity_per_day is Mapped[float] (NOT NULL), migration: nullable=False — OK
    # Model: version is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version_status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("pm_resources")

    # ==================================================================
    # pm_buffers  (created in 0015)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: project_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: buffer_type is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: chain_activity_ids is Mapped[List[Any]] (NOT NULL), migration: nullable=True
    # Model: size_days is Mapped[float] (NOT NULL), migration: nullable=False — OK
    # Model: consumed_days is Mapped[float] (NOT NULL), migration: nullable=False — OK
    # Model: penetration_pct is Mapped[float] (NOT NULL), migration: nullable=False — OK
    # Model: status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("pm_buffers")
    _fill_and_set_not_null("pm_buffers", "chain_activity_ids", "'[]'::jsonb")

    # ==================================================================
    # pm_schedule_snapshots  (created in 0015)
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: project_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: snapshot_date is Mapped[datetime] (NOT NULL), migration: nullable=False — OK
    # Model: snapshot_type is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: activity_data is Mapped[Dict] (NOT NULL), migration: nullable=True
    # Model: evm_data is Mapped[Dict] (NOT NULL), migration: nullable=True
    # ==================================================================
    _fill_and_set_not_null("pm_schedule_snapshots", "activity_data", "'{}'::jsonb")
    _fill_and_set_not_null("pm_schedule_snapshots", "evm_data", "'{}'::jsonb")

    # ==================================================================
    # pm_risks  (created in 0016)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: title is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: project_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: probability is Mapped[float] (NOT NULL), migration: nullable=False — OK
    # Model: impact_score is Mapped[float] (NOT NULL), migration: nullable=False — OK
    # Model: risk_score is Mapped[float] (NOT NULL), migration: nullable=False — OK
    # Model: category is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: response_strategy is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version_status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("pm_risks")

    # ==================================================================
    # pm_benefits  (created in 0016)
    # Model: slug is Mapped[str] (NOT NULL), migration: nullable=True
    # Model: title is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: creator_id is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: benefit_type is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: measurement is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # Model: version_status is Mapped[str] (NOT NULL), migration: nullable=False — OK
    # ==================================================================
    _fill_slug_and_set_not_null("pm_benefits")


def downgrade() -> None:
    """Revert all columns back to nullable=True."""
    # architecture_elements
    op.alter_column("architecture_elements", "slug", nullable=True)

    # architecture_relationships
    op.alter_column("architecture_relationships", "slug", nullable=True)

    # architecture_views
    op.alter_column("architecture_views", "slug", nullable=True)

    # architecture_standards
    op.alter_column("architecture_standards", "slug", nullable=True)

    # pm_programmes
    op.alter_column("pm_programmes", "slug", nullable=True)

    # pm_projects
    op.alter_column("pm_projects", "slug", nullable=True)

    # pm_activities
    op.alter_column("pm_activities", "slug", nullable=True)

    # pm_resources
    op.alter_column("pm_resources", "slug", nullable=True)

    # pm_buffers
    op.alter_column("pm_buffers", "slug", nullable=True)
    op.alter_column("pm_buffers", "chain_activity_ids", nullable=True)

    # pm_schedule_snapshots
    op.alter_column("pm_schedule_snapshots", "activity_data", nullable=True)
    op.alter_column("pm_schedule_snapshots", "evm_data", nullable=True)

    # pm_risks
    op.alter_column("pm_risks", "slug", nullable=True)

    # pm_benefits
    op.alter_column("pm_benefits", "slug", nullable=True)
