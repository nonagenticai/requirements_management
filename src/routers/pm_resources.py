"""Resource CRUD endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from auth.pm_only import PM_ONLY, PM_ONLY_RESPONSES
from db import get_session
from routers.responses import AUTH_RESPONSES
from schemas.common import ErrorResponse
from schemas.pm import ResourceIn, ResourceOut
from services import resource_service as svc

router = APIRouter()


@router.post(
    "",
    response_model=ResourceOut,
    operation_id="create_resource",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def create_resource(
    payload: ResourceIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ResourceOut:
    """Create a new resource (idempotent get-or-create when an AA stable_key is set).

    A resource ``title`` is legitimately non-unique (many "Senior Developer"),
    so there is no title-based dedup. When the AA node forwards a deterministic
    ``stable_key`` (cardId + kind + ordinal), a re-run updates the same row in
    place instead of minting a duplicate; the key is tenant-scoped (resources
    have no ``project_id``) and backed by the disjoint partial index
    ``uq_pm_resources_tenant_stable_key``. Without a stable_key it is pure create.
    """
    if payload.stable_key:
        resource, _created = await svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            identity_fields=("stable_key",),
        )
        return resource
    return await svc.create(session, payload, tenant_id, subject_id, visibility=visibility)


@router.get(
    "/{eid}",
    response_model=ResourceOut,
    operation_id="get_resource",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_resource(
    eid: UUID = Path(..., description="Resource ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ResourceOut:
    """Retrieve resource by ID."""
    return await svc.get(session, eid, tenant_id, subject_id, include_system=True)


@router.get(
    "",
    response_model=list[ResourceOut],
    operation_id="list_resources",
    responses=AUTH_RESPONSES,
)
async def list_resources(
    response: Response,
    resource_type: str | None = Query(None, description="Filter by resource type"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ResourceOut]:
    """List resources with optional filters."""
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        resource_type=resource_type,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{eid}",
    response_model=ResourceOut,
    operation_id="update_resource",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def update_resource(
    eid: UUID = Path(..., description="Resource ID"),
    payload: ResourceIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ResourceOut:
    """Update resource."""
    return await svc.update(session, eid, payload, tenant_id, subject_id)


@router.delete(
    "/{eid}",
    operation_id="delete_resource",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
    },
)
async def delete_resource(
    eid: UUID = Path(..., description="Resource ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete resource."""
    return await svc.delete(session, eid, tenant_id, subject_id)
