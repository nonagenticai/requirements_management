"""Benefit CRUD endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from auth.pm_only import PM_ONLY, PM_ONLY_RESPONSES
from db import get_session
from quality_pm import benefit_quality_check
from routers.responses import AUTH_RESPONSES
from schemas.common import ErrorResponse
from schemas.pm import BenefitIn, BenefitOut
from schemas.requirements import QualityReport
from services import benefit_service as svc
from services.quality_service import QualityResult, persist_quality

router = APIRouter()


@router.post(
    "",
    response_model=BenefitOut,
    operation_id="create_benefit",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def create_benefit(
    payload: BenefitIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> BenefitOut:
    """Create a new benefit."""
    return await svc.create(session, payload, tenant_id, subject_id, visibility=visibility)


@router.get(
    "/{eid}",
    response_model=BenefitOut,
    operation_id="get_benefit",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_benefit(
    eid: UUID = Path(..., description="Benefit ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> BenefitOut:
    """Retrieve benefit by ID."""
    return await svc.get(session, eid, tenant_id, subject_id, include_system=True)


@router.get(
    "",
    response_model=list[BenefitOut],
    operation_id="list_benefits",
    responses=AUTH_RESPONSES,
)
async def list_benefits(
    response: Response,
    status: str | None = Query(None, description="Filter by status"),
    benefit_type: str | None = Query(None, description="Filter by benefit type"),
    programme_id: str | None = Query(None, description="Filter by programme"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[BenefitOut]:
    """List benefits with optional filters."""
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        status=status,
        benefit_type=benefit_type,
        programme_id=programme_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{eid}",
    response_model=BenefitOut,
    operation_id="update_benefit",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def update_benefit(
    eid: UUID = Path(..., description="Benefit ID"),
    payload: BenefitIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> BenefitOut:
    """Update benefit."""
    return await svc.update(session, eid, payload, tenant_id, subject_id)


@router.post(
    "/{eid}/quality",
    response_model=QualityReport,
    operation_id="get_benefit_quality",
    dependencies=PM_ONLY,
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        404: {"description": "Benefit not found", "model": ErrorResponse},
    },
)
async def benefit_quality(
    eid: UUID = Path(..., description="Benefit ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityReport:
    """Heuristic benefit quality check + persist score."""
    obj = await svc._get_owned(session, eid, tenant_id, subject_id)
    score, issues, passes = benefit_quality_check(obj)
    await persist_quality(
        session,
        obj,
        QualityResult(score=score, issues=issues, suggestions=[], agent_name=None),
    )
    await session.commit()
    return QualityReport(score=score, issues=issues, passes=passes)


@router.delete(
    "/{eid}",
    operation_id="delete_benefit",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
    },
)
async def delete_benefit(
    eid: UUID = Path(..., description="Benefit ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete benefit."""
    return await svc.delete(session, eid, tenant_id, subject_id)
