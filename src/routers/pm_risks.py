"""Risk CRUD endpoints."""

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from auth.pm_only import PM_ONLY, PM_ONLY_RESPONSES
from db import get_session
from quality_pm import risk_quality_check
from routers._quality_history import (
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    quality_history_response,
)
from routers.responses import AUTH_RESPONSES
from schemas.common import DeleteResult, ErrorResponse
from schemas.pm import RiskIn, RiskOut
from schemas.requirements import QualityHistory, QualityReport
from services import risk_service as svc
from services.quality_service import QualityResult, persist_quality

router = APIRouter()


@router.post(
    "",
    response_model=RiskOut,
    operation_id="create_risk",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def create_risk(
    payload: RiskIn,
    response: Response,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RiskOut:
    """Create a new risk.

    When ``project_id`` is set this is a get-or-create keyed on
    ``(tenant_id, project_id, title)`` so an AA-intake re-run updates the
    existing risk in place instead of minting a duplicate (best-effort gate
    backed by the WS1 partial unique index).
    """
    if payload.project_id:
        obj, created = await svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            identity_fields=("project_id", "title"),
        )
        response.status_code = 201 if created else 200
        return obj
    return await svc.create(session, payload, tenant_id, subject_id, visibility=visibility)


@router.get(
    "/{eid}",
    response_model=RiskOut,
    operation_id="get_risk",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_risk(
    eid: UUID = Path(..., description="Risk ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RiskOut:
    """Retrieve risk by ID."""
    return await svc.get(session, eid, tenant_id, subject_id, include_system=True)


@router.get(
    "",
    response_model=list[RiskOut],
    operation_id="list_risks",
    responses=AUTH_RESPONSES,
)
async def list_risks(
    response: Response,
    status: str | None = Query(None, description="Filter by status"),
    category: str | None = Query(None, description="Filter by category"),
    project_id: str | None = Query(None, description="Filter by project"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[RiskOut]:
    """List risks with optional filters."""
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        status=status,
        category=category,
        project_id=project_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{eid}",
    response_model=RiskOut,
    operation_id="update_risk",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def update_risk(
    eid: UUID = Path(..., description="Risk ID"),
    payload: RiskIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RiskOut:
    """Update risk."""
    return await svc.update(session, eid, payload, tenant_id, subject_id)


@router.post(
    "/{eid}/quality",
    response_model=QualityReport,
    operation_id="get_risk_quality",
    dependencies=PM_ONLY,
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        404: {"description": "Risk not found", "model": ErrorResponse},
    },
)
async def risk_quality(
    eid: UUID = Path(..., description="Risk ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityReport:
    """Heuristic risk quality check + persist score."""
    obj = await svc._get_owned(session, eid, tenant_id, subject_id)
    score, issues, passes = risk_quality_check(obj)
    await persist_quality(
        session,
        obj,
        QualityResult(score=score, issues=issues, suggestions=[], agent_name=None),
    )
    await session.commit()
    return QualityReport(score=score, issues=issues, passes=passes)


@router.get(
    "/{eid}/quality/history",
    response_model=QualityHistory,
    operation_id="get_risk_quality_history",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Risk not found", "model": ErrorResponse},
    },
)
async def risk_quality_history(
    eid: UUID = Path(..., description="Risk ID"),
    limit: int = Query(HISTORY_LIMIT_DEFAULT, ge=1, le=HISTORY_LIMIT_MAX),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityHistory:
    """Every persisted quality evaluation of this risk, newest first.

    Append-only history behind the entity's ``quality_score`` (which stays the
    latest). Advisory data only — nothing gates on it.
    """
    obj = await svc._get_owned(session, eid, tenant_id, subject_id)
    return await quality_history_response(session, obj, tenant_id, limit)


@router.delete(
    "/{eid}",
    operation_id="delete_risk",
    response_model=DeleteResult,
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
    },
)
async def delete_risk(
    eid: UUID = Path(..., description="Risk ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete risk."""
    return DeleteResult.model_validate(await svc.delete(session, eid, tenant_id, subject_id))
