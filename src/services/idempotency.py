"""``Idempotency-Key`` for create routes (target spec A12, §6 Identity; :3370).

Concepts are identified by id only; a client makes a create retry-safe with the optional
``Idempotency-Key`` header instead of relying on a title get-or-create:

* same key + same body  → the ORIGINAL response (status and body) is replayed, nothing
  is written;
* same key + other body → ``409 idempotency_conflict``;
* same key + same body while the first request is still in flight →
  ``409 idempotency_in_progress`` (retry later);
* a key older than :data:`IDEMPOTENCY_TTL` is treated as absent (its row is deleted and
  the request proceeds as new);
* no header → no idempotency, a plain create. An empty / whitespace-only / >255-char
  header is ``422``.

Keys are scoped per ``(tenant_id, subject_id, route template, key)``
(``idempotency_keys`` unique index), so one key may be reused on another route, by another
tenant, or by another SUBJECT of the same tenant — a key never replays (and so never leaks
the id of) another caller's create, e.g. a ``X-Visibility: user`` row. The effective
visibility is part of the request hash: the same subject resending the same body and key
with another visibility is ``409 idempotency_conflict``, not a replay of the other one.

The claim INSERT runs under ``SET LOCAL lock_timeout`` (:data:`CLAIM_LOCK_TIMEOUT`,
transaction-scoped — this service runs behind pgbouncer TRANSACTION pooling, so a
session-level ``SET`` would leak to the next client) and resets it to ``DEFAULT``
right after, so the create's own statements are not affected. A claim still blocked on a
concurrent same-key transaction after that is ``409 idempotency_in_progress``.

Usage in a route (claim BEFORE any write; stage INSIDE the create's own transaction)::

    idem = Idempotency(session, route="/architecture/elements", tenant_id=tenant_id,
                       subject_id=subject_id, visibility=visibility,
                       key=idempotency_key, body=payload)
    if (replay := await idem.begin()) is not None:
        response.status_code = replay.status_code
        return OutSchema.model_validate(replay.body)
    out = await svc.create(..., before_commit=idem.stager(200, OutSchema))
    return out

Transaction shape: :meth:`Idempotency.begin` INSERTs the claim (``ON CONFLICT DO
NOTHING``) without committing. The create service runs as ONE transaction when handed a
``before_commit`` hook: it only flushes, awaits the hook with the out model it is about to
return, then commits once. The hook is :meth:`Idempotency.stage`, which UPSERTs the status
and body onto the claim without committing (UPSERT, because a create path that rolls back
and retries internally — slug retry, get-or-create race recovery — may have rolled the
claim away with it). So claim, row and stored response become durable together or not at
all: a refused write leaves no key that blocks the corrected retry, and a crash at any
point leaves either nothing (the retry creates) or a complete key (the retry replays) —
never an in-flight key with a committed row. A concurrent request with the same key blocks
on the unique index until that commit, then sees the completed row and replays it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from models.idempotency import KEY_MAX_LENGTH, IdempotencyKey
from schemas.common import ErrorResponse

IDEMPOTENCY_TTL = timedelta(hours=24)
# How long the claim INSERT may wait on a concurrent same-key transaction (SET LOCAL).
CLAIM_LOCK_TIMEOUT = "5s"
# Expired rows of the caller's tenant garbage-collected per keyed request (bounded).
TENANT_GC_BATCH = 100
LOCK_NOT_AVAILABLE = "55P03"
DEADLOCK_DETECTED = "40P01"
HEADER = "Idempotency-Key"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"
INVALID_IDEMPOTENCY_KEY = "invalid_idempotency_key"

# A create service's ``before_commit`` hook: awaited with the out model it returns, after
# every write is flushed and immediately before its single commit.
BeforeCommit = Callable[[Any], Awaitable[None]]
# A create service's ``reclaim`` hook: awaited right after a mid-request
# ``session.rollback()`` (race / slug recovery), before the service touches any row again.
Reclaim = Callable[[], Awaitable[None]]

IDEMPOTENCY_409: dict[str, Any] = {
    "description": (
        f"`{IDEMPOTENCY_CONFLICT}`: the Idempotency-Key was used with a different body; "
        f"`{IDEMPOTENCY_IN_PROGRESS}`: the first request with this key is still in flight"
    ),
    "model": ErrorResponse,
}


async def idempotency_key_header(
    idempotency_key: Annotated[
        str | None,
        Header(
            alias=HEADER,
            max_length=KEY_MAX_LENGTH,
            description=(
                "Optional. Makes the create retry-safe for 24h: the same key with the same "
                "body replays the original response; the same key with a different body is "
                "409 idempotency_conflict. 1..255 characters, scoped per tenant, caller "
                "and route."
            ),
        ),
    ] = None,
) -> str | None:
    """FastAPI dependency: the validated header value, or ``None`` when absent."""
    if idempotency_key is None:
        return None
    if not idempotency_key.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": INVALID_IDEMPOTENCY_KEY,
                "message": f"{HEADER} must be 1..{KEY_MAX_LENGTH} non-blank characters",
            },
        )
    return idempotency_key


def request_hash(body: Any, *, visibility: str | None = None) -> str:
    """sha256 of the canonical JSON of ``body`` (keys sorted, no whitespace).

    With ``visibility`` the hash covers ``{"body": ..., "visibility": ...}``, so the same
    body created under another visibility is a different request.
    """
    if isinstance(body, BaseModel):
        body = body.model_dump(mode="json")
    if visibility is not None:
        body = {"body": body, "visibility": visibility}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _in_progress() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": IDEMPOTENCY_IN_PROGRESS,
            "message": f"a request with this {HEADER} is still in progress; retry later",
        },
    )


@dataclass(frozen=True)
class Replay:
    status_code: int
    body: Any


class IdempotentReplay(Exception):
    """Raised by :meth:`Idempotency.reclaim` when the key completed while the claim was lost.

    The route catches it and returns ``replay`` exactly as :meth:`Idempotency.begin`'s.
    """

    def __init__(self, replay: Replay) -> None:
        super().__init__(f"replay {replay.status_code}")
        self.replay = replay


def _conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": IDEMPOTENCY_CONFLICT,
            "message": f"this {HEADER} was already used with a different request body",
        },
    )


class Idempotency:
    """One request's view of its ``Idempotency-Key`` (a no-op when the key is ``None``)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        route: str,
        tenant_id: str,
        subject_id: str,
        visibility: str,
        key: str | None,
        body: Any,
        method: str = "POST",
    ) -> None:
        # ``route`` is the route TEMPLATE (``/architecture/elements``), passed by the
        # route itself: routers here are included lazily, so ``scope["route"].path`` is
        # the router-relative ``""``, and the raw path would split a templated route's
        # keys per path parameter.
        self.session = session
        self.tenant_id = tenant_id
        # The resolved caller ("" = tenant-only caller, as ``get_subject_id`` returns it).
        self.subject_id = subject_id
        self.key = key
        self.method = method
        self.route = route
        # Hash NOW: routes mutate the payload (e.g. strip ``code_binding``) before writing.
        self.hash = request_hash(body, visibility=visibility) if key is not None else ""

    def _scope(self) -> list[Any]:
        return [
            IdempotencyKey.tenant_id == self.tenant_id,
            IdempotencyKey.subject_id == self.subject_id,
            IdempotencyKey.route == self.route,
            IdempotencyKey.key == self.key,
        ]

    async def lookup(self) -> Replay | None:
        """Read-only: the stored response of a COMPLETED, unexpired key (409 on another
        body), else ``None`` — nothing is claimed or written, and the read transaction is
        ended when it replays.

        For a create that checks something that may change between the original request
        and its retry (e.g. a size cap over the model head) BEFORE it can claim: a replay
        must answer with the original response, never re-run that check. The create still
        calls :meth:`begin` before writing (an in-flight or absent key falls through here).
        """
        if self.key is None:
            return None
        cutoff = datetime.now(UTC) - IDEMPOTENCY_TTL
        row = (
            await self.session.execute(
                select(IdempotencyKey).where(*self._scope(), IdempotencyKey.created_at >= cutoff)
            )
        ).scalar_one_or_none()
        if row is None or row.status_code is None:
            return None
        if row.request_hash != self.hash:
            await self.session.rollback()
            raise _conflict()
        replay = Replay(status_code=row.status_code, body=row.response_body)
        await self.session.rollback()
        return replay

    async def begin(self) -> Replay | None:
        """Claim the key, or return the stored response to replay. Raises 409 on conflict."""
        if self.key is None:
            return None
        cutoff = datetime.now(UTC) - IDEMPOTENCY_TTL
        # Transaction-scoped ONLY (pgbouncer transaction pooling): reset to DEFAULT below.
        await self.session.execute(text(f"SET LOCAL lock_timeout = '{CLAIM_LOCK_TIMEOUT}'"))
        try:
            await self.session.execute(
                delete(IdempotencyKey).where(*self._scope(), IdempotencyKey.created_at < cutoff)
            )
            # Bounded tenant-wide GC so keys never reused do not accumulate forever.
            # SKIP LOCKED: never wait on (or 409 because of) another request's rows.
            stale = (
                select(IdempotencyKey.id)
                .where(
                    IdempotencyKey.tenant_id == self.tenant_id,
                    IdempotencyKey.created_at < cutoff,
                )
                .limit(TENANT_GC_BATCH)
                .with_for_update(skip_locked=True)
            )
            await self.session.execute(
                delete(IdempotencyKey).where(IdempotencyKey.id.in_(stale.scalar_subquery()))
            )
            claimed = (
                await self.session.execute(
                    insert(IdempotencyKey)
                    .values(
                        tenant_id=self.tenant_id,
                        subject_id=self.subject_id,
                        key=self.key,
                        method=self.method,
                        route=self.route,
                        request_hash=self.hash,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["tenant_id", "subject_id", "route", "key"]
                    )
                    .returning(IdempotencyKey.id)
                )
            ).scalar_one_or_none()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
                raise
            await self.session.rollback()
            raise _in_progress() from exc
        await self.session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
        if claimed is not None:
            return None
        row = (
            await self.session.execute(select(IdempotencyKey).where(*self._scope()))
        ).scalar_one_or_none()
        if row is None or row.status_code is None:
            # In flight (or deleted as stale by a racing request between our INSERT and SELECT).
            await self.session.rollback()
            raise _in_progress()
        if row.request_hash != self.hash:
            await self.session.rollback()
            raise _conflict()
        replay = Replay(status_code=row.status_code, body=row.response_body)
        await self.session.rollback()  # end the read transaction; nothing was written
        return replay

    async def stage(self, status_code: int, out: Any, schema: type[BaseModel]) -> None:
        """Write the response for replay onto the claim, WITHOUT committing.

        Called from inside the create's transaction (``before_commit``), so the caller's
        commit makes it durable together with the row. ``out`` is rendered through the
        route's response ``schema`` exactly as it is returned. No-op without a key.
        """
        if self.key is None:
            return
        body = schema.model_validate(out).model_dump(mode="json")
        resource_id = body.get("id") if isinstance(body, dict) else None
        stmt = insert(IdempotencyKey).values(
            tenant_id=self.tenant_id,
            subject_id=self.subject_id,
            key=self.key,
            method=self.method,
            route=self.route,
            request_hash=self.hash,
            status_code=status_code,
            response_body=body,
            resource_id=UUID(str(resource_id)) if resource_id else None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "subject_id", "route", "key"],
            set_={
                "status_code": stmt.excluded.status_code,
                "response_body": stmt.excluded.response_body,
                "resource_id": stmt.excluded.resource_id,
            },
            where=(IdempotencyKey.status_code.is_(None))
            & (IdempotencyKey.request_hash == stmt.excluded.request_hash),
        )
        await self.session.execute(stmt)

    async def reclaim(self) -> None:
        """Re-establish the claim after a mid-request ``session.rollback()``.

        A race / slug recovery path rolls back its failed INSERT, and that rollback also
        discards this request's uncommitted claim row and its unique-index lock. A
        same-key request could then claim the key, and this request — going on to update
        the survivor row while that one waits on the row lock and this one waits on its
        claim — would deadlock (40P01). So the recovery path calls this FIRST, before it
        touches any row: claimed again → carry on; the key completed meanwhile →
        :class:`IdempotentReplay` (the route replays it); in flight elsewhere → ``409
        idempotency_in_progress``; other body → ``409 idempotency_conflict``. No-op
        without a key.
        """
        if self.key is None:
            return
        replay = await self.begin()
        if replay is not None:
            raise IdempotentReplay(replay)

    def reclaimer(self) -> Reclaim | None:
        """The ``reclaim`` hook for a create service; ``None`` without a key."""
        return None if self.key is None else self.reclaim

    @asynccontextmanager
    async def guard(self) -> AsyncGenerator[None]:
        """Backstop around a keyed create: a deadlock (40P01) is ``409 in_progress``.

        Retryable, unlike the ``500`` it would otherwise be; a same-key retry then replays
        or creates. Unkeyed requests and every other database error are untouched.
        """
        try:
            yield
        except DBAPIError as exc:
            if self.key is None or getattr(exc.orig, "sqlstate", None) != DEADLOCK_DETECTED:
                raise
            await self.session.rollback()
            raise _in_progress() from exc

    def stager(self, status_code: int, schema: type[BaseModel]) -> BeforeCommit | None:
        """A ``before_commit`` hook staging ``status_code``; ``None`` without a key."""
        if self.key is None:
            return None

        async def hook(out: Any) -> None:
            await self.stage(status_code, out, schema)

        return hook
