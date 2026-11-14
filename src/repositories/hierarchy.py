"""Parent-chain walking and write-time acyclicity enforcement.

WHY THIS MODULE EXISTS, AND WHY IT IS NOT SOMEWHERE ELSE
--------------------------------------------------------
``repositories/visibility.py`` answers exactly one question — "which rows may
this (tenant, subject) pair see" — and nothing else. Acyclicity is a *structural*
invariant of the ``parent_id`` edge with no visibility axis at all: a cycle that
runs through a row the caller cannot see hangs the caller's query just as hard as
one they own. Putting it there would give that module a second, unrelated job.
``services/entity_service.py`` is the generic CRUD engine (and is owned by an
open PR), so it is not the home either.

THE INVARIANT
-------------
``architecture_elements.parent_id`` is a self-referential edge with **no**
database-level guard: there is no CHECK, no trigger, and no exclusion
constraint that can express "the transitive closure of this edge is acyclic".
Postgres cannot enforce this declaratively, so it has to be enforced on write,
in application code, on every path that can set ``parent_id``.

This is the THIRD of three independent guards. The other two live on the read
side in ``routers/architecture_elements.py`` (a ``path uuid[]`` cycle break and
a depth cap in the ancestors CTE). This one is the only guard that stops the
bad data from ever landing; the read-side pair are the ones that keep an
already-corrupt table from taking the process down. They are complements, not
alternatives — see the block comment at the ancestors CTE for why neither read
guard is sufficient on its own, and note that this write guard cannot fix rows
that predate it, which is precisely why the read guards may not be removed once
this one is in place.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Default ceiling on a parent chain for the WRITE path. Deliberately the same
# number as the read-side ``ANCESTORS_MAX_DEPTH`` so that a chain this module
# accepts is always one the ancestors endpoint can return whole, but it is a
# SEPARATE knob: this one is a function default (bound at def time), the read
# one is a module attribute (read at call time). Tests shorten the read cap
# without loosening or tightening the write cap.
DEFAULT_MAX_PARENT_DEPTH = 64

# Ceiling on the READ path (the ancestors CTE). A module attribute, read at call
# time by the router, so it is one knob rather than a value frozen into another
# module's namespace by a from-import — and so a test can shorten it without
# building a 64-deep fixture.
ANCESTORS_MAX_DEPTH = 64


def _cycle_error(chain: list[UUID], *, reason: str) -> HTTPException:
    """422 naming the offending chain, so the caller can see WHICH edge is bad.

    The chain is reported nearest-first starting at the element being written,
    which is the order a human debugging the payload reads it in.
    """
    rendered = " -> ".join(str(c) for c in chain)
    return HTTPException(
        status_code=422,
        detail=f"parent_id would create a cycle in the element hierarchy ({reason}): {rendered}",
    )


async def assert_parent_acyclic(
    session: AsyncSession,
    *,
    model: Any,
    tenant_id: str,
    element_id: UUID | None,
    parent_id: UUID | None,
    max_depth: int = DEFAULT_MAX_PARENT_DEPTH,
) -> None:
    """Raise 422 if setting ``element_id.parent_id = parent_id`` closes a cycle.

    Walks the prospective parent chain upward one hop at a time, tenant-scoped,
    and refuses when it either re-enters ``element_id`` (the cycle this write
    would create) or re-enters a node it has already visited (a cycle that was
    ALREADY in the table above the requested parent — worth refusing too, since
    accepting the write would attach a new subtree to unreadable data).

    ``element_id`` is ``None`` on a create: a row with no id yet cannot be its
    own ancestor, so only the pre-existing-cycle case applies there.

    A parent that does not resolve within the tenant terminates the walk
    cleanly rather than raising. A dangling or cross-tenant ``parent_id`` is a
    different defect with a different fix; this function has exactly one job and
    must not start failing writes for a reason its name does not describe.
    """
    if parent_id is None:
        return

    if element_id is not None and parent_id == element_id:
        raise _cycle_error([element_id, parent_id], reason="an element cannot be its own parent")

    chain: list[UUID] = [element_id] if element_id is not None else []
    seen: set[UUID] = set(chain)
    current: UUID | None = parent_id

    # ``max_depth + 1`` iterations, not ``max_depth``: the None check at the top
    # of the loop is what recognises a chain that ended legitimately, so a chain
    # of exactly ``max_depth`` ancestors needs one extra pass to be ACCEPTED
    # rather than rejected as too deep.
    for _ in range(max_depth + 1):
        if current is None:
            return  # reached a root — the chain is finite and acyclic.

        chain.append(current)
        if current in seen:
            raise _cycle_error(chain, reason="parent chain revisits an element")
        seen.add(current)

        row = (
            await session.execute(
                select(model.id, model.parent_id).where(
                    model.id == current,
                    model.tenant_id == tenant_id,
                )
            )
        ).first()
        if row is None:
            # Parent not visible in this tenant. Not a cycle; not our problem.
            return
        current = row[1]

    raise HTTPException(
        status_code=422,
        detail=(
            f"parent chain exceeds the maximum depth of {max_depth} elements; "
            "refusing the write rather than persisting a hierarchy that cannot "
            "be walked"
        ),
    )
