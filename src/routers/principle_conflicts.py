"""Principle-conflicts read + manual-lifecycle endpoints.

Serves the persisted governance verdicts — the cockpit's ``principle_conflicts``
block reads these. The in-process LLM detectors that wrote them (principle↔principle
eval, procedure↔principle reconciler) were removed on 2026-10-26; this router is
read + a manual dismiss/un-dismiss over the existing rows.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id
from db import get_session
from models.principle_conflicts import PrincipleConflictRecord
from repositories.visibility import scope
from routers.responses import AUTH_RESPONSES
from schemas.common import ErrorResponse
from schemas.principle_conflicts import PrincipleConflictOut, PrincipleConflictPatch

router = APIRouter()

_PATCHABLE_STATUSES = {"dismissed", "open"}


@router.get(
    "",
    response_model=list[PrincipleConflictOut],
    operation_id="list_principle_conflicts",
    responses=AUTH_RESPONSES,
)
async def list_principle_conflicts(
    response: Response,
    principle_id: UUID | None = Query(None, description="Filter by the principle side"),
    counterparty_kind: str | None = Query(None, description="'principle' | 'procedure'"),
    counterparty_id: str | None = Query(
        None, description="Filter by counterparty (e.g. a procedure asset_uuid)"
    ),
    status: str | None = Query(
        None, description="'open' | 'resolved' | 'dismissed' (default: all)"
    ),
    limit: int = Query(200, ge=1, le=500, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[PrincipleConflictOut]:
    """List persisted principle-conflict records for the tenant."""
    query = scope(
        select(PrincipleConflictRecord),
        PrincipleConflictRecord,
        tenant_id=tenant_id,
        subject_id=subject_id or tenant_id,
    )
    if principle_id is not None:
        query = query.where(PrincipleConflictRecord.principle_id == principle_id)
    if counterparty_kind:
        query = query.where(PrincipleConflictRecord.counterparty_kind == counterparty_kind)
    if counterparty_id:
        query = query.where(PrincipleConflictRecord.counterparty_id == counterparty_id)
    if status:
        query = query.where(PrincipleConflictRecord.status == status)

    total = (
        await session.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0
    query = query.order_by(PrincipleConflictRecord.detected_at.desc()).offset(offset).limit(limit)
    rows = (await session.execute(query)).scalars().all()
    response.headers["X-Total-Count"] = str(total)
    return [PrincipleConflictOut.model_validate(r) for r in rows]


@router.patch(
    "/{conflict_id}",
    response_model=PrincipleConflictOut,
    operation_id="update_principle_conflict",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Conflict not found", "model": ErrorResponse},
        422: {"description": "Unsupported status transition", "model": ErrorResponse},
    },
)
async def update_principle_conflict(
    payload: PrincipleConflictPatch,
    conflict_id: UUID = Path(..., description="Conflict record ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PrincipleConflictOut:
    """Manually dismiss (suppress) or un-dismiss a conflict.

    ``dismissed`` is sticky across re-detection (the reconciler will not
    auto-reopen it unless the counterparty content changes). ``resolved`` is
    owned by the reconciler and cannot be set here.
    """
    if payload.status not in _PATCHABLE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(_PATCHABLE_STATUSES)}",
        )
    row = (
        await session.execute(
            scope(
                select(PrincipleConflictRecord).where(PrincipleConflictRecord.id == conflict_id),
                PrincipleConflictRecord,
                tenant_id=tenant_id,
                subject_id=subject_id or tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    row.status = payload.status
    await session.commit()
    await session.refresh(row)
    return PrincipleConflictOut.model_validate(row)
