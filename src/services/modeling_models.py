"""Modeling models: CRUD, draft head concurrency, sealed revisions (target spec §7.1–7.2, §7.5).

Draft head contract (§7.2):

* every write to a model's concepts, relationships, views or occurrences increments
  ``head`` in the same transaction — done for EVERY write path by the flush hook in
  ``models/modeling.py`` (``UPDATE … head = head + 1`` holds the model row lock until
  commit);
* a write MAY send ``If-Match: <head>``: :func:`check_if_match` takes the model row lock
  (``SELECT … FOR UPDATE``) BEFORE the write and refuses a mismatch with
  ``412 stale_head`` carrying the current head, so check and write are atomic;
* the response carries the new head as ``ETag`` (:func:`set_etag`).

Sealing (:func:`seal_revision`) reads the whole model under the same row lock, so a seal
never interleaves with a write: a write in flight holds the lock until it commits, and a
write that starts after the seal took it waits for the seal's commit.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import Header, HTTPException, Response
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from models.architecture import (
    ArchitectureElement,
    ArchitectureElementRealisationBinding,
    ArchitectureRelationship,
    ArchitectureView,
)
from models.modeling import (
    ArchitectureViewConnection,
    ArchitectureViewObject,
    ModelingModel,
    ModelingModelRevision,
    ModelingTraceLink,
    bump_heads,
    default_model_id,
    etag_head,
    note_etag_model,
    prune_trace_links_async,
)
from models.uml import UmlDiagram, UmlElement, UmlInteraction, UmlRelationship
from repositories.visibility import can_mutate, scope
from schemas.common import ErrorResponse
from schemas.modeling import ArchimateSnapshot, ArchimateViewObjectIn
from schemas.modeling_lifecycle import ModelIn, ModelPatch
from services import model_organizations as organizations
from services import trace_links, uml_manifest
from services.archimate_integrity import relationship_of
from services.idempotency import BeforeCommit
from services.model_integrity import integrity_error, single_finding
from services.relationship_cascade import delete_dependent_relationships
from settings import get_settings
from tools.architecture.registry import get_registry

STALE_HEAD = "stale_head"
# Stable ``evidence.reason`` values of the AM-REF-001 refusal of an explicit ``model_id``.
MODEL_NOT_WRITABLE = "model_not_writable"
MODEL_LANGUAGE_MISMATCH = "model_language_mismatch"
MODEL_VISIBILITY_MISMATCH = "model_visibility_mismatch"
MODEL_HAS_REVISIONS = "model_has_revisions"
# DELETE: rows of ANOTHER tenant carrying the model's id make it undeletable without
# either writing across tenants or failing on the FK (409).
MODEL_HAS_FOREIGN_ROWS = "model_has_foreign_rows"
# PATCH visibility: only the owner may change it (403); making a shared model private is
# refused while it holds another subject's rows or trace links (409).
MODEL_VISIBILITY_OWNER_ONLY = "model_visibility_owner_only"
VISIBILITY_CHANGE_FOREIGN_ROWS = "visibility_change_foreign_rows"
INPUT_TOO_LARGE = "input_too_large"
SEAL_TIMEOUT = "seal_timeout"
QUERY_CANCELED = "57014"  # statement_timeout (SQLSTATE query_canceled)
MANIFEST_FORMAT = "rm-modeling-manifest/1"

STALE_HEAD_412 = {
    "description": (
        f"`{STALE_HEAD}`: If-Match does not equal the model's head (`error.head` is the "
        "current head)"
    ),
    "model": ErrorResponse,
}


def _sub(tenant_id: str, subject_id: str) -> str:
    return subject_id or tenant_id


# ---------------------------------------------------------------------------
# If-Match / ETag
# ---------------------------------------------------------------------------


def parse_if_match(raw: str | None) -> int | None:
    """``If-Match`` as a head number. Accepts ``5``, ``"5"`` and ``W/"5"``; ``*`` = none."""
    if raw is None:
        return None
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip().strip('"')
    if value == "*":
        return None
    if not value.isdigit():
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_if_match", "message": "If-Match must be a model head (int)"},
        )
    return int(value)


async def if_match_header(
    if_match: Annotated[
        str | None,
        Header(
            alias="If-Match",
            description=(
                "Optional expected draft head of the model this write touches (the ETag of "
                "an earlier response). A mismatch is 412 stale_head with the current head."
            ),
        ),
    ] = None,
) -> int | None:
    return parse_if_match(if_match)


def stale_head(head: int) -> HTTPException:
    return HTTPException(
        status_code=412,
        detail={
            "code": STALE_HEAD,
            "message": f"the model's draft head is {head}; re-read and retry",
            "head": head,
        },
        headers={"ETag": f'"{head}"'},
    )


async def lock_model(session: AsyncSession, model_id: UUID) -> ModelingModel | None:
    """The model row under ``SELECT … FOR UPDATE`` (no visibility check — callers do that)."""
    return (
        await session.execute(
            select(ModelingModel)
            .where(ModelingModel.id == model_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def check_if_match(
    session: AsyncSession, model_id: UUID | None, if_match: int | None
) -> None:
    """Lock the model and refuse a stale ``If-Match``; a no-op without one.

    ``model_id`` is also the model whose head the response's ``ETag`` reports.
    """
    note_etag_model(session, model_id)
    if if_match is None or model_id is None:
        return
    model = await lock_model(session, model_id)
    if model is not None and model.head != if_match:
        raise stale_head(model.head)


IF_MATCH_NOT_APPLICABLE = "if_match_not_applicable"


def if_match_not_applicable() -> HTTPException:
    """400: the write touches no model (a PM / TOC / governance edge), so no head to match."""
    return HTTPException(
        status_code=400,
        detail={
            "code": IF_MATCH_NOT_APPLICABLE,
            "message": "this write belongs to no modeling model; If-Match does not apply",
        },
    )


def _mutable_rows(cls: Any, tenant_id: str, subject_id: str) -> Any:
    """``cls`` rows the caller may write: tenant-visible, or the caller's own private ones.

    The If-Match helpers resolve a row through this BEFORE comparing heads, so another
    subject's private row reads as missing (the write then 404s) instead of answering
    ``412`` with its private model's head.
    """
    return (cls.tenant_id == tenant_id) & (
        (cls.visibility == "tenant") | (cls.subject_id == _sub(tenant_id, subject_id))
    )


async def check_edge_if_match(
    session: AsyncSession, rid: UUID, tenant_id: str, if_match: int | None, *, subject_id: str
) -> None:
    """If-Match on an existing relationship: its model's head, or 400 for a model-less edge.

    A missing (or not writable) row is left to the write itself (404).
    """
    row = (
        await session.execute(
            select(ArchitectureRelationship.id, ArchitectureRelationship.model_id).where(
                ArchitectureRelationship.id == rid,
                _mutable_rows(ArchitectureRelationship, tenant_id, subject_id),
            )
        )
    ).one_or_none()
    if row is None:
        return
    note_etag_model(session, row.model_id)
    if if_match is None:
        return
    if row.model_id is None:
        raise if_match_not_applicable()
    await check_if_match(session, row.model_id, if_match)


def set_etag(response: Response, session: AsyncSession) -> None:
    """``ETag: "<head>"`` — the head this request's write produced on its target model."""
    head = etag_head(session)
    if head is not None:
        response.headers["ETag"] = f'"{head}"'


async def row_model_id(
    session: AsyncSession, cls: Any, row_id: UUID, tenant_id: str, *, subject_id: str
) -> UUID | None:
    """``model_id`` of a row the caller may write (None if absent; the write then 404s)."""
    return (
        await session.execute(
            select(cls.model_id).where(cls.id == row_id, _mutable_rows(cls, tenant_id, subject_id))
        )
    ).scalar_one_or_none()


ARCHIMATE_END_TYPES = frozenset({"architecture_elements", "architecture_relationships"})


async def relationship_model_id(
    session: AsyncSession,
    source_entity_type: str,
    source_id: UUID,
    tenant_id: str,
    *,
    subject_id: str,
    target_entity_type: str,
    if_match: int | None,
) -> UUID | None:
    """The model a new relationship joins: its source end's (ArchiMate edges only).

    If-Match on an edge that joins no model (an end is not an ArchiMate concept) is 400
    ``if_match_not_applicable``.
    """
    if not {source_entity_type, target_entity_type} <= ARCHIMATE_END_TYPES:
        if if_match is not None:
            raise if_match_not_applicable()
        return None
    cls = {
        "architecture_elements": ArchitectureElement,
        "architecture_relationships": ArchitectureRelationship,
    }.get(source_entity_type)
    if cls is None:
        return None
    return await row_model_id(session, cls, source_id, tenant_id, subject_id=subject_id)


def visibility_mismatch(model: ModelingModel, *, visibility: str, sub: str) -> bool:
    """True if a ``visibility`` row of subject ``sub`` may not join ``model``.

    Rows carry their model's visibility (and, if private, its owner), so a sealed manifest
    never carries a row some reader of the model could not see.
    """
    return model.visibility != visibility or (visibility == "user" and model.subject_id != sub)


def join_model_refusal(
    model: ModelingModel | None,
    model_id: UUID,
    *,
    tenant_id: str,
    sub: str,
    visibility: str,
) -> tuple[str, str] | None:
    """``(reason, message)`` if a new ``visibility`` row of ``sub`` may not join ``model``.

    The one rule for every ArchiMate row that joins a model: an ArchiMate model of the
    caller's OWN tenant that the caller may write (``can_mutate`` — never another tenant's,
    the system tenant's included), of the row's visibility (and, if private, owner).
    ``None`` = may join. Used by :func:`resolve_create_model` (elements, views) and
    :func:`check_relationship_model` (relationships, whose model is derived).
    """
    if model is None or not can_mutate(model, subject_id=sub, tenant_id=tenant_id):
        return (
            MODEL_NOT_WRITABLE,
            f"model {model_id} does not resolve to a model the caller may write",
        )
    if model.language != "archimate":
        return (
            MODEL_LANGUAGE_MISMATCH,
            f"model {model_id} is a {model.language} model, not an ArchiMate model",
        )
    if visibility_mismatch(model, visibility=visibility, sub=sub):
        return (
            MODEL_VISIBILITY_MISMATCH,
            f"model {model_id} has visibility '{model.visibility}'; a '{visibility}' row "
            "cannot join it",
        )
    return None


def _join_refused(model_id: UUID, refusal: tuple[str, str], *, path: str) -> HTTPException:
    reason, problem = refusal
    return integrity_error(
        [
            single_finding(
                "AM-REF-001",
                get_registry().ruleset_id,
                problem,
                path=path,
                evidence={"model_id": str(model_id), "reason": reason},
            )
        ]
    )


async def check_relationship_model(
    session: AsyncSession,
    *,
    source_entity_type: str,
    source_id: Any,
    target_entity_type: str,
    tenant_id: str,
    visibility: str,
    subject_id: str,
    current_model_id: UUID | None = None,
) -> None:
    """422 ``model_integrity`` (AM-REF-001) unless an ArchiMate edge may join its model.

    The flush hook (``models/modeling.py::_derive_relationship_model``) puts an ArchiMate
    edge in its SOURCE end's model, on create and whenever an end moves. That derived model
    must pass :func:`join_model_refusal` exactly as an explicit ``model_id`` on an element
    or view must (``resolve_create_model``): the caller's own tenant, writable, ArchiMate,
    same visibility. Without the tenant/writability half a platform tenant could relate two
    PUBLISHED system elements and store its own edge inside the system tenant's model.
    ``tenant_id`` / ``subject_id`` are the edge's tenant and resolved owner.

    An update (``current_model_id`` = the edge's stored model) is checked only when it
    moves the edge to a different model: the visibility of an existing edge
    never changes, so a write that keeps the model breaks nothing — an edge that already
    mismatched its model before this gate may still be re-pointed within it. That
    exemption applies only to a model of the edge's OWN tenant: an edge stored in another
    tenant's model (pre-gate data, e.g. a platform edge sitting in the system tenant's
    model) gets no exemption, because a re-point would rewrite that tenant's model content
    through a row it does not own.

    A source with no model (the hook then assigns the caller's scope default), or a
    non-ArchiMate edge, joins no foreign model: nothing to check. Callers have already
    validated the ends (``check_relationship_write``), so the unscoped lookups below read
    only rows the caller may see.
    """
    if not {source_entity_type, target_entity_type} <= ARCHIMATE_END_TYPES:
        return
    cls = (
        ArchitectureElement
        if source_entity_type == "architecture_elements"
        else ArchitectureRelationship
    )
    model_id = (
        await session.execute(select(cls.model_id).where(cls.id == UUID(str(source_id))))
    ).scalar_one_or_none()
    if model_id is None:
        return
    model = (
        await session.execute(select(ModelingModel).where(ModelingModel.id == model_id))
    ).scalar_one_or_none()
    if (
        current_model_id is not None
        and str(model_id) == str(current_model_id)
        and model is not None
        and str(model.tenant_id) == str(tenant_id)
    ):
        return
    refusal = join_model_refusal(
        model, model_id, tenant_id=tenant_id, sub=subject_id, visibility=visibility
    )
    if refusal is not None:
        raise _join_refused(model_id, refusal, path="/source_id")


async def model_visible(
    session: AsyncSession, model_id: UUID, tenant_id: str, subject_id: str
) -> bool:
    """True if the caller may read ``model_id`` (own tenant; private only to its owner)."""
    return await _get_visible(session, model_id, tenant_id, subject_id) is not None


async def resolve_create_model(
    session: AsyncSession,
    *,
    model_id: UUID | None,
    tenant_id: str,
    subject_id: str,
    visibility: str,
    project_id: str | None,
    if_match: int | None,
) -> UUID | None:
    """The model a new element/view joins, validated; If-Match checked under its lock.

    Explicit ``model_id``: it must be an ArchiMate model the caller may write, of the same
    visibility (and, if private, the same owner) as the row — so a sealed manifest never
    carries a row some reader of the model could not see. Returns it. Omitted: ``None``
    (the flush hook assigns the scope default) unless If-Match needs the default resolved
    and locked now.
    """
    sub = _sub(tenant_id, subject_id)
    if model_id is not None:
        model = await _get_visible(session, model_id, tenant_id, subject_id, lock=True)
        refusal = join_model_refusal(
            model, model_id, tenant_id=tenant_id, sub=sub, visibility=visibility
        )
        if refusal is not None:
            raise _join_refused(model_id, refusal, path="/model_id")
        assert model is not None
        note_etag_model(session, model_id)
        if if_match is not None and model.head != if_match:
            raise stale_head(model.head)
        return model_id
    if if_match is not None:
        default = await session.run_sync(
            lambda s: default_model_id(
                s,
                tenant_id=tenant_id,
                project_id=project_id,
                visibility=visibility,
                subject_id=sub,
            )
        )
        await check_if_match(session, default, if_match)
    return None


# ---------------------------------------------------------------------------
# Model CRUD
# ---------------------------------------------------------------------------


async def _get_visible(
    session: AsyncSession,
    model_id: UUID,
    tenant_id: str,
    subject_id: str,
    *,
    lock: bool | str = False,
) -> ModelingModel | None:
    """``lock``: True = ``FOR UPDATE`` (writes, seals); ``"share"`` = ``FOR SHARE`` (reads
    that must see one state of the model, e.g. a draft-head validation run)."""
    stmt = scope(
        select(ModelingModel).where(ModelingModel.id == model_id),
        ModelingModel,
        tenant_id=tenant_id,
        subject_id=_sub(tenant_id, subject_id),
    )
    if lock:
        # populate_existing: a row already in the identity map must be re-read under the
        # lock, not served from what an earlier unlocked SELECT loaded.
        stmt = stmt.with_for_update(read=lock == "share").execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_model(
    session: AsyncSession,
    model_id: UUID,
    tenant_id: str,
    subject_id: str,
    *,
    lock: bool | str = False,
) -> ModelingModel:
    model = await _get_visible(session, model_id, tenant_id, subject_id, lock=lock)
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
    return model


async def get_mutable_model(
    session: AsyncSession, model_id: UUID, tenant_id: str, subject_id: str, *, lock: bool = False
) -> ModelingModel:
    """404 (not 403) for another subject's private model, as everywhere in this service."""
    model = await get_model(session, model_id, tenant_id, subject_id, lock=lock)
    if not can_mutate(model, subject_id=_sub(tenant_id, subject_id), tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="model not found")
    return model


async def latest_revision_id(session: AsyncSession, model_id: UUID) -> UUID | None:
    return (
        await session.execute(
            select(ModelingModelRevision.id)
            .where(ModelingModelRevision.model_id == model_id)
            .order_by(ModelingModelRevision.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_models(
    session: AsyncSession,
    tenant_id: str,
    subject_id: str,
    *,
    language: str | None,
    project_id: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> tuple[list[ModelingModel], int]:
    stmt = scope(
        select(ModelingModel),
        ModelingModel,
        tenant_id=tenant_id,
        subject_id=_sub(tenant_id, subject_id),
    )
    if language:
        stmt = stmt.where(ModelingModel.language == language)
    if project_id:
        stmt = stmt.where(ModelingModel.project_id == project_id)
    if q:
        stmt = stmt.where(ModelingModel.title.icontains(q, autoescape=True))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(ModelingModel.created_at.desc(), ModelingModel.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def create_model(
    session: AsyncSession,
    payload: ModelIn,
    *,
    tenant_id: str,
    subject_id: str,
    visibility: str,
    before_commit: BeforeCommit | None = None,
) -> ModelingModel:
    model = ModelingModel(
        tenant_id=tenant_id,
        project_id=payload.project_id or None,
        visibility=visibility,
        subject_id=_sub(tenant_id, subject_id),
        language=payload.language,
        language_version=payload.language_version,
        title=payload.title,
        description=payload.description,
        head=0,
        is_default=False,
    )
    session.add(model)
    await session.flush()
    await session.refresh(model)
    if before_commit is not None:
        await before_commit(model)
    await session.commit()
    return model


# Concept tables whose rows carry their model's visibility (ArchiMate and UML).
_CONTENT_TABLES = (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    UmlElement,
    UmlRelationship,
    UmlInteraction,
    UmlDiagram,
)


async def _refuse_foreign_rows(session: AsyncSession, model: ModelingModel) -> None:
    """409 unless every row of ``model`` and every link/edge touching it is the owner's.

    Making a shared model private hands its rows to the owner's private scope; a row,
    trace link or cross-domain edge of another subject would then either sit in the
    owner's manifest while private to someone else, or expose a now-private id.
    """
    owner = model.subject_id
    ids = trace_links.model_concept_ids(model.id)
    foreign: list[str] = []
    for cls in _CONTENT_TABLES:
        hit = (
            await session.execute(
                select(cls.id)
                .where(
                    cls.model_id == model.id,
                    or_(cls.tenant_id != model.tenant_id, cls.subject_id != owner),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if hit is not None:
            foreign.append(cls.__tablename__)
    edge = (
        await session.execute(
            select(ArchitectureRelationship.id)
            .where(
                ArchitectureRelationship.tenant_id == model.tenant_id,
                or_(
                    ArchitectureRelationship.model_id.is_(None),
                    ArchitectureRelationship.model_id != model.id,
                ),
                or_(
                    ArchitectureRelationship.source_id.in_(ids),
                    ArchitectureRelationship.target_id.in_(ids),
                ),
                ArchitectureRelationship.subject_id != owner,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if edge is not None:
        foreign.append("architecture_relationships (edges ending on the model)")
    link = (
        await session.execute(
            trace_links.links_touching(model.tenant_id, ids)
            .where(ModelingTraceLink.subject_id != owner)
            .with_only_columns(ModelingTraceLink.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if link is not None:
        foreign.append("modeling_trace_links")
    if foreign:
        raise HTTPException(
            status_code=409,
            detail={
                "code": VISIBILITY_CHANGE_FOREIGN_ROWS,
                "message": (
                    "the model holds rows or links of other subjects or tenants ("
                    + ", ".join(foreign)
                    + "); it cannot become private to its owner"
                ),
            },
        )


# The ArchiMate content tables whose ``model_id`` FK does not cascade (migration 0069):
# a row of ANOTHER tenant carrying the id outlives the tenant-scoped deletes below and
# then blocks the model's own DELETE.
_MODEL_REF_TABLES = (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    ArchitectureViewObject,
    ArchitectureViewConnection,
)


async def _refuse_foreign_model_rows(session: AsyncSession, model: ModelingModel) -> None:
    """409 unless every row carrying ``model``'s id is of the model's own tenant.

    :func:`delete_model` deletes only ``_of_model`` rows, so a foreign row carrying the id
    (an edge stored before the write gate refused foreign models can be one) is left
    alone — and its ``model_id`` FK then blocks the model's DELETE. Refusing says so
    instead of deleting another tenant's row (the cross-tenant write this gate exists to
    close — its owner would never see it happen) or surfacing a ForeignKeyViolation as a
    500. UML rows are deliberately NOT checked: their ``model_id`` FK cascades (migration
    0073), so the database makes every one of them content of the model it names.
    Under the RLS backstop a foreign row is invisible here (and to the scoped deletes), so
    the model's DELETE meets its FK exactly as it did before this scoping; the refusal is
    what a path that does see the row gets.
    """
    foreign: list[str] = []
    for cls in _MODEL_REF_TABLES:
        hit = (
            await session.execute(
                select(cls.id)
                .where(cls.model_id == model.id, cls.tenant_id != model.tenant_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if hit is not None:
            foreign.append(cls.__tablename__)
    if foreign:
        raise HTTPException(
            status_code=409,
            detail={
                "code": MODEL_HAS_FOREIGN_ROWS,
                "message": (
                    "rows of another tenant carry the model's id ("
                    + ", ".join(foreign)
                    + "); their owner must remove them before the model can be deleted"
                ),
            },
        )


async def _narrow_edges_touching(session: AsyncSession, model: ModelingModel) -> None:
    """Cross-domain edges ending on a now-private model's concepts become private too."""
    ids = trace_links.model_concept_ids(model.id)
    await session.execute(
        update(ArchitectureRelationship)
        .where(
            ArchitectureRelationship.tenant_id == model.tenant_id,
            or_(
                ArchitectureRelationship.model_id.is_(None),
                ArchitectureRelationship.model_id != model.id,
            ),
            or_(
                ArchitectureRelationship.source_id.in_(ids),
                ArchitectureRelationship.target_id.in_(ids),
            ),
        )
        .values(visibility="user")
    )


async def patch_model(
    session: AsyncSession,
    model_id: UUID,
    payload: ModelPatch,
    *,
    tenant_id: str,
    subject_id: str,
    if_match: int | None,
) -> ModelingModel:
    """Title / description / visibility. Every change is a header change: head + 1.

    Visibility: only the owner may change it (403 ``model_visibility_owner_only``); a
    default model's is fixed (422); shared → private is refused while another subject
    owns a row or a trace link of the model (409 ``visibility_change_foreign_rows``).
    Rows, and the trace links touching them, move with the model.
    """
    model = await get_mutable_model(session, model_id, tenant_id, subject_id, lock=True)
    if if_match is not None and model.head != if_match:
        raise stale_head(model.head)
    changes = payload.model_dump(exclude_unset=True)
    changed = False
    for field in ("title", "description"):
        if field in changes and (field != "title" or changes[field] is not None):
            if getattr(model, field) != changes[field]:
                setattr(model, field, changes[field])
                changed = True
    new_visibility = changes.get("visibility")
    if new_visibility is not None and new_visibility != model.visibility:
        if model.is_default:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "default_model_visibility_fixed",
                    "message": "a default model's visibility is its scope; it cannot change",
                },
            )
        if model.subject_id != _sub(tenant_id, subject_id):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": MODEL_VISIBILITY_OWNER_ONLY,
                    "message": "only the model's owner may change its visibility",
                },
            )
        if new_visibility == "user":
            await _refuse_foreign_rows(session, model)
            await _narrow_edges_touching(session, model)
        # Rows carry their model's visibility (resolve_create_model), so they move with it —
        # and only the model's OWN tenant's rows: a foreign row carrying the id is not
        # model content and must never be written by this tenant.
        model.visibility = new_visibility
        for cls in _CONTENT_TABLES:
            await session.execute(
                update(cls)
                .where(_of_model(cls, model.id, model.tenant_id))
                .values(visibility=new_visibility)
            )
        await trace_links.resync_model_links(session, model.id, model.tenant_id)
        changed = True
    if changed:
        # Title, description and visibility are manifest header content (build_manifest).
        await session.run_sync(lambda s: bump_heads(s, {model.id}))
    await session.commit()
    await session.refresh(model)
    return model


async def delete_model(
    session: AsyncSession, model_id: UUID, *, tenant_id: str, subject_id: str, force: bool
) -> dict[str, Any]:
    """Delete the model and everything in it; refused once sealed unless ``force``.

    Only the model's OWN tenant's rows go (``_of_model``): a foreign row carrying the id is
    not model content and is never written by this tenant, so such a row refuses the
    delete (409) instead of being deleted or breaking the model's DELETE on its FK.
    """
    model = await get_mutable_model(session, model_id, tenant_id, subject_id, lock=True)
    has_revisions = (
        await session.execute(
            select(ModelingModelRevision.id)
            .where(ModelingModelRevision.model_id == model.id)
            .limit(1)
        )
    ).scalar_one_or_none() is not None
    if has_revisions and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "code": MODEL_HAS_REVISIONS,
                "message": "the model has sealed revisions; pass ?force=true to delete it",
            },
        )
    await _refuse_foreign_model_rows(session, model)
    # Every read and delete below is ``_of_model`` (id AND the model's tenant): only the
    # rows this tenant owns are content of the model.
    element_ids = await _scalars(
        session,
        select(ArchitectureElement.id).where(
            _of_model(ArchitectureElement, model.id, model.tenant_id)
        ),
    )
    rel_ids = await _scalars(
        session,
        select(ArchitectureRelationship.id).where(
            _of_model(ArchitectureRelationship, model.id, model.tenant_id)
        ),
    )
    view_ids = await _scalars(
        session,
        select(ArchitectureView.id).where(_of_model(ArchitectureView, model.id, model.tenant_id)),
    )
    # Edges from OTHER domains (requirement → element traceability, …) carry no model
    # but would dangle: remove every edge touching the model's concepts, as an element
    # delete does (``EntityService._cleanup_relationships``).
    touching = or_(
        (ArchitectureRelationship.source_entity_type == "architecture_elements")
        & ArchitectureRelationship.source_id.in_(element_ids),
        (ArchitectureRelationship.target_entity_type == "architecture_elements")
        & ArchitectureRelationship.target_id.in_(element_ids),
        (ArchitectureRelationship.source_entity_type == "architecture_relationships")
        & ArchitectureRelationship.source_id.in_(rel_ids),
        (ArchitectureRelationship.target_entity_type == "architecture_relationships")
        & ArchitectureRelationship.target_id.in_(rel_ids),
    )
    # Views first (their occurrences go by FK cascade), then edges, then concepts.
    await session.execute(
        delete(ArchitectureView).where(_of_model(ArchitectureView, model.id, model.tenant_id))
    )
    await session.execute(
        delete(ArchitectureViewConnection).where(
            _of_model(ArchitectureViewConnection, model.id, model.tenant_id)
        )
    )
    await session.execute(
        delete(ArchitectureViewObject).where(
            _of_model(ArchitectureViewObject, model.id, model.tenant_id)
        )
    )
    cross_ids = await _scalars(
        session,
        delete(ArchitectureRelationship)
        .where(
            ArchitectureRelationship.tenant_id == tenant_id,
            ArchitectureRelationship.id.not_in(rel_ids),
            touching,
        )
        .returning(ArchitectureRelationship.id),
    )
    # Edges ending (transitively) on a deleted edge — a cross-domain edge included — go
    # too; that helper also prunes their trace links and bumps their models.
    await delete_dependent_relationships(session, [*rel_ids, *cross_ids], tenant_id)
    await session.execute(
        delete(ArchitectureRelationship).where(
            _of_model(ArchitectureRelationship, model.id, model.tenant_id)
        )
    )
    await session.execute(
        delete(ArchitectureElement).where(_of_model(ArchitectureElement, model.id, model.tenant_id))
    )
    # Trace links on any deleted row never dangle — including links of OTHER models (their
    # heads are bumped) and private links that belong to no model (no FK cascade). UML
    # rows go with the model by FK cascade; links on them are deleted first.
    await prune_trace_links_async(session, [*element_ids, *rel_ids, *view_ids, *cross_ids])
    await uml_manifest.delete_links_into_model(session, model.id)
    await session.delete(model)  # revisions (and UML rows) go by FK cascade
    await session.commit()
    return {"status": "deleted", "id": model_id}


# ---------------------------------------------------------------------------
# Manifest + revisions
# ---------------------------------------------------------------------------


def _members(ids: list[Any] | None, own: set[str]) -> list[Any] | None:
    return None if ids is None else [i for i in ids if str(i) in own]


def object_snapshot_fields(obj: dict[str, Any]) -> dict[str, Any]:
    """A manifest view object restricted to the fields the shared snapshot schema declares.

    Manifests carry every stored attribute of an occurrence (e.g. a note's
    ``documentation``); the validator input takes exactly ``ArchimateViewObjectIn``. Once
    that schema declares a field, it passes through unchanged.
    """
    return {k: v for k, v in obj.items() if k in ArchimateViewObjectIn.model_fields}


def archimate_snapshot(manifest: dict[str, Any]) -> ArchimateSnapshot:
    """The validator input (``ArchimateSnapshot``) of a manifest-shaped dict."""
    return ArchimateSnapshot.model_validate(
        {
            "concepts": manifest.get("concepts", []),
            "relationships": manifest.get("relationships", []),
            "views": [
                {**v, "objects": [object_snapshot_fields(o) for o in v.get("objects", [])]}
                for v in manifest.get("views", [])
            ],
            "organizations": manifest.get("organizations"),
            "exchange": manifest.get("exchange"),
        }
    )


def _normalise_number(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# Arrays whose ORDER is model content although their items carry an ``id``: a UML
# interaction's occurrences (list position is the time order), an operation's parameters
# (the signature), a classifier's properties / operations (compartment order), a view's or
# diagram's objects (z-order: the stored ``position``) and folder trees (sibling folders
# keep the order they were written in, as Archi shows them).
ORDERED_ARRAY_KEYS = frozenset(
    {
        "occurrences",
        "parameters",
        "properties",
        "operations",
        "objects",
        "organizations",
        "children",
    }
)


def canonicalize(value: Any, *, _ordered: bool = False) -> Any:
    """Canonical form: arrays of objects with an ``id`` ordered by id, integral floats as ints.

    Other arrays keep their order (bendpoints and property pairs are ordered data), and so
    do the arrays under :data:`ORDERED_ARRAY_KEYS`, whose order is model content (z-ordered
    view / diagram objects, folder trees, UML compartments). Keys are sorted at
    serialisation (:func:`canonical_json`).
    """
    if isinstance(value, dict):
        return {str(k): canonicalize(v, _ordered=k in ORDERED_ARRAY_KEYS) for k, v in value.items()}
    if isinstance(value, list):
        items = [canonicalize(v) for v in value]
        if not _ordered and items and all(isinstance(i, dict) and "id" in i for i in items):
            items.sort(key=lambda i: str(i["id"]))
        return items
    return _normalise_number(value)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


async def canonical_manifest(session: AsyncSession, model: ModelingModel) -> dict[str, Any]:
    """The model's canonical manifest: rows read here, canonicalised in the threadpool.

    Canonicalising (and hashing) a model — up to ``MODEL_ASYNC_MAX_CONCEPTS`` concepts when
    sealing — is CPU work; it never runs on the event loop.
    """
    raw = await build_manifest(session, model)
    return await run_in_threadpool(canonicalize, raw)


async def hash_manifest(manifest: dict[str, Any]) -> str:
    """:func:`content_hash` in the threadpool (CPU work off the event loop)."""
    return await run_in_threadpool(content_hash, manifest)


def _property_pairs(props: Any) -> list[list[str]] | None:
    """Stored JSONB properties as the snapshot's ordered ``[key, value]`` pairs (by key)."""
    if not isinstance(props, dict) or not props:
        return None
    return [
        [str(k), v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)]
        for k, v in sorted(props.items())
    ]


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


async def _scalars(session: AsyncSession, stmt: Any) -> list[Any]:
    return list((await session.execute(stmt)).scalars().all())


def _of_model(cls: Any, model_id: UUID, tenant_id: Any) -> Any:
    """``cls`` rows of the model AND of its tenant.

    ``model_id`` alone is not enough: a row of another tenant can carry the id (a
    relationship derives its model from its source end, and an edge stored before the
    write gate refused foreign models may sit in, say, the system tenant's model). Such a
    row must never enter a seal, an export or a render of the model.
    """
    return (cls.model_id == model_id) & (cls.tenant_id == tenant_id)


def _model_tenant(model_id: UUID) -> Any:
    return select(ModelingModel.tenant_id).where(ModelingModel.id == model_id).scalar_subquery()


async def count_concepts(session: AsyncSession, model_id: UUID) -> int:
    tenant = _model_tenant(model_id)
    elements = (
        await session.execute(
            select(func.count()).where(_of_model(ArchitectureElement, model_id, tenant))
        )
    ).scalar_one()
    rels = (
        await session.execute(
            select(func.count()).where(_of_model(ArchitectureRelationship, model_id, tenant))
        )
    ).scalar_one()
    return int(elements) + int(rels) + await uml_manifest.size(session, model_id, tenant)


async def build_manifest(session: AsyncSession, model: ModelingModel) -> dict[str, Any]:
    """The complete model as a revision manifest (spec §7.2).

    ``concepts`` / ``relationships`` / ``views`` are exactly an ``ArchimateSnapshot`` (the
    validator's input); ``attributes`` holds each row's remaining stored attributes,
    ``bindings`` the elements' realisation bindings. Timestamps and heuristic quality
    scores are not model content and are left out, so an unchanged model always hashes
    the same. ``trace_links`` are the links whose source is in the model (§7.2).
    """
    header = {
        "id": str(model.id),
        "language": model.language,
        "language_version": model.language_version,
        "title": model.title,
        "description": model.description,
        "project_id": model.project_id,
        "visibility": model.visibility,
    }
    links = await _scalars(
        session,
        select(ModelingTraceLink).where(_of_model(ModelingTraceLink, model.id, model.tenant_id)),
    )
    trace_links = [
        {
            "id": str(t.id),
            "source": {"kind": t.source_kind, "id": str(t.source_id)},
            "target": None
            if t.target_id is None
            else {"kind": t.target_kind, "id": str(t.target_id)},
            "target_code": t.target_code,
            "link_type": t.link_type,
            "note": t.note,
            "created_by": t.created_by,
        }
        for t in links
    ]
    if model.language != "archimate":
        # elements / relationships / interactions / diagrams are exactly a UmlSnapshot;
        # ``attributes`` carries stored fields it has no slot for (diagram documentation).
        return {
            "format": MANIFEST_FORMAT,
            "model": header,
            **await uml_manifest.manifest_parts(session, model.id, model.tenant_id),
            "trace_links": trace_links,
        }

    # Every read is scoped to the model's tenant as well as its id (``_of_model``).
    tenant = model.tenant_id
    elements = await _scalars(
        session, select(ArchitectureElement).where(_of_model(ArchitectureElement, model.id, tenant))
    )
    rels = await _scalars(
        session,
        select(ArchitectureRelationship).where(
            _of_model(ArchitectureRelationship, model.id, tenant)
        ),
    )
    views = await _scalars(
        session, select(ArchitectureView).where(_of_model(ArchitectureView, model.id, tenant))
    )
    objects = await _scalars(
        session,
        select(ArchitectureViewObject)
        .where(_of_model(ArchitectureViewObject, model.id, tenant))
        # z-order: a view's objects are listed in their stored order.
        .order_by(ArchitectureViewObject.position, ArchitectureViewObject.id),
    )
    connections = await _scalars(
        session,
        select(ArchitectureViewConnection).where(
            _of_model(ArchitectureViewConnection, model.id, tenant)
        ),
    )
    bindings = await _scalars(
        session,
        select(ArchitectureElementRealisationBinding).where(
            ArchitectureElementRealisationBinding.element_id.in_([e.id for e in elements])
        ),
    )

    concepts = []
    for e in elements:
        props = e.properties if isinstance(e.properties, dict) else {}
        concepts.append(
            {
                "id": str(e.id),
                "type": e.element_type,
                "name": e.title or "",
                "layer": e.layer,
                "parent_id": _str(e.parent_id),
                "junction_type": props.get("junction_type")
                if e.element_type == "junction"
                else None,
                "documentation": e.description,
                "properties": _property_pairs(props),
            }
        )
    relationships = []
    for r in rels:
        rel = relationship_of(r).model_dump(mode="json")
        rel.update(
            {
                "name": r.name or "",
                "documentation": r.description,
                "properties": _property_pairs(r.properties),
            }
        )
        relationships.append(rel)
    by_view_objects: dict[UUID, list[dict[str, Any]]] = {}
    for o in objects:
        by_view_objects.setdefault(o.view_id, []).append(
            {
                "id": str(o.id),
                "kind": o.kind,
                "concept_id": _str(o.concept_ref),
                "parent_object_id": _str(o.parent_object_id),
                "bounds": o.bounds,
                "style": o.style,
                "name": o.name,
                "text": o.text,
                "label": o.label,
                "border_type": o.border_type,
                "documentation": o.documentation,
                # Present only when set, so models without one hash as before.
                **({"view_ref": str(o.view_ref)} if o.view_ref is not None else {}),
            }
        )
    by_view_conns: dict[UUID, list[dict[str, Any]]] = {}
    for c in connections:
        by_view_conns.setdefault(c.view_id, []).append(
            {
                "id": str(c.id),
                "kind": c.kind,
                "relationship_id": _str(c.relationship_ref),
                "source_object_id": str(c.source_object_id),
                "target_object_id": str(c.target_object_id),
                "label": c.label,
                "bendpoints": c.bendpoints,
                "style": c.style,
            }
        )
    element_set = {str(e.id) for e in elements}
    rel_set = {str(r.id) for r in rels}
    view_list = [
        {
            "id": str(v.id),
            "name": v.title or "",
            "viewpoint": v.viewpoint,
            "objects": by_view_objects.get(v.id, []),
            "connections": by_view_conns.get(v.id, []),
            "documentation": v.description,
            "properties": None,
            **({"viewpoint_ref": v.viewpoint_ref} if v.viewpoint_ref is not None else {}),
        }
        for v in views
    ]
    # The snapshot part must be a valid validator input; fail loudly here, not later.
    archimate_snapshot({"concepts": concepts, "relationships": relationships, "views": view_list})
    attributes = {
        "elements": [
            {
                "id": str(e.id),
                "project_id": e.project_id,
                "visibility": e.visibility,
                "status": e.status,
                "architecture_state": e.architecture_state,
                "is_synthetic": e.is_synthetic,
                "version": e.version,
                "version_status": e.version_status,
                "slug": e.slug,
            }
            for e in elements
        ],
        "relationships": [
            {
                "id": str(r.id),
                "project_id": r.project_id,
                "visibility": r.visibility,
                "source_entity_type": r.source_entity_type,
                "target_entity_type": r.target_entity_type,
                "slug": r.slug,
            }
            for r in rels
        ],
        "views": [
            {
                "id": str(v.id),
                "project_id": v.project_id,
                "visibility": v.visibility,
                # Legacy membership lists, restricted to this model's rows (writes are
                # checked and deletes prune them; rows from before that are dropped here).
                "element_ids": _members(v.element_ids, element_set),
                "relationship_ids": _members(v.relationship_ids, rel_set),
                "layout_data": v.layout_data,
                "version": v.version,
                "version_status": v.version_status,
                "slug": v.slug,
            }
            for v in views
        ],
    }
    return {
        "format": MANIFEST_FORMAT,
        "model": header,
        "concepts": concepts,
        "relationships": relationships,
        "views": view_list,
        # The folder tree, without items whose row no longer exists (model_organizations).
        "organizations": organizations.prune(
            model.organizations,
            {c["id"] for c in concepts}
            | {r["id"] for r in relationships}
            | {v["id"] for v in view_list},
        ),
        # Exchange-format model content (properties, metadata, custom viewpoints); the key
        # is present only when there is some, so models without any hash as before.
        **({"exchange": model.exchange} if model.exchange is not None else {}),
        "attributes": attributes,
        "bindings": [
            {
                "id": str(b.id),
                "element_id": str(b.element_id),
                "binding_kind": b.binding_kind,
                "container": b.container,
                "component_id": b.component_id,
                "repository_url": b.repository_url,
                "repository_branch": b.repository_branch,
                "properties": b.properties,
            }
            for b in bindings
        ],
        "trace_links": trace_links,
    }


async def seal_revision(
    session: AsyncSession,
    model_id: UUID,
    *,
    tenant_id: str,
    subject_id: str,
    label: str | None,
    expected_head: int | None,
) -> ModelingModelRevision:
    # The seal holds the model row FOR UPDATE and a request-pool connection while it
    # builds a manifest of up to MODEL_ASYNC_MAX_CONCEPTS: every statement of it (the lock
    # wait included) is bounded. ``set_config(..., true)`` is SET LOCAL — this transaction
    # only, never the pooled server session (PgBouncer transaction pooling); the RLS GUC
    # is bound the same way at transaction-begin and is unaffected.
    await session.execute(
        select(
            func.set_config(
                "statement_timeout", str(get_settings().model_seal_statement_timeout_ms), True
            )
        )
    )
    try:
        return await _seal(
            session,
            model_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            label=label,
            expected_head=expected_head,
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != QUERY_CANCELED:
            raise
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail={
                "code": SEAL_TIMEOUT,
                "message": (
                    "sealing exceeded MODEL_SEAL_STATEMENT_TIMEOUT_MS="
                    f"{get_settings().model_seal_statement_timeout_ms} (a long manifest "
                    "build, or the model row is locked by a concurrent write); retry later"
                ),
            },
            headers={"Retry-After": "30"},
        ) from exc


async def _seal(
    session: AsyncSession,
    model_id: UUID,
    *,
    tenant_id: str,
    subject_id: str,
    label: str | None,
    expected_head: int | None,
) -> ModelingModelRevision:
    model = await get_mutable_model(session, model_id, tenant_id, subject_id, lock=True)
    if expected_head is not None and model.head != expected_head:
        raise stale_head(model.head)
    # Sealing is a snapshot copy (read, canonicalise, hash, insert) and runs no rules, so
    # it is bounded by the BACKGROUND validation bound: a sealed revision above
    # MODEL_SYNC_MAX_CONCEPTS is what a background validation run validates.
    limit = get_settings().model_async_max_concepts
    total = await count_concepts(session, model.id)
    if total > limit:
        raise HTTPException(
            status_code=422,
            detail={
                "code": INPUT_TOO_LARGE,
                "message": (
                    f"the model has {total} concepts and relationships; sealing is "
                    f"bounded by MODEL_ASYNC_MAX_CONCEPTS={limit}"
                ),
            },
        )
    # One transaction under the FOR UPDATE lock, so the snapshot is one state; the
    # canonicalising and hashing run in the threadpool, off the event loop.
    manifest = await canonical_manifest(session, model)
    digest = await hash_manifest(manifest)
    seq = (
        await session.execute(
            select(func.coalesce(func.max(ModelingModelRevision.seq), 0)).where(
                ModelingModelRevision.model_id == model.id
            )
        )
    ).scalar_one()
    revision = ModelingModelRevision(
        tenant_id=tenant_id,
        model_id=model.id,
        seq=int(seq) + 1,
        label=label,
        manifest=manifest,
        content_hash=digest,
        head=model.head,
        sealed_by=_sub(tenant_id, subject_id),
    )
    session.add(revision)
    await session.commit()
    await session.refresh(revision)
    return revision


async def list_revisions(
    session: AsyncSession, model_id: UUID, *, limit: int, offset: int
) -> tuple[list[ModelingModelRevision], int]:
    where = ModelingModelRevision.model_id == model_id
    total = (
        await session.execute(select(func.count()).select_from(ModelingModelRevision).where(where))
    ).scalar_one()
    rows = await _scalars(
        session,
        select(ModelingModelRevision)
        .where(where)
        .order_by(ModelingModelRevision.seq.desc())
        .limit(limit)
        .offset(offset),
    )
    return rows, total


async def get_revision(
    session: AsyncSession, model_id: UUID, revision_id: UUID
) -> ModelingModelRevision:
    row = (
        await session.execute(
            select(ModelingModelRevision).where(
                ModelingModelRevision.id == revision_id,
                ModelingModelRevision.model_id == model_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="revision not found")
    return row
