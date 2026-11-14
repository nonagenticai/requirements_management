"""Programme CRUD endpoints."""

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from routers.responses import AUTH_RESPONSES
from schemas.common import DeleteResult, ErrorResponse
from schemas.pm import ProgrammeIn, ProgrammeOut
from services import programme_service as svc

router = APIRouter()


@router.post(
    "",
    response_model=ProgrammeOut,
    operation_id="create_programme",
    responses={**AUTH_RESPONSES, 422: {"description": "Validation error", "model": ErrorResponse}},
)
async def create_programme(
    payload: ProgrammeIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProgrammeOut:
    """Create a new programme."""
    return await svc.create(session, payload, tenant_id, subject_id, visibility=visibility)


@router.get(
    "/{eid}",
    response_model=ProgrammeOut,
    operation_id="get_programme",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_programme(
    eid: UUID = Path(..., description="Programme ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProgrammeOut:
    """Retrieve programme by ID."""
    return await svc.get(session, eid, tenant_id, subject_id, include_system=True)


@router.get(
    "",
    response_model=list[ProgrammeOut],
    operation_id="list_programmes",
    responses=AUTH_RESPONSES,
)
async def list_programmes(
    response: Response,
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ProgrammeOut]:
    """List programmes with optional filters."""
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        status=status,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{eid}",
    response_model=ProgrammeOut,
    operation_id="update_programme",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def update_programme(
    eid: UUID = Path(..., description="Programme ID"),
    payload: ProgrammeIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProgrammeOut:
    """Update programme."""
    return await svc.update(session, eid, payload, tenant_id, subject_id)


@router.delete(
    "/{eid}",
    operation_id="delete_programme",
    response_model=DeleteResult,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def delete_programme(
    eid: UUID = Path(..., description="Programme ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete programme."""
    return DeleteResult.model_validate(await svc.delete(session, eid, tenant_id, subject_id))
