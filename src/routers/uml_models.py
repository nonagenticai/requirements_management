"""Stored UML concepts, interactions, diagrams and occurrences (``/uml``, target spec §8.2).

Every write runs whole-model write-time integrity (``services/uml_store.py``): refusals are
``422 model_integrity`` with ``findings``. Every write increments the model's draft
``head`` (``ETag``); ``If-Match: <head>`` makes it conditional (``412 stale_head``). Creates
accept ``Idempotency-Key``. Reads see the caller's tenant only, and another subject's
private model as absent (404).

Also the render routes: ``GET /uml/diagrams/{id}/render`` (draft head) and
``GET /modeling/models/{model_id}/revisions/{rev_id}/diagrams/{diagram_id}/render`` (a sealed
revision) — synchronous, size-capped, sanitized (``routers/_render_common.py``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from routers._render_common import (
    RENDER_RESPONSES,
    FormatParam,
    IfNoneMatch,
    ReportParam,
    not_modified,
    render_response,
)
from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.common import ErrorResponse
from schemas.uml_api import (
    UmlDeleteResult,
    UmlDiagramConnectionIn,
    UmlDiagramConnectionOut,
    UmlDiagramCreate,
    UmlDiagramObjectIn,
    UmlDiagramObjectOut,
    UmlDiagramOut,
    UmlDiagramUpdate,
    UmlElementCreate,
    UmlElementOut,
    UmlElementUpdate,
    UmlInteractionCreate,
    UmlInteractionOut,
    UmlInteractionUpdate,
    UmlRelationshipCreate,
    UmlRelationshipOut,
    UmlRelationshipUpdate,
)
from services import modeling_models as models_svc
from services import uml_manifest
from services import uml_store as svc
from services.idempotency import IDEMPOTENCY_409, Idempotency, idempotency_key_header
from services.render_http import matches, render_etag
from services.uml_draft import UmlDraft
from settings import get_settings
from tools.rendering import UML_RENDERER_VERSION, render_uml_diagram

router = APIRouter(route_class=ErrorEnvelopeRoute)
revision_render_router = APIRouter(route_class=ErrorEnvelopeRoute)

_WRITE = {
    **AUTH_RESPONSES,
    404: {"description": "Not found", "model": ErrorResponse},
    409: IDEMPOTENCY_409,
    412: models_svc.STALE_HEAD_412,
    422: {"description": "`model_integrity` (findings), `input_too_large`", "model": ErrorResponse},
}
_READ = {**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}}
_ID = Path(..., description="Row ID")
_LIMIT = Query(100, ge=1, le=1000, description="Max items to return")
_OFFSET = Query(0, ge=0, description="Number of items to skip")
_MODEL_Q = Query(None, description="Filter by UML model")
_PROJECT_Q = Query(None, max_length=100, description="Filter by project")
_OWNER_Q = Query(None, description="Filter by owning element")


def _total(response: Response, total: int) -> None:
    response.headers["X-Total-Count"] = str(total)


async def _create(
    response: Response,
    session: AsyncSession,
    kind: str,
    route: str,
    payload: Any,
    out_schema: type[BaseModel],
    *,
    idempotency_key: str | None,
    if_match: int | None,
    tenant_id: str,
    subject_id: str,
    visibility: str,
) -> Any:
    idem = Idempotency(
        session,
        route=route,
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        key=idempotency_key,
        body=payload,
    )
    if (replay := await idem.begin()) is not None:
        response.status_code = replay.status_code
        return out_schema.model_validate(replay.body)
    model = await svc.resolve_model(
        session,
        model_id=payload.model_id,
        project_id=payload.project_id,
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        if_match=if_match,
    )
    async with idem.guard():
        out = await svc.create(
            session,
            kind,
            model,
            payload,
            subject_id=subject_id,
            before_commit=idem.stager(201, out_schema),
        )
    models_svc.set_etag(response, session)
    return out


async def _update(
    response: Response,
    session: AsyncSession,
    kind: str,
    row_id: UUID,
    payload: BaseModel,
    *,
    if_match: int | None,
    tenant_id: str,
    subject_id: str,
) -> Any:
    row = await svc.get_row(session, kind, row_id, tenant_id, subject_id)
    model = await svc.writable_model(
        session, row.model_id, tenant_id, subject_id, if_match, kind=kind
    )
    out = await svc.update(session, kind, model, row, payload, subject_id=subject_id)
    models_svc.set_etag(response, session)
    return out


async def _delete(
    response: Response,
    session: AsyncSession,
    kind: str,
    row_id: UUID,
    *,
    if_match: int | None,
    tenant_id: str,
    subject_id: str,
) -> UmlDeleteResult:
    row = await svc.get_row(session, kind, row_id, tenant_id, subject_id)
    model = await svc.writable_model(
        session, row.model_id, tenant_id, subject_id, if_match, kind=kind
    )
    out = await svc.delete(session, kind, model, row, subject_id=subject_id)
    models_svc.set_etag(response, session)
    return UmlDeleteResult.model_validate(out)


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------


@router.get(
    "/elements",
    response_model=list[UmlElementOut],
    operation_id="list_uml_elements",
    responses=AUTH_RESPONSES,
)
async def list_uml_elements(
    response: Response,
    model_id: UUID | None = _MODEL_Q,
    kind: str | None = Query(None, max_length=30, description="Filter by element kind"),
    project_id: str | None = _PROJECT_Q,
    owner_id: UUID | None = _OWNER_Q,
    limit: int = _LIMIT,
    offset: int = _OFFSET,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[Any]:
    """UML elements visible to the caller, newest first."""
    rows, total = await svc.list_rows(
        session,
        "uml_element",
        tenant_id,
        subject_id,
        filters={
            "model_id": model_id,
            "kind": kind,
            "project_id": project_id,
            "owner_id": owner_id,
        },
        limit=limit,
        offset=offset,
    )
    _total(response, total)
    return [svc.to_out("uml_element", r) for r in rows]


@router.post(
    "/elements",
    response_model=UmlElementOut,
    status_code=201,
    operation_id="create_uml_element",
    responses=_WRITE,
)
async def create_uml_element(
    response: Response,
    payload: UmlElementCreate,
    idempotency_key: str | None = Depends(idempotency_key_header),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Create a package, classifier, component, port or actor (201)."""
    return await _create(
        response,
        session,
        "uml_element",
        "/uml/elements",
        payload,
        UmlElementOut,
        idempotency_key=idempotency_key,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
    )


@router.get(
    "/elements/{id}",
    response_model=UmlElementOut,
    operation_id="get_uml_element",
    responses=_READ,
)
async def get_uml_element(
    id: UUID = _ID,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """One UML element."""
    return svc.to_out(
        "uml_element", await svc.get_row(session, "uml_element", id, tenant_id, subject_id)
    )


@router.put(
    "/elements/{id}",
    response_model=UmlElementOut,
    operation_id="update_uml_element",
    responses=_WRITE,
)
async def update_uml_element(
    response: Response,
    id: UUID = _ID,
    payload: UmlElementUpdate = Body(...),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Replace an element's kind, name, owner and attributes."""
    return await _update(
        response,
        session,
        "uml_element",
        id,
        payload,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


@router.delete(
    "/elements/{id}",
    operation_id="delete_uml_element",
    response_model=UmlDeleteResult,
    responses=_WRITE,
)
async def delete_uml_element(
    response: Response,
    id: UUID = _ID,
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UmlDeleteResult:
    """Delete an element with what it owns, the relationships ending on it and their
    occurrences (listed in ``cascaded``). Refused (``UML-REF-001``) while another concept
    still references it as a type, interface, lifeline type or operation owner."""
    return await _delete(
        response,
        session,
        "uml_element",
        id,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


@router.get(
    "/relationships",
    response_model=list[UmlRelationshipOut],
    operation_id="list_uml_relationships",
    responses=AUTH_RESPONSES,
)
async def list_uml_relationships(
    response: Response,
    model_id: UUID | None = _MODEL_Q,
    kind: str | None = Query(None, max_length=30, description="Filter by relationship kind"),
    project_id: str | None = _PROJECT_Q,
    limit: int = _LIMIT,
    offset: int = _OFFSET,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[Any]:
    """UML relationships visible to the caller, newest first."""
    rows, total = await svc.list_rows(
        session,
        "uml_relationship",
        tenant_id,
        subject_id,
        filters={"model_id": model_id, "kind": kind, "project_id": project_id},
        limit=limit,
        offset=offset,
    )
    _total(response, total)
    return [svc.to_out("uml_relationship", r) for r in rows]


@router.post(
    "/relationships",
    response_model=UmlRelationshipOut,
    status_code=201,
    operation_id="create_uml_relationship",
    responses=_WRITE,
)
async def create_uml_relationship(
    response: Response,
    payload: UmlRelationshipCreate,
    idempotency_key: str | None = Depends(idempotency_key_header),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Create a relationship (201). Without ``model_id`` it joins its source (or first
    association end's) model; ends in another model do not resolve (``UML-REF-001``)."""
    idem = Idempotency(
        session,
        route="/uml/relationships",
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        key=idempotency_key,
        body=payload,
    )
    if (replay := await idem.begin()) is not None:
        response.status_code = replay.status_code
        return UmlRelationshipOut.model_validate(replay.body)
    model = await svc.relationship_model(
        session, payload, tenant_id=tenant_id, subject_id=subject_id, if_match=if_match
    )
    async with idem.guard():
        out = await svc.create(
            session,
            "uml_relationship",
            model,
            payload,
            subject_id=subject_id,
            before_commit=idem.stager(201, UmlRelationshipOut),
        )
    models_svc.set_etag(response, session)
    return out


@router.get(
    "/relationships/{id}",
    response_model=UmlRelationshipOut,
    operation_id="get_uml_relationship",
    responses=_READ,
)
async def get_uml_relationship(
    id: UUID = _ID,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """One UML relationship."""
    row = await svc.get_row(session, "uml_relationship", id, tenant_id, subject_id)
    return svc.to_out("uml_relationship", row)


@router.put(
    "/relationships/{id}",
    response_model=UmlRelationshipOut,
    operation_id="update_uml_relationship",
    responses=_WRITE,
)
async def update_uml_relationship(
    response: Response,
    id: UUID = _ID,
    payload: UmlRelationshipUpdate = Body(...),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Replace a relationship (its connections must still show its ends: ``UML-DIA-002``)."""
    return await _update(
        response,
        session,
        "uml_relationship",
        id,
        payload,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


@router.delete(
    "/relationships/{id}",
    operation_id="delete_uml_relationship",
    response_model=UmlDeleteResult,
    responses=_WRITE,
)
async def delete_uml_relationship(
    response: Response,
    id: UUID = _ID,
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UmlDeleteResult:
    """Delete a relationship with its diagram connections and trace links."""
    return await _delete(
        response,
        session,
        "uml_relationship",
        id,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------


@router.get(
    "/interactions",
    response_model=list[UmlInteractionOut],
    operation_id="list_uml_interactions",
    responses=AUTH_RESPONSES,
)
async def list_uml_interactions(
    response: Response,
    model_id: UUID | None = _MODEL_Q,
    project_id: str | None = _PROJECT_Q,
    owner_id: UUID | None = _OWNER_Q,
    limit: int = _LIMIT,
    offset: int = _OFFSET,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[Any]:
    """Interactions visible to the caller, newest first (an interaction has no kind)."""
    rows, total = await svc.list_rows(
        session,
        "interaction",
        tenant_id,
        subject_id,
        filters={"model_id": model_id, "project_id": project_id, "owner_id": owner_id},
        limit=limit,
        offset=offset,
    )
    _total(response, total)
    return [svc.to_out("interaction", r) for r in rows]


@router.post(
    "/interactions",
    response_model=UmlInteractionOut,
    status_code=201,
    operation_id="create_uml_interaction",
    responses=_WRITE,
)
async def create_uml_interaction(
    response: Response,
    payload: UmlInteractionCreate,
    idempotency_key: str | None = Depends(idempotency_key_header),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Create an interaction document (lifelines, ordered occurrences, messages, …) (201)."""
    return await _create(
        response,
        session,
        "interaction",
        "/uml/interactions",
        payload,
        UmlInteractionOut,
        idempotency_key=idempotency_key,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
    )


@router.get(
    "/interactions/{id}",
    response_model=UmlInteractionOut,
    operation_id="get_uml_interaction",
    responses=_READ,
)
async def get_uml_interaction(
    id: UUID = _ID,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """One interaction document."""
    return svc.to_out(
        "interaction", await svc.get_row(session, "interaction", id, tenant_id, subject_id)
    )


@router.put(
    "/interactions/{id}",
    response_model=UmlInteractionOut,
    operation_id="update_uml_interaction",
    responses=_WRITE,
)
async def update_uml_interaction(
    response: Response,
    id: UUID = _ID,
    payload: UmlInteractionUpdate = Body(...),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Replace the whole interaction document (occurrence order is checked: UML-SEQ-*).
    Diagram occurrences of lifelines or messages it no longer has are deleted."""
    return await _update(
        response,
        session,
        "interaction",
        id,
        payload,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


@router.delete(
    "/interactions/{id}",
    operation_id="delete_uml_interaction",
    response_model=UmlDeleteResult,
    responses=_WRITE,
)
async def delete_uml_interaction(
    response: Response,
    id: UUID = _ID,
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UmlDeleteResult:
    """Delete an interaction with its diagram occurrences and trace links."""
    return await _delete(
        response,
        session,
        "interaction",
        id,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


# ---------------------------------------------------------------------------
# Diagrams
# ---------------------------------------------------------------------------


@router.get(
    "/diagrams",
    response_model=list[UmlDiagramOut],
    operation_id="list_uml_diagrams",
    responses=AUTH_RESPONSES,
)
async def list_uml_diagrams(
    response: Response,
    model_id: UUID | None = _MODEL_Q,
    kind: str | None = Query(None, max_length=20, description="class | component | sequence"),
    project_id: str | None = _PROJECT_Q,
    limit: int = _LIMIT,
    offset: int = _OFFSET,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[Any]:
    """Diagrams visible to the caller, newest first."""
    rows, total = await svc.list_rows(
        session,
        "diagram",
        tenant_id,
        subject_id,
        filters={"model_id": model_id, "kind": kind, "project_id": project_id},
        limit=limit,
        offset=offset,
    )
    _total(response, total)
    return [svc.to_out("diagram", r) for r in rows]


@router.post(
    "/diagrams",
    response_model=UmlDiagramOut,
    status_code=201,
    operation_id="create_uml_diagram",
    responses=_WRITE,
)
async def create_uml_diagram(
    response: Response,
    payload: UmlDiagramCreate,
    idempotency_key: str | None = Depends(idempotency_key_header),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Create a class, component or sequence diagram (201)."""
    return await _create(
        response,
        session,
        "diagram",
        "/uml/diagrams",
        payload,
        UmlDiagramOut,
        idempotency_key=idempotency_key,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
    )


@router.get(
    "/diagrams/{id}",
    response_model=UmlDiagramOut,
    operation_id="get_uml_diagram",
    responses=_READ,
)
async def get_uml_diagram(
    id: UUID = _ID,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """One diagram (its occurrences: ``…/objects`` and ``…/connections``)."""
    return svc.to_out("diagram", await svc.get_diagram(session, id, tenant_id, subject_id))


@router.put(
    "/diagrams/{id}",
    response_model=UmlDiagramOut,
    operation_id="update_uml_diagram",
    responses=_WRITE,
)
async def update_uml_diagram(
    response: Response,
    id: UUID = _ID,
    payload: UmlDiagramUpdate = Body(...),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Replace a diagram's kind, name and documentation."""
    return await _update(
        response,
        session,
        "diagram",
        id,
        payload,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


@router.delete(
    "/diagrams/{id}",
    operation_id="delete_uml_diagram",
    response_model=UmlDeleteResult,
    responses=_WRITE,
)
async def delete_uml_diagram(
    response: Response,
    id: UUID = _ID,
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UmlDeleteResult:
    """Delete a diagram with its occurrences (the concepts it shows stay)."""
    return await _delete(
        response,
        session,
        "diagram",
        id,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


# -- occurrences -------------------------------------------------------------------


_OCCURRENCE_OUT: dict[str, type[BaseModel]] = {
    "diagram_object": UmlDiagramObjectOut,
    "diagram_connection": UmlDiagramConnectionOut,
}
_OCCURRENCE_PATH = {"diagram_object": "objects", "diagram_connection": "connections"}


async def _occurrence_write(
    response: Response,
    session: AsyncSession,
    kind: str,
    diagram_id: UUID,
    row_id: UUID | None,
    payload: BaseModel | None,
    *,
    if_match: int | None,
    tenant_id: str,
    subject_id: str,
    idempotency_key: str | None = None,
) -> Any:
    diagram = await svc.get_diagram(session, diagram_id, tenant_id, subject_id)
    if row_id is None:
        assert payload is not None
        out_schema = _OCCURRENCE_OUT[kind]
        idem = Idempotency(
            session,
            route=f"/uml/diagrams/{{id}}/{_OCCURRENCE_PATH[kind]}",
            tenant_id=tenant_id,
            subject_id=subject_id,
            visibility=diagram.visibility,
            key=idempotency_key,
            # The diagram is part of the request: the same key on another diagram conflicts.
            body={"diagram_id": str(diagram.id), **payload.model_dump(mode="json")},
        )
        if (replay := await idem.begin()) is not None:
            response.status_code = replay.status_code
            return out_schema.model_validate(replay.body)
        model = await svc.writable_model(
            session, diagram.model_id, tenant_id, subject_id, if_match, kind="diagram"
        )
        async with idem.guard():
            out: Any = await svc.create(
                session,
                kind,
                model,
                payload,
                subject_id=subject_id,
                diagram_id=str(diagram.id),
                before_commit=idem.stager(201, out_schema),
            )
        models_svc.set_etag(response, session)
        return out
    model = await svc.writable_model(
        session, diagram.model_id, tenant_id, subject_id, if_match, kind="diagram"
    )
    row = await svc.get_occurrence(session, kind, diagram, row_id)
    if payload is None:
        changed: Any = await svc.delete(session, kind, model, row, subject_id=subject_id)
    else:
        changed = await svc.update(session, kind, model, row, payload, subject_id=subject_id)
    models_svc.set_etag(response, session)
    return changed


@router.get(
    "/diagrams/{id}/objects",
    response_model=list[UmlDiagramObjectOut],
    operation_id="list_uml_diagram_objects",
    responses=_READ,
)
async def list_uml_diagram_objects(
    response: Response,
    id: UUID = Path(..., description="Diagram ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[Any]:
    """Every object of the diagram in z-order (`position`, then id — the manifest's order)."""
    diagram = await svc.get_diagram(session, id, tenant_id, subject_id)
    rows = await svc.list_occurrences(session, "diagram_object", diagram)
    _total(response, len(rows))
    return [svc.to_out("diagram_object", r) for r in rows]


@router.post(
    "/diagrams/{id}/objects",
    response_model=UmlDiagramObjectOut,
    status_code=201,
    operation_id="create_uml_diagram_object",
    responses=_WRITE,
)
async def create_uml_diagram_object(
    response: Response,
    id: UUID = Path(..., description="Diagram ID"),
    payload: UmlDiagramObjectIn = Body(...),
    idempotency_key: str | None = Depends(idempotency_key_header),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Show an element (class/component diagram) or a lifeline (sequence diagram) (201).
    The same element may be shown on many diagrams; it keeps one identity."""
    return await _occurrence_write(
        response,
        session,
        "diagram_object",
        id,
        None,
        payload,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
        idempotency_key=idempotency_key,
    )


@router.put(
    "/diagrams/{id}/objects/{object_id}",
    response_model=UmlDiagramObjectOut,
    operation_id="update_uml_diagram_object",
    responses=_WRITE,
)
async def update_uml_diagram_object(
    response: Response,
    id: UUID = Path(..., description="Diagram ID"),
    object_id: UUID = Path(..., description="Diagram object ID"),
    payload: UmlDiagramObjectIn = Body(...),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Replace what an object shows, its bounds and style; `position` moves it in z-order."""
    return await _occurrence_write(
        response,
        session,
        "diagram_object",
        id,
        object_id,
        payload,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


@router.delete(
    "/diagrams/{id}/objects/{object_id}",
    operation_id="delete_uml_diagram_object",
    response_model=UmlDeleteResult,
    responses=_WRITE,
)
async def delete_uml_diagram_object(
    response: Response,
    id: UUID = Path(..., description="Diagram ID"),
    object_id: UUID = Path(..., description="Diagram object ID"),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UmlDeleteResult:
    """Delete an object and the connections attached to it."""
    return UmlDeleteResult.model_validate(
        await _occurrence_write(
            response,
            session,
            "diagram_object",
            id,
            object_id,
            None,
            if_match=if_match,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
    )


@router.get(
    "/diagrams/{id}/connections",
    response_model=list[UmlDiagramConnectionOut],
    operation_id="list_uml_diagram_connections",
    responses=_READ,
)
async def list_uml_diagram_connections(
    response: Response,
    id: UUID = Path(..., description="Diagram ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[Any]:
    """Every connection of the diagram, ordered by id."""
    diagram = await svc.get_diagram(session, id, tenant_id, subject_id)
    rows = await svc.list_occurrences(session, "diagram_connection", diagram)
    _total(response, len(rows))
    return [svc.to_out("diagram_connection", r) for r in rows]


@router.post(
    "/diagrams/{id}/connections",
    response_model=UmlDiagramConnectionOut,
    status_code=201,
    operation_id="create_uml_diagram_connection",
    responses=_WRITE,
)
async def create_uml_diagram_connection(
    response: Response,
    id: UUID = Path(..., description="Diagram ID"),
    payload: UmlDiagramConnectionIn = Body(...),
    idempotency_key: str | None = Depends(idempotency_key_header),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Show a relationship or message between the objects showing its ends (201,
    ``UML-DIA-002``)."""
    return await _occurrence_write(
        response,
        session,
        "diagram_connection",
        id,
        None,
        payload,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
        idempotency_key=idempotency_key,
    )


@router.put(
    "/diagrams/{id}/connections/{connection_id}",
    response_model=UmlDiagramConnectionOut,
    operation_id="update_uml_diagram_connection",
    responses=_WRITE,
)
async def update_uml_diagram_connection(
    response: Response,
    id: UUID = Path(..., description="Diagram ID"),
    connection_id: UUID = Path(..., description="Diagram connection ID"),
    payload: UmlDiagramConnectionIn = Body(...),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Replace what a connection shows, its ends, bendpoints and style."""
    return await _occurrence_write(
        response,
        session,
        "diagram_connection",
        id,
        connection_id,
        payload,
        if_match=if_match,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


@router.delete(
    "/diagrams/{id}/connections/{connection_id}",
    operation_id="delete_uml_diagram_connection",
    response_model=UmlDeleteResult,
    responses=_WRITE,
)
async def delete_uml_diagram_connection(
    response: Response,
    id: UUID = Path(..., description="Diagram ID"),
    connection_id: UUID = Path(..., description="Diagram connection ID"),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UmlDeleteResult:
    """Delete a connection."""
    return UmlDeleteResult.model_validate(
        await _occurrence_write(
            response,
            session,
            "diagram_connection",
            id,
            connection_id,
            None,
            if_match=if_match,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _too_large(total: int) -> HTTPException:
    limit = get_settings().model_sync_max_concepts
    return HTTPException(
        status_code=422,
        detail={
            "code": models_svc.INPUT_TOO_LARGE,
            "message": (
                f"the model has {total} UML rows and interaction parts; rendering is "
                f"synchronous up to MODEL_SYNC_MAX_CONCEPTS={limit}"
            ),
        },
    )


@router.get(
    "/diagrams/{id}/render",
    operation_id="render_uml_diagram",
    response_class=Response,
    responses={**_READ, **RENDER_RESPONSES},
)
async def render_uml_diagram_route(
    id: UUID = Path(..., description="Diagram ID"),
    fmt: FormatParam = "svg",
    report: ReportParam = False,
    if_none_match: IfNoneMatch = None,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Render the diagram as it is on the draft head (SVG default, PNG, PDF, or a JSON report).

    Diagnostics name what could not be drawn faithfully; rendering never decides validity.
    """
    diagram = await svc.get_diagram(session, id, tenant_id, subject_id)
    # FOR SHARE: the draft read is one state of the model (no write interleaves).
    model = await models_svc.get_model(
        session, diagram.model_id, tenant_id, subject_id, lock="share"
    )
    total = await uml_manifest.size(session, model.id, model.tenant_id)
    if total > get_settings().model_sync_max_concepts:
        raise _too_large(total)
    draft = await uml_manifest.load_draft(session, model.id, model.tenant_id)
    await session.commit()  # no transaction (or connection) held while rendering
    return await _render(draft, str(id), fmt, report, if_none_match, f"diagram-{id}")


async def _render(
    draft: UmlDraft,
    diagram_id: str,
    fmt: Any,
    report: bool,
    if_none_match: str | None,
    filename: str,
) -> Response:
    if diagram_id not in draft.diagrams:
        raise HTTPException(status_code=404, detail="diagram not found")
    # The ETag covers exactly the renderer's input (the UmlSnapshot), so an edit that
    # cannot change the drawing — a diagram's documentation, a trace link — keeps it.
    snapshot = draft.snapshot()
    content = await run_in_threadpool(
        lambda: models_svc.content_hash(models_svc.canonicalize(snapshot.model_dump(mode="json")))
    )
    etag = render_etag(content, diagram_id, UML_RENDERER_VERSION, fmt, report)
    if matches(if_none_match, etag):
        return not_modified(etag, UML_RENDERER_VERSION)
    return await render_response(
        lambda: render_uml_diagram(snapshot, diagram_id),
        fmt=fmt,
        report=report,
        etag=etag,
        renderer_version=UML_RENDERER_VERSION,
        filename=filename,
    )


@revision_render_router.get(
    "/{model_id}/revisions/{rev_id}/diagrams/{diagram_id}/render",
    operation_id="render_model_revision_diagram",
    response_class=Response,
    responses={**_READ, **RENDER_RESPONSES},
)
async def render_model_revision_diagram(
    model_id: UUID = Path(..., description="Model ID"),
    rev_id: UUID = Path(..., description="Revision ID"),
    diagram_id: UUID = Path(..., description="UML diagram ID in that revision"),
    fmt: FormatParam = "svg",
    report: ReportParam = False,
    if_none_match: IfNoneMatch = None,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Render a UML diagram exactly as a sealed revision holds it (immutable, so its ETag
    changes only with the renderer version)."""
    model = await models_svc.get_model(session, model_id, tenant_id, subject_id)
    revision = await models_svc.get_revision(session, model.id, rev_id)
    language, manifest = model.language, revision.manifest
    await session.commit()  # no transaction (or connection) held while rendering
    if language != "uml":
        raise HTTPException(status_code=404, detail="diagram not found")
    draft = UmlDraft.from_manifest(manifest)
    if draft.size() > get_settings().model_sync_max_concepts:
        raise _too_large(draft.size())
    return await _render(
        draft, str(diagram_id), fmt, report, if_none_match, f"diagram-{diagram_id}-rev-{rev_id}"
    )
