"""UML rows ↔ :class:`~services.uml_draft.UmlDraft`: load, persist, manifest, model housekeeping.

This module is what the shared modeling lifecycle (``services/modeling_models.py``) calls
for a UML model — the revision manifest, the size count and its delete — and what every UML write uses to make the rows equal a gated draft. It imports
no lifecycle service, so the lifecycle can import it.

Persisting a draft (:func:`persist`) diffs it against the draft the write started from:
deleted rows go first (occurrences before concepts), then inserts (owners before what they
own) and updates. Every trace link with an end on a deleted concept is deleted, and the
head of every model whose manifest that changes is bumped (the link belongs to its
source's model, which may be another model).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.modeling import ModelingModel, ModelingTraceLink, bump_models
from models.uml import (
    UmlDiagram,
    UmlDiagramConnection,
    UmlDiagramObject,
    UmlElement,
    UmlInteraction,
    UmlRelationship,
)
from services.uml_draft import UmlDraft

_INTERACTION_PARTS = ("lifelines", "occurrences", "messages", "executions", "fragments")
_CLASSES: dict[str, Any] = {
    "uml_element": UmlElement,
    "uml_relationship": UmlRelationship,
    "interaction": UmlInteraction,
    "diagram": UmlDiagram,
    "diagram_object": UmlDiagramObject,
    "diagram_connection": UmlDiagramConnection,
}
# Trace-link kinds are the four concept tables (occurrences are not trace ends).
_TRACE_KINDS = ("uml_element", "uml_relationship", "interaction", "diagram")


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def _u(value: Any) -> UUID | None:
    return None if value is None else UUID(str(value))


# ---------------------------------------------------------------------------
# rows → draft dicts (the snapshot shape)
# ---------------------------------------------------------------------------


def element_dict(e: UmlElement) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "kind": e.kind,
        "name": e.name or "",
        "owner_id": _s(e.owner_id),
        "attributes": e.attributes,
    }


def relationship_dict(r: UmlRelationship) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "kind": r.kind,
        "name": r.name or "",
        "source": _s(r.source_ref),
        "target": _s(r.target_ref),
        "ends": r.ends,
        "connector_kind": r.connector_kind,
    }


def interaction_dict(i: UmlInteraction) -> dict[str, Any]:
    doc = i.document or {}
    return {
        "id": str(i.id),
        "name": i.name or "",
        "owner_id": _s(i.owner_id),
        **{part: doc.get(part, []) for part in _INTERACTION_PARTS},
    }


def diagram_dict(d: UmlDiagram) -> dict[str, Any]:
    return {"id": str(d.id), "kind": d.kind, "name": d.name or "", "documentation": d.documentation}


def object_dict(o: UmlDiagramObject) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "diagram_id": str(o.diagram_id),
        "element_ref": _s(o.element_ref),
        "lifeline_id": o.lifeline_id,
        "bounds": o.bounds,
        "style": o.style,
    }


def connection_dict(c: UmlDiagramConnection) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "diagram_id": str(c.diagram_id),
        "relationship_ref": _s(c.relationship_ref),
        "message_id": c.message_id,
        "source_object_id": str(c.source_object_id),
        "target_object_id": str(c.target_object_id),
        "bendpoints": c.bendpoints,
        "style": c.style,
    }


_TO_DICT: dict[str, Any] = {
    "uml_element": element_dict,
    "uml_relationship": relationship_dict,
    "interaction": interaction_dict,
    "diagram": diagram_dict,
    "diagram_object": object_dict,
    "diagram_connection": connection_dict,
}


def row_dict(kind: str, row: Any) -> dict[str, Any]:
    return _TO_DICT[kind](row)


def _of_model(cls: Any, model_id: UUID, tenant_id: Any) -> Any:
    """``cls`` rows of the model AND of its tenant.

    A UML row always carries its model's tenant (every write resolves the model through
    ``uml_store.resolve_model`` / ``writable_model``, which require ``can_mutate``), but a
    model's manifest, size and render must never trust that: a row of another tenant
    carrying the id is foreign content, like a relationship edge or a trace link.
    """
    return (cls.model_id == model_id) & (cls.tenant_id == tenant_id)


async def load_draft(session: AsyncSession, model_id: UUID, tenant_id: Any) -> UmlDraft:
    draft = UmlDraft()
    for kind, cls in _CLASSES.items():
        # Diagram objects in z-order (their stored position); the rest by id.
        order = (cls.position, cls.id) if kind == "diagram_object" else (cls.id,)
        rows = (
            await session.execute(
                select(cls).where(_of_model(cls, model_id, tenant_id)).order_by(*order)
            )
        ).scalars()
        table = draft.table(kind)
        for row in rows:
            d = row_dict(kind, row)
            table[d["id"]] = d
    return draft


async def manifest_parts(session: AsyncSession, model_id: UUID, tenant_id: Any) -> dict[str, Any]:
    """The UML part of a revision manifest (spec §7.2): the complete model."""
    return (await load_draft(session, model_id, tenant_id)).manifest_parts()


async def size(session: AsyncSession, model_id: UUID, tenant_id: Any) -> int:
    """:meth:`UmlDraft.size` counted in SQL (without loading the model): every row of the
    six tables plus every part of every interaction document."""
    rows = 0
    for cls in _CLASSES.values():
        count = (
            await session.execute(select(func.count()).where(_of_model(cls, model_id, tenant_id)))
        ).scalar_one()
        rows += int(count)
    lengths = [
        func.coalesce(func.jsonb_array_length(UmlInteraction.document[part]), 0)
        for part in _INTERACTION_PARTS
    ]
    parts_expr = lengths[0]
    for length in lengths[1:]:
        parts_expr = parts_expr + length
    parts = (
        await session.execute(
            select(func.coalesce(func.sum(parts_expr), 0)).where(
                _of_model(UmlInteraction, model_id, tenant_id)
            )
        )
    ).scalar_one()
    return rows + int(parts)


# ---------------------------------------------------------------------------
# draft → rows
# ---------------------------------------------------------------------------


def _columns(kind: str, d: dict[str, Any]) -> dict[str, Any]:
    if kind == "uml_element":
        return {
            "kind": d["kind"],
            "name": d.get("name") or "",
            "owner_id": _u(d.get("owner_id")),
            "attributes": d.get("attributes") or {},
        }
    if kind == "uml_relationship":
        return {
            "kind": d["kind"],
            "name": d.get("name") or "",
            "source_ref": _u(d.get("source")),
            "target_ref": _u(d.get("target")),
            "ends": d.get("ends"),
            "connector_kind": d.get("connector_kind"),
        }
    if kind == "interaction":
        return {
            "name": d.get("name") or "",
            "owner_id": _u(d.get("owner_id")),
            "document": {part: d.get(part) or [] for part in _INTERACTION_PARTS},
        }
    if kind == "diagram":
        return {
            "kind": d["kind"],
            "name": d.get("name") or "",
            "documentation": d.get("documentation"),
        }
    if kind == "diagram_object":
        return {
            "diagram_id": _u(d["diagram_id"]),
            "element_ref": _u(d.get("element_ref")),
            "lifeline_id": d.get("lifeline_id"),
            "bounds": d.get("bounds"),
            "style": d.get("style"),
        }
    return {
        "diagram_id": _u(d["diagram_id"]),
        "relationship_ref": _u(d.get("relationship_ref")),
        "message_id": d.get("message_id"),
        "source_object_id": _u(d["source_object_id"]),
        "target_object_id": _u(d["target_object_id"]),
        "bendpoints": d.get("bendpoints"),
        "style": d.get("style"),
    }


def _owners_first(new: dict[str, dict[str, Any]]) -> list[str]:
    """Ids of new elements, every new owner before what it owns."""
    pending, order = dict(new), []
    while pending:
        ready = [i for i, d in pending.items() if d.get("owner_id") not in pending]
        if not ready:  # an ownership cycle among new rows: the gate refuses it first
            ready = list(pending)
        for i in sorted(ready):
            order.append(i)
            del pending[i]
    return order


_DELETE_ORDER = (
    "diagram_connection",
    "diagram_object",
    "diagram",
    "uml_relationship",
    "interaction",
    "uml_element",
)
_WRITE_ORDER = tuple(reversed(_DELETE_ORDER))


async def persist(
    session: AsyncSession,
    model: ModelingModel,
    before: UmlDraft,
    after: UmlDraft,
    *,
    subject_id: str,
) -> None:
    """Make the model's rows equal ``after`` (they equal ``before``); bump the head."""
    deleted_concepts: list[UUID] = []
    for kind in _DELETE_ORDER:
        gone = sorted(set(before.table(kind)) - set(after.table(kind)))
        if not gone:
            continue
        cls = _CLASSES[kind]
        await session.execute(delete(cls).where(cls.id.in_([UUID(g) for g in gone])))
        if kind in _TRACE_KINDS:
            deleted_concepts += [UUID(g) for g in gone]

    scope = {
        "tenant_id": model.tenant_id,
        "model_id": model.id,
    }
    concept_scope = {
        **scope,
        "project_id": model.project_id,
        "visibility": model.visibility,
        # A private model's rows belong to its owner (they move with the model).
        "subject_id": model.subject_id
        if model.visibility == "user"
        else (subject_id or str(model.tenant_id)),
    }
    # z-order: each diagram object's index in its diagram's list, stored as ``position``.
    positions = after.positions()
    stored = {
        str(i): p
        for i, p in (
            await session.execute(
                select(UmlDiagramObject.id, UmlDiagramObject.position).where(
                    UmlDiagramObject.model_id == model.id
                )
            )
        ).all()
    }
    for kind in _WRITE_ORDER:
        cls = _CLASSES[kind]
        old, new = before.table(kind), after.table(kind)
        added = [i for i in new if i not in old]
        if kind == "uml_element":
            added = _owners_first({i: new[i] for i in added})
        for row_id in added:
            extra = scope if kind in ("diagram_object", "diagram_connection") else concept_scope
            if kind == "diagram_object":
                extra = {**extra, "position": positions[row_id]}
            await session.execute(
                insert(cls).values(id=UUID(row_id), **extra, **_columns(kind, new[row_id]))
            )
        for row_id in sorted(set(new) & set(old)):
            moved = kind == "diagram_object" and stored.get(row_id) != positions[row_id]
            if new[row_id] != old[row_id] or moved:
                values = _columns(kind, new[row_id])
                if kind == "diagram_object":
                    values["position"] = positions[row_id]
                await session.execute(
                    update(cls)
                    .where(cls.id == UUID(row_id))
                    .values(**values, updated_at=func.now())
                )

    touched = {model.id}
    if deleted_concepts:
        touched |= await delete_trace_links(session, deleted_concepts)
    await bump_models(session, touched)


async def delete_trace_links(session: AsyncSession, ids: list[UUID]) -> set[UUID]:
    """Delete every trace link with an end in ``ids``; return the models they belonged to."""
    rows = (
        await session.execute(
            delete(ModelingTraceLink)
            .where(or_(ModelingTraceLink.source_id.in_(ids), ModelingTraceLink.target_id.in_(ids)))
            .returning(ModelingTraceLink.model_id)
        )
    ).all()
    return {r[0] for r in rows if r[0] is not None}


# ---------------------------------------------------------------------------
# Lifecycle hooks (called by services/modeling_models.py for a UML model)
# ---------------------------------------------------------------------------


async def delete_links_into_model(session: AsyncSession, model_id: UUID) -> set[UUID]:
    """Before a model's rows go (FK cascade): links ending on them from OTHER models.

    Links whose source is in the model go with it by FK. Returns the other models whose
    manifests lost a link (their heads are bumped).

    The row ids are collected by ``model_id`` ALONE — deliberately not tenant-scoped like
    ``_of_model``: every UML table's ``model_id`` FK cascades (migration 0073), so when the
    model is deleted the database deletes EVERY row carrying its id, a foreign-tenant one
    included. Those rows' links are not cascade-deleted (``source_id`` / ``target_id`` are
    generic ids), so skipping a foreign row's links would leave them dangling on a row the
    cascade is deleting anyway. This mirrors the FK; it does not write a surviving row.
    """
    ids: list[UUID] = []
    for cls in (UmlElement, UmlRelationship, UmlInteraction, UmlDiagram):
        ids += list(
            (await session.execute(select(cls.id).where(cls.model_id == model_id))).scalars()
        )
    if not ids:
        return set()
    others = await delete_trace_links(session, ids)
    others.discard(model_id)
    await bump_models(session, others)
    return others
