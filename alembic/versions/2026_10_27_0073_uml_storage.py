"""UML storage: elements, relationships, interactions, diagrams and their occurrences.

Target spec 2026-10-26 §8.2 (profile ``uml-2.5.1-p1``), re-architecture spec §4.2.

* ``uml_elements`` — ``kind``, ``owner_id`` (self FK, cascade: an owner takes what it
  owns), ``name``, ``attributes`` JSONB (features with stable ids).
* ``uml_relationships`` — ``kind``, ``source_ref`` / ``target_ref`` (FK to elements,
  cascade), association ``ends`` JSONB, ``connector_kind``.
* ``uml_interactions`` — one JSONB ``document`` per interaction, ``owner_id``.
* ``uml_diagrams`` — ``kind`` (class | component | sequence), ``name``, ``documentation``.
* ``uml_diagram_objects`` / ``uml_diagram_connections`` — occurrences: ``element_ref`` or
  ``lifeline_id``; ``relationship_ref`` or ``message_id``; bounds / style / bendpoints; an
  object's ``position`` (z-order: its index in the diagram's object list).

References between UML rows are composite foreign keys scoped to the model —
``(ref, model_id) → (id, model_id)``, over ``UNIQUE (id, model_id)`` — and a connection's
ends to its diagram (``(object, diagram_id) → (id, diagram_id)``), so no write path can make
a reference cross models or diagrams. All cascade on delete.

Every table carries ``tenant_id`` and a ``model_id`` FK to ``modeling_models`` with
``ON DELETE CASCADE`` (``DELETE /modeling/models/{id}?force`` removes the content), and joins
the RLS ``tenant_isolation`` backstop with the strict uuid predicate (same as 0068–0070);
none joins ``system_published_read``. No GRANTs: production default privileges cover the
application role.

Downgrade drops the six tables and then deletes every ``language = 'uml'`` model with its
revisions, validation runs (and findings), change sets, reviews, publications and trace
links — plus trace links of other models ending on a UML row — children first.

Revision ID: 2026_10_27_0073
Revises: 2026_10_27_0072
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_27_0073"
down_revision = "2026_10_27_0072"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"
_TABLES = (
    "uml_elements",
    "uml_relationships",
    "uml_interactions",
    "uml_diagrams",
    "uml_diagram_objects",
    "uml_diagram_connections",
)


def _fk(name: str, target: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name, sa.Uuid(), sa.ForeignKey(f"{target}.id", ondelete="CASCADE"), nullable=nullable
    )


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"))


def _ref(name: str) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=True)


def _in_model(column: str, table: str, name: str) -> sa.ForeignKeyConstraint:
    """(column, model_id) → table(id, model_id): references stay inside one model."""
    return sa.ForeignKeyConstraint(
        [column, "model_id"], [f"{table}.id", f"{table}.model_id"], ondelete="CASCADE", name=name
    )


def _on_diagram(column: str, name: str) -> sa.ForeignKeyConstraint:
    """(object, diagram_id) → uml_diagram_objects(id, diagram_id): ends on one diagram."""
    return sa.ForeignKeyConstraint(
        [column, "diagram_id"],
        ["uml_diagram_objects.id", "uml_diagram_objects.diagram_id"],
        ondelete="CASCADE",
        name=name,
    )


def _common(table: str, *, scoped: bool) -> list:
    cols: list = [
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        _fk("model_id", "modeling_models"),
    ]
    if scoped:
        cols += [
            sa.Column("project_id", sa.String(100), nullable=True),
            sa.Column("visibility", sa.String(16), server_default="tenant", nullable=False),
            sa.Column("subject_id", sa.String(255), nullable=False),
            sa.CheckConstraint("visibility IN ('tenant', 'user')", name=f"ck_{table}_visibility"),
        ]
    return cols


def upgrade() -> None:
    op.create_table(
        "uml_elements",
        *_common("uml_elements", scoped=True),
        sa.Column("kind", sa.String(30), nullable=False),
        _ref("owner_id"),
        sa.Column("name", sa.String(300), server_default="", nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("id", "model_id", name="uq_uml_elements_id_model"),
        _in_model("owner_id", "uml_elements", "fk_uml_elements_owner_in_model"),
    )
    op.create_table(
        "uml_relationships",
        *_common("uml_relationships", scoped=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("name", sa.String(300), server_default="", nullable=False),
        _ref("source_ref"),
        _ref("target_ref"),
        sa.Column("ends", postgresql.JSONB(), nullable=True),
        sa.Column("connector_kind", sa.String(20), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("id", "model_id", name="uq_uml_relationships_id_model"),
        _in_model("source_ref", "uml_elements", "fk_uml_relationships_source_in_model"),
        _in_model("target_ref", "uml_elements", "fk_uml_relationships_target_in_model"),
    )
    op.create_table(
        "uml_interactions",
        *_common("uml_interactions", scoped=True),
        sa.Column("name", sa.String(300), server_default="", nullable=False),
        _ref("owner_id"),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        _in_model("owner_id", "uml_elements", "fk_uml_interactions_owner_in_model"),
    )
    op.create_table(
        "uml_diagrams",
        *_common("uml_diagrams", scoped=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(300), server_default="", nullable=False),
        sa.Column("documentation", sa.Text(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("id", "model_id", name="uq_uml_diagrams_id_model"),
    )
    op.create_table(
        "uml_diagram_objects",
        *_common("uml_diagram_objects", scoped=False),
        sa.Column("diagram_id", sa.Uuid(), nullable=False),
        _ref("element_ref"),
        sa.Column("lifeline_id", sa.String(100), nullable=True),
        sa.Column("bounds", postgresql.JSONB(), nullable=True),
        sa.Column("style", postgresql.JSONB(), nullable=True),
        # z-order: the object's index in its diagram's object list.
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(
            "(element_ref IS NULL) <> (lifeline_id IS NULL)",
            name="ck_uml_diagram_objects_one_ref",
        ),
        sa.UniqueConstraint("id", "diagram_id", name="uq_uml_diagram_objects_id_diagram"),
        _in_model("diagram_id", "uml_diagrams", "fk_uml_diagram_objects_diagram_in_model"),
        _in_model("element_ref", "uml_elements", "fk_uml_diagram_objects_element_in_model"),
    )
    op.create_table(
        "uml_diagram_connections",
        *_common("uml_diagram_connections", scoped=False),
        sa.Column("diagram_id", sa.Uuid(), nullable=False),
        _ref("relationship_ref"),
        sa.Column("message_id", sa.String(100), nullable=True),
        sa.Column("source_object_id", sa.Uuid(), nullable=False),
        sa.Column("target_object_id", sa.Uuid(), nullable=False),
        sa.Column("bendpoints", postgresql.JSONB(), nullable=True),
        sa.Column("style", postgresql.JSONB(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(
            "(relationship_ref IS NULL) <> (message_id IS NULL)",
            name="ck_uml_diagram_connections_one_ref",
        ),
        _in_model("diagram_id", "uml_diagrams", "fk_uml_diagram_connections_diagram_in_model"),
        _in_model(
            "relationship_ref",
            "uml_relationships",
            "fk_uml_diagram_connections_relationship_in_model",
        ),
        _on_diagram("source_object_id", "fk_uml_diagram_connections_source_on_diagram"),
        _on_diagram("target_object_id", "fk_uml_diagram_connections_target_on_diagram"),
    )

    for table, cols in (
        ("uml_elements", ("tenant_id", "model_id", "owner_id", "project_id")),
        ("uml_relationships", ("tenant_id", "model_id", "source_ref", "target_ref", "project_id")),
        ("uml_interactions", ("tenant_id", "model_id", "owner_id", "project_id")),
        ("uml_diagrams", ("tenant_id", "model_id", "project_id")),
        ("uml_diagram_objects", ("tenant_id", "model_id", "diagram_id", "element_ref")),
        (
            "uml_diagram_connections",
            (
                "tenant_id",
                "model_id",
                "diagram_id",
                "relationship_ref",
                "source_object_id",
                "target_object_id",
            ),
        ),
    ):
        for col in cols:
            op.create_index(f"ix_{table}_{col}", table, [col])
    for table in ("uml_elements", "uml_relationships", "uml_diagrams"):
        op.create_index(f"ix_{table}_model_kind", table, ["model_id", "kind"])

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


# The trace-link end kinds that name a UML row (they dangle once the tables are gone).
_UML_TRACE_KINDS = "('uml_element', 'uml_relationship', 'interaction', 'diagram')"


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
    # Before 0073 a model holds ArchiMate content only: UML models go, with everything
    # that belongs to them, children first (FK-safe without relying on the cascades),
    # and so do trace links ending on a UML row from an ArchiMate model.
    uml = "SELECT id FROM modeling_models WHERE language = 'uml'"
    op.execute(
        f"DELETE FROM modeling_trace_links WHERE model_id IN ({uml}) "
        f"OR source_kind IN {_UML_TRACE_KINDS} OR target_kind IN {_UML_TRACE_KINDS}"
    )
    op.execute(f"DELETE FROM modeling_publications WHERE model_id IN ({uml})")
    op.execute(f"DELETE FROM modeling_reviews WHERE model_id IN ({uml})")
    op.execute(
        "DELETE FROM modeling_validation_findings WHERE run_id IN "
        f"(SELECT id FROM modeling_validation_runs WHERE model_id IN ({uml}))"
    )
    op.execute(f"DELETE FROM modeling_validation_runs WHERE model_id IN ({uml})")
    op.execute(f"DELETE FROM modeling_change_sets WHERE model_id IN ({uml})")
    op.execute(f"DELETE FROM modeling_model_revisions WHERE model_id IN ({uml})")
    op.execute("DELETE FROM modeling_models WHERE language = 'uml'")
