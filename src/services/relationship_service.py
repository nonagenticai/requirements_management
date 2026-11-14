"""CRUD service for ArchitectureRelationship entities.

Per-user-within-tenant scoping mirrors EntityService — see that module for
the behavior contract.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import sqlalchemy.exc
import structlog
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureRelationship
from repositories.visibility import can_mutate, scope
from services import unit_of_work as uow
from services.archimate_integrity import check_relationship_write
from services.idempotency import BeforeCommit, Reclaim
from services.model_integrity import integrity_error, single_finding
from services.modeling_models import check_relationship_model
from services.relationship_cascade import delete_dependent_relationships, prune_view_members
from services.slug_retry import (
    MAX_SLUG_RETRIES,
    find_slug_constraint_name,
    is_fatal_non_slug_violation,
)
from tools.architecture.registry import get_registry

logger = structlog.get_logger(__name__)


def _resolve_subject(tenant_id: str, subject_id: str) -> str:
    return subject_id or tenant_id


_IDENTITY_KEYS = (
    "relationship_type",
    "source_id",
    "target_id",
    "source_entity_type",
    "target_entity_type",
)
_END_KEYS = ("source_entity_type", "source_id", "target_entity_type", "target_id")


class RelationshipService:
    """Async CRUD service for architecture relationships."""

    def __init__(
        self, model_class: type[ArchitectureRelationship], out_schema: type[BaseModel]
    ) -> None:
        self.model_class = model_class
        self.out_schema = out_schema
        # Cached OPTIONAL fast-path for the concurrent-slug-collision recovery:
        # the model's (tenant_id, slug) UniqueConstraint name
        # (uq_arch_rels_tenant_slug). See services/slug_retry.py.
        self._slug_constraint_name = find_slug_constraint_name(model_class)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _generate_slug(
        self,
        session: AsyncSession,
        source_entity_type: str,
        source_id: UUID,
        target_entity_type: str,
        target_id: UUID,
        relationship_type: str,
        tenant_id: str,
    ) -> str:
        """Build slug; per-tenant uniqueness."""
        src_short = str(source_id)[:8]
        tgt_short = str(target_id)[:8]
        slug_base = (
            f"{source_entity_type}-{src_short}-{relationship_type}-{target_entity_type}-{tgt_short}"
        )
        slug_base = slug_base[:110]
        slug = slug_base
        counter = 1

        existing = await session.scalar(
            select(self.model_class).where(
                self.model_class.slug == slug,
                self.model_class.tenant_id == tenant_id,
            )
        )
        while existing:
            slug = f"{slug_base}-{counter}"
            existing = await session.scalar(
                select(self.model_class).where(
                    self.model_class.slug == slug,
                    self.model_class.tenant_id == tenant_id,
                )
            )
            counter += 1

        return slug

    async def _get_visible(
        self,
        session: AsyncSession,
        entity_id: UUID,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> ArchitectureRelationship:
        sub = _resolve_subject(tenant_id, subject_id)
        stmt = scope(
            select(self.model_class).where(self.model_class.id == entity_id),
            self.model_class,
            tenant_id=tenant_id,
            subject_id=sub,
        )
        entity = (await session.execute(stmt)).scalar_one_or_none()
        if not entity:
            raise HTTPException(status_code=404, detail="not found")
        return entity

    async def _get_mutable(
        self,
        session: AsyncSession,
        entity_id: UUID,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> ArchitectureRelationship:
        sub = _resolve_subject(tenant_id, subject_id)
        entity = await self._get_visible(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        if not can_mutate(entity, subject_id=sub, tenant_id=tenant_id):
            raise HTTPException(status_code=404, detail="not found")
        return entity

    async def _validate_metamodel(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        subject_id: str = "",
        source_entity_type: str,
        source_id: Any,
        target_entity_type: str,
        target_id: Any,
        relationship_type: str,
        properties: dict[str, Any] | None = None,
        relationship_id: Any = None,
        properties_sent: bool = True,
    ) -> None:
        """Enforce ArchiMate 3.2 on an edge whose ends are both ArchiMate concepts.

        Delegates to ``services.archimate_integrity.check_relationship_write``,
        which checks the edge in its loaded neighbourhood (junctions,
        specialization chain, relationship ends) with the same pure validator a
        validation run uses. Invoked from ``create`` — so EVERY caller
        (``get_or_create`` and any non-router service caller, not just the
        HTTP route) is validated — and from ``update`` when an identity key or
        the properties change.
        """
        await check_relationship_write(
            session,
            tenant_id=tenant_id,
            subject_id=subject_id,
            source_entity_type=source_entity_type,
            source_id=source_id,
            target_entity_type=target_entity_type,
            target_id=target_id,
            relationship_type=relationship_type,
            properties=properties,
            relationship_id=relationship_id,
            properties_sent=properties_sent,
        )

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        payload: Any,
        tenant_id: str,
        subject_id: str = "",
        *,
        visibility: str = "tenant",
        before_commit: BeforeCommit | None = None,
    ) -> Any:
        """Insert one edge and commit.

        ``before_commit`` (an ``Idempotency-Key`` route's response staging) is awaited with
        the out model after the INSERT is flushed and refreshed, immediately before the
        single commit, so claim, edge and stored response commit together.
        """
        if visibility not in ("tenant", "user"):
            raise HTTPException(status_code=422, detail=f"invalid visibility: {visibility!r}")
        data = payload.model_dump(mode="json")

        # Metamodel gate — fail fast BEFORE slug generation / insert so an
        # illegal ArchiMate edge never lands (and no wasted slug is minted).
        await self._validate_metamodel(
            session,
            tenant_id=tenant_id,
            subject_id=subject_id,
            source_entity_type=data["source_entity_type"],
            source_id=data["source_id"],
            target_entity_type=data["target_entity_type"],
            target_id=data["target_id"],
            relationship_type=data["relationship_type"],
            properties=data.get("properties"),
        )
        # The edge joins its source end's model (flush hook): a model the caller could
        # create into (own tenant, writable, same visibility), or 422.
        await check_relationship_model(
            session,
            source_entity_type=data["source_entity_type"],
            source_id=data["source_id"],
            target_entity_type=data["target_entity_type"],
            tenant_id=tenant_id,
            visibility=visibility,
            subject_id=_resolve_subject(tenant_id, subject_id),
        )

        slug = await self._generate_slug(
            session,
            source_entity_type=data["source_entity_type"],
            source_id=UUID(str(data["source_id"])),
            target_entity_type=data["target_entity_type"],
            target_id=UUID(str(data["target_id"])),
            relationship_type=data["relationship_type"],
            tenant_id=tenant_id,
        )

        data["slug"] = slug
        data["tenant_id"] = tenant_id
        data["subject_id"] = subject_id  # before_insert listener fills "" → tenant_id
        data["visibility"] = visibility

        obj = self.model_class(**data)
        session.add(obj)
        if before_commit is None:
            await uow.commit(session)
            await session.refresh(obj)
            out = self.out_schema.model_validate(obj)
        else:
            await session.flush()
            await session.refresh(obj)
            out = self.out_schema.model_validate(obj)
            await before_commit(out)
            await uow.commit(session)

        logger.info(
            "relationship_created",
            id=str(obj.id),
            slug=slug,
            relationship_type=data["relationship_type"],
        )
        return out

    async def get_or_create(
        self,
        session: AsyncSession,
        payload: Any,
        tenant_id: str,
        subject_id: str = "",
        *,
        visibility: str = "tenant",
        identity_fields: tuple[str, ...],
        before_commit: BeforeCommit | None = None,
        reclaim: Reclaim | None = None,
    ) -> tuple[Any, bool]:
        """Race-safe get-or-create keyed on the edge ``identity_fields``.

        Optimistically inserts via ``create`` and, on the unique violation
        raised by the WS1 ``uq_arch_rels_tenant_edge`` partial index, rolls back
        and re-SELECTs the surviving edge on the identity key, then updates it in
        place. Mirrors ``EntityService.get_or_create`` /
        ``services/realisation_bindings.attach_binding``; the re-SELECT IS the
        discriminator.

        ``reclaim`` (an ``Idempotency-Key`` route's ``Idempotency.reclaim``) is awaited
        right after every recovery ``rollback`` — which also discards the uncommitted claim —
        and BEFORE the survivor is touched, so a same-key request can never hold the claim
        while this one holds the survivor's row lock (a 40P01 deadlock).

        ``RelationshipService.update`` takes a DICT payload and only flushes, so
        this method commits after the in-place update. Returns ``(out, created)``.

        Wrapped in a bounded slug-regeneration retry (mirrors
        ``EntityService.get_or_create``): two DIFFERENT edges whose endpoints
        share the same ``source_id[:8]``/``target_id[:8]`` prefix derive the same
        ``_generate_slug`` base but a distinct edge identity, so only the plain
        ``uq_arch_rels_tenant_slug`` constraint collides — the identity re-SELECT
        is empty. Regenerating the slug on a fresh ``create`` (which now sees the
        committed race-winner and appends a suffix) and retrying resolves it.
        """
        for attempt in range(MAX_SLUG_RETRIES + 1):
            try:
                out = await self.create(
                    session,
                    payload,
                    tenant_id,
                    subject_id,
                    visibility=visibility,
                    before_commit=before_commit,
                )
                return out, True
            except sqlalchemy.exc.IntegrityError as e:
                await session.rollback()
                if reclaim is not None:
                    await reclaim()
                # Match the identity index's covered set — see
                # ``EntityService.get_or_create`` for the full rationale (FIX 2/5).
                conds = [
                    self.model_class.tenant_id == tenant_id,
                    # FIX 5: filter on the caller-requested visibility, not a
                    # hardcoded 'tenant', so a user-visible survivor is found.
                    self.model_class.visibility == visibility,
                    *[getattr(self.model_class, f) == getattr(payload, f) for f in identity_fields],
                ]
                if visibility != "tenant":
                    # NOTE: this branch is LIVE, not dead code — reachable via the
                    # NON-partial ``uq_<table>_tenant_slug`` unique constraint under
                    # concurrent ``X-Visibility: user`` writes (the WS1 partial
                    # identity index is partial on ``visibility='tenant'`` and does
                    # not fire for user-visible rows; the per-tenant slug constraint
                    # does). See tests/test_aa_identity_race.py:167-197. User-
                    # visibility writes are intentionally NON-idempotent for
                    # sequential re-POSTs (no user-scoped identity index).
                    # User-visible edges are subject-private: scope to the caller's
                    # own survivor so the in-place update can't 404 against a
                    # sibling subject's row. Tenant edges are shared (not scoped).
                    conds.append(
                        self.model_class.subject_id == _resolve_subject(tenant_id, subject_id)
                    )
                # FIX 2: ordered single-row fetch — ``scalar_one_or_none`` raises
                # ``MultipleResultsFound`` (uncaught → 500) if conds match >1 row.
                existing = (
                    (
                        await session.execute(
                            select(self.model_class)
                            .where(*conds)
                            .order_by(self.model_class.created_at, self.model_class.id)
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is None:
                    # Empty identity re-SELECT → not an edge dup. The only benign
                    # cause is a slug collision between two DIFFERENT edges that
                    # truncate-collide on the slug prefix; regenerate + retry.
                    if is_fatal_non_slug_violation(e, self._slug_constraint_name):
                        raise
                    if attempt < MAX_SLUG_RETRIES:
                        continue
                    raise
                updated = await self.update(
                    session,
                    existing.id,
                    payload.model_dump(exclude_unset=True),
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                )
                if before_commit is not None:
                    await before_commit(updated)
                await session.commit()
                return updated, False
        # Unreachable: the loop either returns or the final attempt re-raises.
        raise AssertionError("get_or_create retry loop exited without return or raise")

    async def get(
        self,
        session: AsyncSession,
        entity_id: UUID,
        tenant_id: str,
        subject_id: str = "",
    ) -> Any:
        obj = await self._get_visible(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        return self.out_schema.model_validate(obj)

    async def list_all(
        self,
        session: AsyncSession,
        tenant_id: str,
        subject_id: str = "",
        *,
        limit: int = 100,
        offset: int = 0,
        endpoint_types: frozenset[str] | None = None,
        **filters: Any,
    ) -> tuple[list[Any], int]:
        """A page of edges plus the total. ``endpoint_types`` keeps only the edges whose
        BOTH ends are of one of those entity types; ``filters`` are column equalities."""
        sub = _resolve_subject(tenant_id, subject_id)
        query = scope(
            select(self.model_class), self.model_class, tenant_id=tenant_id, subject_id=sub
        )
        if endpoint_types is not None:
            query = query.where(
                self.model_class.source_entity_type.in_(endpoint_types),
                self.model_class.target_entity_type.in_(endpoint_types),
            )

        for key, value in filters.items():
            if value is None or value == "":
                continue
            if hasattr(self.model_class, key):
                query = query.where(getattr(self.model_class, key) == value)

        count_query = select(func.count()).select_from(query.subquery())
        total_count = (await session.execute(count_query)).scalar() or 0

        # ``id`` breaks ``created_at`` ties (rows written in one transaction share a
        # timestamp), so the order is total and ``offset`` pages are disjoint.
        query = (
            query.order_by(self.model_class.created_at.desc(), self.model_class.id.desc())
            .offset(offset)
            .limit(limit)
        )

        rows = (await session.execute(query)).scalars().all()
        return [self.out_schema.model_validate(r) for r in rows], total_count

    async def update(
        self,
        session: AsyncSession,
        entity_id: UUID,
        payload: dict[str, Any],
        *,
        tenant_id: str,
        subject_id: str = "",
    ) -> Any:
        """Update relationship fields, re-validating against metamodel as needed.

        If any of ``relationship_type``, ``source_id``, ``target_id``,
        ``source_entity_type``, or ``target_entity_type`` change, the new
        endpoint pair (and relationship attributes) is re-validated through
        ``_validate_metamodel`` when both ends are ArchiMate concepts. Other keys are
        applied verbatim. ``updated_at`` is bumped manually so callers can
        rely on the timestamp even when only metadata changes.
        """
        obj = await self._get_mutable(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )

        # An explicit null on an identity key is not "leave unchanged": it would
        # reach the endpoint conversions (or skip the ArchiMate branch) as None
        # (finding A07). Refuse it before anything is touched.
        nulled = sorted(k for k in _IDENTITY_KEYS if k in payload and payload[k] is None)
        if nulled:
            raise integrity_error(
                [
                    single_finding(
                        "AM-REF-001",
                        get_registry().ruleset_id,
                        f"{key} may not be null",
                        targets=[("relationship", str(obj.id))],
                        path=f"/{key}",
                    )
                    for key in nulled
                ]
            )
        props_changed = "properties" in payload
        if props_changed or any(key in payload for key in _IDENTITY_KEYS):
            await self._validate_metamodel(
                session,
                tenant_id=tenant_id,
                subject_id=subject_id,
                source_entity_type=payload.get("source_entity_type", obj.source_entity_type),
                source_id=payload.get("source_id", obj.source_id),
                target_entity_type=payload.get("target_entity_type", obj.target_entity_type),
                target_id=payload.get("target_id", obj.target_id),
                relationship_type=payload.get("relationship_type", obj.relationship_type),
                properties=payload["properties"] if props_changed else obj.properties,
                relationship_id=obj.id,
                properties_sent=props_changed,
            )
        if any(key in payload for key in _END_KEYS):
            # A moved end re-derives the edge's model (flush hook): a move into a DIFFERENT
            # model must be one the edge may join; a move within its model is not gated.
            await check_relationship_model(
                session,
                source_entity_type=payload.get("source_entity_type", obj.source_entity_type),
                source_id=payload.get("source_id", obj.source_id),
                target_entity_type=payload.get("target_entity_type", obj.target_entity_type),
                tenant_id=obj.tenant_id,
                visibility=obj.visibility,
                subject_id=obj.subject_id,
                current_model_id=obj.model_id,
            )

        managed = {"id", "slug", "tenant_id", "subject_id", "visibility", "created_at"}
        for key, value in payload.items():
            if key in managed:
                continue
            if not hasattr(self.model_class, key):
                continue
            setattr(obj, key, value)

        obj.updated_at = datetime.now(timezone.utc)
        await session.flush()

        logger.info(
            "relationship_updated",
            id=str(obj.id),
            keys=sorted(payload.keys()),
        )
        return self.out_schema.model_validate(obj)

    async def delete(
        self,
        session: AsyncSession,
        entity_id: UUID,
        tenant_id: str,
        subject_id: str = "",
    ) -> dict[str, Any]:
        obj = await self._get_mutable(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        # Relationships ending on this one go with it, recursively (Archi semantics).
        await delete_dependent_relationships(session, [obj.id], tenant_id)
        await prune_view_members(session, [obj.id], tenant_id)
        await session.delete(obj)
        await uow.commit(session)
        logger.info("relationship_deleted", id=str(entity_id))
        return {"status": "deleted", "id": entity_id}

    async def get_graph(
        self,
        session: AsyncSession,
        entity_type: str,
        entity_id: UUID,
        tenant_id: str,
        subject_id: str = "",
    ) -> list[Any]:
        """Return all relationships where the given entity appears as source OR target.

        Visibility-scoped: tenant-visible rows + caller's user-visible rows.
        """
        sub = _resolve_subject(tenant_id, subject_id)
        query = (
            scope(
                select(self.model_class),
                self.model_class,
                tenant_id=tenant_id,
                subject_id=sub,
            )
            .where(
                or_(
                    (self.model_class.source_entity_type == entity_type)
                    & (self.model_class.source_id == entity_id),
                    (self.model_class.target_entity_type == entity_type)
                    & (self.model_class.target_id == entity_id),
                ),
            )
            .order_by(self.model_class.created_at.desc())
        )

        rows = (await session.execute(query)).scalars().all()
        return [self.out_schema.model_validate(r) for r in rows]
