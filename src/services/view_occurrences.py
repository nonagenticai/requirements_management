"""View occurrences: objects and connections of an ArchiMate view (target spec §8.1).

A view's objects form one ordered list — the z-order (``position``, then id): a create goes
on top unless it names a ``position``; ``position`` on create or update moves the object to
that index and renumbers the list densely.

Objects are element occurrences, notes or groups; connections show a relationship or are
plain ``line``s (Archi's visual-only objects). Nesting into a group or note needs no
relationship; a line may join any two objects/connections of the view but not dangle; a
relationship connection cannot attach to a note, group or line (AM-VIEW-002).

Write-time integrity runs the SAME pure validator as every other ArchiMate write
(``tools.architecture.archimate_rules.validate`` via the before/after diff of
``services.archimate_integrity``): the view's occurrences are loaded as a one-view
``ArchimateSnapshot``, validated as they are and as they would be after the write, and the
write is refused with ``422 model_integrity`` for the error findings it would INTRODUCE —
an unresolved concept or relationship (AM-REF-001), a connection whose ends do not show
its relationship's ends (AM-VIEW-002), a visual parent outside the view or a nesting cycle
(AM-VIEW-003).

"Resolves" means: a row of the view's own model that the caller can see. A concept of
another model, another tenant or another subject's private row reads exactly like a
missing one, so existence never leaks.

The legacy membership lists (``ArchitectureView.element_ids`` / ``relationship_ids``) are
held to the same resolution on every view create/update (:class:`ArchitectureViewService`),
and a listed relationship must have its ends listed (AM-VIEW-002, as a relationship
connection must). Deletes prune them (``relationship_cascade.prune_view_members``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid7

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from models.architecture import ArchitectureElement, ArchitectureRelationship, ArchitectureView
from models.modeling import ArchitectureViewConnection, ArchitectureViewObject
from repositories.visibility import can_mutate, is_visible_to, scope
from schemas.modeling import (
    ArchimateConceptIn,
    ArchimateSnapshot,
    ArchimateViewConnectionIn,
    ArchimateViewIn,
    ArchimateViewObjectIn,
)
from schemas.modeling_lifecycle import ViewConnectionIn, ViewObjectIn
from services import unit_of_work as uow
from services.archimate_integrity import introduced_errors, relationship_of, resolve_visible
from services.entity_service import EntityService
from services.model_integrity import integrity_error, single_finding
from settings import get_settings
from tools.architecture.registry import get_registry

# The rules an occurrence write can be refused on (all error-severity).
OCCURRENCE_RULES = ("AM-REF-001", "AM-VIEW-002", "AM-VIEW-003")
# A view larger than this is refused rather than checked partially.
_MAX_VIEW_OCCURRENCES = 5000


def _sub(tenant_id: str, subject_id: str) -> str:
    return subject_id or tenant_id


async def get_view(
    session: AsyncSession,
    view_id: UUID,
    tenant_id: str,
    subject_id: str,
    *,
    mutable: bool,
) -> ArchitectureView:
    view = (
        await session.execute(
            scope(
                select(ArchitectureView).where(ArchitectureView.id == view_id),
                ArchitectureView,
                tenant_id=tenant_id,
                subject_id=_sub(tenant_id, subject_id),
            )
        )
    ).scalar_one_or_none()
    if view is None or (
        mutable
        and not can_mutate(view, subject_id=_sub(tenant_id, subject_id), tenant_id=tenant_id)
    ):
        raise HTTPException(status_code=404, detail="view not found")
    return view


async def list_objects(
    session: AsyncSession, view: ArchitectureView
) -> list[ArchitectureViewObject]:
    return list(
        (
            await session.execute(
                select(ArchitectureViewObject)
                .where(ArchitectureViewObject.view_id == view.id)
                .order_by(ArchitectureViewObject.position, ArchitectureViewObject.id)
            )
        )
        .scalars()
        .all()
    )


async def list_connections(
    session: AsyncSession, view: ArchitectureView
) -> list[ArchitectureViewConnection]:
    return list(
        (
            await session.execute(
                select(ArchitectureViewConnection)
                .where(ArchitectureViewConnection.view_id == view.id)
                .order_by(ArchitectureViewConnection.id)
            )
        )
        .scalars()
        .all()
    )


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _object_in(oid: Any, src: Any) -> ArchimateViewObjectIn:
    """Snapshot form of a stored object or a :class:`ViewObjectIn` (same field names)."""
    return ArchimateViewObjectIn(
        id=str(oid),
        kind=src.kind,
        concept_id=_str(src.concept_ref),
        parent_object_id=_str(src.parent_object_id),
        view_ref=_str(src.view_ref),
    )


def _connection_in(cid: Any, src: Any) -> ArchimateViewConnectionIn:
    return ArchimateViewConnectionIn(
        id=str(cid),
        kind=src.kind,
        relationship_id=_str(src.relationship_ref),
        source_object_id=str(src.source_object_id),
        target_object_id=str(src.target_object_id),
    )


def _object_fields(payload: ViewObjectIn) -> dict[str, Any]:
    return {
        "kind": payload.kind,
        "concept_ref": payload.concept_ref,
        "parent_object_id": payload.parent_object_id,
        "bounds": None if payload.bounds is None else payload.bounds.model_dump(),
        "style": payload.style,
        "name": payload.name,
        "text": payload.text,
        "label": payload.label,
        "border_type": payload.border_type,
        "documentation": payload.documentation,
        "view_ref": payload.view_ref,
    }


async def _check_view_ref(
    session: AsyncSession, view: ArchitectureView, ref: UUID | None, tenant_id: str, subject_id: str
) -> None:
    """A drill-down ``view_ref`` names a visible view of the same model (AM-REF-001)."""
    if ref is None:
        return
    target = await session.get(ArchitectureView, ref)
    if (
        target is None
        or target.model_id != view.model_id
        or not is_visible_to(
            target,
            tenant_id=tenant_id,
            subject_id=_sub(tenant_id, subject_id),
            system_tenant_id=get_settings().system_tenant_id,
        )
    ):
        raise integrity_error(
            [
                single_finding(
                    "AM-REF-001",
                    get_registry().ruleset_id,
                    f"view_ref {ref} does not resolve to a visible view of this model",
                    path="/view_ref",
                    evidence={"view_ref": str(ref)},
                )
            ]
        )


def _connection_fields(payload: ViewConnectionIn) -> dict[str, Any]:
    return {
        "kind": payload.kind,
        "relationship_ref": payload.relationship_ref,
        "source_object_id": payload.source_object_id,
        "target_object_id": payload.target_object_id,
        "bendpoints": None
        if payload.bendpoints is None
        else [p.model_dump() for p in payload.bendpoints],
        "style": payload.style,
        "label": payload.label,
    }


class _ViewCheck:
    """One view's occurrences, before and after a proposed write."""

    def __init__(
        self,
        session: AsyncSession,
        view: ArchitectureView,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> None:
        self.session = session
        self.view = view
        self.tenant_id = tenant_id
        self.subject_id = _sub(tenant_id, subject_id)
        self.objects: dict[str, ArchimateViewObjectIn] = {}
        self.connections: dict[str, ArchimateViewConnectionIn] = {}

    async def load(self) -> None:
        objs = await list_objects(self.session, self.view)
        conns = await list_connections(self.session, self.view)
        if len(objs) + len(conns) > _MAX_VIEW_OCCURRENCES:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "input_too_large",
                    "message": f"view has more than {_MAX_VIEW_OCCURRENCES} occurrences",
                },
            )
        self.objects = {str(o.id): _object_in(o.id, o) for o in objs}
        self.connections = {str(c.id): _connection_in(c.id, c) for c in conns}

    def _visible(self, row: Any) -> bool:
        return row.model_id == self.view.model_id and is_visible_to(
            row,
            tenant_id=self.tenant_id,
            subject_id=self.subject_id,
            system_tenant_id=get_settings().system_tenant_id,
        )

    async def _resolve(
        self, objects: list[ArchimateViewObjectIn], connections: list[ArchimateViewConnectionIn]
    ) -> tuple[list[ArchimateConceptIn], list[Any]]:
        """Resolvable concepts and relationships referenced by the occurrences (+ rel ends)."""
        rel_ids = {UUID(c.relationship_id) for c in connections if c.relationship_id}
        rels = (
            [
                r
                for r in (
                    await self.session.execute(
                        select(ArchitectureRelationship).where(
                            ArchitectureRelationship.id.in_(rel_ids)
                        )
                    )
                )
                .scalars()
                .all()
                if self._visible(r)
            ]
            if rel_ids
            else []
        )
        element_ids = {UUID(o.concept_id) for o in objects if o.concept_id}
        for r in rels:
            for etype, ref in (
                (r.source_entity_type, r.source_id),
                (r.target_entity_type, r.target_id),
            ):
                if etype == "architecture_elements":
                    element_ids.add(ref)
        elements = (
            [
                e
                for e in (
                    await self.session.execute(
                        select(ArchitectureElement).where(ArchitectureElement.id.in_(element_ids))
                    )
                )
                .scalars()
                .all()
                if self._visible(e)
            ]
            if element_ids
            else []
        )
        concepts = [
            ArchimateConceptIn(id=str(e.id), type=e.element_type, name=e.title or "", layer=e.layer)
            for e in elements
        ]
        return concepts, [relationship_of(r) for r in rels]

    async def refuse_new_errors(
        self,
        *,
        objects: dict[str, ArchimateViewObjectIn],
        connections: dict[str, ArchimateViewConnectionIn],
    ) -> None:
        all_objects = list({**self.objects, **objects}.values())
        all_connections = list({**self.connections, **connections}.values())
        # Same concept/relationship set on both sides: only the occurrences differ, so a
        # finding about a relationship's own ends can never read as introduced.
        concepts, rels = await self._resolve(all_objects, all_connections)

        def snap(objs: dict, conns: dict) -> ArchimateSnapshot:
            return ArchimateSnapshot(
                concepts=concepts,
                relationships=rels,
                views=[
                    ArchimateViewIn(
                        id=str(self.view.id),
                        viewpoint=None,
                        objects=list(objs.values()),
                        connections=list(conns.values()),
                    )
                ],
            )

        # The validator is CPU work over the whole view: in the threadpool, off the loop.
        errors = await run_in_threadpool(
            lambda: introduced_errors(
                snap(self.objects, self.connections),
                snap({**self.objects, **objects}, {**self.connections, **connections}),
                rules=OCCURRENCE_RULES,
            )
        )
        if errors:
            raise integrity_error(errors)


async def _place(
    session: AsyncSession, view: ArchitectureView, row: ArchitectureViewObject, position: int
) -> None:
    """Move ``row`` to index ``position`` of the view's object list (clamped) and number
    the list densely; rows whose position does not change are not written."""
    order = [o for o in await list_objects(session, view) if o.id != row.id]
    order.insert(min(position, len(order)), row)
    for index, obj in enumerate(order):
        if obj.position != index:
            obj.position = index


async def create_object(
    session: AsyncSession,
    view: ArchitectureView,
    payload: ViewObjectIn,
    *,
    tenant_id: str,
    subject_id: str,
) -> ArchitectureViewObject:
    await _check_view_ref(session, view, payload.view_ref, tenant_id, subject_id)
    check = _ViewCheck(session, view, tenant_id=tenant_id, subject_id=subject_id)
    await check.load()
    oid = uuid7()
    proposed = _object_in(oid, payload)
    await check.refuse_new_errors(objects={proposed.id: proposed}, connections={})
    existing = await list_objects(session, view)
    row = ArchitectureViewObject(
        id=oid,
        tenant_id=tenant_id,
        model_id=view.model_id,
        view_id=view.id,
        # On top (appended) unless a position is given.
        position=max((o.position for o in existing), default=-1) + 1,
        **_object_fields(payload),
    )
    session.add(row)
    if payload.position is not None:
        await _place(session, view, row, payload.position)
    await uow.commit(session)
    await session.refresh(row)
    return row


async def _get_object(
    session: AsyncSession, view: ArchitectureView, object_id: UUID
) -> ArchitectureViewObject:
    row = (
        await session.execute(
            select(ArchitectureViewObject).where(
                ArchitectureViewObject.id == object_id,
                ArchitectureViewObject.view_id == view.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="view object not found")
    return row


async def update_object(
    session: AsyncSession,
    view: ArchitectureView,
    object_id: UUID,
    payload: ViewObjectIn,
    *,
    tenant_id: str,
    subject_id: str,
) -> ArchitectureViewObject:
    row = await _get_object(session, view, object_id)
    await _check_view_ref(session, view, payload.view_ref, tenant_id, subject_id)
    check = _ViewCheck(session, view, tenant_id=tenant_id, subject_id=subject_id)
    await check.load()
    proposed = _object_in(row.id, payload)
    await check.refuse_new_errors(objects={proposed.id: proposed}, connections={})
    for field, value in _object_fields(payload).items():
        setattr(row, field, value)
    if payload.position is not None:
        await _place(session, view, row, payload.position)
    await uow.commit(session)
    await session.refresh(row)
    return row


async def delete_object(session: AsyncSession, view: ArchitectureView, object_id: UUID) -> None:
    """Delete an object; its nested objects (FK cascade) and attached connections go too."""
    row = await _get_object(session, view, object_id)
    await session.delete(row)
    await uow.commit(session)


async def create_connection(
    session: AsyncSession,
    view: ArchitectureView,
    payload: ViewConnectionIn,
    *,
    tenant_id: str,
    subject_id: str,
) -> ArchitectureViewConnection:
    check = _ViewCheck(session, view, tenant_id=tenant_id, subject_id=subject_id)
    await check.load()
    cid = uuid7()
    proposed = _connection_in(cid, payload)
    await check.refuse_new_errors(objects={}, connections={proposed.id: proposed})
    row = ArchitectureViewConnection(
        id=cid,
        tenant_id=tenant_id,
        model_id=view.model_id,
        view_id=view.id,
        **_connection_fields(payload),
    )
    session.add(row)
    await uow.commit(session)
    await session.refresh(row)
    return row


async def _get_connection(
    session: AsyncSession, view: ArchitectureView, connection_id: UUID
) -> ArchitectureViewConnection:
    row = (
        await session.execute(
            select(ArchitectureViewConnection).where(
                ArchitectureViewConnection.id == connection_id,
                ArchitectureViewConnection.view_id == view.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="view connection not found")
    return row


async def update_connection(
    session: AsyncSession,
    view: ArchitectureView,
    connection_id: UUID,
    payload: ViewConnectionIn,
    *,
    tenant_id: str,
    subject_id: str,
) -> ArchitectureViewConnection:
    row = await _get_connection(session, view, connection_id)
    check = _ViewCheck(session, view, tenant_id=tenant_id, subject_id=subject_id)
    await check.load()
    proposed = _connection_in(row.id, payload)
    await check.refuse_new_errors(objects={}, connections={proposed.id: proposed})
    for field, value in _connection_fields(payload).items():
        setattr(row, field, value)
    await uow.commit(session)
    await session.refresh(row)
    return row


async def delete_connection(
    session: AsyncSession, view: ArchitectureView, connection_id: UUID
) -> None:
    """Delete a connection; connections that end on it go too (flush hook)."""
    row = await _get_connection(session, view, connection_id)
    await session.delete(row)
    await uow.commit(session)


# ---------------------------------------------------------------------------
# Legacy membership lists (``ArchitectureView.element_ids`` / ``relationship_ids``)
# ---------------------------------------------------------------------------


def _ids(values: Any) -> set[str]:
    return {str(v) for v in values or ()}


async def _bad_members(
    session: AsyncSession,
    model_id: Any,
    element_ids: set[str],
    relationship_ids: set[str],
    *,
    tenant_id: str,
    subject_id: str,
) -> tuple[set[str], set[str], set[str]]:
    """(unresolved element ids, unresolved relationship ids, relationships whose ends
    are not members). Resolves like an occurrence: a visible row of the view's model."""
    elements, rels = await resolve_visible(
        session,
        tenant_id=tenant_id,
        subject_id=_sub(tenant_id, subject_id),
        element_ids=element_ids,
        relationship_ids=relationship_ids,
    )
    same = {str(model_id) if model_id is not None else None}
    resolved_el = {i for i, e in elements.items() if _str(e.model_id) in same}
    resolved_rel = {i: r for i, r in rels.items() if _str(r.model_id) in same}
    # As for a relationship connection (AM-VIEW-002): a relationship is shown only
    # between members showing its ends.
    detached = {
        i
        for i, r in resolved_rel.items()
        if any(
            str(ref) not in (element_ids if etype == "architecture_elements" else relationship_ids)
            for etype, ref in (
                (r.source_entity_type, r.source_id),
                (r.target_entity_type, r.target_id),
            )
        )
    }
    return element_ids - resolved_el, relationship_ids - resolved_rel.keys(), detached


async def check_view_members(
    session: AsyncSession,
    *,
    model_id: Any,
    element_ids: Any,
    relationship_ids: Any,
    tenant_id: str,
    subject_id: str,
    stored: tuple[Any, Any] | None = None,
) -> None:
    """Refuse ``422 model_integrity`` for member ids this write introduces that do not
    resolve (AM-REF-001) or relationships whose ends are not members (AM-VIEW-002).

    ``stored`` (an update) are the lists as they are: what was already wrong there is
    not this write's doing, so — like every ArchiMate write — only new errors refuse.
    """
    kw = {"tenant_id": tenant_id, "subject_id": subject_id}
    bad = await _bad_members(session, model_id, _ids(element_ids), _ids(relationship_ids), **kw)
    if stored is not None and any(bad):
        old = await _bad_members(session, model_id, _ids(stored[0]), _ids(stored[1]), **kw)
        bad = (bad[0] - old[0], bad[1] - old[1], bad[2] - old[2])
    unresolved_el, unresolved_rel, detached = (sorted(b) for b in bad)
    ruleset = get_registry().ruleset_id
    findings = []
    for field, kind, ids in (
        ("element_ids", "element", unresolved_el),
        ("relationship_ids", "relationship", unresolved_rel),
    ):
        if ids:
            findings.append(
                single_finding(
                    "AM-REF-001",
                    ruleset,
                    f"{field} {', '.join(ids)} do not resolve to visible rows of the view's model",
                    targets=[(kind, i) for i in ids],
                    path=f"/{field}",
                    evidence={field: ids},
                )
            )
    if detached:
        findings.append(
            single_finding(
                "AM-VIEW-002",
                ruleset,
                f"relationship_ids {', '.join(detached)} have an end that is not in the view",
                targets=[("relationship", i) for i in detached],
                path="/relationship_ids",
                evidence={"relationship_ids": detached},
            )
        )
    if findings:
        raise integrity_error(findings)


class ArchitectureViewService(EntityService):
    """Views: the legacy membership lists are checked on every create and update."""

    async def create(self, session, payload, tenant_id, subject_id="", **kw):  # type: ignore[override]
        await check_view_members(
            session,
            model_id=(kw.get("extra_columns") or {}).get("model_id"),
            element_ids=payload.element_ids,
            relationship_ids=payload.relationship_ids,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        return await super().create(session, payload, tenant_id, subject_id, **kw)

    async def update(self, session, entity_id, payload, tenant_id, subject_id="", **kw):  # type: ignore[override]
        row = await self._get_mutable(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        await check_view_members(
            session,
            model_id=row.model_id,
            element_ids=payload.element_ids,
            relationship_ids=payload.relationship_ids,
            tenant_id=tenant_id,
            subject_id=subject_id,
            stored=(row.element_ids, row.relationship_ids),
        )
        return await super().update(session, entity_id, payload, tenant_id, subject_id, **kw)
