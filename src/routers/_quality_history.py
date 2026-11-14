"""Shared body of the ``GET /<entity>/{id}/quality/history`` routes."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from schemas.requirements import QualityHistory
from services.quality_service import entity_type_of, history_item, read_history

HISTORY_LIMIT_DEFAULT = 50
HISTORY_LIMIT_MAX = 500


async def quality_history_response(
    session: AsyncSession, obj: Any, tenant_id: str, limit: int
) -> QualityHistory:
    """History for an entity the caller has ALREADY loaded through ``_get_owned``.

    Ownership is checked by the caller's ``_get_owned`` (404 otherwise); the
    history query is additionally scoped to ``tenant_id`` so it cannot read a
    row the entity lookup did not authorise.
    """
    entity_type = entity_type_of(obj)
    entity_id: UUID = obj.id
    rows = await read_history(
        session, tenant_id=tenant_id, entity_type=entity_type, entity_id=entity_id, limit=limit
    )
    items = [history_item(r) for r in rows]
    return QualityHistory.model_validate(
        {"entity_type": entity_type, "entity_id": str(entity_id), "n": len(items), "items": items}
    )
