"""Per-project CAPABILITY selection (R260 v1, George 2026-10-15).

The project type's composition is the first filter; the PM then selects which capabilities a
project needs. Only PM / n8n writes the selection (v4 ruling 1, the router's ``PM_ONLY``),
like the phase; fr reads it.

* ``get`` — the selection, the implicit capabilities (``project_management`` and
  ``kanban_coach``: every project has them, nobody selects them) and the SELECTABLE ones:
  every capability with at least one card type in the project type's composition
  (``pm_project_type_card_types`` ⋈ ``pm_card_types.capability_id``), minus the implicit two,
  each with its card types per phase in the type's phase order.
* ``set_capabilities`` — replace-set under a row lock on the project. An event only when
  the NAME set changes; a change to a kept row's reason or R302 decision (involvement,
  purpose, rigor, complexity, consult, order_after) updates the row and writes none
  (``changed=false``). ``if_current`` is a compare-and-set on the name set. Rows that stay
  keep their ``approval_id`` / ``subject_id`` / ``created_at``; added rows carry the body's
  ``approval_id``.
* ``list_events`` — the selection history, newest first.

The UNIQUE ``(project_id, capability_id)`` is the backstop behind the check-then-act; a race
that slips past the lock is a 409, not a 500 (``_commit``).
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.pm import Project, ProjectCapability, ProjectCapabilityEvent
from models.pm_catalogue import (
    Capability,
    CardType,
    ProjectPhase,
    ProjectType,
    ProjectTypeCardType,
    ProjectTypePhase,
)
from schemas.pm import (
    IMPLICIT_CAPABILITIES,
    ProjectCapabilitiesOut,
    ProjectCapabilitiesSetIn,
    ProjectCapabilitiesSetOut,
    ProjectCapabilityEventOut,
    ProjectCapabilityIn,
    SelectableCapabilityOut,
    SelectedCapabilityOut,
)
from services.pm_project_phase_service import ProjectService, _commit


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": f"{code}: {message}"})


def _decision(cap: ProjectCapabilityIn) -> dict[str, Any]:
    """The stored fields of one selected capability (reason + the R302 consult decision).

    Replace-set: every field comes from the body, so an omitted R302 field is stored null.
    """
    return {
        "reason": cap.reason,
        "involvement": cap.involvement,
        "purpose": cap.purpose,
        "rigor": cap.rigor,
        "complexity": cap.complexity,
        "consult": cap.consult,
        "order_after": list(cap.order_after),
    }


async def _selectable(
    session: AsyncSession, project_type_id: UUID | None, tenant_id: str
) -> dict[str, tuple[UUID, SelectableCapabilityOut]]:
    """Capability name → (id, selectable view) for the type's composition, implicit excluded."""
    if project_type_id is None:
        return {}
    rows = (
        await session.execute(
            select(
                Capability.id,
                Capability.name,
                Capability.description,
                ProjectPhase.name,
                CardType.name,
            )
            .select_from(ProjectTypeCardType)
            .join(CardType, CardType.id == ProjectTypeCardType.card_type_id)
            .join(Capability, Capability.id == CardType.capability_id)
            .join(
                ProjectTypePhase,
                (ProjectTypePhase.project_type_id == ProjectTypeCardType.project_type_id)
                & (ProjectTypePhase.phase_id == ProjectTypeCardType.phase_id),
            )
            .join(ProjectPhase, ProjectPhase.id == ProjectTypeCardType.phase_id)
            .where(
                ProjectTypeCardType.project_type_id == project_type_id,
                ProjectTypeCardType.tenant_id == tenant_id,
                Capability.tenant_id == tenant_id,
                Capability.name.not_in(IMPLICIT_CAPABILITIES),
            )
            .order_by(Capability.name, ProjectTypePhase.position, CardType.name)
        )
    ).all()
    out: dict[str, tuple[UUID, SelectableCapabilityOut]] = {}
    for cap_id, cap_name, purpose, phase_name, card_type_name in rows:
        if cap_name not in out:
            out[cap_name] = (cap_id, SelectableCapabilityOut(name=cap_name, purpose=purpose))
        out[cap_name][1].phases.setdefault(phase_name, []).append(card_type_name)
    return out


async def _selected_rows(
    session: AsyncSession, project_id: UUID, tenant_id: str
) -> list[tuple[ProjectCapability, str]]:
    return [
        (row, name)
        for row, name in (
            await session.execute(
                select(ProjectCapability, Capability.name)
                .join(Capability, Capability.id == ProjectCapability.capability_id)
                .where(
                    ProjectCapability.project_id == project_id,
                    ProjectCapability.tenant_id == tenant_id,
                )
                .order_by(Capability.name)
            )
        ).all()
    ]


async def _view(session: AsyncSession, project: Project, tenant_id: str) -> ProjectCapabilitiesOut:
    type_name = None
    if project.project_type_id is not None:
        type_name = (
            await session.execute(
                select(ProjectType.name).where(ProjectType.id == project.project_type_id)
            )
        ).scalar_one_or_none()
    selectable = await _selectable(session, project.project_type_id, tenant_id)
    selected = [
        SelectedCapabilityOut(
            name=name,
            capability_id=row.capability_id,
            reason=row.reason,
            involvement=row.involvement,
            purpose=row.purpose,
            rigor=row.rigor,
            complexity=row.complexity,
            consult=row.consult,
            order_after=list(row.order_after or []),
            approval_id=row.approval_id,
            selected_at=row.created_at,
        )
        for row, name in await _selected_rows(session, project.id, tenant_id)
    ]
    return ProjectCapabilitiesOut(
        project_id=project.id,
        project_type=type_name,
        selected=selected,
        implicit=list(IMPLICIT_CAPABILITIES),
        selectable=[view for _, view in selectable.values()],
    )


async def get(
    service: ProjectService,
    session: AsyncSession,
    eid: UUID,
    tenant_id: str,
    subject_id: str,
) -> ProjectCapabilitiesOut:
    project = await service._get_visible(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    return await _view(session, project, tenant_id)


async def set_capabilities(
    service: ProjectService,
    session: AsyncSession,
    eid: UUID,
    body: ProjectCapabilitiesSetIn,
    tenant_id: str,
    subject_id: str,
) -> ProjectCapabilitiesSetOut:
    project = await service._get_mutable(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    # Serialise concurrent selection writes on this project so the compare-and-set is real.
    await session.refresh(project, with_for_update=True)
    if project.project_type_id is None:
        raise HTTPException(status_code=409, detail="project has no project_type_id")

    current = {name: row for row, name in await _selected_rows(session, project.id, tenant_id)}
    if body.if_current is not None and set(body.if_current) != set(current):
        raise _error(
            409,
            "if_current_mismatch",
            f"if_current does not match; current={sorted(current)}",
        )

    wanted = {c.name: c for c in body.capabilities}
    implicit = sorted(n for n in wanted if n in IMPLICIT_CAPABILITIES)
    if implicit:
        raise _error(
            422,
            "implicit_capability",
            f"{implicit} are implicit on every project and are never selected",
        )
    selectable = await _selectable(session, project.project_type_id, tenant_id)
    offending = sorted(n for n in wanted if n not in selectable)
    if offending:
        raise _error(
            422,
            "capability_not_in_type_composition",
            f"{offending} have no card type in this project type's composition; "
            f"selectable: {sorted(selectable)}",
        )

    added = sorted(set(wanted) - set(current))
    removed = sorted(set(current) - set(wanted))
    dirty = False
    for name, row in current.items():
        if name in removed:
            await session.delete(row)
            dirty = True
        else:
            for field, value in _decision(wanted[name]).items():
                if getattr(row, field) != value:
                    setattr(row, field, value)
                    dirty = True
    for name in added:
        session.add(
            ProjectCapability(
                tenant_id=tenant_id,
                project_id=project.id,
                capability_id=selectable[name][0],
                **_decision(wanted[name]),
                approval_id=body.approval_id,
                subject_id=subject_id or tenant_id,
            )
        )
    changed = bool(added or removed)
    if changed:
        session.add(
            ProjectCapabilityEvent(
                tenant_id=tenant_id,
                project_id=project.id,
                added=added,
                removed=removed,
                approval_id=body.approval_id,
                subject_id=subject_id or tenant_id,
            )
        )
    if changed or dirty:
        await _commit(session)
    view = await _view(session, project, tenant_id)
    return ProjectCapabilitiesSetOut(**view.model_dump(), changed=changed)


async def list_events(
    service: ProjectService,
    session: AsyncSession,
    eid: UUID,
    tenant_id: str,
    subject_id: str,
    *,
    limit: int,
    offset: int,
) -> tuple[list[ProjectCapabilityEventOut], int]:
    await service._get_visible(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    where = (
        ProjectCapabilityEvent.project_id == eid,
        ProjectCapabilityEvent.tenant_id == tenant_id,
    )
    total = (
        await session.execute(
            select(func.count()).select_from(ProjectCapabilityEvent).where(*where)
        )
    ).scalar() or 0
    rows = (
        (
            await session.execute(
                select(ProjectCapabilityEvent)
                .where(*where)
                .order_by(
                    ProjectCapabilityEvent.created_at.desc(), ProjectCapabilityEvent.id.desc()
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [ProjectCapabilityEventOut.model_validate(r) for r in rows], total
