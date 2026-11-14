"""View occurrences gain notes, groups and plain lines (Archi's visual-only diagram objects).

* ``architecture_view_objects``: ``kind`` ∈ {element, note, group} (default element),
  ``concept_ref`` nullable with CHECK (set iff kind = element), ``name`` (group title),
  ``text`` (note body), ``label`` (Archi label expression), ``border_type``,
  ``documentation`` (notes and groups only — CHECK; an element occurrence's documentation
  is its element's), ``position`` (z-order: the object's index in its view's object list;
  existing rows are numbered in id order, the order manifests listed them in).
* ``architecture_view_connections``: ``kind`` ∈ {relationship, line} (default
  relationship), ``relationship_ref`` nullable with CHECK (set iff kind = relationship),
  ``label``.

Existing rows are element occurrences and relationship connections, so the defaults and
CHECKs hold for them without a backfill. No RLS change (same tables, same policy).
Downgrade deletes notes, groups and lines (the old shape cannot represent them) after
lifting element occurrences nested in a group to the group's parent, then drops the
columns and restores NOT NULL.

Revision ID: 2026_10_27_0071
Revises: 2026_10_27_0070
Create Date: 2026-10-27
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_10_27_0071"
down_revision = "2026_10_27_0070"
branch_labels = None
depends_on = None

_OBJ = "architecture_view_objects"
_CONN = "architecture_view_connections"


def upgrade() -> None:
    op.add_column(_OBJ, sa.Column("kind", sa.String(10), server_default="element", nullable=False))
    for col in ("name", "text", "label", "documentation"):
        op.add_column(_OBJ, sa.Column(col, sa.Text(), nullable=True))
    op.add_column(_OBJ, sa.Column("border_type", sa.String(20), nullable=True))
    op.add_column(_OBJ, sa.Column("position", sa.Integer(), server_default="0", nullable=False))
    op.execute(
        f"UPDATE {_OBJ} o SET position = n.rn FROM (SELECT id, "
        f"row_number() OVER (PARTITION BY view_id ORDER BY id) - 1 AS rn FROM {_OBJ}) n "
        "WHERE o.id = n.id"
    )
    op.alter_column(_OBJ, "concept_ref", nullable=True)
    op.create_check_constraint(
        "ck_architecture_view_objects_kind", _OBJ, "kind IN ('element', 'note', 'group')"
    )
    op.create_check_constraint(
        "ck_architecture_view_objects_concept_iff_element",
        _OBJ,
        "(kind = 'element') = (concept_ref IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_architecture_view_objects_documentation_not_element",
        _OBJ,
        "kind <> 'element' OR documentation IS NULL",
    )

    op.add_column(
        _CONN, sa.Column("kind", sa.String(15), server_default="relationship", nullable=False)
    )
    op.add_column(_CONN, sa.Column("label", sa.Text(), nullable=True))
    op.alter_column(_CONN, "relationship_ref", nullable=True)
    op.create_check_constraint(
        "ck_architecture_view_connections_kind", _CONN, "kind IN ('relationship', 'line')"
    )
    op.create_check_constraint(
        "ck_architecture_view_connections_relationship_iff_kind",
        _CONN,
        "(kind = 'relationship') = (relationship_ref IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_architecture_view_connections_relationship_iff_kind", _CONN)
    op.drop_constraint("ck_architecture_view_connections_kind", _CONN)
    op.execute(f"DELETE FROM {_CONN} WHERE kind = 'line'")
    op.alter_column(_CONN, "relationship_ref", nullable=False)
    op.drop_column(_CONN, "label")
    op.drop_column(_CONN, "kind")

    op.drop_constraint("ck_architecture_view_objects_documentation_not_element", _OBJ)
    op.drop_constraint("ck_architecture_view_objects_concept_iff_element", _OBJ)
    op.drop_constraint("ck_architecture_view_objects_kind", _OBJ)
    # Element occurrences nested in a group/note move up to the nearest element (or top
    # level) first, so deleting the group does not cascade them away.
    op.execute(
        "DO $$ BEGIN LOOP "
        f"UPDATE {_OBJ} c SET parent_object_id = p.parent_object_id FROM {_OBJ} p "
        "WHERE c.parent_object_id = p.id AND p.kind <> 'element'; "
        "EXIT WHEN NOT FOUND; END LOOP; END $$"
    )
    op.execute(f"DELETE FROM {_OBJ} WHERE kind <> 'element'")
    op.alter_column(_OBJ, "concept_ref", nullable=False)
    for col in ("position", "documentation", "border_type", "label", "text", "name", "kind"):
        op.drop_column(_OBJ, col)
