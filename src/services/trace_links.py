"""Trace links (target spec §7.4): immutable, typed, both ends resolved at write time.

* Both ends MUST resolve to a row the caller can see; otherwise ``422 model_integrity``
  with rule ``TRC-REF-001``. Another subject's private row reads exactly like a missing
  one. UML kinds resolve to the ``uml_*`` concept tables (a UML row carries its model's
  visibility).
* A link is private (``visibility='user'``) when either end is private, so a tenant-wide
  link can never expose a private row's id; otherwise it takes the caller's visibility.
* ``model_id`` is the source concept's model (ArchiMate element / relationship / view, or
  UML element / relationship / interaction / diagram, of the caller's own tenant): the
  link is part of that model's manifest and creating or deleting it increments that
  model's ``head`` (flush hook in ``models/modeling.py``).
  A link MORE private than that model (a private link whose source is in a shared
  model) belongs to no model (``model_id`` NULL): a shared model's manifest — a sealed
  revision every tenant member reads — must never carry it. :func:`resync_model_links`
  keeps that true when a model's visibility changes.
* A link whose end is deleted is deleted with it (flush hooks / Core delete paths).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureElement, ArchitectureRelationship, ArchitectureView
from models.decisions import Decision
from models.modeling import ModelingTraceLink, bump_heads, note_etag_model
from models.principles import Principle
from models.requirements import Requirement
from models.uml import UmlDiagram, UmlElement, UmlInteraction, UmlRelationship
from repositories.visibility import can_mutate, is_visible_to, same_tenant, scope
from schemas.modeling_lifecycle import TraceLinkIn, TraceLinkOut, TraceRef
from services.model_integrity import integrity_error, single_finding
from settings import get_settings

TRACE_RULESET = "modeling/trace-links"
_KIND_CLASSES: dict[str, Any] = {
    "requirement": Requirement,
    "principle": Principle,
    "decision": Decision,
    "element": ArchitectureElement,
    "relationship": ArchitectureRelationship,
    "view": ArchitectureView,
    "uml_element": UmlElement,
    "uml_relationship": UmlRelationship,
    "interaction": UmlInteraction,
    "diagram": UmlDiagram,
}
_MODEL_KINDS = (
    "element",
    "relationship",
    "view",
    "uml_element",
    "uml_relationship",
    "interaction",
    "diagram",
)
_MODEL_CLASSES = (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    UmlElement,
    UmlRelationship,
    UmlInteraction,
    UmlDiagram,
)


def _sub(tenant_id: str, subject_id: str) -> str:
    return subject_id or tenant_id


async def _resolve(
    session: AsyncSession, ref: TraceRef, end: str, *, tenant_id: str, subject_id: str
) -> Any:
    row = await session.get(_KIND_CLASSES[ref.kind], ref.id)
    if row is None or not is_visible_to(
        row,
        tenant_id=tenant_id,
        subject_id=_sub(tenant_id, subject_id),
        system_tenant_id=get_settings().system_tenant_id,
    ):
        raise integrity_error(
            [
                single_finding(
                    "TRC-REF-001",
                    TRACE_RULESET,
                    f"{end} {ref.kind} {ref.id} does not resolve to a visible {ref.kind}",
                    path=f"/{end}",
                    evidence={"kind": ref.kind, "id": str(ref.id)},
                )
            ]
        )
    return row


def link_model_id(source: Any, source_kind: str, visibility: str, tenant_id: str) -> UUID | None:
    """The model a link belongs to: its source concept's, unless the link is narrower.

    A row carries its model's visibility, so the source row's ``visibility`` is the
    model's. A private link on a shared model's concept belongs to no model.
    """
    if source is None or source_kind not in _MODEL_KINDS:
        return None
    if not same_tenant(source.tenant_id, tenant_id):
        return None
    if visibility == "user" and getattr(source, "visibility", "tenant") != "user":
        return None
    return source.model_id


def model_concept_ids(model_id: UUID) -> Any:
    """Ids of every concept of ``model_id`` — ArchiMate or UML (a subquery)."""
    return union_all(*(select(cls.id).where(cls.model_id == model_id) for cls in _MODEL_CLASSES))


def links_touching(tenant_id: str, ids: Any) -> Any:
    """Trace links of ``tenant_id`` whose source or target is one of ``ids``."""
    return select(ModelingTraceLink).where(
        ModelingTraceLink.tenant_id == tenant_id,
        or_(ModelingTraceLink.source_id.in_(ids), ModelingTraceLink.target_id.in_(ids)),
    )


async def _fresh(session: AsyncSession, kind: str | None, ref: UUID | None) -> Any:
    cls = _KIND_CLASSES.get(kind or "")
    if cls is None or ref is None:
        return None
    # populate_existing: the caller may just have changed visibility with a Core UPDATE.
    return (
        await session.execute(
            select(cls).where(cls.id == ref).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def resync_model_links(session: AsyncSession, model_id: UUID, tenant_id: str) -> None:
    """Re-derive visibility and model of every link touching ``model_id``'s concepts.

    Called after the model's rows changed visibility: a link is private iff an end is
    private (it moves with its rows), and belongs to its source's model unless it is
    narrower than that model (:func:`link_model_id`). Every model that gains or loses a
    link has its head bumped (Core-level change, so bumped here explicitly).
    """
    links = (
        (
            await session.execute(
                links_touching(tenant_id, model_concept_ids(model_id)).execution_options(
                    populate_existing=True
                )
            )
        )
        .scalars()
        .all()
    )
    touched: set[UUID] = set()
    for link in links:
        source = await _fresh(session, link.source_kind, link.source_id)
        target = await _fresh(session, link.target_kind, link.target_id)
        private = any(getattr(r, "visibility", "tenant") == "user" for r in (source, target))
        link.visibility = "user" if private else "tenant"
        new_model = link_model_id(source, link.source_kind, link.visibility, tenant_id)
        if new_model != link.model_id:
            touched |= {m for m in (link.model_id, new_model) if m is not None}
            link.model_id = new_model
    await session.flush()
    if touched:
        await session.run_sync(lambda s: bump_heads(s, touched))


def link_out(link: ModelingTraceLink) -> TraceLinkOut:
    return TraceLinkOut(
        id=link.id,
        model_id=link.model_id,
        source={"kind": link.source_kind, "id": str(link.source_id)},
        target=None
        if link.target_id is None
        else {"kind": link.target_kind, "id": str(link.target_id)},
        target_code=link.target_code,
        link_type=link.link_type,
        note=link.note,
        visibility=link.visibility,
        created_by=link.created_by,
        created_at=link.created_at,
    )


async def create_link(
    session: AsyncSession,
    payload: TraceLinkIn,
    *,
    tenant_id: str,
    subject_id: str,
    visibility: str,
) -> ModelingTraceLink:
    source = await _resolve(
        session, payload.source, "source", tenant_id=tenant_id, subject_id=subject_id
    )
    rows = [source]
    if payload.target is not None:
        rows.append(
            await _resolve(
                session, payload.target, "target", tenant_id=tenant_id, subject_id=subject_id
            )
        )
    private = any(getattr(r, "visibility", "tenant") == "user" for r in rows)
    link_visibility = "user" if private else visibility
    model_id = link_model_id(source, payload.source.kind, link_visibility, tenant_id)
    note_etag_model(session, model_id)
    link = ModelingTraceLink(
        tenant_id=tenant_id,
        subject_id=_sub(tenant_id, subject_id),
        visibility=link_visibility,
        model_id=model_id,
        source_kind=payload.source.kind,
        source_id=payload.source.id,
        target_kind=None if payload.target is None else payload.target.kind,
        target_id=None if payload.target is None else payload.target.id,
        target_code=None if payload.target_code is None else payload.target_code.model_dump(),
        link_type=payload.link_type,
        note=payload.note,
        created_by=_sub(tenant_id, subject_id),
    )
    session.add(link)
    await session.commit()
    await session.refresh(link)
    return link


def _scoped(tenant_id: str, subject_id: str) -> Any:
    return scope(
        select(ModelingTraceLink),
        ModelingTraceLink,
        tenant_id=tenant_id,
        subject_id=_sub(tenant_id, subject_id),
    )


async def get_link(
    session: AsyncSession, link_id: UUID, tenant_id: str, subject_id: str
) -> ModelingTraceLink:
    row = (
        await session.execute(_scoped(tenant_id, subject_id).where(ModelingTraceLink.id == link_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="trace link not found")
    return row


async def list_links(
    session: AsyncSession,
    tenant_id: str,
    subject_id: str,
    *,
    source_id: UUID | None,
    target_id: UUID | None,
    link_type: str | None,
    limit: int,
    offset: int,
) -> tuple[list[ModelingTraceLink], int]:
    stmt = _scoped(tenant_id, subject_id)
    if source_id:
        stmt = stmt.where(ModelingTraceLink.source_id == source_id)
    if target_id:
        stmt = stmt.where(ModelingTraceLink.target_id == target_id)
    if link_type:
        stmt = stmt.where(ModelingTraceLink.link_type == link_type)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(ModelingTraceLink.created_at.desc(), ModelingTraceLink.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def delete_link(
    session: AsyncSession, link_id: UUID, tenant_id: str, subject_id: str
) -> dict[str, Any]:
    link = await get_link(session, link_id, tenant_id, subject_id)
    if not can_mutate(link, subject_id=_sub(tenant_id, subject_id), tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="trace link not found")
    note_etag_model(session, link.model_id)
    await session.delete(link)
    await session.commit()
    return {"status": "deleted", "id": link_id}
