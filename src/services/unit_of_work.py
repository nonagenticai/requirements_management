"""A caller-owned transaction the write services honour (the modeling unit of work).

The generic write services commit step by step (``EntityService.create`` commits the row,
then its version snapshot, then its heuristic score). A caller that must run several
service writes as ONE transaction — change-set apply (``services/change_sets.py``) — wraps
them in :func:`caller_owned_transaction`; inside it every service's :func:`commit` only
flushes, and the caller makes the single commit (or rolls everything back).

Plan 2026-10-26 §7.3: "Generic CRUD's intermediate commits must not split new modeling
transactions."
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

_DEPTH = "uow_caller_owned_depth"


@asynccontextmanager
async def caller_owned_transaction(session: AsyncSession) -> AsyncGenerator[None]:
    """Within the block, :func:`commit` flushes; the caller commits once afterwards."""
    depth = session.info.get(_DEPTH, 0)
    session.info[_DEPTH] = depth + 1
    try:
        yield
    finally:
        if depth:
            session.info[_DEPTH] = depth
        else:
            session.info.pop(_DEPTH, None)


def caller_owns_transaction(session: AsyncSession) -> bool:
    return bool(session.info.get(_DEPTH))


async def commit(session: AsyncSession) -> None:
    """A service's commit point: commit, or only flush inside a caller-owned transaction."""
    if caller_owns_transaction(session):
        await session.flush()
    else:
        await session.commit()


def committer(session: AsyncSession) -> Callable[[], Awaitable[None]]:
    async def _commit() -> None:
        await commit(session)

    return _commit
