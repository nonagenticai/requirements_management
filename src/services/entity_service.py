"""Generic CRUD service for all entity types.

Per-user-within-tenant scoping (mirrors procedure_management pilot, 2026-04-27):

- list/get/update/delete take ``tenant_id, subject_id`` (kwarg-only) and
  scope SELECTs through ``repositories.visibility.scope`` so callers see
  every tenant-visible row plus their own user-visible rows.
- create takes ``tenant_id, subject_id, visibility`` (kwarg-only).
- Mutations on user-visible rows use ``can_mutate`` and raise 404 (NOT 403)
  when the caller doesn't own the row — matches the secrets/BOLA pattern,
  doesn't leak existence.

Empty ``subject_id`` ("tenant-only caller", e.g. trusted-network without
X-Subject-Id) collapses to ``tenant_id`` for write operations so backfilled
rows continue to behave exactly as they did pre-Plan-2.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy.exc
import structlog
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from slugify import slugify
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureRelationship
from models.modeling import bump_models, prune_trace_links_async
from models.version_history import EntityVersionHistory
from repositories.visibility import can_mutate, scope
from services import unit_of_work as uow
from services.idempotency import BeforeCommit
from services.relationship_cascade import delete_dependent_relationships, prune_view_members
from services.slug_retry import (
    MAX_SLUG_RETRIES,
    find_slug_constraint_name,
    is_fatal_non_slug_violation,
)
from settings import get_settings
from utils.version_refs import validate_version_refs

logger = structlog.get_logger(__name__)

# Fields managed by the server — never overwritten from a client payload.
SERVER_MANAGED_FIELDS = frozenset(
    {
        "id",
        "slug",
        "tenant_id",
        "subject_id",
        "visibility",
        "created_at",
        "updated_at",
        "tsv",
    }
)


def _kept_if_omitted(payload: BaseModel) -> frozenset[str]:
    """Fields the schema keeps when an update omits them (an explicit ``null`` still clears)."""
    keep: frozenset[str] = getattr(type(payload), "KEEP_IF_OMITTED", frozenset())
    return keep - payload.model_fields_set


def _resolve_subject(tenant_id: str, subject_id: str) -> str:
    """Empty subject_id collapses to tenant_id (legacy "tenant-only caller")."""
    return subject_id or tenant_id


class EntityService:
    """Generic async CRUD service parameterised by ORM model and Pydantic schemas."""

    def __init__(
        self,
        model_class,
        out_schema,
        entity_type: str,
        snapshot_fields: tuple[str, ...] = (),
        *,
        extra_server_managed: frozenset[str] = frozenset(),
        extra_patch_managed: frozenset[str] = frozenset(),
        ignored_on_create: frozenset[str] = frozenset(),
    ):
        self.model_class = model_class
        self.out_schema = out_schema
        self.entity_type = entity_type
        self.snapshot_fields = snapshot_fields
        # Per-kind additions to ``SERVER_MANAGED_FIELDS`` (G8 / R313: the governed assets'
        # scope and provenance columns). Never written by ``update`` (PUT keeps the stored
        # value) and refused by ``patch``.
        self.server_managed = SERVER_MANAGED_FIELDS | extra_server_managed
        # Refused by ``patch`` only: PUT still writes these (G8 lead decision 2026-10-19 —
        # fr's PUT may legitimately change a governed asset's origin / project).
        self.patch_server_managed = self.server_managed | extra_patch_managed
        # Client-supplied fields a create drops (stored as their column default) — the
        # modeling rows' legacy approval/publication fields, which only the review/publish
        # workflow may set (plan 2026-10-26 §7.3).
        self.ignored_on_create = ignored_on_create
        # Cached OPTIONAL fast-path for the concurrent-slug-collision recovery:
        # the model's (tenant_id, slug) UniqueConstraint name. Only used to
        # short-circuit a re-raise when the IntegrityError names some OTHER
        # constraint; when None (model expresses slug uniqueness as a
        # unique Index, or diag is empty) we fall back to retry-on-empty-
        # re-SELECT, which is self-sufficient.
        self._slug_constraint_name = find_slug_constraint_name(model_class)
        if self._slug_constraint_name is None:
            logger.info(
                "slug_constraint_name_unresolved",
                entity_type=entity_type,
                note="get_or_create falls back to retry-on-empty-re-SELECT",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _generate_unique_slug(self, session: AsyncSession, title: str, tenant_id: str) -> str:
        """Slug uniqueness is per-tenant (matches the new uq_<table>_tenant_slug)."""
        slug_base = slugify(title)[:110]
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
        self, session: AsyncSession, entity_id: UUID, *, tenant_id: str, subject_id: str
    ):
        """Fetch a row visible to (tenant_id, subject_id) or 404."""
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

    async def _get_readable(
        self, session: AsyncSession, entity_id: UUID, *, tenant_id: str, subject_id: str
    ):
        """Fetch a row the caller may READ (own-tenant or inherited system) or 404.

        Widens ``_get_visible`` with the system tenant's PUBLISHED rows
        (``scope(include_system=True)``). READ-ONLY: never feed its result to
        ``can_mutate``/update/delete — mutations go through ``_get_mutable``,
        which stays own-tenant.
        """
        sub = _resolve_subject(tenant_id, subject_id)
        stmt = scope(
            select(self.model_class).where(self.model_class.id == entity_id),
            self.model_class,
            tenant_id=tenant_id,
            subject_id=sub,
            include_system=True,
            system_tenant_id=get_settings().system_tenant_id,
        )
        entity = (await session.execute(stmt)).scalar_one_or_none()
        if not entity:
            raise HTTPException(status_code=404, detail="not found")
        return entity

    async def _get_mutable(
        self, session: AsyncSession, entity_id: UUID, *, tenant_id: str, subject_id: str
    ):
        """Fetch a row visible to caller AND mutable by them (else 404).

        404 rather than 403 — matches the secrets/BOLA pattern: don't leak
        the existence of a sibling user's private row.
        """
        sub = _resolve_subject(tenant_id, subject_id)
        entity = await self._get_visible(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        if not can_mutate(entity, subject_id=sub, tenant_id=tenant_id):
            raise HTTPException(status_code=404, detail="not found")
        return entity

    async def _record_snapshot(self, session: AsyncSession, entity, version_status: str) -> None:
        if not self.snapshot_fields:
            return

        def _jsonable(v: Any) -> Any:
            """Convert non-JSON-serializable values for JSONB storage."""
            if isinstance(v, UUID):
                return str(v)
            if isinstance(v, datetime):
                return v.isoformat()
            if isinstance(v, list):
                return [_jsonable(item) for item in v]
            return v

        snapshot_data = {f: _jsonable(getattr(entity, f)) for f in self.snapshot_fields}
        history = EntityVersionHistory(
            entity_type=self.entity_type,
            entity_id=entity.id,
            version=entity.version,
            version_status=version_status,
            snapshot_data=snapshot_data,
        )
        session.add(history)
        logger.info(
            "version_snapshot_recorded",
            entity_type=self.entity_type,
            entity_id=entity.id,
            version=entity.version,
        )

    async def _cleanup_relationships(
        self, session: AsyncSession, entity_id: UUID, tenant_id: str
    ) -> int:
        """Delete architecture relationships referencing this entity as source or target.

        Returns the number of deleted relationship rows.
        """
        table_name = self.model_class.__tablename__
        stmt = (
            delete(ArchitectureRelationship)
            .where(
                ArchitectureRelationship.tenant_id == tenant_id,
                or_(
                    (ArchitectureRelationship.source_entity_type == table_name)
                    & (ArchitectureRelationship.source_id == entity_id),
                    (ArchitectureRelationship.target_entity_type == table_name)
                    & (ArchitectureRelationship.target_id == entity_id),
                ),
            )
            .returning(ArchitectureRelationship.id, ArchitectureRelationship.model_id)
        )
        # Core DELETE bypasses the ORM flush hooks: bump the models whose content it
        # changed and drop trace links that ended on the deleted edges.
        rows = (await session.execute(stmt)).all()
        await bump_models(session, {r.model_id for r in rows})
        await prune_trace_links_async(session, [r.id for r in rows])
        # ... and the relationships that ended on those edges, recursively.
        dependents = await delete_dependent_relationships(session, [r.id for r in rows], tenant_id)
        # The entity (an element) and its edges leave every view's membership lists.
        own = [entity_id] if table_name == "architecture_elements" else []
        await prune_view_members(session, [*own, *(r.id for r in rows)], tenant_id)
        deleted = len(rows) + len(dependents)
        if deleted:
            logger.info(
                "relationships_cleaned_up",
                entity_type=self.entity_type,
                entity_id=str(entity_id),
                table_name=table_name,
                deleted_relationships=deleted,
            )
        return deleted

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        payload,
        tenant_id: str,
        subject_id: str = "",
        *,
        visibility: str = "tenant",
        evaluate: bool = True,
        extra_columns: dict[str, Any] | None = None,
        snapshot_label: str = "created",
        before_commit: BeforeCommit | None = None,
    ):
        """Insert one row and commit.

        ``before_commit`` (an ``Idempotency-Key`` route's response staging) switches the
        create to ONE transaction: every step only flushes, the hook is awaited with the
        final out model (exactly what is returned) and then the single commit makes the
        caller's claim, the row, its snapshot/score and the hook's writes durable together.
        Without it the create commits step by step, as it always has.
        """
        commit = uow.committer(session) if before_commit is None else session.flush
        if visibility not in ("tenant", "user"):
            raise HTTPException(status_code=422, detail=f"invalid visibility: {visibility!r}")

        data = payload.model_dump(mode="json")
        for field in self.ignored_on_create:
            data.pop(field, None)
        data["tenant_id"] = tenant_id
        data["subject_id"] = subject_id  # before_insert listener fills "" → tenant_id
        data["visibility"] = visibility
        # Server-controlled columns not exposed on the In schema (e.g. the
        # synthetic-identity key). Never from client payload → unspoofable.
        if extra_columns:
            data.update(extra_columns)
        # Concurrent same-title creates both derive the same slug (check-then-insert)
        # and the loser trips ``uq_<table>_tenant_slug``. Same-title concepts are legal
        # (A12: identity is the id), so that loser must not be a 500: INSERT inside a
        # SAVEPOINT and, on a slug violation, regenerate the slug and retry (bounded).
        # The savepoint keeps everything else the caller did in this transaction (e.g.
        # an Idempotency-Key claim) intact. Any other violation re-raises at once.
        # ⚠️ ``begin_nested`` flushes pending objects BEFORE the SAVEPOINT exists, so a
        # caller that already added rows referencing this one (``ProjectService.create``
        # adds brief v1 + the first phase event) keeps the single-flush path: those rows
        # must flush together with this one in FK order, and outside a savepoint.
        retry = not (session.new or session.dirty or session.deleted)
        for attempt in range(MAX_SLUG_RETRIES + 1 if retry else 1):
            slug = await self._generate_unique_slug(session, payload.title, tenant_id)
            try:
                validate_version_refs(payload.supersedes, payload.superseded_by, slug)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            obj = self.model_class(**{**data, "slug": slug})
            if not retry:
                session.add(obj)
                break
            try:
                async with session.begin_nested():
                    session.add(obj)
                break
            except sqlalchemy.exc.IntegrityError as e:
                if attempt >= MAX_SLUG_RETRIES or is_fatal_non_slug_violation(
                    e, self._slug_constraint_name
                ):
                    raise
        await commit()
        await session.refresh(obj)

        if self.snapshot_fields:
            await self._record_snapshot(session, obj, snapshot_label)
            await commit()

        if evaluate:
            await self._dispatch_eval(
                session, obj, tenant_id=tenant_id, subject_id=subject_id, commit=commit
            )

        return await self._finish(session, obj, before_commit)

    async def _finish(self, session: AsyncSession, obj: Any, before_commit: BeforeCommit | None):
        """The out model; with a hook, await it on that model and make the ONE commit."""
        out = self.out_schema.model_validate(obj)
        if before_commit is not None:
            await before_commit(out)
            await uow.commit(session)
        return out

    async def get_or_create(
        self,
        session: AsyncSession,
        payload,
        tenant_id: str,
        subject_id: str = "",
        *,
        visibility: str = "tenant",
        evaluate: bool = True,
        identity_fields: tuple[str, ...],
        extra_filters: dict[str, Any] | None = None,
    ):
        """Race-safe get-or-create keyed on ``identity_fields``.

        Optimistically inserts via ``create`` and, on the unique violation
        raised by the WS1 partial unique index, rolls back and re-SELECTs the
        surviving row on the same identity key, then updates it in place. This
        mirrors ``services/realisation_bindings.attach_binding``: the re-SELECT
        on the identity key IS the discriminator — we do not sniff
        ``constraint_name``.

        The recovery re-SELECT must match the index's covered row set *exactly*
        so it resolves to at most one row (``scalar_one_or_none`` else raises
        ``MultipleResultsFound``): the WS1 indexes are partial on
        ``visibility = 'tenant'`` (the AA-intake write posture — a project-scoped
        asset is a tenant asset), so we filter tenant-visible rows here rather
        than subject-scoping via ``scope()``. ``extra_filters`` carries any extra
        index-predicate columns (e.g. ``architecture_elements`` excludes
        ``is_synthetic = true``, so its caller passes ``{"is_synthetic": False}``).
        A tenant-visible survivor is visible to every subject in the tenant, so
        this leaks nothing a subject-scoped read wouldn't.

        Returns ``(out_schema_instance, created_bool)``. ``created`` is True
        when this call inserted the surviving row, False when it lost the race
        (or a prior re-POST already created it) and updated in place instead.
        """
        for attempt in range(MAX_SLUG_RETRIES + 1):
            try:
                out = await self.create(
                    session,
                    payload,
                    tenant_id,
                    subject_id,
                    visibility=visibility,
                    evaluate=evaluate,
                )
                return out, True
            except sqlalchemy.exc.IntegrityError as e:
                await session.rollback()
                existing = await self._find_identity_survivor(
                    session,
                    payload,
                    tenant_id,
                    subject_id,
                    visibility=visibility,
                    identity_fields=identity_fields,
                    extra_filters=extra_filters,
                )
                if existing is None:
                    # The identity re-SELECT is EMPTY → the violation was NOT an
                    # identity dup (the codebase's re-SELECT-is-the-discriminator
                    # convention). The only benign cause is a slug collision: two
                    # concurrent same-base-slug / different-identity creates both
                    # derive the same base slug, and the loser trips the plain
                    # ``uq_<table>_tenant_slug`` constraint (NOT the identity
                    # index). Regenerating the slug (a fresh ``create`` re-runs
                    # ``_generate_unique_slug``) and retrying resolves it.
                    if is_fatal_non_slug_violation(e, self._slug_constraint_name):
                        # Fast-path: the IntegrityError names some OTHER known
                        # constraint — genuinely fatal, no point retrying.
                        raise
                    if attempt < MAX_SLUG_RETRIES:
                        # Retry with a regenerated slug. All ``create`` side
                        # effects (snapshot, eval dispatch) run strictly AFTER
                        # commit, so this failed attempt fired NONE of them; the
                        # successful retry fires them exactly once.
                        continue
                    # Bounded exhaustion — surface the original error rather than
                    # masking it (exactly today's re-raise behaviour).
                    raise
                updated = await self._update_identity_survivor(
                    session, existing, payload, tenant_id, subject_id
                )
                return updated, False
        # Unreachable: the loop either returns or the final attempt re-raises.
        raise AssertionError("get_or_create retry loop exited without return or raise")

    async def _find_identity_survivor(
        self,
        session: AsyncSession,
        payload,
        tenant_id: str,
        subject_id: str,
        *,
        visibility: str,
        identity_fields: tuple[str, ...],
        extra_filters: dict[str, Any] | None,
    ):
        """The existing row on ``get_or_create``'s identity key, or None (see there)."""
        conds = [
            self.model_class.tenant_id == tenant_id,
            # FIX 5: filter on the visibility actually requested by the
            # caller (client-controlled X-Visibility), NOT a hardcoded
            # 'tenant'. A user-visible survivor is filed under 'user', so
            # a hardcoded 'tenant' filter would miss it → re-raise → 500.
            self.model_class.visibility == visibility,
            *[getattr(self.model_class, f) == getattr(payload, f) for f in identity_fields],
            *[getattr(self.model_class, k) == v for k, v in (extra_filters or {}).items()],
        ]
        if visibility != "tenant":
            # NOTE: this branch is LIVE, not dead code. For a model
            # WITHOUT a user-scoped identity index it is reachable only
            # via the plain ``uq_<table>_tenant_slug`` constraint, and
            # only when both racers derived the same base slug before
            # either committed; if the winner commits first the loser
            # mints ``<slug>-1`` and a DUPLICATE row lands (rm#191).
            # Such models are therefore not race-safe for user rows.
            # ``requirements`` carries user-scoped identity indexes
            # (``uq_requirements_user_project_*``, keyed on subject_id,
            # migration 0049) so its user-visible creates are idempotent
            # in every interleaving, sequential re-POSTs included.
            # A user-visible row is subject-private: scope the recovery
            # re-SELECT to the caller's own survivor so the in-place
            # update can't target (and 404 against) a sibling subject's
            # row. Tenant rows are shared, so not subject-scoped.
            conds.append(self.model_class.subject_id == _resolve_subject(tenant_id, subject_id))
        # FIX 2: ordered single-row fetch. On the uncovered null-project /
        # user-visible paths the conds can match >1 row;
        # ``scalar_one_or_none`` would raise ``MultipleResultsFound`` (an
        # InvalidRequestError, NOT an IntegrityError → uncaught → 500).
        # ``order_by(...).limit(1).first()`` is deterministic and total.
        return (
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

    async def _update_identity_survivor(
        self,
        session: AsyncSession,
        existing,
        payload,
        tenant_id: str,
        subject_id: str,
    ):
        """Update ``get_or_create``'s identity survivor in place (see there)."""
        # Whether to re-evaluate quality + write a version snapshot on
        # the in-place update depends on WHY we landed on the survivor:
        #   • concurrent race-loss — the winning racer already inserted an
        #     identical row AND ran eval + wrote a "created" snapshot;
        #     re-running eval and writing an "updated" snapshot here is
        #     redundant churn (the original FIX 4 intent).
        #   • sequential BLOCKED→DOING re-POST (the routers' PRIMARY use) —
        #     the content GENUINELY changed; the row must be re-scored
        #     (else quality_score goes stale) and the change must land in
        #     EntityVersionHistory (else GET /{id}/versions misses it),
        #     exactly as the normal PUT path (evaluate/record_snapshot
        #     default True) does.
        # Discriminate on whether the incoming payload differs from the
        # survivor over the SAME field set update() writes (server-managed
        # fields excluded — mirror the loop in update()). Compare in native
        # Python space (payload attr vs ORM attr) so a JSON-serialised
        # value never spuriously reads as "changed" (e.g. UUID vs str).
        # Both eval and snapshot are idempotent-safe (PUT runs them every
        # call), so an over-eager True is harmless; a stuck False is the bug.
        # Only fields the payload DUMPS: ``update()`` skips the rest (write-only fields and
        # ``exclude_if`` ones such as an unset ``raised_at``), so they cannot be a change.
        dumped = payload.model_dump(mode="json")
        kept = _kept_if_omitted(payload)
        changed = any(
            getattr(existing, field_name) != getattr(payload, field_name)
            for field_name in type(payload).model_fields
            if field_name not in self.server_managed
            and field_name not in kept
            and field_name in dumped
            and hasattr(existing, field_name)
        )
        updated = await self.update(
            session,
            existing.id,
            payload,
            tenant_id,
            subject_id,
            evaluate=changed,
            record_snapshot=changed,
        )
        return updated

    async def get(
        self,
        session: AsyncSession,
        entity_id: UUID,
        tenant_id: str,
        subject_id: str = "",
        *,
        include_system: bool = False,
    ):
        """Fetch one row. ``include_system=True`` ONLY from a pure read route
        (GET by id) — it admits the system tenant's PUBLISHED rows."""
        if include_system:
            obj = await self._get_readable(
                session, entity_id, tenant_id=tenant_id, subject_id=subject_id
            )
        else:
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
        include_system: bool = False,
        extra_where: tuple[Any, ...] = (),
        **filters,
    ) -> tuple[list, int]:
        """List entities with pagination. Returns (items, total_count).

        Returns: tenant-visible rows + caller's user-visible rows, plus — only
        when ``include_system`` — the system tenant's PUBLISHED rows (own rows
        shadow system rows by slug, in SQL).

        ⛔ ``include_system`` defaults to False on purpose: ``list_all`` is also
        used as a get-or-create LOOKUP whose hit is then ``update``d
        (``routers/architecture_elements.py`` create path). Widening that lookup
        would resolve to a system row the caller cannot mutate → 404 on a
        legitimate create. Only the pure LIST routes pass True.
        """
        sub = _resolve_subject(tenant_id, subject_id)
        query = scope(
            select(self.model_class),
            self.model_class,
            tenant_id=tenant_id,
            subject_id=sub,
            include_system=include_system,
            system_tenant_id=get_settings().system_tenant_id,
        )
        # Apply optional filters
        active_only = filters.pop("active_only", False)
        if active_only and hasattr(self.model_class, "active"):
            query = query.where(self.model_class.active.is_(True))

        for key, value in filters.items():
            if value is None or value == "":
                continue
            if hasattr(self.model_class, key):
                query = query.where(getattr(self.model_class, key) == value)
        # Caller-built predicates (e.g. the G8 project view); they only narrow, like filters.
        for clause in extra_where:
            query = query.where(clause)

        # Total count before pagination
        count_query = select(func.count()).select_from(query.subquery())
        total_count = (await session.execute(count_query)).scalar() or 0

        # Apply ordering and pagination
        # ``id`` breaks ``created_at`` ties (rows created in one transaction share
        # a timestamp), so the order is total and ``offset`` pages are disjoint.
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
        payload,
        tenant_id: str,
        subject_id: str = "",
        *,
        evaluate: bool = True,
        record_snapshot: bool = True,
        keep_fields: frozenset[str] = frozenset(),
        before_commit: BeforeCommit | None = None,
    ):
        """Apply ``payload`` and commit; ``before_commit`` as in :meth:`create`."""
        commit = uow.committer(session) if before_commit is None else session.flush
        obj = await self._get_mutable(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        try:
            validate_version_refs(payload.supersedes, payload.superseded_by, obj.slug)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

        # Apply all payload fields except server-managed ones, the caller's ``keep_fields``
        # (stored value kept), the schema's KEEP_IF_OMITTED fields the caller did not send,
        # and write-only fields (``Field(exclude=True)``: not in the dump).
        json_data = payload.model_dump(mode="json")
        keep = keep_fields | _kept_if_omitted(payload)
        for field_name in type(payload).model_fields:
            if (
                field_name in self.server_managed
                or field_name in keep
                or field_name not in json_data
            ):
                continue
            setattr(obj, field_name, json_data[field_name])

        await commit()
        await session.refresh(obj)

        if self.snapshot_fields and record_snapshot:
            await self._record_snapshot(session, obj, "updated")
            await commit()

        if evaluate:
            await self._dispatch_eval(
                session, obj, tenant_id=tenant_id, subject_id=subject_id, commit=commit
            )

        return await self._finish(session, obj, before_commit)

    async def patch(
        self,
        session: AsyncSession,
        entity_id: UUID,
        changes: dict[str, Any],
        in_schema: type[BaseModel],
        tenant_id: str,
        subject_id: str = "",
    ):
        """Partial update: fields absent from ``changes`` keep their stored value.

        ``update`` (PUT) replaces every ``in_schema`` field, so an omitted field
        is reset to its schema default. PATCH instead overlays ``changes`` on the
        row's CURRENT values and validates the merged record through the same
        ``in_schema`` PUT uses, then delegates to ``update`` so snapshots, version
        refs and quality evaluation behave exactly as for PUT. An explicit
        ``null`` is a real change (it clears an optional field, and is a 422 for a
        required one).
        """
        if not changes:
            raise HTTPException(status_code=422, detail="PATCH body must name at least one field")
        # Fields only a workflow may set (the modeling rows' approval / publication fields,
        # spec §7.6) are IGNORED here as on create and PUT — not refused — so all three
        # write verbs agree.
        changes = {k: v for k, v in changes.items() if k not in self.ignored_on_create}
        unknown = sorted(set(changes) - set(in_schema.model_fields))
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown field(s): {', '.join(unknown)}")
        managed = sorted(set(changes) & self.patch_server_managed)
        if managed:
            # ``update`` would silently keep the stored value; say so instead.
            raise HTTPException(
                status_code=422, detail=f"Server-managed field(s): {', '.join(managed)}"
            )
        obj = await self._get_mutable(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        merged = {name: getattr(obj, name) for name in in_schema.model_fields if hasattr(obj, name)}
        merged.update(changes)
        try:
            payload = in_schema.model_validate(merged)
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=[
                    {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
                    for err in e.errors()
                ],
            )
        return await self.update(session, entity_id, payload, tenant_id, subject_id)

    async def _dispatch_eval(
        self,
        session: AsyncSession,
        obj: Any,
        *,
        tenant_id: str,
        subject_id: str,
        commit: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Run the synchronous quality heuristic and commit, so the score is in the response.

        ``commit`` defaults to ``session.commit``; a one-transaction create passes ``flush``.
        """
        # Lazy import keeps module-import cycles tame.
        from services.eval_registrations import run_heuristic_now

        ran = await run_heuristic_now(session, obj, self.entity_type)
        if ran:
            await (commit or uow.committer(session))()
            await session.refresh(obj)

    async def _cascade_children(
        self,
        session: AsyncSession,
        entity_id: UUID,
        tenant_id: str,
        subject_id: str,
    ) -> None:
        """Depth-first delete child rows that reference ``entity_id`` via parent_id.

        Only models exposing a ``parent_id`` column participate. Each child
        is also visibility-checked, has its own children cascaded, and has
        its inbound/outbound relationships cleaned up.
        """
        if not hasattr(self.model_class, "parent_id"):
            return
        sub = _resolve_subject(tenant_id, subject_id)
        children_stmt = scope(
            select(self.model_class).where(self.model_class.parent_id == entity_id),
            self.model_class,
            tenant_id=tenant_id,
            subject_id=sub,
        )
        children = (await session.execute(children_stmt)).scalars().all()
        for child in children:
            await self._cascade_children(session, child.id, tenant_id, subject_id)
            await self._cleanup_relationships(session, child.id, tenant_id)
            await session.delete(child)

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
        await self._cascade_children(session, entity_id, tenant_id, subject_id)
        await self._cleanup_relationships(session, entity_id, tenant_id)
        await session.delete(obj)
        await uow.commit(session)
        return {"status": "deleted", "id": entity_id}

    async def bulk_delete(self, session: AsyncSession, tenant_id: str, subject_id: str = "") -> int:
        """Delete all entities visible to (tenant_id, subject_id). Returns deleted count."""
        sub = _resolve_subject(tenant_id, subject_id)
        try:
            table_name = self.model_class.__tablename__
            # Visibility-aware id set: tenant rows + caller's user rows. Computed
            # FIRST so the relationship cleanup below scopes to exactly the entities
            # this call deletes.
            visible_subq = scope(
                select(self.model_class.id),
                self.model_class,
                tenant_id=tenant_id,
                subject_id=sub,
            ).subquery()
            # Clean up relationships that TOUCH the entities being deleted — scoped
            # to the visible id set (mirrors the single-delete ``_cleanup_relationships``).
            # Deleting by ``entity_type`` alone would purge edges belonging to OTHER
            # subjects' rows in the tenant that survive this visibility-scoped delete
            # (e.g. a subject's ``visibility='user'`` requirement + its trace edges).
            edge_models = await session.execute(
                delete(ArchitectureRelationship)
                .where(
                    ArchitectureRelationship.tenant_id == tenant_id,
                    or_(
                        (ArchitectureRelationship.source_entity_type == table_name)
                        & (ArchitectureRelationship.source_id.in_(select(visible_subq))),
                        (ArchitectureRelationship.target_entity_type == table_name)
                        & (ArchitectureRelationship.target_id.in_(select(visible_subq))),
                    ),
                )
                .returning(ArchitectureRelationship.id, ArchitectureRelationship.model_id)
            )
            # Core DELETEs bypass the ORM flush hooks: bump every model they changed and
            # drop the trace links that ended on what they deleted.
            edges = edge_models.all()
            touched = {r.model_id for r in edges}
            gone = [r.id for r in edges]
            stmt = delete(self.model_class).where(self.model_class.id.in_(select(visible_subq)))
            if hasattr(self.model_class, "model_id"):
                rows = (
                    await session.execute(
                        stmt.returning(self.model_class.id, self.model_class.model_id)
                    )
                ).all()
                touched |= {r.model_id for r in rows}
            else:
                rows = (await session.execute(stmt.returning(self.model_class.id))).all()
            gone += [r.id for r in rows]
            gone += await delete_dependent_relationships(session, [r.id for r in edges], tenant_id)
            await prune_view_members(session, gone, tenant_id)
            deleted = len(rows)
            await bump_models(session, touched)
            await prune_trace_links_async(session, gone)
            await session.commit()
            return deleted
        except (OSError, sqlalchemy.exc.SQLAlchemyError) as e:
            await session.rollback()
            raise HTTPException(status_code=500, detail=f"Bulk deletion failed: {e!s}")

    # ------------------------------------------------------------------
    # Back-compat shim used by routers' quality/priority endpoints
    # ------------------------------------------------------------------
    async def _get_owned(
        self,
        session: AsyncSession,
        entity_id: UUID,
        tenant_id: str,
        subject_id: str = "",
    ):
        """Legacy helper. Read-only endpoints (quality, priority, version
        info) call this with the same scoping rules as ``get`` — visible
        rows include tenant + caller's user-private rows.
        """
        return await self._get_visible(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
