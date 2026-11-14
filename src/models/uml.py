"""UML storage (profile ``uml-2.5.1-p1``; target spec §8.2, re-architecture spec §4.2).

* ``uml_elements`` — packages, classifiers, components, ports, actors. Features
  (properties, operations, parameters, literals, provided/required interfaces) live in
  ``attributes`` with stable caller-chosen ids, exactly as ``schemas.uml.UmlElementAttributes``.
* ``uml_relationships`` — associations (``ends``), generalizations, realizations,
  dependencies, usages and connectors (``connector_kind``).
* ``uml_interactions`` — one document per interaction (lifelines, ordered occurrences,
  messages, executions, fragments); ``PUT`` replaces it whole.
* ``uml_diagrams`` / ``uml_diagram_objects`` / ``uml_diagram_connections`` — class,
  component and sequence diagrams: occurrences REFER to concepts, never copy them, so one
  class shown on two diagrams keeps one identity.

References between UML rows are composite, model-scoped foreign keys (``(ref, model_id) →
(id, model_id)``; a connection's ends ``(object, diagram_id) → (id, diagram_id)``), so the
database itself refuses a reference across models or diagrams, whatever path writes it.

Every row belongs to one UML model (``model_id``, ``ON DELETE CASCADE``) and carries that
model's ``project_id`` / ``visibility`` / ``subject_id``, so a reader of the model reads all
of it. Writes go through ``services/uml_store.py`` (whole-model write-time integrity, head
bump, trace-link clean-up). Migration ``2026_10_27_0073``; RLS ``tenant_isolation`` on all six.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


def _ts() -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _model_fk() -> Mapped[UUID]:
    return mapped_column(Uuid, ForeignKey("modeling_models.id", ondelete="CASCADE"), index=True)


def _ref() -> Mapped[Any]:
    """A nullable reference column; its (model-scoped) FK is a table-level constraint."""
    return mapped_column(Uuid, index=True, nullable=True)


def _in_model(column: str, table: str, name: str) -> ForeignKeyConstraint:
    """``(column, model_id) → table(id, model_id)``: a reference can only name a row of the
    SAME model (MATCH SIMPLE: a NULL reference is not checked)."""
    return ForeignKeyConstraint(
        [column, "model_id"], [f"{table}.id", f"{table}.model_id"], ondelete="CASCADE", name=name
    )


class _Scoped:
    """Columns every UML concept row carries: tenant + the model's scope."""

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    project_id: Mapped[str | None] = mapped_column(String(100), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    subject_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UmlElement(_Scoped, Base):
    __tablename__ = "uml_elements"

    model_id: Mapped[UUID] = _model_fk()
    kind: Mapped[str] = mapped_column(String(30))
    owner_id: Mapped[UUID | None] = _ref()
    name: Mapped[str] = mapped_column(String(300), default="", server_default="")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__: tuple = (
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_uml_elements_visibility"),
        UniqueConstraint("id", "model_id", name="uq_uml_elements_id_model"),
        _in_model("owner_id", "uml_elements", "fk_uml_elements_owner_in_model"),
        Index("ix_uml_elements_model_kind", "model_id", "kind"),
    )


class UmlRelationship(_Scoped, Base):
    __tablename__ = "uml_relationships"

    model_id: Mapped[UUID] = _model_fk()
    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(300), default="", server_default="")
    source_ref: Mapped[UUID | None] = _ref()
    target_ref: Mapped[UUID | None] = _ref()
    # association only: [{element_ref, role, multiplicity, aggregation, navigable}]
    ends: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    connector_kind: Mapped[str | None] = mapped_column(String(20))

    __table_args__: tuple = (
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_uml_relationships_visibility"),
        UniqueConstraint("id", "model_id", name="uq_uml_relationships_id_model"),
        _in_model("source_ref", "uml_elements", "fk_uml_relationships_source_in_model"),
        _in_model("target_ref", "uml_elements", "fk_uml_relationships_target_in_model"),
        Index("ix_uml_relationships_model_kind", "model_id", "kind"),
    )


class UmlInteraction(_Scoped, Base):
    __tablename__ = "uml_interactions"

    model_id: Mapped[UUID] = _model_fk()
    name: Mapped[str] = mapped_column(String(300), default="", server_default="")
    owner_id: Mapped[UUID | None] = _ref()
    # {lifelines, occurrences (ordered), messages, executions, fragments}
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__: tuple = (
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_uml_interactions_visibility"),
        _in_model("owner_id", "uml_elements", "fk_uml_interactions_owner_in_model"),
    )


class UmlDiagram(_Scoped, Base):
    __tablename__ = "uml_diagrams"

    model_id: Mapped[UUID] = _model_fk()
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(300), default="", server_default="")
    documentation: Mapped[str | None] = mapped_column(Text)

    __table_args__: tuple = (
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_uml_diagrams_visibility"),
        UniqueConstraint("id", "model_id", name="uq_uml_diagrams_id_model"),
        Index("ix_uml_diagrams_model_kind", "model_id", "kind"),
    )


class UmlDiagramObject(Base):
    """One occurrence on a diagram: an element (class/component) or a lifeline (sequence)."""

    __tablename__ = "uml_diagram_objects"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    model_id: Mapped[UUID] = _model_fk()
    diagram_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    element_ref: Mapped[UUID | None] = _ref()
    # A lifeline id inside one of the model's interaction documents (no FK: JSONB member).
    lifeline_id: Mapped[str | None] = mapped_column(String(100))
    bounds: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    style: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # z-order: the order of the diagram's object list (position, then id).
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        CheckConstraint(
            "(element_ref IS NULL) <> (lifeline_id IS NULL)",
            name="ck_uml_diagram_objects_one_ref",
        ),
        UniqueConstraint("id", "diagram_id", name="uq_uml_diagram_objects_id_diagram"),
        _in_model("diagram_id", "uml_diagrams", "fk_uml_diagram_objects_diagram_in_model"),
        _in_model("element_ref", "uml_elements", "fk_uml_diagram_objects_element_in_model"),
    )


class UmlDiagramConnection(Base):
    """One occurrence of a relationship (class/component) or a message (sequence)."""

    __tablename__ = "uml_diagram_connections"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    model_id: Mapped[UUID] = _model_fk()
    diagram_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    relationship_ref: Mapped[UUID | None] = _ref()
    message_id: Mapped[str | None] = mapped_column(String(100))
    # Both ends are objects of the SAME diagram (composite FK on (object, diagram)).
    source_object_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    target_object_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    bendpoints: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    style: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        CheckConstraint(
            "(relationship_ref IS NULL) <> (message_id IS NULL)",
            name="ck_uml_diagram_connections_one_ref",
        ),
        _in_model("diagram_id", "uml_diagrams", "fk_uml_diagram_connections_diagram_in_model"),
        _in_model(
            "relationship_ref",
            "uml_relationships",
            "fk_uml_diagram_connections_relationship_in_model",
        ),
        ForeignKeyConstraint(
            ["source_object_id", "diagram_id"],
            ["uml_diagram_objects.id", "uml_diagram_objects.diagram_id"],
            ondelete="CASCADE",
            name="fk_uml_diagram_connections_source_on_diagram",
        ),
        ForeignKeyConstraint(
            ["target_object_id", "diagram_id"],
            ["uml_diagram_objects.id", "uml_diagram_objects.diagram_id"],
            ondelete="CASCADE",
            name="fk_uml_diagram_connections_target_on_diagram",
        ),
    )


UML_CONCEPT_CLASSES = (UmlElement, UmlRelationship, UmlInteraction, UmlDiagram)
UML_TABLES = (
    "uml_elements",
    "uml_relationships",
    "uml_interactions",
    "uml_diagrams",
    "uml_diagram_objects",
    "uml_diagram_connections",
)
