"""Governed assets (principles, requirements, decisions): project copies and promotion.

G8 / R313 (George 2026-10-19, ``~/.claude/plans/program/v4-alignment/G8-asset-versioning.md``):
assets exist for reuse, so an update made FROM a running project never overwrites the
tenant-level asset. The invariant lives here, in RM, not in each writer:

* **Ownership.** A row is owned by project ``P`` when ``origin_kind = 'project'`` and
  ``origin_project_id = P``, or — for requirements and decisions, which carry a
  ``project_id`` column — ``project_id = P``. Anything else is INHERITED by ``P``.
* **Guarded PATCH.** A PATCH that names its project scope (``?scope_project_id=P``) on a
  row ``P`` does not own is 409 ``inherited_asset_requires_copy``. The scope is a QUERY
  parameter read only for this check: it is never a field write, and the scope / provenance
  columns (``origin_kind``, ``origin_project_id``, ``supersedes`` and, where it exists,
  ``project_id``) are refused by PATCH (422), so no PATCH can move a record. PUT keeps its
  previous behaviour for origin / project (fr's withPreservedOrigin writes them; changing
  that is a George decision); only ``supersedes`` is server-managed on PUT and create. A
  PATCH without a scope stays allowed (tenant-level edits by fr).
* **Project copy.** ``POST /{kind}/{id}/project-copy`` creates ``P``'s copy of an inherited
  row (``origin_kind='project'``, ``origin_project_id=P``, ``supersedes=<original slug>``,
  ``version_status='DRAFT'``, a history snapshot). Idempotent on (original, ``P``): a second
  call patches the existing copy. The race is closed by the partial unique index
  ``uq_<table>_tenant_supersedes_origin_project``.
* **Project view.** A list carrying ``scope_project_id=P`` returns ``P``'s copy IN PLACE OF
  the original it supersedes, and hides other projects' copies and promoted copies.
* **Promote.** ``POST /{kind}/{copy_id}/promote`` (fr only, after a tenant_admin approval):
  the original takes the copy's content as a new minor ``version`` with a history snapshot;
  the copy becomes ``SUPERSEDED`` with ``superseded_by=<original slug>``.

``supersedes`` holds the original's SLUG, not its id: that is the column's existing meaning
(``VersionFieldsMixin``: "Superseded entity slug"; ``validate_version_refs`` compares it with
slugs), and a slug is immutable and unique per tenant, so it identifies the original exactly.
"""

from typing import Any
from uuid import UUID

import sqlalchemy.exc
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import ColumnElement, and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from repositories.visibility import can_mutate, scope
from services.entity_service import EntityService, _resolve_subject
from services.slug_retry import MAX_SLUG_RETRIES, is_fatal_non_slug_violation
from utils.version_refs import validate_version_refs
from version_utils import increment_version

SUPERSEDED = "SUPERSEDED"
INHERITED_CODE = "inherited_asset_requires_copy"

# The scope / provenance columns no client may write once the row exists.
# ``supersedes`` marks a project copy: never client-written (PATCH 422, PUT keeps it,
# create refuses it).
GOVERNED_SERVER_MANAGED = frozenset({"supersedes"})
# Scope / provenance: refused by PATCH (a PATCH must never move a record); PUT keeps its
# previous behaviour and may still write them (fr's withPreservedOrigin, lead 2026-10-19).
GOVERNED_PATCH_ONLY = frozenset({"origin_kind", "origin_project_id"})

# What a promotion does NOT carry from the copy into the original: identity, scope,
# provenance and the version lifecycle (the original keeps its own and gets a new version).
_NOT_PROMOTED = frozenset(
    {
        "project_id",
        "stable_key",
        "origin_kind",
        "origin_project_id",
        "origin_workflow_run_id",
        "created_by_user_id",
        "version",
        "version_status",
        "supersedes",
        "superseded_by",
        "version_notes",
        "approved_by",
        "approved_at",
        "published_at",
        # Register provenance (REG-1): where and when THIS row was raised.
        "phase",
        "card_id",
        "raised_at",
    }
)

# Register provenance a project copy does not inherit from its original: the copy is raised
# in the copying project (``raised_at`` None = the insert's server default, now).
_COPY_RESETS = {"phase": None, "card_id": None, "raised_at": None}


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": f"{code}: {message}"})


def _superseded_error(entity_type: str, superseded_by: str | None) -> HTTPException:
    return _error(
        409,
        "asset_superseded",
        f"this {entity_type} was promoted into {superseded_by!r} and is read-only; "
        "POST .../project-copy on the original to edit it for the project again",
    )


def _validation_422(e: ValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=[
            {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]} for err in e.errors()
        ],
    )


class GovernedAssetService(EntityService):
    """``EntityService`` for the three governed asset kinds, plus the G8 rules."""

    def __init__(
        self,
        model_class,
        out_schema,
        entity_type: str,
        in_schema: type[BaseModel],
        snapshot_fields: tuple[str, ...] = (),
    ):
        self.has_project_column = hasattr(model_class, "project_id")
        patch_only = GOVERNED_PATCH_ONLY | (
            {"project_id"} if self.has_project_column else frozenset()
        )
        super().__init__(
            model_class,
            out_schema,
            entity_type,
            snapshot_fields,
            extra_server_managed=GOVERNED_SERVER_MANAGED,
            extra_patch_managed=frozenset(patch_only),
        )
        self.in_schema = in_schema

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------

    def owns(self, row: Any, project_id: UUID) -> bool:
        """True iff ``row`` belongs to project ``project_id`` (see module docstring)."""
        origin = row.origin_project_id
        if row.origin_kind == "project" and origin is not None and str(origin) == str(project_id):
            return True
        if self.has_project_column:
            pid = (row.project_id or "").strip().lower()
            return pid == str(project_id)
        return False

    @staticmethod
    def is_copy(row: Any) -> bool:
        return row.origin_kind == "project" and bool(row.supersedes)

    def _copy_clause(self, model: Any) -> ColumnElement[bool]:
        return and_(model.origin_kind == "project", model.supersedes.is_not(None))

    def _active_copy_clause(self, model: Any, project_id: UUID) -> ColumnElement[bool]:
        return and_(
            self._copy_clause(model),
            model.origin_project_id == project_id,
            model.version_status != SUPERSEDED,
        )

    # ------------------------------------------------------------------
    # Guarded PATCH
    # ------------------------------------------------------------------

    async def patch(
        self,
        session: AsyncSession,
        entity_id: UUID,
        changes: dict[str, Any],
        in_schema: type[BaseModel],
        tenant_id: str,
        subject_id: str = "",
        *,
        scope_project_id: UUID | None = None,
    ):
        """``EntityService.patch`` with the G8 guard.

        A server-managed field is refused (422) before anything is read; then 404 (not
        visible / not mutable) is decided BEFORE 409, so the guard never reveals a row the
        caller could not otherwise see.
        """
        managed = sorted(set(changes) & self.patch_server_managed)
        if managed:
            raise HTTPException(
                status_code=422, detail=f"Server-managed field(s): {', '.join(managed)}"
            )
        obj = await self._get_mutable(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        if scope_project_id is not None and not self.owns(obj, scope_project_id):
            raise _error(
                409,
                INHERITED_CODE,
                f"this {self.entity_type} is not owned by project {scope_project_id}; "
                f"POST /{self.model_class.__tablename__}/{entity_id}/project-copy to edit "
                "it for the project",
            )
        if obj.version_status == SUPERSEDED:
            raise _superseded_error(self.entity_type, obj.superseded_by)
        return await super().patch(session, entity_id, changes, in_schema, tenant_id, subject_id)

    async def require_owned(
        self,
        session: AsyncSession,
        entity_id: UUID,
        tenant_id: str,
        subject_id: str,
        scope_project_id: UUID,
    ) -> None:
        """The PATCH ownership rule for PUT / DELETE carrying ``scope_project_id`` (G3): 404
        first (never reveals a row the caller cannot mutate), then 409
        ``inherited_asset_requires_copy`` when the project does not own the row."""
        obj = await self._get_mutable(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        if not self.owns(obj, scope_project_id):
            raise _error(
                409,
                INHERITED_CODE,
                f"this {self.entity_type} is not owned by project {scope_project_id}; "
                f"POST /{self.model_class.__tablename__}/{entity_id}/project-copy to edit "
                "it for the project",
            )

    async def create(self, session: AsyncSession, payload, tenant_id: str, *args, **kwargs):
        """``EntityService.create``, refusing a client-set ``supersedes``: on a governed asset
        that column marks a PROJECT COPY (it shadows the original in the project's view), so
        only ``project_copy`` writes it."""
        try:  # the version-chain rule keeps its own message and precedence
            validate_version_refs(payload.supersedes, payload.superseded_by, "")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if payload.supersedes:
            raise HTTPException(status_code=422, detail="Server-managed field(s): supersedes")
        return await super().create(session, payload, tenant_id, *args, **kwargs)

    async def update(
        self,
        session: AsyncSession,
        entity_id: UUID,
        payload,
        tenant_id: str,
        subject_id: str = "",
        **kwargs: Any,
    ):
        """``EntityService.update`` (PUT, and every PATCH / get-or-create write under it),
        refusing a promoted copy: it is history now; the next project-copy revives it."""
        obj = await self._get_mutable(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        if obj.version_status == SUPERSEDED:
            raise _superseded_error(self.entity_type, obj.superseded_by)
        return await super().update(session, entity_id, payload, tenant_id, subject_id, **kwargs)

    # ------------------------------------------------------------------
    # Project view (list)
    # ------------------------------------------------------------------

    def project_view_where(
        self,
        project_id: UUID,
        *,
        tenant_id: str,
        subject_id: str,
        include_shadowed: bool = False,
    ) -> tuple[ColumnElement[bool], ...]:
        """The list predicates of project ``project_id``'s view.

        * a project copy is listed only if it is ``project_id``'s own, un-promoted copy;
        * unless ``include_shadowed``, an own-tenant original that such a copy supersedes is
          left out (the copy stands in its place).
        """
        model = self.model_class
        clauses: list[ColumnElement[bool]] = [
            or_(not_(self._copy_clause(model)), self._active_copy_clause(model, project_id))
        ]
        if not include_shadowed:
            shadow = aliased(model)
            superseded_slugs = scope(
                select(shadow.supersedes).where(self._active_copy_clause(shadow, project_id)),
                shadow,
                tenant_id=tenant_id,
                subject_id=_resolve_subject(tenant_id, subject_id),
            )
            clauses.append(or_(model.tenant_id != tenant_id, model.slug.not_in(superseded_slugs)))
        return tuple(clauses)

    # ------------------------------------------------------------------
    # Project copy
    # ------------------------------------------------------------------

    async def _find_copy(
        self, session: AsyncSession, tenant_id: str, original_slug: str, project_id: UUID
    ):
        """``project_id``'s copy of the original (live or promoted), on exactly the unique
        index's key — there is at most one per (original, project), ever."""
        model = self.model_class
        return (
            await session.execute(
                select(model).where(
                    model.tenant_id == tenant_id,
                    model.supersedes == original_slug,
                    model.origin_project_id == project_id,
                )
            )
        ).scalar_one_or_none()

    def _check_changes(self, changes: dict[str, Any]) -> None:
        unknown = sorted(set(changes) - set(self.in_schema.model_fields))
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown field(s): {', '.join(unknown)}")
        managed = sorted(set(changes) & self.patch_server_managed)
        if managed:
            raise HTTPException(
                status_code=422, detail=f"Server-managed field(s): {', '.join(managed)}"
            )

    async def _resume_copy(
        self,
        session: AsyncSession,
        copy: Any,
        original_id: UUID,
        changes: dict[str, Any],
        project_id: UUID,
        tenant_id: str,
        subject_id: str,
    ) -> tuple[Any, str]:
        """The project already has a copy of this original: patch it, or revive it."""
        if copy.version_status == SUPERSEDED:
            return await self._revive_copy(
                session, copy, original_id, changes, tenant_id, subject_id
            ), "revived"
        if not changes:
            return self.out_schema.model_validate(copy), "existing"
        return await self.patch(
            session,
            copy.id,
            changes,
            self.in_schema,
            tenant_id,
            subject_id,
            scope_project_id=project_id,
        ), "existing"

    async def _revive_copy(
        self,
        session: AsyncSession,
        copy: Any,
        original_id: UUID,
        changes: dict[str, Any],
        tenant_id: str,
        subject_id: str,
    ):
        """A promoted copy becomes the project's live copy again (a new edit after the merge).

        It restarts from the original's CURRENT content (which now includes the merged
        edit) plus ``changes``, as a DRAFT. One row per (original, project) for life: the
        project's own title/key identity indexes would refuse a second row anyway, and the
        history keeps every earlier state.
        """
        if not can_mutate(
            copy, subject_id=_resolve_subject(tenant_id, subject_id), tenant_id=tenant_id
        ):
            raise HTTPException(status_code=404, detail="not found")
        original = await self._get_visible(
            session, original_id, tenant_id=tenant_id, subject_id=subject_id
        )
        content = [
            name
            for name in self.in_schema.model_fields
            if name not in _NOT_PROMOTED and name not in self.patch_server_managed
        ]
        merged = {
            name: getattr(copy, name) for name in self.in_schema.model_fields if hasattr(copy, name)
        }
        merged.update(
            {name: getattr(original, name) for name in content if hasattr(original, name)}
        )
        merged.update(changes)
        merged.update(
            version_status="DRAFT",
            superseded_by=None,
            version_notes=None,
            approved_by=None,
            approved_at=None,
            published_at=None,
            version=original.version,
        )
        try:
            payload = self.in_schema.model_validate(merged)
        except ValidationError as e:
            raise _validation_422(e)
        dumped = payload.model_dump(mode="json")
        for name in [*content, "version_status", "superseded_by", "version_notes", "version"]:
            if name in dumped and hasattr(copy, name):
                setattr(copy, name, dumped[name])
        for name in ("approved_by", "approved_at", "published_at"):
            setattr(copy, name, None)
        await session.commit()
        await session.refresh(copy)
        await self._record_snapshot(session, copy, "project_copy")
        await session.commit()
        await self._dispatch_eval(session, copy, tenant_id=tenant_id, subject_id=subject_id)
        return self.out_schema.model_validate(copy)

    async def project_copy(
        self,
        session: AsyncSession,
        entity_id: UUID,
        project_id: UUID,
        changes: dict[str, Any],
        tenant_id: str,
        subject_id: str = "",
    ) -> tuple[Any, str]:
        """Create (or, idempotently, update) ``project_id``'s copy of ``entity_id``.

        Returns ``(copy_out, state)``: ``created``, ``existing`` (the live copy was patched)
        or ``revived`` (a promoted copy restarted as a draft).
        """
        self._check_changes(changes)
        # Own tenant only (never an inherited system row: its slug is in another
        # tenant's namespace, so ``supersedes`` could not name it).
        original = await self._get_visible(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        if self.is_copy(original):
            raise _error(
                409,
                "asset_is_a_project_copy",
                f"this {self.entity_type} is already a project copy of {original.supersedes!r}; "
                "copy the original instead",
            )
        if self.owns(original, project_id):
            raise _error(
                409,
                "asset_owned_by_project",
                f"project {project_id} owns this {self.entity_type}; PATCH it with "
                f"scope_project_id={project_id} instead",
            )
        original_slug = original.slug
        existing = await self._find_copy(session, tenant_id, original_slug, project_id)
        if existing is not None:
            return await self._resume_copy(
                session, existing, entity_id, changes, project_id, tenant_id, subject_id
            )

        data = {
            name: getattr(original, name)
            for name in self.in_schema.model_fields
            if hasattr(original, name)
        }
        data.update({k: v for k, v in _COPY_RESETS.items() if k in self.in_schema.model_fields})
        data.update(changes)
        data.update(
            origin_kind="project",
            origin_project_id=project_id,
            origin_workflow_run_id=None,
            supersedes=original_slug,
            superseded_by=None,
            version_status="DRAFT",
            version_notes=None,
            approved_by=None,
            approved_at=None,
            published_at=None,
            created_by_user_id=_resolve_subject(tenant_id, subject_id),
        )
        if "stable_key" in self.in_schema.model_fields:
            data["stable_key"] = None  # the AA dedup key belongs to the original's card
        if self.has_project_column:
            data["project_id"] = str(project_id)
        try:
            payload = self.in_schema.model_validate(data)
        except ValidationError as e:
            raise _validation_422(e)
        visibility = original.visibility

        for attempt in range(MAX_SLUG_RETRIES + 1):
            try:
                out = await EntityService.create(  # the one writer of ``supersedes``
                    self,
                    session,
                    payload,
                    tenant_id,
                    subject_id,
                    visibility=visibility,
                    snapshot_label="project_copy",
                )
                return out, "created"
            except sqlalchemy.exc.IntegrityError as e:
                await session.rollback()
                # The re-SELECT on the unique index's key IS the discriminator: a concurrent
                # project-copy of the same (original, project) won the race → update it.
                winner = await self._find_copy(session, tenant_id, original_slug, project_id)
                if winner is not None:
                    return await self._resume_copy(
                        session, winner, entity_id, changes, project_id, tenant_id, subject_id
                    )
                if attempt < MAX_SLUG_RETRIES and not is_fatal_non_slug_violation(
                    e, self._slug_constraint_name
                ):
                    continue  # a concurrent same-base-slug create; a fresh slug resolves it
                raise _error(
                    409,
                    "project_copy_conflict",
                    f"project {project_id} already has a {self.entity_type} with this "
                    "identity (title / key); edit that one instead",
                )
        raise AssertionError("project_copy retry loop exited without return or raise")

    # ------------------------------------------------------------------
    # Promote
    # ------------------------------------------------------------------

    async def promote(
        self,
        session: AsyncSession,
        copy_id: UUID,
        tenant_id: str,
        subject_id: str = "",
        *,
        approved_by: str | None = None,
    ):
        """Merge a project copy into its tenant original (one transaction, rows locked)."""
        model = self.model_class
        # Visibility + mutability first (404 for anything the caller cannot write).
        await self._get_mutable(session, copy_id, tenant_id=tenant_id, subject_id=subject_id)
        copy = (
            await session.execute(
                select(model)
                .where(model.id == copy_id, model.tenant_id == tenant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        # Every refusal releases the row locks (rollback) — and builds its message first:
        # a rollback expires the loaded rows, and an expired attribute read here is sync IO.
        if not self.is_copy(copy):
            err = _error(
                409, "not_a_project_copy", f"this {self.entity_type} is not a project copy"
            )
            await session.rollback()
            raise err
        if copy.version_status == SUPERSEDED:
            err = _error(
                409,
                "copy_already_promoted",
                f"this copy was already promoted into {copy.superseded_by!r}",
            )
            await session.rollback()
            raise err
        original = (
            await session.execute(
                select(model)
                .where(model.tenant_id == tenant_id, model.slug == copy.supersedes)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        sub = _resolve_subject(tenant_id, subject_id)
        if original is None or not can_mutate(original, subject_id=sub, tenant_id=tenant_id):
            err = _error(
                409,
                "original_missing",
                f"the original {copy.supersedes!r} this copy supersedes no longer exists",
            )
            await session.rollback()
            raise err

        for name in self.in_schema.model_fields:
            if name in _NOT_PROMOTED or name in self.patch_server_managed:
                continue
            if hasattr(original, name) and hasattr(copy, name):
                setattr(original, name, getattr(copy, name))
        try:
            original.version = increment_version(original.version or "1.0.0", "minor")
        except ValueError:
            original.version = increment_version("1.0.0", "minor")
        original.version_notes = (
            f"promoted from project copy {copy.slug} (project {copy.origin_project_id})"
        )
        if approved_by:
            original.approved_by = approved_by
            original.approved_at = func.now()
        copy.version_status = SUPERSEDED
        copy.superseded_by = original.slug
        await session.flush()
        await session.refresh(original)
        await session.refresh(copy)
        await self._record_snapshot(session, original, "promoted")
        await self._record_snapshot(session, copy, "superseded")
        await session.commit()
        await session.refresh(original)
        await self._dispatch_eval(session, original, tenant_id=tenant_id, subject_id=subject_id)
        return self.out_schema.model_validate(original)
