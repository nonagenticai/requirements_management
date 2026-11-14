"""PM catalogue service: CRUD for the four catalogues plus project-type composition.

Catalogue rows are tenant-wide vocabulary — always ``visibility='tenant'``, never
user-private, because a project type composed by one member must resolve for every other
member. Reads and writes are scoped by ``tenant_id`` here and by the RLS backstop below.

Errors are explicit, not swallowed:
* a duplicate ``name`` in the tenant → 409;
* a reference to a row the tenant does not own (unknown phase, capability, card type) → 422;
* deleting a phase, capability or card type still in use → 409 naming every user;
* a recipe item whose card type is not in the project type's matrix for the recipe's
  phase → 422; a matrix or phase-selection change that would strand a recipe → 409.
* deleting a project type a project uses → 409 naming the projects; a phase-selection
  change that would strand a project's current phase → 409.
* a second WIP limit for one scope, a second priority level with one rank, or a second
  default priority level → 409.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.pm import Project, ProjectCapability
from models.pm_catalogue import (
    BoardTemplate,
    Capability,
    CardType,
    DeliverableKind,
    Gate,
    MilestoneKind,
    PriorityLevel,
    ProjectPhase,
    ProjectType,
    ProjectTypeCardType,
    ProjectTypePhase,
    Recipe,
    RecipeItem,
    WipLimit,
)
from schemas.pm_catalogue import (
    CatalogueImportCounts,
    CatalogueImportOut,
    CompositionCapabilityOut,
    CompositionCardTypeOut,
    CompositionPhaseOut,
    PhaseCardTypesIn,
    ProjectTypeCardTypesIn,
    ProjectTypeCardTypesOut,
    ProjectTypeOut,
    ProjectTypePhaseOut,
    RecipeIn,
    RecipeItemIn,
    RecipeItemOut,
    RecipeOut,
    WipLimitOut,
    WipLimitResolveOut,
)

CatalogueModel = (
    type[ProjectPhase]
    | type[Capability]
    | type[CardType]
    | type[ProjectType]
    | type[Recipe]
    | type[PriorityLevel]
    | type[DeliverableKind]
    | type[MilestoneKind]
    | type[Gate]
    | type[BoardTemplate]
)
# Every tenant-owned catalogue table; WIP limits are the one without a name.
OwnedModel = CatalogueModel | type[WipLimit]

DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "data" / "pm_catalogue_defaults.json"

# Columns a client may write on a catalogue row. Everything else is server-managed.
_WRITABLE = (
    "name",
    "title",
    "description",
    # card types
    "capability_id",
    "inputs",
    "outputs",
    "acceptance",
    "rules",
    "human_task",
    "definition_of_done",
    # phases
    "output",
    "exit_rule",
    # recipes
    "project_type_id",
    "phase_id",
    "size",
    # wip limits
    "card_type_id",
    "wip_limit",
    # priority levels
    "rank",
    "is_default",
    "conveyor_may_set",
    # deliverable kinds
    "counts_for_phase_exit",
    # gates
    "approver_role",
    "quorum",
    "expires_after_hours",
    # board templates
    "lists",
    "labels",
    "custom_field_groups",
)

# Attribute columns the default import fills on an EXISTING row when they are NULL ("never
# set"). A value the tenant set is never overwritten — and that includes an explicit ``[]``
# or ``""``, which means "deliberately cleared". References (e.g. a milestone kind's
# ``phase_id``) are never fillable: import must never re-parent a row.
_FILLABLE: dict[Any, tuple[str, ...]] = {
    ProjectPhase: ("output", "exit_rule"),
    CardType: ("inputs", "outputs", "acceptance", "rules", "human_task", "definition_of_done"),
    BoardTemplate: ("lists", "labels", "custom_field_groups"),
}

# References a client may set on a row, checked against the caller's tenant (unknown → 422).
_REFS: dict[Any, tuple[tuple[str, Any], ...]] = {
    CardType: (("capability_id", Capability),),
    WipLimit: (
        ("project_type_id", ProjectType),
        ("phase_id", ProjectPhase),
        ("capability_id", Capability),
        ("card_type_id", CardType),
    ),
    MilestoneKind: (("phase_id", ProjectPhase),),
    Gate: (("phase_id", ProjectPhase),),
    BoardTemplate: (("project_type_id", ProjectType),),
}

# WIP-limit dimensions, most specific first: the resolve tie-break order.
WIP_DIMENSIONS = ("card_type_id", "capability_id", "phase_id", "project_type_id")


# ---------------------------------------------------------------------------
# Generic catalogue CRUD
# ---------------------------------------------------------------------------


async def get_row(session: AsyncSession, model: OwnedModel, eid: UUID, tenant_id: str) -> Any:
    row = (
        await session.execute(select(model).where(model.id == eid, model.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


async def list_rows(
    session: AsyncSession,
    model: OwnedModel,
    tenant_id: str,
    *,
    limit: int,
    offset: int,
    order_by: tuple[Any, ...] | None = None,
    **filters: Any,
) -> tuple[list[Any], int]:
    """A page of the tenant's rows, by name unless ``order_by`` says otherwise."""
    stmt = select(model).where(model.tenant_id == tenant_id)
    for key, value in filters.items():
        if value is not None:
            stmt = stmt.where(getattr(model, key) == value)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    order = order_by or (getattr(model, "name"), model.id)
    rows = (
        (await session.execute(stmt.order_by(*order).offset(offset).limit(limit))).scalars().all()
    )
    return list(rows), total


async def _flush(session: AsyncSession) -> None:
    """Flush, turning a constraint race into the 409 the pre-checks would give.

    `_assert_name_free` and `_users` are check-then-act: two concurrent creates
    with one name, or a delete racing a new reference, both pass the check and
    the loser hits the unique constraint / FK RESTRICT. That is a conflict, not
    a server fault.
    """
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="conflicting concurrent change; retry the request"
        ) from exc


async def _commit(session: AsyncSession) -> None:
    """Commit with the same constraint-race → 409 mapping as `_flush`."""
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="conflicting concurrent change; retry the request"
        ) from exc


async def _assert_name_free(
    session: AsyncSession,
    model: OwnedModel,
    name: str,
    tenant_id: str,
    exclude_id: UUID | None = None,
) -> None:
    stmt = select(model.id).where(model.tenant_id == tenant_id, getattr(model, "name") == name)
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    if (await session.execute(stmt)).first() is not None:
        raise HTTPException(
            status_code=409, detail=f"{model.__tablename__}: name {name!r} already exists"
        )


async def _assert_refs(
    session: AsyncSession, model: OwnedModel, data: dict[str, Any], tenant_id: str
) -> None:
    """Every reference the row sets must be the tenant's own (a foreign id → 422)."""
    for field, target in _REFS.get(model, ()):
        value = data.get(field)
        if value is not None and not await _tenant_ids(session, target, [value], tenant_id):
            raise HTTPException(status_code=422, detail=f"unknown {field} {value}")


async def _assert_rank_free(
    session: AsyncSession, rank: int, tenant_id: str, exclude_id: UUID | None = None
) -> None:
    stmt = select(PriorityLevel.name).where(
        PriorityLevel.tenant_id == tenant_id, PriorityLevel.rank == rank
    )
    if exclude_id is not None:
        stmt = stmt.where(PriorityLevel.id != exclude_id)
    taken = (await session.execute(stmt)).scalars().first()
    if taken is not None:
        raise HTTPException(
            status_code=409, detail=f"pm_priority_levels: rank {rank} is already {taken!r}"
        )


async def _assert_single_default(
    session: AsyncSession, tenant_id: str, exclude_id: UUID | None = None
) -> None:
    stmt = select(PriorityLevel.name).where(
        PriorityLevel.tenant_id == tenant_id, PriorityLevel.is_default.is_(True)
    )
    if exclude_id is not None:
        stmt = stmt.where(PriorityLevel.id != exclude_id)
    current = (await session.execute(stmt)).scalars().first()
    if current is not None:
        raise HTTPException(
            status_code=409,
            detail=f"pm_priority_levels: {current!r} is already the default; unset it first",
        )


async def _wip_scope_taken(
    session: AsyncSession,
    scope: dict[str, Any],
    tenant_id: str,
    exclude_id: UUID | None = None,
) -> UUID | None:
    stmt = select(WipLimit.id).where(WipLimit.tenant_id == tenant_id)
    for dim in WIP_DIMENSIONS:
        col, value = getattr(WipLimit, dim), scope.get(dim)
        stmt = stmt.where(col.is_(None) if value is None else col == value)
    if exclude_id is not None:
        stmt = stmt.where(WipLimit.id != exclude_id)
    return (await session.execute(stmt)).scalars().first()


async def _assert_wip_scope_free(
    session: AsyncSession, scope: dict[str, Any], tenant_id: str, exclude_id: UUID | None = None
) -> None:
    taken = await _wip_scope_taken(session, scope, tenant_id, exclude_id)
    if taken is not None:
        raise HTTPException(
            status_code=409, detail=f"pm_wip_limits: WIP limit {taken} already has this scope"
        )


async def _check_row(
    session: AsyncSession,
    model: OwnedModel,
    data: dict[str, Any],
    tenant_id: str,
    exclude_id: UUID | None = None,
    effective: dict[str, Any] | None = None,
) -> None:
    """The pre-checks a create / update must pass (the constraints behind them → 409).

    ``data`` is what the client sent; ``effective`` is the row as it will be stored (on an
    update, the sent fields over the stored ones — an omitted field is unchanged).
    """
    final = data if effective is None else effective
    if "name" in data:
        await _assert_name_free(session, model, data["name"], tenant_id, exclude_id=exclude_id)
    await _assert_refs(session, model, data, tenant_id)
    if model is PriorityLevel:
        await _assert_rank_free(session, final["rank"], tenant_id, exclude_id)
        if final.get("is_default"):
            await _assert_single_default(session, tenant_id, exclude_id)
    elif model is WipLimit:
        await _assert_wip_scope_free(session, final, tenant_id, exclude_id)


async def create_row(
    session: AsyncSession,
    model: OwnedModel,
    data: dict[str, Any],
    tenant_id: str,
    subject_id: str,
    *,
    commit: bool = True,
) -> Any:
    await _check_row(session, model, data, tenant_id)
    row = model(
        **{k: v for k, v in data.items() if k in _WRITABLE},
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility="tenant",
    )
    session.add(row)
    await _flush(session)
    if commit:
        await _commit(session)
        await session.refresh(row)
    return row


async def update_row(
    session: AsyncSession,
    model: OwnedModel,
    eid: UUID,
    data: dict[str, Any],
    tenant_id: str,
    *,
    commit: bool = True,
) -> Any:
    """Write the sent fields; a field absent from ``data`` keeps its stored value."""
    row = await get_row(session, model, eid, tenant_id)
    effective = {k: getattr(row, k) for k in _WRITABLE if hasattr(row, k)} | data
    await _check_row(session, model, data, tenant_id, exclude_id=eid, effective=effective)
    for key in _WRITABLE:
        if key in data:
            setattr(row, key, data[key])
    await _flush(session)
    if commit:
        await _commit(session)
        await session.refresh(row)
    return row


def _wip_users(dim: str, eid: UUID) -> tuple[str, Any]:
    # A WIP limit has no name; it is named by its id.
    return ("wip limit", select(WipLimit.id).where(getattr(WipLimit, dim) == eid))


async def _users(session: AsyncSession, model: OwnedModel, eid: UUID) -> list[str]:
    """Names of the rows that still reference ``eid`` (the reason a delete is refused)."""
    checks: list[tuple[str, Any]] = []
    if model is ProjectPhase:
        checks.append(
            (
                "project type",
                select(ProjectType.name)
                .join(ProjectTypePhase, ProjectTypePhase.project_type_id == ProjectType.id)
                .where(ProjectTypePhase.phase_id == eid),
            )
        )
        checks.append(("recipe", select(Recipe.name).where(Recipe.phase_id == eid)))
        checks.append(("gate", select(Gate.name).where(Gate.phase_id == eid)))
        checks.append(
            ("milestone kind", select(MilestoneKind.name).where(MilestoneKind.phase_id == eid))
        )
        checks.append(_wip_users("phase_id", eid))
    elif model is Capability:
        checks.append(("card type", select(CardType.name).where(CardType.capability_id == eid)))
        checks.append(_wip_users("capability_id", eid))
        # pm_project_capabilities.capability_id is NO ACTION: a project selected it (R260).
        checks.append(
            (
                "project",
                select(Project.slug)
                .join(ProjectCapability, ProjectCapability.project_id == Project.id)
                .where(ProjectCapability.capability_id == eid),
            )
        )
    elif model is CardType:
        checks.append(
            (
                "project type",
                select(ProjectType.name)
                .join(ProjectTypeCardType, ProjectTypeCardType.project_type_id == ProjectType.id)
                .where(ProjectTypeCardType.card_type_id == eid),
            )
        )
        checks.append(
            (
                "recipe",
                select(Recipe.name)
                .join(RecipeItem, RecipeItem.recipe_id == Recipe.id)
                .where(RecipeItem.card_type_id == eid),
            )
        )
        checks.append(_wip_users("card_type_id", eid))
    elif model is ProjectType:
        # pm_projects.project_type_id is RESTRICT; a project is named by its slug.
        checks.append(("project", select(Project.slug).where(Project.project_type_id == eid)))
    elif model is PriorityLevel:
        # pm_projects.priority_level_id is RESTRICT (the fr intake's priority).
        checks.append(("project", select(Project.slug).where(Project.priority_level_id == eid)))
    out: list[str] = []
    for kind, stmt in checks:
        names = sorted(set((await session.execute(stmt)).scalars().all()))
        out.extend(f"{kind} {n}" for n in names)
    return out


async def delete_row(
    session: AsyncSession, model: OwnedModel, eid: UUID, tenant_id: str
) -> dict[str, Any]:
    row = await get_row(session, model, eid, tenant_id)
    users = await _users(session, model, eid)
    if users:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{model.__tablename__} {getattr(row, 'name', str(row.id))!r} "
                f"is in use by: {', '.join(users)}"
            ),
        )
    await session.delete(row)
    await _commit(session)
    return {"status": "deleted", "id": eid}


# ---------------------------------------------------------------------------
# Project types: ordered phase selection
# ---------------------------------------------------------------------------


async def _tenant_ids(
    session: AsyncSession, model: OwnedModel, ids: list[UUID], tenant_id: str
) -> set[UUID]:
    if not ids:
        return set()
    found = await session.execute(
        select(model.id).where(model.id.in_(ids), model.tenant_id == tenant_id)
    )
    return set(found.scalars().all())


async def _stranded_recipes(
    session: AsyncSession, project_type_id: UUID, phase_ids: list[UUID]
) -> list[str]:
    """Recipes of the project type whose phase is not in ``phase_ids``.

    The composite FK ``fk_pm_recipes_selected_phase`` (deferred) is the backstop behind it.
    """
    return list(
        (
            await session.execute(
                select(Recipe.name).where(
                    Recipe.project_type_id == project_type_id, Recipe.phase_id.not_in(phase_ids)
                )
            )
        )
        .scalars()
        .all()
    )


async def _stranded_projects(
    session: AsyncSession, project_type_id: UUID, phase_ids: list[UUID]
) -> list[str]:
    """Projects of the project type whose current phase is not in ``phase_ids``.

    The composite FK ``fk_pm_projects_selected_phase`` (deferred) is the backstop behind it.
    """
    return list(
        (
            await session.execute(
                select(Project.slug).where(
                    Project.project_type_id == project_type_id,
                    Project.current_phase_id.is_not(None),
                    Project.current_phase_id.not_in(phase_ids),
                )
            )
        )
        .scalars()
        .all()
    )


async def set_phases(
    session: AsyncSession, project_type_id: UUID, phase_ids: list[UUID], tenant_id: str
) -> None:
    """Replace the ordered phase selection, keeping card types of phases that stay."""
    owned = await _tenant_ids(session, ProjectPhase, phase_ids, tenant_id)
    missing = [str(p) for p in phase_ids if p not in owned]
    if missing:
        raise HTTPException(status_code=422, detail=f"unknown phase_ids: {missing}")
    stranded = await _stranded_recipes(session, project_type_id, phase_ids)
    if stranded:
        raise HTTPException(
            status_code=409,
            detail=f"phase still used by recipe(s) of this project type: {sorted(stranded)}",
        )
    on_phase = await _stranded_projects(session, project_type_id, phase_ids)
    if on_phase:
        raise HTTPException(
            status_code=409,
            detail=f"phase is the current phase of project(s) of this type: {sorted(on_phase)}",
        )

    overrides = {
        r.phase_id: (r.output, r.exit_rule)
        for r in (
            await session.execute(
                select(
                    ProjectTypePhase.phase_id, ProjectTypePhase.output, ProjectTypePhase.exit_rule
                ).where(ProjectTypePhase.project_type_id == project_type_id)
            )
        ).all()
    }
    kept = (
        await session.execute(
            select(
                ProjectTypeCardType.phase_id,
                ProjectTypeCardType.card_type_id,
                ProjectTypeCardType.is_entry,
            ).where(
                ProjectTypeCardType.project_type_id == project_type_id,
                ProjectTypeCardType.phase_id.in_(phase_ids),
            )
        )
    ).all()
    # Delete-and-reinsert: an in-place reorder would trip the (type, position) unique
    # constraint mid-way. The composite FK cascades the matrix rows; the kept ones are
    # re-inserted below.
    await session.execute(
        delete(ProjectTypeCardType).where(ProjectTypeCardType.project_type_id == project_type_id)
    )
    await session.execute(
        delete(ProjectTypePhase).where(ProjectTypePhase.project_type_id == project_type_id)
    )
    await _flush(session)
    session.add_all(
        ProjectTypePhase(
            project_type_id=project_type_id,
            phase_id=pid,
            position=i,
            tenant_id=tenant_id,
            output=overrides.get(pid, (None, None))[0],
            exit_rule=overrides.get(pid, (None, None))[1],
        )
        for i, pid in enumerate(phase_ids)
    )
    await _flush(session)
    session.add_all(
        ProjectTypeCardType(
            project_type_id=project_type_id,
            phase_id=phase_id,
            card_type_id=card_type_id,
            tenant_id=tenant_id,
            is_entry=is_entry,
        )
        for phase_id, card_type_id, is_entry in kept
    )
    await _flush(session)


async def project_type_out(session: AsyncSession, row: ProjectType) -> ProjectTypeOut:
    phases = (
        await session.execute(
            select(
                ProjectTypePhase.phase_id,
                ProjectPhase.name,
                ProjectPhase.title,
                ProjectTypePhase.position,
                ProjectPhase.has_gate,
            )
            .join(ProjectPhase, ProjectPhase.id == ProjectTypePhase.phase_id)
            .where(ProjectTypePhase.project_type_id == row.id)
            .order_by(ProjectTypePhase.position)
        )
    ).all()
    out = ProjectTypeOut.model_validate(row, from_attributes=True)
    out.phases = [
        ProjectTypePhaseOut(
            phase_id=p.phase_id,
            name=p.name,
            title=p.title,
            position=p.position,
            has_gate=bool(p.has_gate),
        )
        for p in phases
    ]
    return out


async def create_project_type(
    session: AsyncSession,
    data: dict[str, Any],
    phase_ids: list[UUID],
    tenant_id: str,
    subject_id: str,
) -> ProjectTypeOut:
    row = await create_row(session, ProjectType, data, tenant_id, subject_id, commit=False)
    await set_phases(session, row.id, phase_ids, tenant_id)
    await _commit(session)
    await session.refresh(row)
    return await project_type_out(session, row)


async def update_project_type(
    session: AsyncSession,
    eid: UUID,
    data: dict[str, Any],
    phase_ids: list[UUID] | None,
    tenant_id: str,
) -> ProjectTypeOut:
    """``phase_ids`` None (omitted by the client) leaves the phase selection unchanged."""
    row = await update_row(session, ProjectType, eid, data, tenant_id, commit=False)
    if phase_ids is not None:
        await set_phases(session, row.id, phase_ids, tenant_id)
    await _commit(session)
    await session.refresh(row)
    return await project_type_out(session, row)


# ---------------------------------------------------------------------------
# Project types: card types per phase
# ---------------------------------------------------------------------------


async def get_composition(
    session: AsyncSession, eid: UUID, tenant_id: str
) -> ProjectTypeCardTypesOut:
    row = await get_row(session, ProjectType, eid, tenant_id)
    pt = await project_type_out(session, row)
    effective = {
        r.phase_id: r
        for r in (
            await session.execute(
                select(
                    ProjectTypePhase.phase_id,
                    ProjectTypePhase.output.label("o_output"),
                    ProjectTypePhase.exit_rule.label("o_exit"),
                    ProjectPhase.output.label("d_output"),
                    ProjectPhase.exit_rule.label("d_exit"),
                )
                .join(ProjectPhase, ProjectPhase.id == ProjectTypePhase.phase_id)
                .where(ProjectTypePhase.project_type_id == eid)
            )
        ).all()
    }
    placed = (
        await session.execute(
            select(
                ProjectTypeCardType.phase_id,
                Capability.id,
                Capability.name,
                Capability.title,
                CardType.id,
                CardType.name,
                CardType.title,
                ProjectTypeCardType.is_entry,
            )
            .join(CardType, CardType.id == ProjectTypeCardType.card_type_id)
            .join(Capability, Capability.id == CardType.capability_id)
            .where(ProjectTypeCardType.project_type_id == eid)
            .order_by(Capability.name, CardType.name)
        )
    ).all()
    phases: list[CompositionPhaseOut] = []
    for p in pt.phases:
        caps: dict[UUID, CompositionCapabilityOut] = {}
        for phase_id, cap_id, cap_name, cap_title, ct_id, ct_name, ct_title, entry in placed:
            if phase_id != p.phase_id:
                continue
            cap = caps.setdefault(
                cap_id,
                CompositionCapabilityOut(capability_id=cap_id, name=cap_name, title=cap_title),
            )
            cap.card_types.append(
                CompositionCardTypeOut(
                    card_type_id=ct_id, name=ct_name, title=ct_title, is_entry=bool(entry)
                )
            )
        e = effective[p.phase_id]
        phases.append(
            CompositionPhaseOut(
                phase_id=p.phase_id,
                name=p.name,
                title=p.title,
                position=p.position,
                output=e.o_output if e.o_output is not None else e.d_output,
                output_overridden=e.o_output is not None,
                output_override=e.o_output,
                exit_rule=e.o_exit if e.o_exit is not None else e.d_exit,
                exit_rule_overridden=e.o_exit is not None,
                exit_rule_override=e.o_exit,
                has_gate=p.has_gate,
                capabilities=list(caps.values()),
            )
        )
    return ProjectTypeCardTypesOut(
        project_type_id=row.id, name=row.name, title=row.title, phases=phases
    )


async def _replace_card_types(
    session: AsyncSession, eid: UUID, body: ProjectTypeCardTypesIn, tenant_id: str
) -> None:
    selected = set(
        (
            await session.execute(
                select(ProjectTypePhase.phase_id).where(ProjectTypePhase.project_type_id == eid)
            )
        )
        .scalars()
        .all()
    )
    unselected = [str(p.phase_id) for p in body.phases if p.phase_id not in selected]
    if unselected:
        raise HTTPException(
            status_code=422,
            detail=f"phases not selected by this project type: {unselected}",
        )
    wanted = [ct for p in body.phases for ct in p.card_type_ids]
    owned = await _tenant_ids(session, CardType, wanted, tenant_id)
    unknown = sorted({str(ct) for ct in wanted if ct not in owned})
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown card_type_ids: {unknown}")
    new_matrix = {(p.phase_id, ct) for p in body.phases for ct in p.card_type_ids}
    stranded = sorted(
        {
            name
            for name, phase_id, ct in (
                await session.execute(
                    select(Recipe.name, Recipe.phase_id, RecipeItem.card_type_id)
                    .join(RecipeItem, RecipeItem.recipe_id == Recipe.id)
                    .where(Recipe.project_type_id == eid)
                )
            ).all()
            if (phase_id, ct) not in new_matrix
        }
    )
    if stranded:
        raise HTTPException(
            status_code=409,
            detail=f"card types still used by recipe(s) of this project type: {stranded}",
        )

    # An omitted entry_card_type_ids keeps the stored entry flags of the card types that stay.
    was_entry = {
        tuple(row)
        for row in (
            await session.execute(
                select(ProjectTypeCardType.phase_id, ProjectTypeCardType.card_type_id).where(
                    ProjectTypeCardType.project_type_id == eid,
                    ProjectTypeCardType.is_entry.is_(True),
                )
            )
        ).all()
    }

    def _is_entry(p: PhaseCardTypesIn, ct: UUID) -> bool:
        if "entry_card_type_ids" in p.model_fields_set:
            return ct in p.entry_card_type_ids
        return (p.phase_id, ct) in was_entry or (str(p.phase_id), str(ct)) in was_entry

    await session.execute(
        delete(ProjectTypeCardType).where(ProjectTypeCardType.project_type_id == eid)
    )
    session.add_all(
        ProjectTypeCardType(
            project_type_id=eid,
            phase_id=p.phase_id,
            card_type_id=ct,
            tenant_id=tenant_id,
            is_entry=_is_entry(p, ct),
        )
        for p in body.phases
        for ct in p.card_type_ids
    )
    # Per-phase overrides: a sent override is written (null clears it); an omitted one, and
    # every override of an unlisted phase, is unchanged.
    listed = {p.phase_id: p for p in body.phases}
    for tp in (
        (
            await session.execute(
                select(ProjectTypePhase).where(ProjectTypePhase.project_type_id == eid)
            )
        )
        .scalars()
        .all()
    ):
        given = listed.get(tp.phase_id)
        if given is None:
            continue
        if "output" in given.model_fields_set:
            tp.output = given.output
        if "exit_rule" in given.model_fields_set:
            tp.exit_rule = given.exit_rule
    await _flush(session)


async def set_composition(
    session: AsyncSession, eid: UUID, body: ProjectTypeCardTypesIn, tenant_id: str
) -> ProjectTypeCardTypesOut:
    await get_row(session, ProjectType, eid, tenant_id)
    await _replace_card_types(session, eid, body, tenant_id)
    await _commit(session)
    return await get_composition(session, eid, tenant_id)


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


async def _recipe_problems(
    session: AsyncSession,
    project_type_id: UUID,
    phase_id: UUID,
    card_type_ids: list[UUID],
    tenant_id: str,
) -> list[str]:
    """Why a recipe would not fit its project type (empty = it fits).

    Every reference must be the tenant's own, the phase must be selected by the project
    type, and every item's card type must be in the project type's matrix for that phase.
    """
    if not await _tenant_ids(session, ProjectType, [project_type_id], tenant_id):
        return [f"unknown project_type_id {project_type_id}"]
    if not await _tenant_ids(session, ProjectPhase, [phase_id], tenant_id):
        return [f"unknown phase_id {phase_id}"]
    selected = (
        await session.execute(
            select(ProjectTypePhase.phase_id).where(
                ProjectTypePhase.project_type_id == project_type_id,
                ProjectTypePhase.phase_id == phase_id,
            )
        )
    ).first()
    if selected is None:
        return [f"phase {phase_id} is not selected by the project type"]
    owned = await _tenant_ids(session, CardType, card_type_ids, tenant_id)
    unknown = sorted({str(ct) for ct in card_type_ids if ct not in owned})
    if unknown:
        return [f"unknown card_type_ids: {unknown}"]
    in_matrix = set(
        (
            await session.execute(
                select(ProjectTypeCardType.card_type_id).where(
                    ProjectTypeCardType.project_type_id == project_type_id,
                    ProjectTypeCardType.phase_id == phase_id,
                )
            )
        )
        .scalars()
        .all()
    )
    outside = sorted({str(ct) for ct in card_type_ids if ct not in in_matrix})
    if outside:
        return [f"card types not in the project type's matrix for this phase: {outside}"]
    return []


async def _slot_taken(
    session: AsyncSession,
    project_type_id: UUID,
    phase_id: UUID,
    size: str | None,
    tenant_id: str,
    exclude_id: UUID | None = None,
) -> str | None:
    stmt = select(Recipe.name).where(
        Recipe.tenant_id == tenant_id,
        Recipe.project_type_id == project_type_id,
        Recipe.phase_id == phase_id,
        Recipe.size.is_(None) if size is None else Recipe.size == size,
    )
    if exclude_id is not None:
        stmt = stmt.where(Recipe.id != exclude_id)
    return (await session.execute(stmt)).scalars().first()


async def _check_recipe(
    session: AsyncSession,
    project_type_id: UUID,
    phase_id: UUID,
    size: str | None,
    card_type_ids: list[UUID],
    tenant_id: str,
    exclude_id: UUID | None = None,
) -> None:
    problems = await _recipe_problems(session, project_type_id, phase_id, card_type_ids, tenant_id)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    taken = await _slot_taken(session, project_type_id, phase_id, size, tenant_id, exclude_id)
    if taken:
        raise HTTPException(
            status_code=409,
            detail=f"recipe {taken!r} already covers this project type, phase and size",
        )


def _items(recipe_id: UUID, items: list[RecipeItemIn], tenant_id: str) -> list[RecipeItem]:
    return [
        RecipeItem(
            recipe_id=recipe_id,
            position=n,
            card_type_id=i.card_type_id,
            count_mode=i.count_mode,
            count=i.count,
            per_field=i.per_field,
            cap=i.cap,
            inputs=list(i.inputs),
            outputs=list(i.outputs),
            tenant_id=tenant_id,
        )
        for n, i in enumerate(items)
    ]


async def recipes_out(session: AsyncSession, rows: list[Recipe]) -> list[RecipeOut]:
    if not rows:
        return []
    ids = [r.id for r in rows]
    names = {
        **{
            pid: n
            for pid, n in (
                await session.execute(
                    select(ProjectType.id, ProjectType.name).where(
                        ProjectType.id.in_({r.project_type_id for r in rows})
                    )
                )
            ).all()
        },
        **{
            pid: n
            for pid, n in (
                await session.execute(
                    select(ProjectPhase.id, ProjectPhase.name).where(
                        ProjectPhase.id.in_({r.phase_id for r in rows})
                    )
                )
            ).all()
        },
    }
    items: dict[UUID, list[RecipeItemOut]] = {}
    for item, ct_name in (
        await session.execute(
            select(RecipeItem, CardType.name)
            .join(CardType, CardType.id == RecipeItem.card_type_id)
            .where(RecipeItem.recipe_id.in_(ids))
            .order_by(RecipeItem.recipe_id, RecipeItem.position)
        )
    ).all():
        items.setdefault(item.recipe_id, []).append(
            RecipeItemOut(
                position=item.position,
                card_type_id=item.card_type_id,
                card_type=ct_name,
                count_mode=item.count_mode,
                count=item.count,
                per_field=item.per_field,
                cap=item.cap,
                inputs=list(item.inputs or []),
                outputs=list(item.outputs or []),
            )
        )
    out: list[RecipeOut] = []
    for r in rows:
        o = RecipeOut.model_validate(
            {
                "id": r.id,
                "name": r.name,
                "title": r.title,
                "description": r.description,
                "tenant_id": r.tenant_id,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "project_type_id": r.project_type_id,
                "project_type": names[r.project_type_id],
                "phase_id": r.phase_id,
                "phase": names[r.phase_id],
                "size": r.size,
                "items": items.get(r.id, []),
            }
        )
        out.append(o)
    return out


async def create_recipe(
    session: AsyncSession, body: RecipeIn, tenant_id: str, subject_id: str
) -> RecipeOut:
    await _check_recipe(
        session,
        body.project_type_id,
        body.phase_id,
        body.size,
        [i.card_type_id for i in body.items],
        tenant_id,
    )
    row = await create_row(
        session, Recipe, body.model_dump(exclude={"items"}), tenant_id, subject_id, commit=False
    )
    session.add_all(_items(row.id, body.items, tenant_id))
    await _flush(session)
    await _commit(session)
    await session.refresh(row)
    return (await recipes_out(session, [row]))[0]


async def update_recipe(
    session: AsyncSession, eid: UUID, body: RecipeIn, tenant_id: str
) -> RecipeOut:
    """Omitted fields are unchanged: no ``size`` keeps the size, no ``items`` keeps the items."""
    current = await get_row(session, Recipe, eid, tenant_id)
    sent = body.model_fields_set
    size = body.size if "size" in sent else current.size
    if "items" in sent:
        card_type_ids = [i.card_type_id for i in body.items]
    else:
        card_type_ids = list(
            (
                await session.execute(
                    select(RecipeItem.card_type_id).where(RecipeItem.recipe_id == eid)
                )
            )
            .scalars()
            .all()
        )
    await _check_recipe(
        session,
        body.project_type_id,
        body.phase_id,
        size,
        card_type_ids,
        tenant_id,
        exclude_id=eid,
    )
    data = body.model_dump(exclude_unset=True, exclude={"items"})
    row = await update_row(session, Recipe, eid, data, tenant_id, commit=False)
    if "items" in sent:
        await session.execute(delete(RecipeItem).where(RecipeItem.recipe_id == eid))
        await _flush(session)
        session.add_all(_items(eid, body.items, tenant_id))
        await _flush(session)
    await _commit(session)
    await session.refresh(row)
    return (await recipes_out(session, [row]))[0]


async def project_type_recipes(session: AsyncSession, eid: UUID, tenant_id: str) -> list[RecipeOut]:
    """A project type's recipes, in phase order, then by size (any-size first)."""
    await get_row(session, ProjectType, eid, tenant_id)
    rows = (
        (
            await session.execute(
                select(Recipe)
                .join(
                    ProjectTypePhase,
                    (ProjectTypePhase.project_type_id == Recipe.project_type_id)
                    & (ProjectTypePhase.phase_id == Recipe.phase_id),
                    isouter=True,
                )
                .where(Recipe.project_type_id == eid, Recipe.tenant_id == tenant_id)
                .order_by(
                    ProjectTypePhase.position,
                    Recipe.size.is_not(None),
                    Recipe.size.desc(),  # S, M, L
                    Recipe.name,
                )
            )
        )
        .scalars()
        .all()
    )
    return await recipes_out(session, list(rows))


# ---------------------------------------------------------------------------
# WIP limits
# ---------------------------------------------------------------------------


def _wip_specificity(r: WipLimit) -> tuple[int, ...]:
    """Most non-NULL dimensions first; ties card type > capability > phase > project type."""
    set_dims = [getattr(r, d) is not None for d in WIP_DIMENSIONS]
    return (sum(set_dims), *set_dims)


def wip_row_matches(row: WipLimit, query: dict[str, UUID | None]) -> bool:
    """Each non-NULL dimension of ``row`` equals the query's; a NULL query value matches
    only a NULL dimension (the same rule ``_wip_stmt`` applies in SQL)."""
    for dim in WIP_DIMENSIONS:
        value, have = query.get(dim), getattr(row, dim)
        if value is None and have is not None:
            return False
        if have is not None and have != value:
            return False
    return True


def best_wip_row(rows: Any, query: dict[str, UUID | None]) -> WipLimit | None:
    """The most specific row of ``rows`` that matches ``query``, or None."""
    matching = [r for r in rows if wip_row_matches(r, query)]
    return max(matching, key=_wip_specificity) if matching else None


def wip_matched_on(row: WipLimit) -> list[str]:
    return [d for d in reversed(WIP_DIMENSIONS) if getattr(row, d) is not None]


def _wip_stmt(tenant_id: str, query: dict[str, UUID | None]) -> Any:
    stmt = select(WipLimit).where(WipLimit.tenant_id == tenant_id)
    for dim in WIP_DIMENSIONS:
        col, value = getattr(WipLimit, dim), query.get(dim)
        stmt = stmt.where(col.is_(None) if value is None else or_(col.is_(None), col == value))
    return stmt


async def match_wip_limit(
    session: AsyncSession, tenant_id: str, query: dict[str, UUID | None]
) -> WipLimit | None:
    """The tenant's most specific WIP-limit row for ``query``, or None."""
    rows = (await session.execute(_wip_stmt(tenant_id, query))).scalars().all()
    return best_wip_row(rows, query)


async def project_total_wip_limits(
    session: AsyncSession, tenant_id: str, project_type_ids: set[UUID | None]
) -> dict[UUID | None, WipLimit | None]:
    """The project-TOTAL catalogue row for each project type, in ONE query.

    The total is the resolve with phase, capability and card type all NULL: the row for
    the project type, else the tenant-global row (every dimension NULL). Only the tenant's
    own rows are read.
    """
    if not project_type_ids:
        return {}
    typed = [t for t in project_type_ids if t is not None]
    stmt = select(WipLimit).where(
        WipLimit.tenant_id == tenant_id,
        WipLimit.phase_id.is_(None),
        WipLimit.capability_id.is_(None),
        WipLimit.card_type_id.is_(None),
        or_(WipLimit.project_type_id.is_(None), WipLimit.project_type_id.in_(typed)),
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {t: best_wip_row(rows, {"project_type_id": t}) for t in project_type_ids}


async def resolve_wip_limit(
    session: AsyncSession, tenant_id: str, query: dict[str, UUID | None]
) -> WipLimitResolveOut:
    """The WIP limit that applies to ``query``: the most specific matching row.

    A row matches when each of its non-NULL dimensions equals the query's value (a NULL
    dimension matches anything). Most non-NULL dimensions wins; ties go to card type, then
    capability, then phase, then project type. 404 when nothing matches.
    """
    best = await match_wip_limit(session, tenant_id, query)
    if best is None:
        raise HTTPException(status_code=404, detail="no WIP limit matches")
    out = WipLimitResolveOut.model_validate(WipLimitOut.model_validate(best).model_dump())
    out.matched_on = wip_matched_on(best)
    return out


# ---------------------------------------------------------------------------
# Default catalogue import
# ---------------------------------------------------------------------------


@cache
def load_defaults() -> dict[str, Any]:
    return json.loads(DEFAULTS_PATH.read_text())


async def _by_name(session: AsyncSession, model: CatalogueModel, tenant_id: str) -> dict[str, Any]:
    rows = (await session.execute(select(model).where(model.tenant_id == tenant_id))).scalars()
    return {r.name: r for r in rows}


def _unset(value: Any) -> bool:
    """NULL is "never set". ``[]`` / ``""`` / ``False`` are values the tenant chose."""
    return value is None


def _fill(row: Any, entry: dict[str, Any], fields: tuple[str, ...]) -> bool:
    """Set each NULL attribute of ``row`` from ``entry``; True if anything changed."""
    changed = False
    for f in fields:
        default = entry.get(f)
        if f == "human_task" and default is None:
            default = False
        if _unset(getattr(row, f)) and not _unset(default):
            setattr(row, f, default)
            changed = True
    return changed


async def import_defaults(
    session: AsyncSession, tenant_id: str, subject_id: str
) -> CatalogueImportOut:
    """Copy the default catalogue into the tenant. Idempotent by name; never overwrites.

    Missing rows are created. On an existing row only the attribute columns that are NULL
    ("never set") are filled; every value the tenant set is left as it is — an explicit
    ``[]`` or ``""`` ("deliberately cleared") included — and the row is never re-parented. A project type gets its default phases, card types and entry flags
    only when THIS import creates it. A default recipe is created when its name is free and
    it fits the tenant's project type; otherwise it is reported in ``skipped_recipes``.
    """
    d = load_defaults()

    async def ensure(model: CatalogueModel, entries: list[dict[str, Any]], fields_for=None):
        have = await _by_name(session, model, tenant_id)
        fillable = _FILLABLE.get(model, ())
        created = filled = 0
        for e in entries:
            # The row's attributes, with names resolved to the tenant's ids.
            attrs = {
                **{f: e[f] for f in fillable if f in e},
                **(fields_for(e) if fields_for else {}),
            }
            if e["name"] in have:
                if fillable and _fill(have[e["name"]], attrs, fillable):
                    filled += 1
                continue
            data = {
                "name": e["name"],
                "title": e.get("title") or e["name"],
                "description": e.get("description"),
                **attrs,
            }
            have[e["name"]] = await create_row(
                session, model, data, tenant_id, subject_id, commit=False
            )
            created += 1
        await _flush(session)
        return have, CatalogueImportCounts(
            created=created, existing=len(entries) - created, filled=filled
        )

    phases, phase_counts = await ensure(ProjectPhase, d["project_phases"])
    caps, cap_counts = await ensure(Capability, d["capabilities"])
    card_types, ct_counts = await ensure(
        CardType, d["card_types"], lambda e: {"capability_id": caps[e["capability"]].id}
    )

    existing_types = await _by_name(session, ProjectType, tenant_id)
    created_types = 0
    skipped: set[str] = set()
    for e in d["project_types"]:
        if e["name"] in existing_types:
            continue
        row = await create_row(
            session,
            ProjectType,
            {
                "name": e["name"],
                "title": e.get("title") or e["name"],
                "description": e.get("description"),
            },
            tenant_id,
            subject_id,
            commit=False,
        )
        existing_types[e["name"]] = row
        created_types += 1
        phase_names = e.get("phases", [])
        await set_phases(session, row.id, [phases[n].id for n in phase_names], tenant_id)
        entry = e.get("entry_card_types", {})
        placements: list[PhaseCardTypesIn] = []
        for phase_name, ct_names in e.get("card_types", {}).items():
            skipped.update(n for n in ct_names if n not in card_types)
            placements.append(
                PhaseCardTypesIn(
                    phase_id=phases[phase_name].id,
                    card_type_ids=[card_types[n].id for n in ct_names if n in card_types],
                    entry_card_type_ids=[
                        card_types[n].id
                        for n in entry.get(phase_name, [])
                        if n in card_types and n in ct_names
                    ],
                )
            )
        body = ProjectTypeCardTypesIn(phases=placements)
        await _replace_card_types(session, row.id, body, tenant_id)

    have_recipes = await _by_name(session, Recipe, tenant_id)
    created_recipes = 0
    skipped_recipes: list[str] = []
    for e in d.get("recipes", []):
        if e["name"] in have_recipes:
            continue
        pt = existing_types.get(e["project_type"])
        phase = phases.get(e["phase"])
        missing = [i["card_type"] for i in e["items"] if i["card_type"] not in card_types]
        if pt is None or phase is None or missing:
            skipped_recipes.append(e["name"])
            continue
        ct_ids = [card_types[i["card_type"]].id for i in e["items"]]
        if await _recipe_problems(session, pt.id, phase.id, ct_ids, tenant_id) or (
            await _slot_taken(session, pt.id, phase.id, e["size"], tenant_id)
        ):
            skipped_recipes.append(e["name"])
            continue
        row = await create_row(
            session,
            Recipe,
            {
                "name": e["name"],
                "title": e.get("title") or e["name"],
                "description": e.get("description"),
                "project_type_id": pt.id,
                "phase_id": phase.id,
                "size": e["size"],
            },
            tenant_id,
            subject_id,
            commit=False,
        )
        items = [
            RecipeItemIn(
                card_type_id=card_types[i["card_type"]].id,
                count_mode=i["count_mode"],
                count=i["count"],
                per_field=i["per_field"],
                cap=i["cap"],
                inputs=i["inputs"],
                outputs=i["outputs"],
            )
            for i in e["items"]
        ]
        session.add_all(_items(row.id, items, tenant_id))
        await _flush(session)
        have_recipes[e["name"]] = row
        created_recipes += 1

    # Tier 2. WIP limits are identified by their scope, not a name.
    created_wip = 0
    for e in d.get("wip_limits", []):
        scope = {
            "project_type_id": existing_types[e["project_type"]].id if e["project_type"] else None,
            "phase_id": phases[e["phase"]].id if e["phase"] else None,
            "capability_id": caps[e["capability"]].id if e["capability"] else None,
            "card_type_id": card_types[e["card_type"]].id if e["card_type"] else None,
        }
        if await _wip_scope_taken(session, scope, tenant_id) is not None:
            continue
        await create_row(
            session,
            WipLimit,
            {**scope, "wip_limit": e["wip_limit"]},
            tenant_id,
            subject_id,
            commit=False,
        )
        created_wip += 1

    # Priority levels: a rank the tenant already uses is skipped; the tenant's own default
    # stays the default.
    levels = await _by_name(session, PriorityLevel, tenant_id)
    ranks = {r.rank for r in levels.values()}
    has_default = any(r.is_default for r in levels.values())
    created_levels = 0
    skipped_levels: list[str] = []
    for e in d.get("priority_levels", []):
        if e["name"] in levels:
            continue
        if e["rank"] in ranks:
            skipped_levels.append(e["name"])
            continue
        is_default = bool(e["is_default"]) and not has_default
        levels[e["name"]] = await create_row(
            session,
            PriorityLevel,
            {
                "name": e["name"],
                "title": e.get("title") or e["name"],
                "description": e.get("description"),
                "rank": e["rank"],
                "is_default": is_default,
                "conveyor_may_set": e["conveyor_may_set"],
            },
            tenant_id,
            subject_id,
            commit=False,
        )
        ranks.add(e["rank"])
        has_default = has_default or is_default
        created_levels += 1

    def _phase_id(e: dict[str, Any]) -> Any:
        return phases[e["phase"]].id if e.get("phase") else None

    _, deliverable_counts = await ensure(
        DeliverableKind,
        d.get("deliverable_kinds", []),
        lambda e: {"counts_for_phase_exit": e["counts_for_phase_exit"]},
    )
    _, milestone_counts = await ensure(
        MilestoneKind, d.get("milestone_kinds", []), lambda e: {"phase_id": _phase_id(e)}
    )
    _, gate_counts = await ensure(
        Gate,
        d.get("gates", []),
        lambda e: {
            "phase_id": _phase_id(e),
            "approver_role": e["approver_role"],
            "quorum": e["quorum"],
            "expires_after_hours": e["expires_after_hours"],
        },
    )
    _, template_counts = await ensure(
        BoardTemplate,
        d.get("board_templates", []),
        lambda e: {
            "project_type_id": (
                existing_types[e["project_type"]].id if e["project_type"] else None
            ),
            "lists": e["lists"],
            "labels": e["labels"],
            "custom_field_groups": e["custom_field_groups"],
        },
    )

    await _commit(session)
    n_levels = len(d.get("priority_levels", []))
    return CatalogueImportOut(
        project_phases=phase_counts,
        capabilities=cap_counts,
        card_types=ct_counts,
        project_types=CatalogueImportCounts(
            created=created_types, existing=len(d["project_types"]) - created_types
        ),
        recipes=CatalogueImportCounts(
            created=created_recipes,
            existing=len(d.get("recipes", [])) - created_recipes - len(skipped_recipes),
        ),
        wip_limits=CatalogueImportCounts(
            created=created_wip, existing=len(d.get("wip_limits", [])) - created_wip
        ),
        priority_levels=CatalogueImportCounts(
            created=created_levels,
            existing=n_levels - created_levels - len(skipped_levels),
        ),
        deliverable_kinds=deliverable_counts,
        milestone_kinds=milestone_counts,
        gates=gate_counts,
        board_templates=template_counts,
        skipped_card_types=sorted(skipped),
        skipped_recipes=sorted(skipped_recipes),
        skipped_priority_levels=sorted(skipped_levels),
    )
