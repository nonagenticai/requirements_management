"""Architecture views CRUD endpoints."""

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from models.architecture import ArchitectureView
from quality_arch import view_quality_check
from routers._quality_history import (
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    quality_history_response,
)
from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.architecture import ArchitectureViewIn, ArchitectureViewOut
from schemas.common import DeleteResult, ErrorResponse, VersionComparison, VersionInfo
from schemas.modeling_lifecycle import (
    ViewConnectionIn,
    ViewConnectionOut,
    ViewObjectIn,
    ViewObjectOut,
)
from schemas.requirements import QualityHistory, QualityReport
from services import architecture_view_service as svc
from services import version_service
from services import view_occurrences as occ
from services.idempotency import IDEMPOTENCY_409, Idempotency, idempotency_key_header
from services.modeling_models import (
    STALE_HEAD_412,
    check_if_match,
    if_match_header,
    model_visible,
    resolve_create_model,
    row_model_id,
    set_etag,
)
from services.quality_service import QualityResult, persist_quality

router = APIRouter(route_class=ErrorEnvelopeRoute)


@router.post(
    "",
    response_model=ArchitectureViewOut,
    operation_id="create_architecture_view",
    responses={
        **AUTH_RESPONSES,
        409: IDEMPOTENCY_409,
        412: STALE_HEAD_412,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def create_architecture_view(
    response: Response,
    payload: ArchitectureViewIn,
    idempotency_key: str | None = Depends(idempotency_key_header),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureViewOut:
    """Create a new architecture view.

    Views are identified by id only (target spec A12): two views may share a title.
    Make a retry safe with the optional ``Idempotency-Key`` header.
    """
    idem = Idempotency(
        session,
        route="/architecture/views",
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        key=idempotency_key,
        body={**payload.model_dump(mode="json"), "model_id": payload.model_id},
    )
    if (replay := await idem.begin()) is not None:
        response.status_code = replay.status_code
        return ArchitectureViewOut.model_validate(replay.body)
    model_id = await resolve_create_model(
        session,
        model_id=payload.model_id,
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        project_id=payload.project_id,
        if_match=if_match,
    )
    async with idem.guard():
        out = await svc.create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            extra_columns={"model_id": model_id} if model_id is not None else None,
            before_commit=idem.stager(200, ArchitectureViewOut),
        )
    set_etag(response, session)
    return out


@router.get(
    "/{vid}",
    response_model=ArchitectureViewOut,
    operation_id="get_architecture_view",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "View not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_architecture_view(
    vid: UUID = Path(..., description="Architecture view ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureViewOut:
    """Retrieve architecture view by ID."""
    return await svc.get(session, vid, tenant_id, subject_id, include_system=True)


@router.get(
    "",
    response_model=list[ArchitectureViewOut],
    operation_id="list_architecture_views",
    responses=AUTH_RESPONSES,
)
async def list_architecture_views(
    response: Response,
    viewpoint: str | None = Query(None, description="Filter by viewpoint"),
    project_id: str | None = Query(None, description="Filter by project"),
    model_id: UUID | None = Query(
        None,
        description=(
            "Filter to the views of this ArchiMate model (the view's `model_id`). "
            "Only a model the caller may read (`GET /modeling/models/{id}`) filters to "
            "anything: another tenant's model (the system tenant's included) or another "
            "subject's private model returns an empty list."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ArchitectureViewOut]:
    """List architecture views with optional filters."""
    if model_id is not None and not await model_visible(session, model_id, tenant_id, subject_id):
        # Rows are listed under a model only for a caller who may read the model itself.
        response.headers["X-Total-Count"] = "0"
        return []
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        viewpoint=viewpoint,
        project_id=project_id,
        model_id=model_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{vid}",
    response_model=ArchitectureViewOut,
    operation_id="update_architecture_view",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "View not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        412: STALE_HEAD_412,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def update_architecture_view(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    payload: ArchitectureViewIn = Body(...),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureViewOut:
    """Update architecture view. ``model_id`` is fixed at create and ignored here."""
    await check_if_match(
        session,
        await row_model_id(session, ArchitectureView, vid, tenant_id, subject_id=subject_id),
        if_match,
    )
    out = await svc.update(session, vid, payload, tenant_id, subject_id)
    set_etag(response, session)
    return out


@router.post(
    "/{vid}/quality",
    response_model=QualityReport,
    operation_id="get_architecture_view_quality",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "View not found", "model": ErrorResponse},
    },
)
async def architecture_view_quality(
    vid: UUID = Path(..., description="Architecture view ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityReport:
    """Run heuristic quality checks and persist score onto the view."""
    obj = await svc._get_owned(session, vid, tenant_id, subject_id)
    score, issues, passes = view_quality_check(obj)
    await persist_quality(
        session,
        obj,
        QualityResult(score=score, issues=issues, suggestions=[], agent_name=None),
    )
    await session.commit()
    return QualityReport(score=score, issues=issues, passes=passes)


@router.get(
    "/{vid}/quality/history",
    response_model=QualityHistory,
    operation_id="get_architecture_view_quality_history",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Architecture view not found", "model": ErrorResponse},
    },
)
async def architecture_view_quality_history(
    vid: UUID = Path(..., description="Architecture view ID"),
    limit: int = Query(HISTORY_LIMIT_DEFAULT, ge=1, le=HISTORY_LIMIT_MAX),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityHistory:
    """Every persisted quality evaluation of this architecture view, newest first.

    Append-only history behind the entity's ``quality_score`` (which stays the
    latest). Advisory data only — nothing gates on it.
    """
    obj = await svc._get_owned(session, vid, tenant_id, subject_id)
    return await quality_history_response(session, obj, tenant_id, limit)


# -- Version endpoints --


@router.get(
    "/{vid}/version",
    response_model=VersionInfo,
    operation_id="get_architecture_view_version_info",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "View not found", "model": ErrorResponse},
    },
)
async def get_architecture_view_version_info(
    vid: UUID = Path(..., description="Architecture view ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionInfo:
    """Get architecture view version info."""
    obj = await svc._get_owned(session, vid, tenant_id, subject_id)
    return await version_service.get_info(obj)


@router.get(
    "/{vid}/versions",
    response_model=list[VersionInfo],
    operation_id="list_architecture_view_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "View not found", "model": ErrorResponse},
    },
)
async def list_architecture_view_versions(
    vid: UUID = Path(..., description="Architecture view ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[VersionInfo]:
    """List architecture view version history."""
    obj = await svc._get_owned(session, vid, tenant_id, subject_id)
    return await version_service.list_versions(session, "architecture_view", vid, obj)


@router.get(
    "/{vid}/versions/{v1}/compare/{v2}",
    response_model=VersionComparison,
    operation_id="compare_architecture_view_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "View or version not found", "model": ErrorResponse},
        422: {"description": "Invalid version format", "model": ErrorResponse},
    },
)
async def compare_architecture_view_versions(
    vid: UUID = Path(..., description="Architecture view ID"),
    v1: str = Path(..., description="First version"),
    v2: str = Path(..., description="Second version"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionComparison:
    """Compare architecture view versions using stored snapshots."""
    await svc._get_owned(session, vid, tenant_id, subject_id)
    return await version_service.compare(session, "architecture_view", vid, v1, v2)


@router.delete(
    "/{vid}",
    operation_id="delete_architecture_view",
    response_model=DeleteResult,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "View not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        412: STALE_HEAD_412,
    },
)
async def delete_architecture_view(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete architecture view (its occurrences go with it)."""
    await check_if_match(
        session,
        await row_model_id(session, ArchitectureView, vid, tenant_id, subject_id=subject_id),
        if_match,
    )
    out = await svc.delete(session, vid, tenant_id, subject_id)
    set_etag(response, session)
    return DeleteResult.model_validate(out)


# -- View occurrences (target spec §8.1) --

_OCC_404 = {"description": "View or occurrence not found", "model": ErrorResponse}
_OCC_WRITE = {
    **AUTH_RESPONSES,
    404: _OCC_404,
    412: STALE_HEAD_412,
    422: {"description": "`model_integrity` (AM-REF-001, AM-VIEW-002, AM-VIEW-003)"},
}


async def _writable_view(
    session: AsyncSession, vid: UUID, tenant_id: str, subject_id: str, if_match: int | None
) -> ArchitectureView:
    view = await occ.get_view(session, vid, tenant_id, subject_id, mutable=True)
    await check_if_match(session, view.model_id, if_match)
    return view


@router.get(
    "/{vid}/objects",
    response_model=list[ViewObjectOut],
    operation_id="list_architecture_view_objects",
    responses={**AUTH_RESPONSES, 404: _OCC_404},
)
async def list_architecture_view_objects(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ViewObjectOut]:
    """Every object (element occurrence, note, group) of the view in z-order (`position`,
    then id — the manifest's order)."""
    view = await occ.get_view(session, vid, tenant_id, subject_id, mutable=False)
    rows = await occ.list_objects(session, view)
    response.headers["X-Total-Count"] = str(len(rows))
    return [ViewObjectOut.model_validate(r) for r in rows]


@router.post(
    "/{vid}/objects",
    response_model=ViewObjectOut,
    status_code=201,
    operation_id="create_architecture_view_object",
    responses=_OCC_WRITE,
)
async def create_architecture_view_object(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    payload: ViewObjectIn = Body(...),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ViewObjectOut:
    """Show an element of the view's model in the view (201)."""
    view = await _writable_view(session, vid, tenant_id, subject_id, if_match)
    row = await occ.create_object(
        session, view, payload, tenant_id=tenant_id, subject_id=subject_id
    )
    set_etag(response, session)
    return ViewObjectOut.model_validate(row)


@router.put(
    "/{vid}/objects/{object_id}",
    response_model=ViewObjectOut,
    operation_id="update_architecture_view_object",
    responses=_OCC_WRITE,
)
async def update_architecture_view_object(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    object_id: UUID = Path(..., description="View object ID"),
    payload: ViewObjectIn = Body(...),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ViewObjectOut:
    """Replace an object's concept, visual parent, bounds and style; `position` moves it in
    z-order."""
    view = await _writable_view(session, vid, tenant_id, subject_id, if_match)
    row = await occ.update_object(
        session, view, object_id, payload, tenant_id=tenant_id, subject_id=subject_id
    )
    set_etag(response, session)
    return ViewObjectOut.model_validate(row)


@router.delete(
    "/{vid}/objects/{object_id}",
    operation_id="delete_architecture_view_object",
    response_model=DeleteResult,
    responses={**AUTH_RESPONSES, 404: _OCC_404, 412: STALE_HEAD_412},
)
async def delete_architecture_view_object(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    object_id: UUID = Path(..., description="View object ID"),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete an object with its nested objects and every connection attached to them."""
    view = await _writable_view(session, vid, tenant_id, subject_id, if_match)
    await occ.delete_object(session, view, object_id)
    set_etag(response, session)
    return DeleteResult(status="deleted", id=object_id)


@router.get(
    "/{vid}/connections",
    response_model=list[ViewConnectionOut],
    operation_id="list_architecture_view_connections",
    responses={**AUTH_RESPONSES, 404: _OCC_404},
)
async def list_architecture_view_connections(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ViewConnectionOut]:
    """Every connection (relationship occurrence) of the view, ordered by id."""
    view = await occ.get_view(session, vid, tenant_id, subject_id, mutable=False)
    rows = await occ.list_connections(session, view)
    response.headers["X-Total-Count"] = str(len(rows))
    return [ViewConnectionOut.model_validate(r) for r in rows]


@router.post(
    "/{vid}/connections",
    response_model=ViewConnectionOut,
    status_code=201,
    operation_id="create_architecture_view_connection",
    responses=_OCC_WRITE,
)
async def create_architecture_view_connection(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    payload: ViewConnectionIn = Body(...),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ViewConnectionOut:
    """Show a relationship between two occurrences of its ends (201, AM-VIEW-002)."""
    view = await _writable_view(session, vid, tenant_id, subject_id, if_match)
    row = await occ.create_connection(
        session, view, payload, tenant_id=tenant_id, subject_id=subject_id
    )
    set_etag(response, session)
    return ViewConnectionOut.model_validate(row)


@router.put(
    "/{vid}/connections/{connection_id}",
    response_model=ViewConnectionOut,
    operation_id="update_architecture_view_connection",
    responses=_OCC_WRITE,
)
async def update_architecture_view_connection(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    connection_id: UUID = Path(..., description="View connection ID"),
    payload: ViewConnectionIn = Body(...),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ViewConnectionOut:
    """Replace a connection's relationship, ends, bendpoints and style."""
    view = await _writable_view(session, vid, tenant_id, subject_id, if_match)
    row = await occ.update_connection(
        session, view, connection_id, payload, tenant_id=tenant_id, subject_id=subject_id
    )
    set_etag(response, session)
    return ViewConnectionOut.model_validate(row)


@router.delete(
    "/{vid}/connections/{connection_id}",
    operation_id="delete_architecture_view_connection",
    response_model=DeleteResult,
    responses={**AUTH_RESPONSES, 404: _OCC_404, 412: STALE_HEAD_412},
)
async def delete_architecture_view_connection(
    response: Response,
    vid: UUID = Path(..., description="Architecture view ID"),
    connection_id: UUID = Path(..., description="View connection ID"),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete a connection and every connection that ends on it."""
    view = await _writable_view(session, vid, tenant_id, subject_id, if_match)
    await occ.delete_connection(session, view, connection_id)
    set_etag(response, session)
    return DeleteResult(status="deleted", id=connection_id)
