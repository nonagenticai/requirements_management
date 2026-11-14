"""Kanban-coach ADMISSION (R311, kanban-coach-errors.md §3, §4.3, §4.4).

RM is the only arbiter between two coaches of one incident and the only place the caps are
counted. Everything here runs in ONE transaction under the project row lock
(``session.refresh(project, with_for_update=True)``, the repo's serialisation pattern), so
every admission of a project is serialised.

* ``admit`` (``POST``): (1) lock; (2) a row of this ``coach_execution_id`` ⇒ answered
  unchanged (idempotent re-POST); (3) a row of this incident key ⇒ nothing inserted,
  ``{admitted: false, reason: same_incident, winner}``; (4) count by EFFECTIVE remedy
  (``coalesce(applied_remedy, remedy)``; ``note`` rows count for no cap): the project's
  distinct incidents ``(root_execution_id, exit_group)`` of the current phase visit
  (``phase_visit_at IS NOT DISTINCT FROM project.phase_updated_at``, both read under the
  lock), the target's rows of the current phase visit, the target + entry rows; apply the
  caps, moving the requested remedy only UP the order note < rerun = move = repull < ask <
  block; (5) insert with ``phase`` / ``phase_visit_at`` read under the lock. A unique
  violation at commit (a path the lock misses) is answered as (2) or (3).
* ``record_applied`` (``PATCH``): same lock. Only the row's own coach, only an admitted row;
  the applied remedy may only KEEP the admitted one, DROP to ``note``, or (admitted remedy
  automatic) UPGRADE to ``ask`` / ``block`` with ``reason: post_admit_unverified``. RM applies
  the per-entry rule: an ``ask`` upgrade becomes ``block`` when another row of this target +
  entry already has effective ``ask``. Idempotent for the same value; a different value once
  set is 409.
* ``list_actions`` (``GET``): the rows, oldest first.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy.exc
from fastapi import HTTPException
from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.pm import COACH_AUTOMATIC_REMEDIES, COACH_REMEDY_ORDER, CoachAction, Project
from schemas.pm_coach import (
    CoachActionAppliedIn,
    CoachActionIn,
    CoachActionOut,
    CoachAdmissionOut,
    CoachCaps,
)
from services.pm_project_phase_service import ProjectService

SAME_INCIDENT = "same_incident"
POST_ADMIT = "post_admit_unverified"
# The post-Admit ask upgrade that RM turned into a block (another effective ask in the entry).
POST_ADMIT_ASK_IN_ENTRY = "post_admit_unverified_ask_in_entry"

_EFFECTIVE = func.coalesce(CoachAction.applied_remedy, CoachAction.remedy)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": f"{code}: {message}"})


def _rank(remedy: str) -> int:
    return COACH_REMEDY_ORDER.index(remedy)


def apply_caps(
    requested: str,
    *,
    ask_allowed: bool,
    caps: CoachCaps,
    incident_rank: int,
    card_count: int,
    entry_automatic: int,
    entry_asks: int,
) -> tuple[str, str | None]:
    """The §4.4 cap table over the counts BEFORE this row: (remedy, reason it moved up).

    Only ever moves ``requested`` up the order; the first rule that raises it to a level
    names the reason. ``incident_rank`` is this request's incident's 1-based place, in
    admission order, among the current phase visit's incidents (a new incident ranks last + 1).
    A row of an incident already counted adds no incident (a ``pull`` exit that fans out to
    six moved cards is one incident), and it inherits that incident's place: a sibling of the
    10th incident is within the cap, a sibling of the 11th (itself blocked) is blocked.
    """
    remedy, reason = requested, None

    def up(to: str, why: str) -> None:
        nonlocal remedy, reason
        if _rank(to) > _rank(remedy):
            remedy, reason = to, why

    if incident_rank > caps.project:
        up("block", "project_cap")
    if card_count >= caps.card:
        up("block", "card_cap")
    # After any admitted ask in this entry, every later request in it (automatic or ask) is a
    # block (§4.4 per-entry row); the §4.6-comment half arrives as ``ask_allowed: false``.
    if entry_asks > 0:
        up("block", "ask_in_entry")
    if requested in COACH_AUTOMATIC_REMEDIES and entry_automatic >= caps.entry:
        up("ask", "entry_cap")
    if remedy == "ask" and not ask_allowed:
        up("block", "ask_not_allowed")
    return remedy, reason


def _incident_rank(incidents: list[tuple[str, str]], body: CoachActionIn) -> int:
    key = (body.root_execution_id, body.exit_group)
    return incidents.index(key) + 1 if key in incidents else len(incidents) + 1


def _out(row: CoachAction) -> CoachActionOut:
    data: dict[str, Any] = {
        name: getattr(row, name)
        for name in CoachActionOut.model_fields
        if name != "effective_remedy"
    }
    return CoachActionOut(**data, effective_remedy=row.applied_remedy or row.remedy)


async def _locked_project(
    service: ProjectService, session: AsyncSession, eid: UUID, tenant_id: str, subject_id: str
) -> Project:
    project = await service._get_mutable(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    # Serialise every admission (and PATCH) of this project: the arbiter and the counts.
    await session.refresh(project, with_for_update=True)
    return project


async def _by_coach(session: AsyncSession, tenant_id: str, coach_execution_id: str):
    return (
        await session.execute(
            select(CoachAction).where(
                CoachAction.tenant_id == tenant_id,
                CoachAction.coach_execution_id == coach_execution_id,
            )
        )
    ).scalar_one_or_none()


async def _by_incident(
    session: AsyncSession, project_id: UUID, tenant_id: str, body: CoachActionIn
):
    return (
        await session.execute(
            select(CoachAction).where(
                CoachAction.tenant_id == tenant_id,
                CoachAction.project_id == project_id,
                CoachAction.target_card_id == body.target_card_id,
                CoachAction.entry == body.entry,
                CoachAction.root_execution_id == body.root_execution_id,
                CoachAction.exit_group == body.exit_group,
            )
        )
    ).scalar_one_or_none()


async def _counts(
    session: AsyncSession, project: Project, tenant_id: str, target_card_id: str, entry: str
) -> dict[str, Any]:
    """Every count by effective remedy; ``note`` rows count for no cap."""
    base = (CoachAction.tenant_id == tenant_id, CoachAction.project_id == project.id)
    acting = _EFFECTIVE != "note"
    # The project and per-card caps count the current phase visit only: a phase change or a
    # restart (``PUT .../phase`` with ``restart: true``) bumps ``phase_updated_at`` and both
    # start again at zero.
    this_visit = CoachAction.phase_visit_at.is_not_distinct_from(project.phase_updated_at)
    # The visit's incidents in admission order (uuid7 ids sort by time, also as text;
    # admissions of a project are serialised by the row lock).
    incidents = [
        (root, group)
        for root, group in (
            await session.execute(
                select(CoachAction.root_execution_id, CoachAction.exit_group)
                .where(*base, acting, this_visit)
                .group_by(CoachAction.root_execution_id, CoachAction.exit_group)
                .order_by(func.min(cast(CoachAction.id, Text)))
            )
        ).all()
    ]
    card = (
        await session.execute(
            select(func.count())
            .select_from(CoachAction)
            .where(*base, acting, this_visit, CoachAction.target_card_id == target_card_id)
        )
    ).scalar_one()
    entry_rows = (
        await session.execute(
            select(_EFFECTIVE, func.count())
            .where(*base, CoachAction.target_card_id == target_card_id, CoachAction.entry == entry)
            .group_by(_EFFECTIVE)
        )
    ).all()
    by_remedy = {remedy: n for remedy, n in entry_rows}
    return {
        "incidents": incidents,
        "card": card,
        "entry_automatic": sum(by_remedy.get(r, 0) for r in COACH_AUTOMATIC_REMEDIES),
        "entry_asks": by_remedy.get("ask", 0),
    }


async def _answer(
    session: AsyncSession, project: Project, tenant_id: str, row: CoachAction, *, replayed: bool
) -> CoachAdmissionOut:
    counts = await _counts(session, project, tenant_id, row.target_card_id, row.entry)
    return CoachAdmissionOut(
        id=row.id,
        admitted=row.admitted,
        remedy=row.remedy,
        reason=row.reason,
        replayed=replayed,
        project_incidents=len(counts["incidents"]),
        card_count=counts["card"],
        entry_count=counts["entry_automatic"],
    )


def _loser(winner: CoachAction) -> CoachAdmissionOut:
    return CoachAdmissionOut(admitted=False, reason=SAME_INCIDENT, winner=winner.coach_execution_id)


async def _replay(
    session: AsyncSession, project: Project, tenant_id: str, row: CoachAction
) -> CoachAdmissionOut:
    if row.project_id != project.id:
        raise _error(
            409,
            "coach_execution_id_reused",
            "this coach_execution_id was admitted on another project",
        )
    return await _answer(session, project, tenant_id, row, replayed=True)


async def admit(
    service: ProjectService,
    session: AsyncSession,
    eid: UUID,
    body: CoachActionIn,
    tenant_id: str,
    subject_id: str,
) -> CoachAdmissionOut:
    project = await _locked_project(service, session, eid, tenant_id, subject_id)

    existing = await _by_coach(session, tenant_id, body.coach_execution_id)
    if existing is not None:
        return await _replay(session, project, tenant_id, existing)
    winner = await _by_incident(session, project.id, tenant_id, body)
    if winner is not None:
        return _loser(winner)

    counts = await _counts(session, project, tenant_id, body.target_card_id, body.entry)
    remedy, reason = apply_caps(
        body.requested_remedy,
        ask_allowed=body.ask_allowed,
        caps=body.caps,
        incident_rank=_incident_rank(counts["incidents"], body),
        card_count=counts["card"],
        entry_automatic=counts["entry_automatic"],
        entry_asks=counts["entry_asks"],
    )
    row = CoachAction(
        tenant_id=tenant_id,
        project_id=project.id,
        card_id=body.card_id,
        target_card_id=body.target_card_id,
        exit_group=body.exit_group,
        entry=body.entry,
        phase=project.current_phase,
        phase_visit_at=project.phase_updated_at,
        error_class=body.error_class,
        requested_remedy=body.requested_remedy,
        remedy=remedy,
        admitted=True,
        reason=reason,
        source_execution_id=body.source_execution_id,
        root_execution_id=body.root_execution_id,
        coach_execution_id=body.coach_execution_id,
    )
    session.add(row)
    try:
        await session.flush()
        answer = await _answer(session, project, tenant_id, row, replayed=False)
        await session.commit()
    except sqlalchemy.exc.IntegrityError as exc:
        # A race the lock does not cover: answer it as the idempotent replay or the loser.
        await session.rollback()
        project = await _locked_project(service, session, eid, tenant_id, subject_id)
        existing = await _by_coach(session, tenant_id, body.coach_execution_id)
        if existing is not None:
            return await _replay(session, project, tenant_id, existing)
        winner = await _by_incident(session, project.id, tenant_id, body)
        if winner is not None:
            return _loser(winner)
        raise _error(409, "conflict", "conflicting concurrent change; retry the request") from exc
    return answer


def _same_value(row: CoachAction, wanted: str) -> bool:
    """Is ``wanted`` the value this row already recorded (an ``ask`` RM turned into a
    ``block`` is recorded as that ask)?"""
    converted = row.reason == POST_ADMIT_ASK_IN_ENTRY
    if converted:
        return wanted == "ask"
    return row.applied_remedy == wanted


async def record_applied(
    service: ProjectService,
    session: AsyncSession,
    eid: UUID,
    action_id: UUID,
    body: CoachActionAppliedIn,
    tenant_id: str,
    subject_id: str,
) -> CoachActionOut:
    project = await _locked_project(service, session, eid, tenant_id, subject_id)
    row = (
        await session.execute(
            select(CoachAction).where(
                CoachAction.id == action_id,
                CoachAction.project_id == project.id,
                CoachAction.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.coach_execution_id != body.coach_execution_id:
        raise _error(409, "coach_execution_mismatch", "only the admitted coach records its remedy")
    if not row.admitted:
        raise _error(409, "not_admitted", "the row was not admitted")

    wanted = body.applied_remedy
    if row.applied_remedy is not None:
        if _same_value(row, wanted):
            return _out(row)
        raise _error(
            409,
            "applied_remedy_already_set",
            f"applied_remedy is already {row.applied_remedy!r}",
        )

    upgrade = row.remedy in COACH_AUTOMATIC_REMEDIES and wanted in ("ask", "block")
    if wanted not in (row.remedy, "note") and not upgrade:
        raise _error(
            422,
            "applied_remedy_not_allowed",
            f"admitted {row.remedy!r} may only be kept, dropped to 'note'"
            + (", or upgraded to 'ask'/'block'" if row.remedy in COACH_AUTOMATIC_REMEDIES else "")
            + f"; got {wanted!r}",
        )
    if upgrade and body.reason != POST_ADMIT:
        raise _error(
            422,
            "reason_required",
            f"an upgrade to {wanted!r} needs reason {POST_ADMIT!r}",
        )

    effective, reason = wanted, row.reason
    if upgrade:
        reason = POST_ADMIT
        if wanted == "ask":
            other_asks = (
                await session.execute(
                    select(func.count())
                    .select_from(CoachAction)
                    .where(
                        CoachAction.tenant_id == tenant_id,
                        CoachAction.project_id == project.id,
                        CoachAction.target_card_id == row.target_card_id,
                        CoachAction.entry == row.entry,
                        CoachAction.id != row.id,
                        _EFFECTIVE == "ask",
                    )
                )
            ).scalar_one()
            if other_asks:
                effective, reason = "block", POST_ADMIT_ASK_IN_ENTRY

    row.applied_remedy = effective
    row.applied_at = datetime.now(UTC)
    row.reason = reason
    await session.commit()
    return _out(row)


async def list_actions(
    service: ProjectService,
    session: AsyncSession,
    eid: UUID,
    tenant_id: str,
    subject_id: str,
    *,
    target_card_id: str | None,
    limit: int,
    offset: int,
) -> tuple[list[CoachActionOut], int]:
    await service._get_visible(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    where = [CoachAction.tenant_id == tenant_id, CoachAction.project_id == eid]
    if target_card_id is not None:
        where.append(CoachAction.target_card_id == target_card_id)
    total = (
        await session.execute(select(func.count()).select_from(CoachAction).where(*where))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(CoachAction)
                .where(*where)
                .order_by(CoachAction.created_at, CoachAction.id)
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_out(r) for r in rows], total
