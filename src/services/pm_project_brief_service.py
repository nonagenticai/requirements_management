"""Versioned project briefs (George 2026-10-13).

RM stores the project BRIEF; the conveyor's enhancements to it are stored in RM too; fr
shows the brief WITH VERSIONS. Every version is a FULL snapshot validated against
``schemas.pm_intake.ProjectBrief`` in ``pm_project_brief_versions`` (append-only);
``pm_projects.current_brief_version`` names the current one.

* The next number is assigned under a row lock on the project (``SELECT … FOR UPDATE``), so
  concurrent appends serialise: the second sees the first's number. The UNIQUE
  ``(project_id, version)`` is the backstop and maps to 409.
* ``if_current_version`` is a compare-and-set, checked under the same lock.
* Re-sending an identical snapshot still creates a version — intentionally: the ``note``
  can say why, and the history records that someone re-affirmed it.
* ``author_subject_id`` is the caller's principal; the body cannot name it.
* ``author_kind`` is derived from the principal (``auth.author_kind``): ``user`` iff the
  call is fr-delegated, else ``conveyor``. Never a body field.
* ⛔ v1 is written at create (where the brief is required — 422 ``brief_required``); it is
  ``user`` when fr creates the project on a user's behalf (delegated), otherwise
  ``conveyor``. Appending to a project without v1 is refused (409 ``brief_missing_v1``).
* ⛔ CONSTRAINTS ARE IMMUTABLE AFTER v1, for every author (422
  ``brief_constraints_immutable``). fr's ``ProjectBriefUpdateSchema`` leaves ``constraints``
  out on purpose — "the conveyor must never be able to loosen the gate that checks its
  work" — so neither a user nor the conveyor may change them once a version exists. Compared
  normalised: the same list in the same order, deep-equal (absent = null = ``[]``). Version 1
  (on create) sets them freely.
* Acceptance criteria are items with a SERVER-OWNED check (``status``, ``evidence``,
  ``checked_at``, ``checked_by``; R-CK7). Every version a brief write stages goes through
  ``reconcile_criteria``: a criterion keeps the current version's check when it is sent with
  the same id and text, or (unmatched) with the same text — so re-sending the list as plain
  strings keeps ids and checks; any other criterion is ``unchecked``. Only
  ``set_criterion_check`` (``PATCH /pm/projects/{eid}/criteria/{criterion_id}``, PM / n8n
  only) changes a check; it appends a version like any other write.
"""

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy.exc
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.pm import Project, ProjectBriefVersion
from schemas.pm_intake import (
    AcceptanceCriterion,
    BriefVersionOut,
    BriefVersionSummaryOut,
    CriterionCheckIn,
    CriterionCheckOut,
    ProjectBrief,
)

_UNSET = object()


def normalised_constraints(snapshot: dict | None) -> list:
    """The constraints of a stored/sent snapshot in comparable form (absent/null → [])."""
    if not snapshot:
        return []
    return ProjectBrief.model_validate(snapshot).snapshot().get("constraints") or []


def validated_snapshot(brief: ProjectBrief) -> dict:
    """``brief``'s stored form, re-validated as a whole ``ProjectBrief``.

    ⛔ Every brief the server BUILDS (``model_copy`` skips validators) goes through this
    before it is staged: a criterion check or a carried-over check adds evidence, and a
    stored brief over the 128 KiB cap fails every later read and write — the project's
    brief would be bricked. A failure is the same 422 envelope as an oversize brief sent
    in a request body (``body.brief:brief_over_128_kib``), and nothing is staged.
    """
    snapshot = brief.snapshot()
    try:
        ProjectBrief.model_validate(snapshot)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**e, "loc": ("body", "brief", *e["loc"])} for e in exc.errors()]
        ) from exc
    return snapshot


def reconcile_criteria(brief: ProjectBrief, stored: dict | None) -> ProjectBrief:
    """``brief`` with every acceptance criterion's check taken from ``stored`` (the current
    version's snapshot) or cleared — a brief write never sets a check itself.

    Match order: the same id with the same text keeps the stored item's id and check; then
    an unmatched stored item with the same text gives its id and check (plain strings
    re-sent). An id whose text changed keeps the id but is ``unchecked``. Everything else is
    ``unchecked``.
    """
    sent = brief.acceptance_criteria
    if sent is None:
        return brief
    previous = [
        AcceptanceCriterion.model_validate(c)
        for c in ((stored or {}).get("acceptance_criteria") or [])
    ]
    by_id = {c.id: c for c in previous}
    used: set[str] = set()
    out: list[AcceptanceCriterion] = []
    pending: list[tuple[int, AcceptanceCriterion]] = []
    for i, c in enumerate(sent):
        old = by_id.get(c.id)
        if old is not None and old.text == c.text:
            used.add(old.id)
            out.append(old)
        else:
            out.append(c.unchecked())
            if old is None:
                pending.append((i, c))
            else:
                used.add(old.id)
    taken = {c.id for c in out}
    for i, c in pending:
        twin = next(
            (o for o in previous if o.id not in used and o.id not in taken and o.text == c.text),
            None,
        )
        if twin is not None:
            used.add(twin.id)
            taken.discard(c.id)
            taken.add(twin.id)
            out[i] = twin
    return brief.model_copy(update={"acceptance_criteria": out})


async def _current_snapshot(session: AsyncSession, project: Project) -> dict | None:
    if project.current_brief_version is None:
        return None
    return (
        await session.execute(
            select(ProjectBriefVersion.brief).where(
                ProjectBriefVersion.project_id == project.id,
                ProjectBriefVersion.tenant_id == project.tenant_id,
                ProjectBriefVersion.version == project.current_brief_version,
            )
        )
    ).scalar_one_or_none()


async def assert_constraints_unchanged(
    session: AsyncSession, project: Project, brief: ProjectBrief
) -> None:
    """422 ``brief_constraints_immutable`` unless ``brief`` keeps the current constraints.

    No current version → nothing to compare (``stage_version`` refuses that case first).
    """
    current = project.current_brief_version
    if current is None:
        return
    stored = (
        await session.execute(
            select(ProjectBriefVersion.brief).where(
                ProjectBriefVersion.project_id == project.id,
                ProjectBriefVersion.tenant_id == project.tenant_id,
                ProjectBriefVersion.version == current,
            )
        )
    ).scalar_one_or_none()
    if normalised_constraints(stored) != normalised_constraints(brief.snapshot()):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "brief_constraints_immutable",
                "message": (
                    "brief_constraints_immutable: constraints are fixed by brief version 1 "
                    f"and cannot change (current version {current}); send them unchanged"
                ),
                "current_version": current,
            },
        )


async def stage_version(
    session: AsyncSession,
    project: Project,
    brief: ProjectBrief,
    *,
    author_kind: str,
    subject_id: str,
    note: str | None = None,
    if_current_version: object = _UNSET,
    now: datetime | None = None,
) -> ProjectBriefVersion:
    """Lock the project row, check the compare-and-set and the constraints, stage the next
    version.

    The caller commits (one transaction with whatever else it changes on the project).
    """
    await session.refresh(project, attribute_names=["current_brief_version"], with_for_update=True)
    current = project.current_brief_version
    if current is None:
        # v1 is written only at creation, by the user; a legacy project without one cannot
        # gain it through an append (that would let the conveyor author v1).
        raise HTTPException(
            status_code=409,
            detail={
                "code": "brief_missing_v1",
                "message": (
                    "brief_missing_v1: this project has no brief version 1; version 1 is "
                    "written only when the project is created, so no later version can be "
                    "appended"
                ),
            },
        )
    if if_current_version is not _UNSET and if_current_version != current:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "brief_version_mismatch",
                "message": (
                    f"brief_version_mismatch: if_current_version={if_current_version} but the "
                    f"current brief version is {current}"
                ),
                "current_version": current,
            },
        )
    # Under the row lock, so the "current" constraints cannot move under the check.
    await assert_constraints_unchanged(session, project, brief)
    brief = reconcile_criteria(brief, await _current_snapshot(session, project))
    return _stage_row(
        session,
        project,
        validated_snapshot(brief),
        author_kind=author_kind,
        subject_id=subject_id,
        note=note,
        now=now,
    )


def _stage_row(
    session: AsyncSession,
    project: Project,
    snapshot: dict,
    *,
    author_kind: str,
    subject_id: str,
    note: str | None,
    now: datetime | None = None,
) -> ProjectBriefVersion:
    """Stage the next version (the caller holds the project row lock and commits)."""
    current = project.current_brief_version
    at = now or datetime.now(UTC)
    row = ProjectBriefVersion(
        tenant_id=project.tenant_id,
        project_id=project.id,
        version=(current or 0) + 1,
        brief=snapshot,
        author_kind=author_kind,
        author_subject_id=subject_id or project.tenant_id,
        note=note,
        created_at=at,
    )
    session.add(row)
    project.current_brief_version = row.version
    project.intake_updated_at = at
    return row


async def commit(session: AsyncSession) -> None:
    """Commit; a lost version race (UNIQUE / CHECK) is a 409, not a 500."""
    try:
        await session.commit()
    except sqlalchemy.exc.IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="conflicting concurrent brief change; retry the request"
        ) from exc


def _out(row: ProjectBriefVersion) -> BriefVersionOut:
    return BriefVersionOut(
        project_id=row.project_id,
        version=row.version,
        author_kind=row.author_kind,  # ty: ignore[invalid-argument-type]
        author_subject_id=row.author_subject_id,
        note=row.note,
        created_at=row.created_at,
        brief=ProjectBrief.model_validate(row.brief),
    )


async def append(
    service,
    session: AsyncSession,
    eid: UUID,
    *,
    brief: ProjectBrief,
    author_kind: str,
    note: str | None,
    if_current_version: object,
    tenant_id: str,
    subject_id: str,
) -> BriefVersionOut:
    project = await service._get_mutable(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    row = await stage_version(
        session,
        project,
        brief,
        author_kind=author_kind,
        subject_id=subject_id,
        note=note,
        if_current_version=if_current_version,
    )
    out = _out(row)
    await commit(session)
    return out


async def list_versions(
    service,
    session: AsyncSession,
    eid: UUID,
    *,
    tenant_id: str,
    subject_id: str,
    limit: int,
    offset: int,
) -> tuple[list[BriefVersionSummaryOut], int]:
    await service._get_visible(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    where = (ProjectBriefVersion.project_id == eid, ProjectBriefVersion.tenant_id == tenant_id)
    total = (
        await session.execute(select(func.count()).select_from(ProjectBriefVersion).where(*where))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(ProjectBriefVersion)
                .where(*where)
                .order_by(ProjectBriefVersion.version.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [BriefVersionSummaryOut.model_validate(r) for r in rows], total


async def get_version(
    service,
    session: AsyncSession,
    eid: UUID,
    version: int,
    *,
    tenant_id: str,
    subject_id: str,
) -> BriefVersionOut:
    await service._get_visible(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    row = (
        await session.execute(
            select(ProjectBriefVersion).where(
                ProjectBriefVersion.project_id == eid,
                ProjectBriefVersion.tenant_id == tenant_id,
                ProjectBriefVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"project has no brief version {version}")
    return _out(row)


async def set_criterion_check(
    service,
    session: AsyncSession,
    eid: UUID,
    criterion_id: str,
    body: CriterionCheckIn,
    *,
    author_kind: str,
    tenant_id: str,
    subject_id: str,
) -> CriterionCheckOut:
    """Set one criterion's check (status + evidence; RM stamps ``checked_at`` /
    ``checked_by``) by appending the next brief version. Idempotent: the same status and
    evidence again is ``changed: false`` and writes nothing. 404 when the current brief has
    no criterion with that id."""
    project = await service._get_mutable(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    await session.refresh(project, attribute_names=["current_brief_version"], with_for_update=True)
    snapshot = await _current_snapshot(session, project)
    brief = ProjectBrief.model_validate(snapshot) if snapshot is not None else None
    criteria = list((brief.acceptance_criteria if brief else None) or [])
    index = next((i for i, c in enumerate(criteria) if c.id == criterion_id), None)
    if brief is None or index is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "criterion_not_found",
                "message": (
                    f"criterion_not_found: the current brief has no acceptance criterion "
                    f"{criterion_id!r}"
                ),
            },
        )
    old = criteria[index]
    if old.status == body.status and old.evidence == body.evidence:
        return CriterionCheckOut(
            project_id=project.id,
            brief_version=project.current_brief_version or 0,
            changed=False,
            criterion=old,
        )
    now = datetime.now(UTC)
    if body.status == "unchecked":
        new = old.unchecked()
    else:
        new = old.model_copy(
            update={
                "status": body.status,
                "evidence": list(body.evidence),
                "checked_at": now,
                "checked_by": subject_id or tenant_id,
            }
        )
    criteria[index] = new
    updated = brief.model_copy(update={"acceptance_criteria": criteria})
    row = _stage_row(
        session,
        project,
        validated_snapshot(updated),
        author_kind=author_kind,
        subject_id=subject_id,
        note=f"acceptance criterion {criterion_id}: {body.status}",
        now=now,
    )
    version = row.version
    await commit(session)
    return CriterionCheckOut(project_id=eid, brief_version=version, changed=True, criterion=new)
