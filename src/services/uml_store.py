"""Stored UML concepts and diagrams (target spec §8.2) on the shared modeling lifecycle (§7).

Every write:

1. resolves the UML model and takes its row lock (``SELECT … FOR UPDATE``), refusing a
   stale ``If-Match`` with ``412 stale_head`` — so the whole-model check below and the
   write are one atomic step, and a seal never interleaves;
2. loads the model's draft head, applies the ONE operation in memory
   (``services/uml_draft.py``: cascades included) and runs the pure UML validator on the
   model before and after; an error the write would introduce is ``422 model_integrity``;
3. makes the rows equal the new draft (``services/uml_manifest.persist``), deletes trace
   links on deleted concepts, bumps ``head`` once and commits (or only flushes inside a
   change set's caller-owned transaction).

The check runs over the WHOLE model, not a neighbourhood: identifier uniqueness
(``UML-REF-002``), ownership and generalization cycles and connector interface
conformance all depend on rows far from the one written, and the model is bounded by
``MODEL_SYNC_MAX_CONCEPTS`` — counting every UML row and every part of every interaction
document (``UmlDraft.size``); a write that would exceed it is ``422 input_too_large``. The
validator runs in a worker thread, never on the event loop. A write's ``ETag`` is its
target model's head even when it also bumps another model (a pruned trace link).

Visibility: a UML row carries its model's scope, so "resolves and is visible" is "is a
row of the same model": a reference to another model's, another tenant's or another
subject's private row is refused exactly like a missing one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid7

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from models.modeling import ModelingModel, default_model_id, note_etag_model
from models.uml import (
    UmlDiagram,
    UmlDiagramConnection,
    UmlDiagramObject,
    UmlElement,
    UmlInteraction,
    UmlRelationship,
)
from repositories.visibility import can_mutate, scope
from schemas.uml_api import (
    UmlDiagramConnectionOut,
    UmlDiagramObjectOut,
    UmlDiagramOut,
    UmlElementOut,
    UmlInteractionOut,
    UmlRelationshipCreate,
    UmlRelationshipOut,
)
from services import modeling_models as models_svc
from services import uml_manifest as store
from services import unit_of_work as uow
from services.idempotency import BeforeCommit
from services.model_integrity import integrity_error, single_finding
from services.uml_draft import UML_KINDS, UmlDraft, gate, payload_dict
from settings import get_settings
from tools.uml.profile import get_profile

CLASSES: dict[str, Any] = {
    "uml_element": UmlElement,
    "uml_relationship": UmlRelationship,
    "interaction": UmlInteraction,
    "diagram": UmlDiagram,
    "diagram_object": UmlDiagramObject,
    "diagram_connection": UmlDiagramConnection,
}
_NOUN = {
    "uml_element": "UML element",
    "uml_relationship": "UML relationship",
    "interaction": "interaction",
    "diagram": "diagram",
    "diagram_object": "diagram object",
    "diagram_connection": "diagram connection",
}
MODEL_PROJECT_MISMATCH = "model_project_mismatch"


def _sub(tenant_id: str, subject_id: str) -> str:
    return subject_id or tenant_id


def _not_found(kind: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{_NOUN[kind]} not found")


def _ref_error(problem: str, *, path: str, evidence: dict[str, Any]) -> HTTPException:
    return integrity_error(
        [
            single_finding(
                "UML-REF-001", get_profile().ruleset_id, problem, path=path, evidence=evidence
            )
        ]
    )


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


def _row_common(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "model_id": row.model_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def to_out(kind: str, row: Any) -> BaseModel:
    if kind == "uml_element":
        return UmlElementOut.model_validate(row)
    if kind == "diagram":
        return UmlDiagramOut.model_validate(row)
    if kind == "diagram_object":
        return UmlDiagramObjectOut.model_validate(row)
    if kind == "diagram_connection":
        return UmlDiagramConnectionOut.model_validate(row)
    scope_fields = {"project_id": row.project_id, "visibility": row.visibility}
    if kind == "uml_relationship":
        return UmlRelationshipOut(
            **_row_common(row),
            **scope_fields,
            kind=row.kind,
            name=row.name,
            source=row.source_ref,
            target=row.target_ref,
            ends=row.ends,
            connector_kind=row.connector_kind,
        )
    doc = row.document or {}
    return UmlInteractionOut(
        **_row_common(row),
        **scope_fields,
        name=row.name,
        owner_id=row.owner_id,
        lifelines=doc.get("lifelines", []),
        occurrences=doc.get("occurrences", []),
        messages=doc.get("messages", []),
        executions=doc.get("executions", []),
        fragments=doc.get("fragments", []),
    )


# ---------------------------------------------------------------------------
# Model resolution and locking
# ---------------------------------------------------------------------------


async def _locked(session: AsyncSession, model_id: UUID) -> ModelingModel:
    model = await models_svc.lock_model(session, model_id)
    assert model is not None  # resolved (and visible) just before
    return model


def _check_head(model: ModelingModel, if_match: int | None) -> None:
    if if_match is not None and model.head != if_match:
        raise models_svc.stale_head(model.head)


async def resolve_model(
    session: AsyncSession,
    *,
    model_id: UUID | None,
    project_id: str | None,
    tenant_id: str,
    subject_id: str,
    visibility: str | None,
    if_match: int | None,
) -> ModelingModel:
    """The UML model a new element / interaction / diagram joins, locked; If-Match checked.

    Explicit ``model_id``: a UML model the caller may write, of the request's visibility
    (``X-Visibility``; ``None`` = not checked) and, when ``project_id`` is given, of that
    project — otherwise
    ``422 model_integrity`` ``UML-REF-001`` with ``evidence.reason``. Omitted: the scope's
    default UML model (tenant, ``project_id``, visibility, subject if private), created on
    first use.
    """
    sub = _sub(tenant_id, subject_id)
    if model_id is None:
        assert visibility is not None
        mid = await session.run_sync(
            lambda s: default_model_id(
                s,
                tenant_id=tenant_id,
                project_id=project_id,
                visibility=visibility,
                subject_id=sub,
                language="uml",
            )
        )
        model = await _locked(session, mid)
        _check_head(model, if_match)
        return model
    model = (
        await session.execute(
            scope(
                select(ModelingModel).where(ModelingModel.id == model_id),
                ModelingModel,
                tenant_id=tenant_id,
                subject_id=sub,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    reason = problem = None
    if model is None or not can_mutate(model, subject_id=sub, tenant_id=tenant_id):
        reason = models_svc.MODEL_NOT_WRITABLE
        problem = f"model {model_id} does not resolve to a model the caller may write"
    elif model.language != "uml":
        reason = models_svc.MODEL_LANGUAGE_MISMATCH
        problem = f"model {model_id} is a {model.language} model, not a UML model"
    elif visibility is not None and model.visibility != visibility:
        reason = models_svc.MODEL_VISIBILITY_MISMATCH
        problem = (
            f"model {model_id} has visibility '{model.visibility}'; a '{visibility}' row "
            "cannot join it"
        )
    elif project_id is not None and project_id != model.project_id:
        reason = MODEL_PROJECT_MISMATCH
        problem = f"model {model_id} belongs to project {model.project_id!r}, not {project_id!r}"
    if problem is not None:
        raise _ref_error(
            problem, path="/model_id", evidence={"model_id": str(model_id), "reason": reason}
        )
    assert model is not None
    _check_head(model, if_match)
    return model


async def relationship_model(
    session: AsyncSession,
    payload: UmlRelationshipCreate,
    *,
    tenant_id: str,
    subject_id: str,
    if_match: int | None,
) -> ModelingModel:
    """The model a new relationship joins: ``model_id``, else its source (or first) end's.

    The derived model is the end's own; an end the caller cannot see (or that is not a
    UML element) does not resolve (``422 UML-REF-001``), like any other reference.
    """
    if payload.model_id is not None:
        return await resolve_model(
            session,
            model_id=payload.model_id,
            project_id=None,
            tenant_id=tenant_id,
            subject_id=subject_id,
            visibility=None,
            if_match=if_match,
        )
    if payload.source is not None:
        ref, path = payload.source, "/source"
    elif payload.ends:
        ref, path = payload.ends[0].element_ref, "/ends/0/element_ref"
    else:
        raise _ref_error(
            "a relationship that names no model_id needs an end to take its model from",
            path="/model_id",
            evidence={"reason": "no_model"},
        )
    try:
        row = await get_row(session, "uml_element", UUID(ref), tenant_id, subject_id)
    except ValueError, HTTPException:
        raise _ref_error(
            f"{path.lstrip('/')} {ref} does not resolve to a visible UML element",
            path=path,
            evidence={"ref": ref},
        ) from None
    return await writable_model(
        session, row.model_id, tenant_id, subject_id, if_match, kind="uml_element"
    )


async def get_row(
    session: AsyncSession, kind: str, row_id: UUID, tenant_id: str, subject_id: str
) -> Any:
    """A concept row the caller can see, else 404."""
    cls = CLASSES[kind]
    row = (
        await session.execute(
            scope(
                select(cls).where(cls.id == row_id),
                cls,
                tenant_id=tenant_id,
                subject_id=_sub(tenant_id, subject_id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found(kind)
    return row


async def get_diagram(
    session: AsyncSession, diagram_id: UUID, tenant_id: str, subject_id: str
) -> UmlDiagram:
    return await get_row(session, "diagram", diagram_id, tenant_id, subject_id)


async def writable_model(
    session: AsyncSession,
    model_id: UUID,
    tenant_id: str,
    subject_id: str,
    if_match: int | None,
    *,
    kind: str,
) -> ModelingModel:
    """The (visible) row's model, locked; 404 if the caller may not write it."""
    model = await _locked(session, model_id)
    if not can_mutate(model, subject_id=_sub(tenant_id, subject_id), tenant_id=tenant_id):
        raise _not_found(kind)
    _check_head(model, if_match)
    return model


async def get_occurrence(
    session: AsyncSession, kind: str, diagram: UmlDiagram, row_id: UUID
) -> Any:
    cls = CLASSES[kind]
    row = (
        await session.execute(select(cls).where(cls.id == row_id, cls.diagram_id == diagram.id))
    ).scalar_one_or_none()
    if row is None:
        raise _not_found(kind)
    return row


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def _too_large(total: int) -> HTTPException:
    limit = get_settings().model_sync_max_concepts
    return HTTPException(
        status_code=422,
        detail={
            "code": models_svc.INPUT_TOO_LARGE,
            "message": (
                f"the write would leave the model with {total} UML rows and interaction parts; "
                f"write-time integrity is synchronous up to MODEL_SYNC_MAX_CONCEPTS={limit}"
            ),
        },
    )


async def _write(
    session: AsyncSession,
    model: ModelingModel,
    *,
    subject_id: str,
    put: tuple[str, dict[str, Any], int | None] | None = None,
    delete: tuple[str, str] | None = None,
) -> tuple[UmlDraft, UmlDraft]:
    """Apply one operation to the model's draft, gate it, persist it: ``(before, after)``."""
    before = await store.load_draft(session, model.id, model.tenant_id)
    after = before.copy()
    if put is not None:
        after.put(*put)
    if delete is not None:
        after.delete(*delete)
    await commit_draft(session, model, before, after, subject_id=subject_id)
    return before, after


async def commit_draft(
    session: AsyncSession,
    model: ModelingModel,
    before: UmlDraft,
    after: UmlDraft,
    *,
    subject_id: str,
) -> None:
    """Gate ``before`` → ``after`` and make the rows equal ``after`` (no commit).

    The validator is CPU-bound and runs in a worker thread, so the event loop keeps
    serving other requests while this one holds the model lock.
    """
    note_etag_model(session, model.id)  # ETag = this model's head, whatever else is bumped
    total = after.size()
    if total > get_settings().model_sync_max_concepts and total > before.size():
        raise _too_large(total)
    errors = await run_in_threadpool(gate, before, after)
    if errors:
        raise integrity_error(errors)
    await store.persist(session, model, before, after, subject_id=subject_id)


async def _finish(
    session: AsyncSession, kind: str, row_id: UUID, before_commit: BeforeCommit | None
) -> BaseModel:
    row = (
        await session.execute(
            select(CLASSES[kind])
            .where(CLASSES[kind].id == row_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    out = to_out(kind, row)
    if before_commit is not None:
        await before_commit(out)
    await uow.commit(session)
    return out


async def create(
    session: AsyncSession,
    kind: str,
    model: ModelingModel,
    payload: BaseModel,
    *,
    subject_id: str,
    diagram_id: str | None = None,
    before_commit: BeforeCommit | None = None,
) -> BaseModel:
    """Create one row in ``model`` (already resolved, locked and head-checked)."""
    row_id = uuid7()
    await _write(
        session,
        model,
        subject_id=subject_id,
        put=(
            kind,
            payload_dict(kind, str(row_id), payload, diagram_id),
            getattr(payload, "position", None),
        ),
    )
    return await _finish(session, kind, row_id, before_commit)


async def update(
    session: AsyncSession,
    kind: str,
    model: ModelingModel,
    row: Any,
    payload: BaseModel,
    *,
    subject_id: str,
) -> BaseModel:
    diagram_id = str(row.diagram_id) if hasattr(row, "diagram_id") else None
    row_id = row.id
    await _write(
        session,
        model,
        subject_id=subject_id,
        put=(
            kind,
            payload_dict(kind, str(row_id), payload, diagram_id),
            getattr(payload, "position", None),
        ),
    )
    return await _finish(session, kind, row_id, None)


async def delete(
    session: AsyncSession, kind: str, model: ModelingModel, row: Any, *, subject_id: str
) -> dict[str, Any]:
    """Delete one row with its cascade; returns what went (per kind, sorted ids)."""
    row_id = str(row.id)
    before, after = await _write(session, model, subject_id=subject_id, delete=(kind, row_id))
    cascaded = {k: sorted(set(before.table(k)) - set(after.table(k)) - {row_id}) for k in UML_KINDS}
    await uow.commit(session)
    return {
        "status": "deleted",
        "id": row_id,
        "cascaded": {k: v for k, v in cascaded.items() if v},
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_rows(
    session: AsyncSession,
    kind: str,
    tenant_id: str,
    subject_id: str,
    *,
    filters: dict[str, Any],
    limit: int,
    offset: int,
) -> tuple[list[Any], int]:
    cls = CLASSES[kind]
    stmt = scope(select(cls), cls, tenant_id=tenant_id, subject_id=_sub(tenant_id, subject_id))
    for column, value in filters.items():
        if value is not None:
            stmt = stmt.where(getattr(cls, column) == value)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(cls.created_at.desc(), cls.id.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def list_occurrences(session: AsyncSession, kind: str, diagram: UmlDiagram) -> list[Any]:
    cls = CLASSES[kind]
    # Objects in z-order (the manifest's order); connections by id.
    order = (cls.position, cls.id) if kind == "diagram_object" else (cls.id,)
    return list(
        (await session.execute(select(cls).where(cls.diagram_id == diagram.id).order_by(*order)))
        .scalars()
        .all()
    )
