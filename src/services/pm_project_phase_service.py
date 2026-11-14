"""Project TYPE and current PHASE on ``pm_projects`` (George 2026-10-13).

RM owns a project's type and its current phase; n8n is the only writer of the phase
(``PUT /pm/projects/{eid}/phase``); fr reads both (``ProjectOut``).

* ``ProjectService`` — the project CRUD service, plus: a ``project_type_id`` the tenant
  does not own, or that is RETIRED (``retired_at`` set → 422 ``project_type_retired``), → 422;
  on update an omitted ``project_type_id`` is unchanged; a type change
  that would leave the current phase outside the new type → 409 (never a silent clear).
  On create the type is required and the project starts in the type's ``setup`` phase with
  a first phase event (null → setup); no ``setup`` phase → 409
  ``project_type_missing_setup_phase`` (George 10-13 v4 ruling 2 / D16). A fr-delegated
  get-or-create re-POST onto an existing row → 403 ``project_writes_are_pm_only`` (ruling 1).
* ``set_phase`` — validates ONLY that the phase is one of the type's selected phases.
  Gates are approvals, not transition rules: a forward skip and a backwards move are both
  accepted. Re-stamping the current phase is a no-op (``changed=false``, no event, no new
  ``phase_updated_at``) unless ``restart`` is set, which starts a new visit of it (new
  ``phase_updated_at``, an event with from = to). ``if_current_phase`` is a compare-and-set.
* ``list_events`` — the phase history, newest first.

Every check-then-act has the database constraint behind it (the composite FK
``fk_pm_projects_selected_phase``, deferred, and ``ck_pm_projects_phase_needs_type``); a
race that slips past a check is a 409, not a 500.
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid7

import sqlalchemy.exc
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from auth.author_kind import AuthorKind, author_kind_of
from auth.pm_only import project_writes_are_pm_only_error
from models.pm import Project, ProjectBriefVersion, ProjectPhaseEvent
from models.pm_catalogue import ProjectPhase, ProjectType, ProjectTypePhase
from schemas.pm import (
    PhaseRef,
    ProjectPhaseEventOut,
    ProjectPhaseSetIn,
    ProjectPhaseSetOut,
)
from services import pm_project_brief_service as brief_svc
from services import pm_project_intake_service as intake
from services import pm_project_wip_service as wip
from services.entity_service import EntityService
from services.idempotency import BeforeCommit
from services.slug_retry import violated_constraint

_RACE = "conflicting concurrent change; retry the request"

# The project's creation identity (``POST``'s get-or-create key): set at create and
# IMMUTABLE afterwards. On update an omitted one is kept; a sent one must equal the stored
# value (fr round-trips whole objects); any other value is 409 ``origin_immutable``.
_ORIGIN_FIELDS = ("origin_kind", "origin_project_id", "origin_workflow_run_id")


def _omitted_fields(payload: Any) -> frozenset[str]:
    """The ``ProjectIn`` fields the client left out of a PUT: their stored values are kept.

    An explicit value (including an explicit null) is in ``model_fields_set`` and is written.
    """
    return frozenset(type(payload).model_fields) - payload.model_fields_set


def assert_origin_unchanged(project: Project, payload: Any) -> None:
    """409 ``origin_immutable`` when the payload sends an ``origin_*`` value that differs
    from the stored one (an explicit null against a stored value included). Omitted fields
    and equal values pass."""
    changed = [
        f
        for f in _ORIGIN_FIELDS
        if f in payload.model_fields_set and getattr(payload, f) != getattr(project, f)
    ]
    if changed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "origin_immutable",
                "message": (
                    f"origin_immutable: {', '.join(changed)} is set when the project is "
                    "created and cannot change; omit it or send the stored value"
                ),
                "field": changed[0],
                "fields": changed,
            },
        )


async def _commit(session: AsyncSession) -> None:
    """Commit; a constraint race (deferred FK, CHECK) is a 409, not a 500."""
    try:
        await session.commit()
    except sqlalchemy.exc.IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=_RACE) from exc


async def assert_project_type_ref(
    session: AsyncSession, project_type_id: UUID | None, tenant_id: str
) -> None:
    """A ``project_type_id`` must be the caller's tenant's own (else 422) and live.

    A RETIRED type (``retired_at`` set, George 2026-11-07) keeps every row for history, but it
    starts no new project and no project may be moved onto it: 422 ``project_type_retired``.
    """
    if project_type_id is None:
        return
    found = (
        await session.execute(
            select(ProjectType.id, ProjectType.retired_at).where(
                ProjectType.id == project_type_id, ProjectType.tenant_id == tenant_id
            )
        )
    ).first()
    if found is None:
        raise HTTPException(status_code=422, detail=f"unknown project_type_id {project_type_id}")
    if found.retired_at is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "project_type_retired",
                "message": (
                    f"project_type_retired: project type {project_type_id} is retired; it starts "
                    "no new project (its existing projects keep reading)"
                ),
            },
        )


async def type_phases(session: AsyncSession, project_type_id: UUID) -> list[PhaseRef]:
    """The type's selected phases, in order."""
    rows = (
        await session.execute(
            select(ProjectPhase.id, ProjectPhase.name)
            .join(ProjectTypePhase, ProjectTypePhase.phase_id == ProjectPhase.id)
            .where(ProjectTypePhase.project_type_id == project_type_id)
            .order_by(ProjectTypePhase.position)
        )
    ).all()
    return [PhaseRef(id=r.id, name=r.name) for r in rows]


async def _type_has_phase(session: AsyncSession, project_type_id: UUID, phase_id: UUID) -> bool:
    return (
        await session.execute(
            select(ProjectTypePhase.phase_id).where(
                ProjectTypePhase.project_type_id == project_type_id,
                ProjectTypePhase.phase_id == phase_id,
            )
        )
    ).first() is not None


async def assert_type_change_keeps_phase(
    session: AsyncSession, project: Project, new_type_id: UUID | None
) -> None:
    """A type change may not strand the current phase (409; never a silent clear)."""
    if project.current_phase_id is None or new_type_id == project.project_type_id:
        return
    if new_type_id is None or not await _type_has_phase(
        session, new_type_id, project.current_phase_id
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"project is in phase {project.current_phase!r}, which project_type_id "
                f"{new_type_id} does not select; move the phase first"
            ),
        )


def _author_kind(payload: Any) -> AuthorKind:
    """The router-attached author kind (``ProjectIn._author_kind``); none set = ``conveyor``
    (anything not proven fr-delegated is the conveyor)."""
    return getattr(payload, "_author_kind", None) or author_kind_of(None)


# Every project starts in this system phase of its type (George 10-13 v4 ruling 2 / D16).
SETUP_PHASE = "setup"
# The terminal project status; reaching it writes a ``closure`` phase event (fr cockpit A4).
CLOSED = "closed"


async def setup_phase_id(session: AsyncSession, project_type_id: UUID) -> UUID:
    """The type's ``setup`` phase (selected by the type); 409
    ``project_type_missing_setup_phase`` when it selects none."""
    found = (
        await session.execute(
            select(ProjectPhase.id)
            .join(ProjectTypePhase, ProjectTypePhase.phase_id == ProjectPhase.id)
            .where(
                ProjectTypePhase.project_type_id == project_type_id,
                ProjectPhase.name == SETUP_PHASE,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project_type_missing_setup_phase",
                "message": (
                    f"project_type_missing_setup_phase: project type {project_type_id} does "
                    f"not select a {SETUP_PHASE!r} phase; every project starts in it"
                ),
            },
        )
    return found


def _brief_required() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "brief_required",
            "message": (
                "brief_required: a project is created with its brief — it becomes version 1, "
                "the user's own; send ``brief``"
            ),
        },
    )


ORIGIN_PROJECT_INDEX = "uq_pm_projects_tenant_origin_project"


async def _origin_project_exists(
    session: AsyncSession, tenant_id: str, origin_project_id: UUID
) -> HTTPException | None:
    """409 ``origin_project_exists`` if the tenant already has a PM project for this fr project
    (R345: one per fr project for life, closed rows and every visibility included). The
    existing row's id is deliberately NOT returned: the caller cannot read that row, and it
    may be another subject's ``user`` row."""
    # Reached only when the caller's get-or-create could not land on that row: it is filed
    # under another visibility (or another subject's ``user`` row), so updating it in place
    # would write a row the caller cannot read.
    existing = (
        await session.execute(
            select(Project.id).where(
                Project.tenant_id == tenant_id,
                Project.origin_kind == "project",
                Project.origin_project_id == origin_project_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    return HTTPException(
        status_code=409,
        detail={
            "code": "origin_project_exists",
            "message": (
                f"origin_project_exists: fr project {origin_project_id} already has a PM project "
                "in this tenant; one PM project per fr project"
            ),
        },
    )


def _no_before_commit(before_commit: BeforeCommit | None) -> None:
    """Projects enrich their out model after the write (WIP limit) and run no keyed
    ``Idempotency-Key`` route, so a ``before_commit`` hook would stage the wrong body."""
    if before_commit is not None:
        raise TypeError("ProjectService does not support before_commit")


class ProjectService(EntityService):
    """Project CRUD with the ``project_type_id`` and ``wip_limit`` rules on top of
    ``EntityService``. Every ``ProjectOut`` it returns carries the effective WIP limit
    (``pm_project_wip_service.enrich``; one catalogue query per call, never per row)."""

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
        _no_before_commit(before_commit)
        if payload.brief is None:
            # v1 is written at create; it is `user` when fr creates the project on a
            # user's behalf (delegated), otherwise `conveyor`. Appending to a project
            # without v1 is refused (409 brief_missing_v1).
            raise _brief_required()
        if payload.project_type_id is None:
            # The route answers 422 project_type_required first; this is the backstop for
            # any other caller: there is no project without a type (and so a setup phase).
            raise HTTPException(status_code=422, detail="project_type_id is required")
        intake.assert_rewrite_shape(payload)
        await assert_project_type_ref(session, payload.project_type_id, tenant_id)
        setup_id = await setup_phase_id(session, payload.project_type_id)
        given, level_id = await intake.resolve_priority(session, payload, tenant_id)
        if given:
            payload = payload.model_copy(update={"priority_level_id": level_id})
        extra = dict(extra_columns or {})
        if intake.intake_given(payload):
            extra["intake_updated_at"] = intake.now()
        # The id is chosen here so the rows written in the SAME transaction as the project
        # (brief version 1, the first phase event) can reference it before the flush.
        extra["id"] = uuid7()
        now = datetime.now(UTC)
        # Every project starts in its type's setup phase, with the first phase event
        # (null → setup) — George 10-13 v4 ruling 2 / D16.
        extra["current_phase_id"] = setup_id
        extra["phase_updated_at"] = now
        session.add(
            ProjectPhaseEvent(
                tenant_id=tenant_id,
                project_id=extra["id"],
                from_phase_id=None,
                to_phase_id=setup_id,
                at=now,
                subject_id=subject_id or tenant_id,
            )
        )
        if payload.status == CLOSED:
            # Created closed: it closes in its setup phase.
            session.add(
                ProjectPhaseEvent(
                    tenant_id=tenant_id,
                    project_id=extra["id"],
                    from_phase_id=setup_id,
                    to_phase_id=setup_id,
                    at=now,
                    subject_id=subject_id or tenant_id,
                    kind="closure",
                )
            )
        if payload.brief is not None:
            # Version 1 sets the constraints freely; every later version must keep them.
            extra["current_brief_version"] = 1
            session.add(
                ProjectBriefVersion(
                    tenant_id=tenant_id,
                    project_id=extra["id"],
                    version=1,
                    # v1 sets no criterion check: every criterion starts ``unchecked``.
                    brief=brief_svc.validated_snapshot(
                        brief_svc.reconcile_criteria(payload.brief, None)
                    ),
                    author_kind=_author_kind(payload),
                    author_subject_id=subject_id or tenant_id,
                    created_at=extra.get("intake_updated_at") or intake.now(),
                )
            )
        with session.no_autoflush:
            out = await super().create(
                session,
                payload,
                tenant_id,
                subject_id,
                visibility=visibility,
                evaluate=evaluate,
                extra_columns=extra,
                snapshot_label=snapshot_label,
            )
        await wip.enrich(session, tenant_id, [out])
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
        """``EntityService.get_or_create``, with the brief rule: a brief is required only
        when a row is actually CREATED. A brief-less re-POST onto an existing project
        updates it in place (omitted brief = unchanged); one that would create → 422
        ``brief_required``."""
        if payload.brief is None:
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
                if conflict := await _origin_project_exists(
                    session, tenant_id, payload.origin_project_id
                ):
                    raise conflict
                raise _brief_required()
            updated = await self._update_identity_survivor(
                session, existing, payload, tenant_id, subject_id
            )
            return updated, False
        try:
            return await super().get_or_create(
                session,
                payload,
                tenant_id,
                subject_id,
                visibility=visibility,
                evaluate=evaluate,
                identity_fields=identity_fields,
                extra_filters=extra_filters,
            )
        except sqlalchemy.exc.IntegrityError as e:
            # The one-per-fr-project index fired but no survivor is reachable under the
            # caller's visibility (``EntityService.get_or_create`` re-raises then).
            if violated_constraint(e) != ORIGIN_PROJECT_INDEX:
                raise
            await session.rollback()
            conflict = await _origin_project_exists(session, tenant_id, payload.origin_project_id)
            if conflict is None:
                raise
            raise conflict from e

    async def _update_identity_survivor(
        self,
        session: AsyncSession,
        existing,
        payload,
        tenant_id: str,
        subject_id: str,
    ):
        """The get-or-create re-POST landed on an EXISTING project: an in-place write after
        the request. A fr-delegated caller may not make it (v4 ruling 1 / D15): 403
        ``project_writes_are_pm_only``. The conveyor keeps the idempotent update."""
        if _author_kind(payload) == "user":
            raise project_writes_are_pm_only_error()
        return await super()._update_identity_survivor(
            session, existing, payload, tenant_id, subject_id
        )

    async def get(
        self,
        session: AsyncSession,
        entity_id: UUID,
        tenant_id: str,
        subject_id: str = "",
        *,
        include_system: bool = False,
    ):
        out = await super().get(
            session, entity_id, tenant_id, subject_id, include_system=include_system
        )
        await wip.enrich(session, tenant_id, [out])
        intake.redact_foreign(tenant_id, [out])
        return out

    async def list_all(
        self,
        session: AsyncSession,
        tenant_id: str,
        subject_id: str = "",
        *,
        limit: int = 100,
        offset: int = 0,
        include_system: bool = False,
        **filters,
    ) -> tuple[list, int]:
        items, total = await super().list_all(
            session,
            tenant_id,
            subject_id,
            limit=limit,
            offset=offset,
            include_system=include_system,
            **filters,
        )
        await wip.enrich(session, tenant_id, items)
        intake.redact_foreign(tenant_id, items)
        return items, total

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
        reject_origin_change: bool = False,
        before_commit: BeforeCommit | None = None,
    ):
        """Update a project. ``origin_*`` is never rewritten: omitted or not, the stored
        creation identity is kept. ``reject_origin_change`` (the PUT route only) makes a
        differing sent value a 409 ``origin_immutable``; without it (the get-or-create
        re-POST, the idempotent "give me the existing row" path) a differing value is
        ignored."""
        _no_before_commit(before_commit)
        obj = await self._get_mutable(
            session, entity_id, tenant_id=tenant_id, subject_id=subject_id
        )
        # Omitted = kept, explicit null clears — for EVERY ProjectIn field, not just the
        # intake: a partial PUT must never reset status / created_by_user_id / dates / ... to
        # their schema defaults. Read before any ``model_copy`` (it adds to fields_set).
        keep = _omitted_fields(payload)
        if reject_origin_change:
            assert_origin_unchanged(obj, payload)
        # The stored creation identity always wins: on PUT every sent origin_* equals it
        # (checked above); on the re-POST a differing one is ignored, never written.
        keep |= frozenset(_ORIGIN_FIELDS)
        given, level_id = await intake.resolve_priority(session, payload, tenant_id)
        if given:
            payload = payload.model_copy(update={"priority_level_id": level_id})
            keep = keep - {"priority_level_id"}
        intake.assert_shape_unchanged(obj, payload)
        intake.assert_rewrite_shape(payload, obj)
        for kept in ("wip_limit", "rework_limit"):
            if kept not in payload.model_fields_set:
                # Omitted = kept, like the type; an explicit null clears it.
                payload = payload.model_copy(update={kept: getattr(obj, kept)})
        if "project_type_id" not in payload.model_fields_set:
            # PUT replaces the other fields; the type is RM-owned state, so omitted = kept.
            payload = payload.model_copy(update={"project_type_id": obj.project_type_id})
        else:
            await assert_project_type_ref(session, payload.project_type_id, tenant_id)
            await assert_type_change_keeps_phase(session, obj, payload.project_type_id)
        if "status" not in keep and payload.status == "closed":
            # Lock first, so two concurrent closes see one transition (one closure event).
            await session.refresh(obj, attribute_names=["status"], with_for_update=True)
            stage_closure_event(session, obj, subject_id or tenant_id)
        # Every check has passed: stage the intake changes in the same transaction.
        if payload.brief is not None:
            # Appends the next version (no compare-and-set; constraints must be unchanged —
            # 422 brief_constraints_immutable; 409 brief_missing_v1 without a v1); stamps
            # intake_updated_at.
            await brief_svc.stage_version(
                session,
                obj,
                payload.brief,
                author_kind=_author_kind(payload),
                subject_id=subject_id,
            )
        elif intake.intake_changed(obj, payload, keep | keep_fields):
            obj.intake_updated_at = intake.now()
        try:
            out = await super().update(
                session,
                entity_id,
                payload,
                tenant_id,
                subject_id,
                evaluate=evaluate,
                record_snapshot=record_snapshot,
                keep_fields=keep | keep_fields,
            )
        except sqlalchemy.exc.IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=_RACE) from exc
        await wip.enrich(session, tenant_id, [out])
        return out


def stage_closure_event(session: AsyncSession, project: Project, subject_id: str) -> None:
    """Stage the ``closure`` phase event when ``project`` is not yet ``closed`` (the caller
    is moving it there and commits). Idempotent: an already-closed project gets none. A
    project without a phase (a legacy, untyped row) has no phase history to close."""
    if project.status == CLOSED or project.current_phase_id is None:
        return
    session.add(
        ProjectPhaseEvent(
            tenant_id=project.tenant_id,
            project_id=project.id,
            from_phase_id=project.current_phase_id,
            to_phase_id=project.current_phase_id,
            at=datetime.now(UTC),
            subject_id=subject_id,
            kind="closure",
        )
    )


def _matches(expected: UUID | str | None, current: PhaseRef | None) -> bool:
    """``if_current_phase`` (id, name, or None = no phase) against the current phase."""
    if expected is None:
        return current is None
    if current is None:
        return False
    if isinstance(expected, UUID):
        return expected == current.id
    try:
        return UUID(expected) == current.id
    except ValueError:
        return expected == current.name


async def set_phase(
    service: ProjectService,
    session: AsyncSession,
    eid: UUID,
    body: ProjectPhaseSetIn,
    tenant_id: str,
    subject_id: str,
) -> ProjectPhaseSetOut:
    project = await service._get_mutable(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    # Serialise concurrent phase writes on this project so the compare-and-set is real.
    await session.refresh(project, with_for_update=True)
    if project.project_type_id is None:
        raise HTTPException(status_code=409, detail="project has no project_type_id")

    allowed = await type_phases(session, project.project_type_id)
    current = (
        PhaseRef(id=project.current_phase_id, name=project.current_phase or "")
        if project.current_phase_id is not None
        else None
    )
    if "if_current_phase" in body.model_fields_set and not _matches(body.if_current_phase, current):
        actual = json.dumps(current.model_dump(mode="json") if current else None)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "phase_mismatch",
                "message": f"if_current_phase does not match; current_phase={actual}",
            },
        )

    target = next(
        (p for p in allowed if (p.id == body.phase_id if body.phase_id else p.name == body.phase)),
        None,
    )
    if target is None:
        asked = body.phase_id if body.phase_id is not None else body.phase
        raise HTTPException(
            status_code=422,
            detail=(
                f"phase {str(asked)!r} is not one of this project type's phases; "
                f"allowed: {[p.name for p in allowed]}"
            ),
        )

    if current is not None and current.id == target.id and not body.restart:
        # Idempotent re-stamp: no write, no event, phase_updated_at unchanged. Nothing is
        # flushed; the session's teardown ends the transaction and releases the row lock.
        return ProjectPhaseSetOut(
            project_id=project.id,
            phase=target,
            previous_phase=current,
            phase_updated_at=project.phase_updated_at,
            changed=False,
        )

    now = datetime.now(UTC)
    project.current_phase_id = target.id
    project.phase_updated_at = now
    session.add(
        ProjectPhaseEvent(
            tenant_id=tenant_id,
            project_id=project.id,
            from_phase_id=current.id if current else None,
            to_phase_id=target.id,
            at=now,
            subject_id=subject_id or tenant_id,
        )
    )
    await _commit(session)
    return ProjectPhaseSetOut(
        project_id=eid,
        phase=target,
        previous_phase=current,
        phase_updated_at=now,
        changed=True,
    )


async def list_events(
    service: ProjectService,
    session: AsyncSession,
    eid: UUID,
    tenant_id: str,
    subject_id: str,
    *,
    limit: int,
    offset: int,
) -> tuple[list[ProjectPhaseEventOut], int]:
    await service._get_visible(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    src, dst = aliased(ProjectPhase), aliased(ProjectPhase)
    where = (ProjectPhaseEvent.project_id == eid, ProjectPhaseEvent.tenant_id == tenant_id)
    total = (
        await session.execute(select(func.count()).select_from(ProjectPhaseEvent).where(*where))
    ).scalar() or 0
    rows = (
        await session.execute(
            select(ProjectPhaseEvent, src.name, dst.name)
            .outerjoin(src, src.id == ProjectPhaseEvent.from_phase_id)
            .outerjoin(dst, dst.id == ProjectPhaseEvent.to_phase_id)
            .where(*where)
            .order_by(ProjectPhaseEvent.at.desc(), ProjectPhaseEvent.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    out = []
    for event, from_name, to_name in rows:
        item = ProjectPhaseEventOut.model_validate(event)
        item.from_phase, item.to_phase = from_name, to_name
        out.append(item)
    return out, total
