"""Modeling lifecycle part 1: models, revisions, view occurrences; ``model_id`` on concepts.

Target spec 2026-10-26 §7.1 (models), §7.2 (draft head + revisions), §8.1 (views gain
``model_id``; occurrences live in ``architecture_view_objects`` /
``architecture_view_connections``).

* CREATE ``modeling_models`` (``uq_modeling_models_default_scope``: one DEFAULT model per
  ``(tenant, project_id, visibility, subject-if-private, language)``),
  ``modeling_model_revisions`` (unique ``(model_id, seq)``), ``architecture_view_objects``
  and ``architecture_view_connections``. All four join the RLS ``tenant_isolation``
  backstop with the strict uuid predicate (same as ``0068``); none joins
  ``system_published_read``.
* ADD ``model_id`` to ``architecture_elements``, ``architecture_views`` (NOT NULL after the
  backfill) and ``architecture_relationships`` (nullable: the table also carries PM, TOC
  and governance links, which belong to no model). ``architecture_views.viewpoint``
  already exists.
* Backfill (§7.1): one default ArchiMate model per (tenant, project_id, visibility,
  subject) over existing elements and views; each row joins its scope's model, keeping its
  id, bindings and parent links. A relationship whose both ends are ArchiMate concepts
  inherits its source end's model (relationship-on-relationship ends resolved
  iteratively); one whose source has no model joins its own scope's default.

Production has 0 architecture rows (purged 2026-10-26), so the backfill is a no-op there;
it is written for any database that still has rows. Downgrade drops the columns and the
four tables.

Revision ID: 2026_10_27_0069
Revises: 2026_10_27_0068
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_27_0069"
down_revision = "2026_10_27_0068"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"
_NEW_TABLES = (
    "modeling_models",
    "modeling_model_revisions",
    "architecture_view_objects",
    "architecture_view_connections",
)
_CONCEPT_TABLES = ("architecture_elements", "architecture_relationships", "architecture_views")
# Same scope key as the unique index: subject only for private rows.
_SCOPE_MATCH = (
    "m.is_default AND m.language = 'archimate' AND m.tenant_id = r.tenant_id "
    "AND COALESCE(m.project_id, '') = COALESCE(r.project_id, '') "
    "AND m.visibility = r.visibility "
    "AND (CASE WHEN m.visibility = 'user' THEN m.subject_id ELSE '' END) = "
    "(CASE WHEN r.visibility = 'user' THEN r.subject_id ELSE '' END)"
)
_ARCHIMATE_EDGE = (
    "r.source_entity_type IN ('architecture_elements', 'architecture_relationships') "
    "AND r.target_entity_type IN ('architecture_elements', 'architecture_relationships')"
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
        ),
    ]


def _default_models_for(table: str, where: str = "true") -> None:
    op.execute(
        "INSERT INTO modeling_models (id, tenant_id, project_id, visibility, subject_id, "
        "language, language_version, title, description, head, is_default) "
        "SELECT uuidv7(), s.tenant_id, s.project_id, s.visibility, s.subject_id, "
        "'archimate', '3.2', "
        "'Default archimate model' || COALESCE(' (' || s.project_id || ')', ''), "
        "'Implicit model of writes that name no model_id', 0, true "
        "FROM (SELECT r.tenant_id, NULLIF(COALESCE(r.project_id, ''), '') AS project_id, "
        "  r.visibility, "
        # A private scope has one subject; a tenant scope records its first subject.
        "  min(r.subject_id) AS subject_id "
        f"  FROM {table} r WHERE {where} "
        "  GROUP BY r.tenant_id, COALESCE(r.project_id, ''), r.visibility, "
        "    CASE WHEN r.visibility = 'user' THEN r.subject_id ELSE '' END) s "
        "ON CONFLICT DO NOTHING"
    )


def upgrade() -> None:
    op.create_table(
        "modeling_models",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("project_id", sa.String(100), nullable=True),
        sa.Column("visibility", sa.String(16), server_default="tenant", nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("language", sa.String(20), nullable=False),
        sa.Column("language_version", sa.String(30), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("head", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("published_revision_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "visibility IN ('tenant', 'user')", name="ck_modeling_models_visibility"
        ),
        sa.CheckConstraint("language IN ('archimate', 'uml')", name="ck_modeling_models_language"),
    )
    op.create_index("ix_modeling_models_tenant_id", "modeling_models", ["tenant_id"])
    op.create_index(
        "ix_modeling_models_tenant_created", "modeling_models", ["tenant_id", "created_at"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_modeling_models_default_scope ON modeling_models "
        "(tenant_id, COALESCE(project_id, ''), visibility, "
        "(CASE WHEN visibility = 'user' THEN subject_id ELSE '' END), language) "
        "WHERE is_default"
    )

    op.create_table(
        "modeling_model_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column(
            "model_id",
            sa.Uuid(),
            sa.ForeignKey("modeling_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("head", sa.Integer(), nullable=False),
        sa.Column("sealed_by", sa.String(255), nullable=False),
        sa.Column(
            "sealed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
        ),
        sa.UniqueConstraint("model_id", "seq", name="uq_modeling_model_revisions_model_seq"),
    )
    op.create_index(
        "ix_modeling_model_revisions_tenant_id", "modeling_model_revisions", ["tenant_id"]
    )

    for table in _CONCEPT_TABLES:
        op.add_column(table, sa.Column("model_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_model_id", table, "modeling_models", ["model_id"], ["id"]
        )
        op.create_index(f"ix_{table}_model_id", table, ["model_id"])

    op.create_table(
        "architecture_view_objects",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("model_id", sa.Uuid(), sa.ForeignKey("modeling_models.id"), nullable=False),
        sa.Column(
            "view_id",
            sa.Uuid(),
            sa.ForeignKey("architecture_views.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "concept_ref",
            sa.Uuid(),
            sa.ForeignKey("architecture_elements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_object_id",
            sa.Uuid(),
            sa.ForeignKey("architecture_view_objects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("bounds", postgresql.JSONB(), nullable=True),
        sa.Column("style", postgresql.JSONB(), nullable=True),
        *_timestamps(),
    )
    op.create_table(
        "architecture_view_connections",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("model_id", sa.Uuid(), sa.ForeignKey("modeling_models.id"), nullable=False),
        sa.Column(
            "view_id",
            sa.Uuid(),
            sa.ForeignKey("architecture_views.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "relationship_ref",
            sa.Uuid(),
            sa.ForeignKey("architecture_relationships.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_object_id", sa.Uuid(), nullable=False),
        sa.Column("target_object_id", sa.Uuid(), nullable=False),
        sa.Column("bendpoints", postgresql.JSONB(), nullable=True),
        sa.Column("style", postgresql.JSONB(), nullable=True),
        *_timestamps(),
    )
    for table, cols in (
        ("architecture_view_objects", ("tenant_id", "model_id", "view_id", "concept_ref")),
        ("architecture_view_objects", ("parent_object_id",)),
        ("architecture_view_connections", ("tenant_id", "model_id", "view_id")),
        ("architecture_view_connections", ("relationship_ref",)),
    ):
        for col in cols:
            op.create_index(f"ix_{table}_{col}", table, [col])

    # --- backfill (§7.1) ----------------------------------------------------
    _default_models_for("architecture_elements")
    _default_models_for("architecture_views")
    for table in ("architecture_elements", "architecture_views"):
        op.execute(
            f"UPDATE {table} r SET model_id = m.id FROM modeling_models m WHERE {_SCOPE_MATCH}"
        )
    op.execute(
        "UPDATE architecture_relationships r SET model_id = e.model_id "
        "FROM architecture_elements e "
        f"WHERE {_ARCHIMATE_EDGE} AND r.source_entity_type = 'architecture_elements' "
        "AND r.source_id = e.id"
    )
    # Relationship-on-relationship sources: propagate until nothing changes (an edge
    # chain is short; a cycle simply stops propagating once every member is set).
    op.execute(
        "DO $$ BEGIN LOOP "
        "UPDATE architecture_relationships r SET model_id = s.model_id "
        "FROM architecture_relationships s "
        "WHERE r.model_id IS NULL AND r.source_entity_type = 'architecture_relationships' "
        "AND r.target_entity_type IN ('architecture_elements', 'architecture_relationships') "
        "AND r.source_id = s.id AND s.model_id IS NOT NULL; "
        "EXIT WHEN NOT FOUND; END LOOP; END $$"
    )
    _default_models_for("architecture_relationships", f"{_ARCHIMATE_EDGE} AND r.model_id IS NULL")
    op.execute(
        "UPDATE architecture_relationships r SET model_id = m.id FROM modeling_models m "
        f"WHERE {_ARCHIMATE_EDGE} AND r.model_id IS NULL AND {_SCOPE_MATCH}"
    )
    op.alter_column("architecture_elements", "model_id", nullable=False)
    op.alter_column("architecture_views", "model_id", nullable=False)

    for table in _NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("architecture_view_connections")
    op.drop_table("architecture_view_objects")
    for table in _CONCEPT_TABLES:
        op.drop_index(f"ix_{table}_model_id", table_name=table)
        op.drop_constraint(f"fk_{table}_model_id", table, type_="foreignkey")
        op.drop_column(table, "model_id")
    op.drop_table("modeling_model_revisions")
    op.drop_table("modeling_models")
