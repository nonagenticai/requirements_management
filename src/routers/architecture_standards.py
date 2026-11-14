"""Architecture standards CRUD endpoints."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from models.architecture import ArchitectureElement, Standard
from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.architecture import StandardIn, StandardOut
from schemas.common import DeleteResult, ErrorResponse, VersionComparison, VersionInfo
from services import architecture_standard_service as svc
from services import version_service
from services.idempotency import IDEMPOTENCY_409, Idempotency, idempotency_key_header

# Layer → standard_type mapping for applicable-standards lookups.
# Keys are ArchitectureElement.layer values; values are the
# Standard.standard_type values that are considered applicable.
LAYER_TO_STANDARD_TYPES: dict[str, list[str]] = {
    "business": ["business_standard", "data_standard", "guideline"],
    "application": ["application_standard", "data_standard", "guideline"],
    "technology": ["technology_standard", "guideline"],
    "physical": ["technology_standard", "guideline"],
    "strategy": ["business_standard", "guideline"],
    "implementation_migration": ["guideline"],
    "motivation": ["business_standard", "guideline"],
}

router = APIRouter(route_class=ErrorEnvelopeRoute)


@router.get(
    "/element/{eid}/applicable-standards",
    response_model=list[StandardOut],
    operation_id="list_applicable_standards_for_element",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
    },
)
async def applicable_standards_for_element(
    eid: UUID = Path(..., description="Architecture element ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[StandardOut]:
    """Return the standards that apply to an element based on its layer.

    Filters out expired standards (``valid_until`` < today) and standards
    with ``compliance_level=optional`` — only mandatory and recommended
    rows are surfaced. Returns ``[]`` for layers that have no mapped
    ``standard_type`` values.
    """
    el = await session.get(ArchitectureElement, eid)
    if not el or el.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="not found")

    types = LAYER_TO_STANDARD_TYPES.get(el.layer, [])
    if not types:
        return []

    now = datetime.now(timezone.utc)
    rows = await session.execute(
        select(Standard).where(
            Standard.tenant_id == tenant_id,
            Standard.standard_type.in_(types),
            or_(Standard.valid_from.is_(None), Standard.valid_from <= now),
            or_(Standard.valid_until.is_(None), Standard.valid_until >= now),
            Standard.compliance_level.in_(("mandatory", "recommended")),
        )
    )
    return [StandardOut.model_validate(r) for r in rows.scalars().all()]


@router.post(
    "",
    response_model=StandardOut,
    operation_id="create_architecture_standard",
    responses={
        **AUTH_RESPONSES,
        409: IDEMPOTENCY_409,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def create_architecture_standard(
    response: Response,
    payload: StandardIn,
    idempotency_key: str | None = Depends(idempotency_key_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> StandardOut:
    """Create a new architecture standard.

    Standards are identified by id only (target spec A12): two standards may share a
    title. Make a retry safe with the optional ``Idempotency-Key`` header.
    """
    idem = Idempotency(
        session,
        route="/architecture/standards",
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        key=idempotency_key,
        body=payload,
    )
    if (replay := await idem.begin()) is not None:
        response.status_code = replay.status_code
        return StandardOut.model_validate(replay.body)
    async with idem.guard():
        return await svc.create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            before_commit=idem.stager(200, StandardOut),
        )


@router.get(
    "/{sid}",
    response_model=StandardOut,
    operation_id="get_architecture_standard",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Standard not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_architecture_standard(
    sid: UUID = Path(..., description="Architecture standard ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> StandardOut:
    """Retrieve architecture standard by ID."""
    return await svc.get(session, sid, tenant_id, subject_id, include_system=True)


@router.get(
    "",
    response_model=list[StandardOut],
    operation_id="list_architecture_standards",
    responses=AUTH_RESPONSES,
)
async def list_architecture_standards(
    response: Response,
    standard_type: str | None = Query(None, description="Filter by standard type"),
    status: str | None = Query(None, description="Filter by status"),
    compliance_level: str | None = Query(None, description="Filter by compliance level"),
    project_id: str | None = Query(None, description="Filter by project"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[StandardOut]:
    """List architecture standards with optional filters."""
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        standard_type=standard_type,
        status=status,
        compliance_level=compliance_level,
        project_id=project_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{sid}",
    response_model=StandardOut,
    operation_id="update_architecture_standard",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Standard not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def update_architecture_standard(
    sid: UUID = Path(..., description="Architecture standard ID"),
    payload: StandardIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> StandardOut:
    """Update architecture standard."""
    return await svc.update(session, sid, payload, tenant_id, subject_id)


@router.get(
    "/{sid}/version",
    response_model=VersionInfo,
    operation_id="get_architecture_standard_version_info",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Standard not found", "model": ErrorResponse},
    },
)
async def get_architecture_standard_version_info(
    sid: UUID = Path(..., description="Architecture standard ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionInfo:
    """Get architecture standard version info."""
    obj = await svc._get_owned(session, sid, tenant_id, subject_id)
    return await version_service.get_info(obj)


@router.get(
    "/{sid}/versions",
    response_model=list[VersionInfo],
    operation_id="list_architecture_standard_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Standard not found", "model": ErrorResponse},
    },
)
async def list_architecture_standard_versions(
    sid: UUID = Path(..., description="Architecture standard ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[VersionInfo]:
    """List architecture standard version history."""
    obj = await svc._get_owned(session, sid, tenant_id, subject_id)
    return await version_service.list_versions(session, "architecture_standard", sid, obj)


@router.get(
    "/{sid}/versions/{v1}/compare/{v2}",
    response_model=VersionComparison,
    operation_id="compare_architecture_standard_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Standard or version not found", "model": ErrorResponse},
        422: {"description": "Invalid version format", "model": ErrorResponse},
    },
)
async def compare_architecture_standard_versions(
    sid: UUID = Path(..., description="Architecture standard ID"),
    v1: str = Path(..., description="First version"),
    v2: str = Path(..., description="Second version"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionComparison:
    """Compare architecture standard versions using stored snapshots."""
    await svc._get_owned(session, sid, tenant_id, subject_id)
    return await version_service.compare(session, "architecture_standard", sid, v1, v2)


@router.delete(
    "/{sid}",
    operation_id="delete_architecture_standard",
    response_model=DeleteResult,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Standard not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def delete_architecture_standard(
    sid: UUID = Path(..., description="Architecture standard ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete architecture standard."""
    return DeleteResult.model_validate(await svc.delete(session, sid, tenant_id, subject_id))
