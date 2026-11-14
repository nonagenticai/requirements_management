"""Bulk delete operations.

⛔ Every route here wipes EVERY visible row of its kind in the caller's tenant
(``DELETE /bulk`` wipes all three kinds). They take no filter, and they are
reachable from the generic n8n dispatcher, whose resource/operation are
expressions. So each one demands an explicit confirmation that names the tenant
being wiped: ``?confirm=<tenant_id>`` or a JSON body ``{"confirm": "<tenant_id>"}``.
A missing or mismatched value is a 400 and NOTHING is deleted: the check is a
dependency that runs before the handler body.

The value must equal the RESOLVED tenant UUID (what the response echoes as
``tenant_id``), not a tenant name. Naming the tenant is the point: a caller that
does not know which tenant it is about to wipe must not be able to wipe it.
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id
from auth.governed_scope import SCOPE_REQUIRED_422, require_conveyor_project_scope
from db import get_session
from routers.responses import AUTH_RESPONSES
from schemas.common import ErrorResponse
from services import decision_service, principle_service, requirement_service

router = APIRouter()


class BulkDeleteConfirm(BaseModel):
    """Optional JSON body for the bulk-delete routes (alternative to ``?confirm=``)."""

    confirm: str | None = Field(
        None, max_length=100, description="Must equal the caller's resolved tenant id"
    )


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


async def require_bulk_confirm(
    tenant_id: str = Depends(get_tenant_id),
    confirm: str | None = Query(
        None,
        max_length=100,
        description="Required. Must equal the caller's resolved tenant id, or nothing is deleted",
    ),
    body: BulkDeleteConfirm | None = Body(None),
) -> str:
    """Refuse (400, nothing deleted) unless ``confirm`` names the caller's tenant."""
    supplied = confirm if confirm is not None else (body.confirm if body else None)
    if not _norm(supplied):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "BULK_DELETE_CONFIRM_REQUIRED",
                "message": (
                    "Bulk delete wipes every visible row in the tenant. "
                    "Pass confirm=<tenant_id> (query or JSON body); nothing was deleted."
                ),
            },
        )
    if _norm(supplied) != _norm(tenant_id):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "BULK_DELETE_CONFIRM_MISMATCH",
                "message": ("confirm does not match the caller's tenant; nothing was deleted."),
            },
        )
    return tenant_id


_CONFIRM_400 = {
    "description": "Missing or mismatched confirm=<tenant_id>; nothing deleted",
    "model": ErrorResponse,
}


@router.delete(
    "/requirements/bulk",
    operation_id="bulk_delete_requirements",
    tags=["Requirements"],
    responses={**AUTH_RESPONSES, 400: _CONFIRM_400, **SCOPE_REQUIRED_422},
)
async def bulk_delete_requirements(
    request: Request,
    tenant_id: str = Depends(require_bulk_confirm),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete all requirements for authenticated user."""
    require_conveyor_project_scope(
        request, kind="requirement", op="bulk_delete", project_scope=None
    )
    count = await requirement_service.bulk_delete(session, tenant_id, subject_id)
    return {
        "status": "deleted",
        "tenant_id": tenant_id,
        "deleted_count": count,
        "entity_type": "requirements",
    }


@router.delete(
    "/principles/bulk",
    operation_id="bulk_delete_principles",
    tags=["Principles"],
    responses={**AUTH_RESPONSES, 400: _CONFIRM_400, **SCOPE_REQUIRED_422},
)
async def bulk_delete_principles(
    request: Request,
    tenant_id: str = Depends(require_bulk_confirm),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete all principles for authenticated user."""
    require_conveyor_project_scope(request, kind="principle", op="bulk_delete", project_scope=None)
    count = await principle_service.bulk_delete(session, tenant_id, subject_id)
    return {
        "status": "deleted",
        "tenant_id": tenant_id,
        "deleted_count": count,
        "entity_type": "principles",
    }


@router.delete(
    "/decisions/bulk",
    operation_id="bulk_delete_decisions",
    tags=["Decisions"],
    responses={**AUTH_RESPONSES, 400: _CONFIRM_400, **SCOPE_REQUIRED_422},
)
async def bulk_delete_decisions(
    request: Request,
    tenant_id: str = Depends(require_bulk_confirm),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete all decisions for authenticated user."""
    require_conveyor_project_scope(request, kind="decision", op="bulk_delete", project_scope=None)
    count = await decision_service.bulk_delete(session, tenant_id, subject_id)
    return {
        "status": "deleted",
        "tenant_id": tenant_id,
        "deleted_count": count,
        "entity_type": "decisions",
    }


@router.delete(
    "/bulk",
    operation_id="bulk_delete_all",
    responses={**AUTH_RESPONSES, 400: _CONFIRM_400},
)
async def bulk_delete_all(
    tenant_id: str = Depends(require_bulk_confirm),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete all entities for authenticated user."""
    req_count = await requirement_service.bulk_delete(session, tenant_id, subject_id)
    prin_count = await principle_service.bulk_delete(session, tenant_id, subject_id)
    dec_count = await decision_service.bulk_delete(session, tenant_id, subject_id)
    return {
        "status": "deleted",
        "tenant_id": tenant_id,
        "total_deleted": req_count + prin_count + dec_count,
        "requirements_deleted": req_count,
        "principles_deleted": prin_count,
        "decisions_deleted": dec_count,
    }
