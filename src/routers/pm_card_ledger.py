"""Card ledger endpoints (R350 (1); migration 2026_11_04_0080), mounted under ``/pm/cards``.

Append-only: ``POST /{planka_card_id}/ledger`` appends a batch, ``GET`` reads the card's entries
in append order. No update or delete route. Writes are PM / n8n only (``PM_ONLY``); a
fr-delegated caller may read.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_tenant_id
from auth.pm_only import PM_ONLY, PM_ONLY_RESPONSES
from db import get_session
from models.pm import CardLedgerEntry
from routers.responses import AUTH_RESPONSES
from schemas.common import ErrorResponse
from schemas.pm_card_ledger import LedgerAppendIn, LedgerEntryOut, LedgerKind

router = APIRouter()

CardId = Annotated[
    str,
    Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="Planka card id"),
]


@router.post(
    "/{planka_card_id}/ledger",
    response_model=list[LedgerEntryOut],
    status_code=status.HTTP_201_CREATED,
    operation_id="append_card_ledger",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        **PM_ONLY_RESPONSES,
        422: {
            "description": "Validation error (incl. ``laya_score``/``accepted``/``reason`` on a fact)",
            "model": ErrorResponse,
        },
    },
)
async def append_card_ledger(
    planka_card_id: CardId,
    payload: LedgerAppendIn,
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[CardLedgerEntry]:
    """Append facts / claims to a Planka card, in order, in one transaction."""
    rows = [
        CardLedgerEntry(
            tenant_id=tenant_id,
            planka_card_id=planka_card_id,
            kind=e.kind,
            key=e.key,
            value=e.value,
            source=e.source.model_dump(),
            laya_score=e.laya_score,
            accepted=e.accepted,
            reason=e.reason,
        )
        for e in payload.entries
    ]
    session.add_all(rows)
    await session.commit()
    for row in rows:
        await session.refresh(row)
    return rows


@router.get(
    "/{planka_card_id}/ledger",
    response_model=list[LedgerEntryOut],
    operation_id="list_card_ledger",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "``after_id`` is not an entry of this card", "model": ErrorResponse},
    },
)
async def list_card_ledger(
    planka_card_id: CardId,
    kind: LedgerKind | None = Query(None, description="Only facts or only claims"),
    limit: int = Query(500, ge=1, le=1000, description="Maximum entries returned"),
    after_id: UUID | None = Query(
        None, description="Return entries after this entry (next page: the last id returned)"
    ),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[CardLedgerEntry]:
    """The card's ledger entries ordered by ``(created_at, id)``, at most ``limit`` of them.

    ``created_at`` is the appending transaction's start time (``now()``), not its commit time:
    the order is by when each append began, and every entry of one batch shares one
    ``created_at`` (``id``, a UUIDv7, orders them within it). Page with ``after_id``.
    """
    scope = (
        CardLedgerEntry.tenant_id == tenant_id,
        CardLedgerEntry.planka_card_id == planka_card_id,
    )
    stmt = select(CardLedgerEntry).where(*scope)
    if kind is not None:
        stmt = stmt.where(CardLedgerEntry.kind == kind)
    if after_id is not None:
        anchor = (
            await session.execute(
                select(CardLedgerEntry.created_at, CardLedgerEntry.id).where(
                    *scope, CardLedgerEntry.id == after_id
                )
            )
        ).one_or_none()
        if anchor is None:
            raise HTTPException(status_code=404, detail="after_id is not an entry of this card")
        stmt = stmt.where(tuple_(CardLedgerEntry.created_at, CardLedgerEntry.id) > tuple_(*anchor))
    stmt = stmt.order_by(CardLedgerEntry.created_at, CardLedgerEntry.id).limit(limit)
    return list((await session.execute(stmt)).scalars())
