"""Deleting a relationship deletes the relationships that end on it (Archi semantics).

ArchiMate lets a relationship end on another relationship (an association to a flow, an
aggregation of a relationship in a junction-free model). When the relationship goes, an
edge ending on it has nothing to end on — Archi deletes it too, recursively. The view
connections that show any deleted relationship go by FK cascade; connections that ended on
those connections are pruned; trace links on them are deleted; every affected model's
``head`` is bumped (Core DELETEs bypass the ORM flush hooks). Deleted ids also leave every
view's legacy ``element_ids`` / ``relationship_ids`` lists (:func:`prune_view_members`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureRelationship
from models.modeling import bump_models, prune_dangling_connections, prune_trace_links_async

_REL = "architecture_relationships"
# A relationship chain is short in practice; the bound only guards a malformed graph.
_MAX_ROUNDS = 100


async def delete_dependent_relationships(
    session: AsyncSession, relationship_ids: Iterable[Any], tenant_id: str
) -> list[UUID]:
    """Delete every relationship ending (transitively) on ``relationship_ids``.

    ``relationship_ids`` themselves are not deleted here (their caller does that). Returns
    the ids deleted.
    """
    frontier = {UUID(str(r)) for r in relationship_ids}
    deleted: list[UUID] = []
    models: set[Any] = set()
    for _ in range(_MAX_ROUNDS):
        if not frontier:
            break
        rows = (
            await session.execute(
                delete(ArchitectureRelationship)
                .where(
                    ArchitectureRelationship.tenant_id == tenant_id,
                    or_(
                        (ArchitectureRelationship.source_entity_type == _REL)
                        & ArchitectureRelationship.source_id.in_(frontier),
                        (ArchitectureRelationship.target_entity_type == _REL)
                        & ArchitectureRelationship.target_id.in_(frontier),
                    ),
                )
                .returning(ArchitectureRelationship.id, ArchitectureRelationship.model_id)
            )
        ).all()
        frontier = {r.id for r in rows}
        deleted += [r.id for r in rows]
        models |= {r.model_id for r in rows if r.model_id is not None}
    if deleted:
        await bump_models(session, models)
        await prune_trace_links_async(session, deleted)
        await session.run_sync(lambda s: prune_dangling_connections(s, models))
        await prune_view_members(session, deleted, tenant_id)
    return deleted


# ``jsonb - text[]`` drops matching string elements of an array; a non-array (NULL/null)
# list is left alone.
_PRUNE_VIEW_MEMBERS = text(
    """
    UPDATE architecture_views SET
      element_ids = CASE WHEN jsonb_typeof(element_ids) = 'array'
                    THEN element_ids - CAST(:ids AS text[]) ELSE element_ids END,
      relationship_ids = CASE WHEN jsonb_typeof(relationship_ids) = 'array'
                         THEN relationship_ids - CAST(:ids AS text[]) ELSE relationship_ids END
    WHERE tenant_id = CAST(:tenant_id AS uuid)
      AND (element_ids ?| CAST(:ids AS text[]) OR relationship_ids ?| CAST(:ids AS text[]))
    RETURNING model_id
    """
)


async def prune_view_members(session: AsyncSession, ids: Iterable[Any], tenant_id: str) -> None:
    """Remove deleted element/relationship ``ids`` from every view's membership lists."""
    keys = sorted({str(i) for i in ids})
    if not keys:
        return
    rows = await session.execute(_PRUNE_VIEW_MEMBERS, {"ids": keys, "tenant_id": tenant_id})
    await bump_models(session, {r.model_id for r in rows})
