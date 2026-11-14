"""PM catalogue endpoints: project phases, capabilities, card types, project types.

Four routers, mounted under ``/pm/project-phases``, ``/pm/capabilities``,
``/pm/card-types`` and ``/pm/project-types`` (plus ``/pm/catalogue`` for the default
import) and ``/pm/recipes``. Tier 2 adds ``/pm/wip-limits``, ``/pm/priority-levels``,
``/pm/deliverable-kinds``, ``/pm/milestone-kinds``, ``/pm/gates`` and
``/pm/board-templates``. All rows are tenant-wide vocabulary; see
``services/pm_catalogue_service.py``.
"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id
from db import get_session
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
    Recipe,
    WipLimit,
)
from routers.responses import AUTH_RESPONSES
from schemas.common import ErrorResponse
from schemas.pm_catalogue import (
    BoardTemplateIn,
    BoardTemplateOut,
    CapabilityIn,
    CapabilityOut,
    CardTypeIn,
    CardTypeOut,
    CatalogueImportOut,
    DeliverableKindIn,
    DeliverableKindOut,
    GateIn,
    GateOut,
    MilestoneKindIn,
    MilestoneKindOut,
    PriorityLevelIn,
    PriorityLevelOut,
    ProjectPhaseIn,
    ProjectPhaseOut,
    ProjectTypeCardTypesIn,
    ProjectTypeCardTypesOut,
    ProjectTypeIn,
    ProjectTypeOut,
    RecipeIn,
    RecipeOut,
    WipLimitIn,
    WipLimitOut,
    WipLimitResolveOut,
)
from services import pm_catalogue_service as svc

_NOT_FOUND = {404: {"description": "Not found", "model": ErrorResponse}}
_CONFLICT = {409: {"description": "Name taken, or still in use", "model": ErrorResponse}}
_INVALID = {422: {"description": "Validation error", "model": ErrorResponse}}

project_phases_router = APIRouter()
capabilities_router = APIRouter()
card_types_router = APIRouter()
project_types_router = APIRouter()
catalogue_router = APIRouter()
recipes_router = APIRouter()
wip_limits_router = APIRouter()
priority_levels_router = APIRouter()
deliverable_kinds_router = APIRouter()
milestone_kinds_router = APIRouter()
gates_router = APIRouter()
board_templates_router = APIRouter()


# ---------------------------------------------------------------------------
# Project phases
# ---------------------------------------------------------------------------


@project_phases_router.post(
    "",
    response_model=ProjectPhaseOut,
    operation_id="create_project_phase",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_project_phase(
    payload: ProjectPhaseIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectPhaseOut:
    """Create a project phase."""
    row = await svc.create_row(session, ProjectPhase, payload.model_dump(), tenant_id, subject_id)
    return ProjectPhaseOut.model_validate(row)


@project_phases_router.get(
    "/{eid}",
    response_model=ProjectPhaseOut,
    operation_id="get_project_phase",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_project_phase(
    eid: UUID = Path(..., description="Project phase ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectPhaseOut:
    """Get a project phase."""
    return ProjectPhaseOut.model_validate(await svc.get_row(session, ProjectPhase, eid, tenant_id))


@project_phases_router.get(
    "",
    response_model=list[ProjectPhaseOut],
    operation_id="list_project_phases",
    responses=AUTH_RESPONSES,
)
async def list_project_phases(
    response: Response,
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ProjectPhaseOut]:
    """List project phases, by name."""
    rows, total = await svc.list_rows(session, ProjectPhase, tenant_id, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return [ProjectPhaseOut.model_validate(r) for r in rows]


@project_phases_router.put(
    "/{eid}",
    response_model=ProjectPhaseOut,
    operation_id="update_project_phase",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_project_phase(
    eid: UUID = Path(..., description="Project phase ID"),
    payload: ProjectPhaseIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectPhaseOut:
    """Update a project phase. Omitted fields are unchanged; an explicit null sets a field to not set."""
    row = await svc.update_row(session, ProjectPhase, eid, _sent(payload), tenant_id)
    return ProjectPhaseOut.model_validate(row)


@project_phases_router.delete(
    "/{eid}",
    operation_id="delete_project_phase",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT},
)
async def delete_project_phase(
    eid: UUID = Path(..., description="Project phase ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a project phase. 409 while a project type, recipe, gate, milestone kind or WIP
    limit still uses it."""
    return await svc.delete_row(session, ProjectPhase, eid, tenant_id)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


@capabilities_router.post(
    "",
    response_model=CapabilityOut,
    operation_id="create_pm_capability",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_pm_capability(
    payload: CapabilityIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CapabilityOut:
    """Create a capability."""
    row = await svc.create_row(session, Capability, payload.model_dump(), tenant_id, subject_id)
    return CapabilityOut.model_validate(row)


@capabilities_router.get(
    "/{eid}",
    response_model=CapabilityOut,
    operation_id="get_pm_capability",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_pm_capability(
    eid: UUID = Path(..., description="Capability ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CapabilityOut:
    """Get a capability."""
    return CapabilityOut.model_validate(await svc.get_row(session, Capability, eid, tenant_id))


@capabilities_router.get(
    "",
    response_model=list[CapabilityOut],
    operation_id="list_pm_capabilities",
    responses=AUTH_RESPONSES,
)
async def list_pm_capabilities(
    response: Response,
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[CapabilityOut]:
    """List capabilities, by name."""
    rows, total = await svc.list_rows(session, Capability, tenant_id, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return [CapabilityOut.model_validate(r) for r in rows]


@capabilities_router.put(
    "/{eid}",
    response_model=CapabilityOut,
    operation_id="update_pm_capability",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_pm_capability(
    eid: UUID = Path(..., description="Capability ID"),
    payload: CapabilityIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CapabilityOut:
    """Update a capability. Omitted fields are unchanged; an explicit null sets a field to not set."""
    row = await svc.update_row(session, Capability, eid, _sent(payload), tenant_id)
    return CapabilityOut.model_validate(row)


@capabilities_router.delete(
    "/{eid}",
    operation_id="delete_pm_capability",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT},
)
async def delete_pm_capability(
    eid: UUID = Path(..., description="Capability ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a capability. 409 while card types belong to it or a WIP limit uses it."""
    return await svc.delete_row(session, Capability, eid, tenant_id)


# ---------------------------------------------------------------------------
# Card types
# ---------------------------------------------------------------------------


@card_types_router.post(
    "",
    response_model=CardTypeOut,
    operation_id="create_card_type",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_card_type(
    payload: CardTypeIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CardTypeOut:
    """Create a card type in a capability."""
    row = await svc.create_row(session, CardType, payload.model_dump(), tenant_id, subject_id)
    return CardTypeOut.model_validate(row)


@card_types_router.get(
    "/{eid}",
    response_model=CardTypeOut,
    operation_id="get_card_type",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_card_type(
    eid: UUID = Path(..., description="Card type ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CardTypeOut:
    """Get a card type."""
    return CardTypeOut.model_validate(await svc.get_row(session, CardType, eid, tenant_id))


@card_types_router.get(
    "",
    response_model=list[CardTypeOut],
    operation_id="list_card_types",
    responses=AUTH_RESPONSES,
)
async def list_card_types(
    response: Response,
    capability_id: UUID | None = Query(None, description="Filter by capability"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[CardTypeOut]:
    """List card types, by name."""
    rows, total = await svc.list_rows(
        session, CardType, tenant_id, limit=limit, offset=offset, capability_id=capability_id
    )
    response.headers["X-Total-Count"] = str(total)
    return [CardTypeOut.model_validate(r) for r in rows]


@card_types_router.put(
    "/{eid}",
    response_model=CardTypeOut,
    operation_id="update_card_type",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_card_type(
    eid: UUID = Path(..., description="Card type ID"),
    payload: CardTypeIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CardTypeOut:
    """Update a card type (including moving it to another capability). Omitted fields are unchanged; an explicit null sets a field to not set."""
    row = await svc.update_row(session, CardType, eid, _sent(payload), tenant_id)
    return CardTypeOut.model_validate(row)


@card_types_router.delete(
    "/{eid}",
    operation_id="delete_card_type",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT},
)
async def delete_card_type(
    eid: UUID = Path(..., description="Card type ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a card type. 409 while a project type, recipe or WIP limit still uses it."""
    return await svc.delete_row(session, CardType, eid, tenant_id)


# ---------------------------------------------------------------------------
# Project types
# ---------------------------------------------------------------------------


def _sent(payload: BaseModel, exclude: set[str] | None = None) -> dict[str, Any]:
    """The fields the client sent: on update, an omitted field keeps its stored value."""
    return payload.model_dump(exclude_unset=True, exclude=exclude)


def _type_fields(payload: ProjectTypeIn) -> dict[str, Any]:
    return payload.model_dump(exclude={"phase_ids"})


@project_types_router.post(
    "",
    response_model=ProjectTypeOut,
    operation_id="create_project_type",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_project_type(
    payload: ProjectTypeIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectTypeOut:
    """Create a project type with its ordered phase selection."""
    return await svc.create_project_type(
        session, _type_fields(payload), payload.phase_ids, tenant_id, subject_id
    )


@project_types_router.get(
    "/{eid}",
    response_model=ProjectTypeOut,
    operation_id="get_project_type",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_project_type(
    eid: UUID = Path(..., description="Project type ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectTypeOut:
    """Get a project type with its ordered phases."""
    row = await svc.get_row(session, ProjectType, eid, tenant_id)
    return await svc.project_type_out(session, row)


@project_types_router.get(
    "",
    response_model=list[ProjectTypeOut],
    operation_id="list_project_types",
    responses=AUTH_RESPONSES,
)
async def list_project_types(
    response: Response,
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ProjectTypeOut]:
    """List project types, by name, each with its ordered phases."""
    rows, total = await svc.list_rows(session, ProjectType, tenant_id, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return [await svc.project_type_out(session, r) for r in rows]


@project_types_router.put(
    "/{eid}",
    response_model=ProjectTypeOut,
    operation_id="update_project_type",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_project_type(
    eid: UUID = Path(..., description="Project type ID"),
    payload: ProjectTypeIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectTypeOut:
    """Update a project type. A sent ``phase_ids`` replaces the phase selection; omitted
    fields (including ``phase_ids``) are unchanged."""
    phase_ids = payload.phase_ids if "phase_ids" in payload.model_fields_set else None
    return await svc.update_project_type(
        session, eid, _sent(payload, exclude={"phase_ids"}), phase_ids, tenant_id
    )


@project_types_router.delete(
    "/{eid}",
    operation_id="delete_project_type",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT},
)
async def delete_project_type(
    eid: UUID = Path(..., description="Project type ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a project type with its phase selection, card-type placements, recipes and WIP
    limits; its board templates become any-project-type templates. A project type a
    project still uses is refused (409, naming the projects)."""
    return await svc.delete_row(session, ProjectType, eid, tenant_id)


@project_types_router.get(
    "/{eid}/card-types",
    response_model=ProjectTypeCardTypesOut,
    operation_id="get_project_type_card_types",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_project_type_card_types(
    eid: UUID = Path(..., description="Project type ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectTypeCardTypesOut:
    """The project type's picture: each phase → the capabilities used → their card types."""
    return await svc.get_composition(session, eid, tenant_id)


@project_types_router.put(
    "/{eid}/card-types",
    response_model=ProjectTypeCardTypesOut,
    operation_id="set_project_type_card_types",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def set_project_type_card_types(
    eid: UUID = Path(..., description="Project type ID"),
    payload: ProjectTypeCardTypesIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectTypeCardTypesOut:
    """Replace which card types the project type uses in each of its selected phases.

    Per listed phase, an omitted ``entry_card_type_ids`` / ``output`` / ``exit_rule`` is
    unchanged (card types that stay keep their entry flag); an unlisted phase loses its card
    types but keeps its overrides.
    """
    return await svc.set_composition(session, eid, payload, tenant_id)


@project_types_router.get(
    "/{eid}/recipes",
    response_model=list[RecipeOut],
    operation_id="list_project_type_recipes",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def list_project_type_recipes(
    eid: UUID = Path(..., description="Project type ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[RecipeOut]:
    """The project type's recipes, in phase order, then by size (any-size first, S, M, L)."""
    return await svc.project_type_recipes(session, eid, tenant_id)


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


@recipes_router.post(
    "",
    response_model=RecipeOut,
    operation_id="create_pm_recipe",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_pm_recipe(
    payload: RecipeIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RecipeOut:
    """Create a recipe. 422 if an item's card type is not in the project type's matrix for
    the phase; 409 if the (project type, phase, size) slot already has a recipe."""
    return await svc.create_recipe(session, payload, tenant_id, subject_id)


@recipes_router.get(
    "/{eid}",
    response_model=RecipeOut,
    operation_id="get_pm_recipe",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_pm_recipe(
    eid: UUID = Path(..., description="Recipe ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RecipeOut:
    """Get a recipe with its items."""
    row = await svc.get_row(session, Recipe, eid, tenant_id)
    return (await svc.recipes_out(session, [row]))[0]


@recipes_router.get(
    "",
    response_model=list[RecipeOut],
    operation_id="list_pm_recipes",
    responses=AUTH_RESPONSES,
)
async def list_pm_recipes(
    response: Response,
    project_type_id: UUID | None = Query(None, description="Filter by project type"),
    phase_id: UUID | None = Query(None, description="Filter by phase"),
    size: Literal["S", "M", "L"] | None = Query(None, description="Filter by size"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[RecipeOut]:
    """List recipes, by name."""
    rows, total = await svc.list_rows(
        session,
        Recipe,
        tenant_id,
        limit=limit,
        offset=offset,
        project_type_id=project_type_id,
        phase_id=phase_id,
        size=size,
    )
    response.headers["X-Total-Count"] = str(total)
    return await svc.recipes_out(session, rows)


@recipes_router.put(
    "/{eid}",
    response_model=RecipeOut,
    operation_id="update_pm_recipe",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_pm_recipe(
    eid: UUID = Path(..., description="Recipe ID"),
    payload: RecipeIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RecipeOut:
    """Update a recipe. A sent ``items`` replaces the whole item list; omitted fields
    (including ``items``) are unchanged."""
    return await svc.update_recipe(session, eid, payload, tenant_id)


@recipes_router.delete(
    "/{eid}",
    operation_id="delete_pm_recipe",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def delete_pm_recipe(
    eid: UUID = Path(..., description="Recipe ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a recipe and its items."""
    return await svc.delete_row(session, Recipe, eid, tenant_id)


# ---------------------------------------------------------------------------
# Default catalogue
# ---------------------------------------------------------------------------


@catalogue_router.post(
    "/import-defaults",
    response_model=CatalogueImportOut,
    operation_id="import_pm_catalogue_defaults",
    responses=AUTH_RESPONSES,
)
async def import_pm_catalogue_defaults(
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CatalogueImportOut:
    """Copy the default catalogue into the tenant (idempotent by name; never overwrites).

    Defaults: the conveyor v4 phases (`setup`, `preliminary`, `execution`), the
    trigger-registry capabilities plus `kanban_coach`, every card type the one IT project
    type `it` places (R228: all v4-defined card types; definitions from the v4 capability designs, definitions of
    done from the landing predicates), the registry project types with `it`'s phases,
    matrix and entry card type, and the tier-2 rows. On existing rows only unset (NULL)
    attributes are filled.
    """
    return await svc.import_defaults(session, tenant_id, subject_id)


# ---------------------------------------------------------------------------
# WIP limits
# ---------------------------------------------------------------------------


@wip_limits_router.post(
    "",
    response_model=WipLimitOut,
    operation_id="create_pm_wip_limit",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_pm_wip_limit(
    payload: WipLimitIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> WipLimitOut:
    """Create a WIP limit. 409 if the scope already has one; 422 for a foreign id."""
    row = await svc.create_row(session, WipLimit, payload.model_dump(), tenant_id, subject_id)
    return WipLimitOut.model_validate(row)


@wip_limits_router.get(
    "/resolve",
    response_model=WipLimitResolveOut,
    operation_id="resolve_pm_wip_limit",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def resolve_pm_wip_limit(
    project_type_id: UUID | None = Query(None, description="The card's project type"),
    phase_id: UUID | None = Query(None, description="The card's phase"),
    capability_id: UUID | None = Query(None, description="The card's capability"),
    card_type_id: UUID | None = Query(None, description="The card's card type"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> WipLimitResolveOut:
    """The WIP limit that applies: the most specific matching row.

    A row matches when each of its non-null dimensions equals the query's. The row with
    the most non-null dimensions wins; ties go to card type, then capability, then phase,
    then project type. ``matched_on`` lists the winner's non-null dimensions. 404 when no
    row matches.
    """
    return await svc.resolve_wip_limit(
        session,
        tenant_id,
        {
            "project_type_id": project_type_id,
            "phase_id": phase_id,
            "capability_id": capability_id,
            "card_type_id": card_type_id,
        },
    )


@wip_limits_router.get(
    "/{eid}",
    response_model=WipLimitOut,
    operation_id="get_pm_wip_limit",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_pm_wip_limit(
    eid: UUID = Path(..., description="WIP limit ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> WipLimitOut:
    """Get a WIP limit."""
    return WipLimitOut.model_validate(await svc.get_row(session, WipLimit, eid, tenant_id))


@wip_limits_router.get(
    "",
    response_model=list[WipLimitOut],
    operation_id="list_pm_wip_limits",
    responses=AUTH_RESPONSES,
)
async def list_pm_wip_limits(
    response: Response,
    project_type_id: UUID | None = Query(None, description="Rows with this project type"),
    phase_id: UUID | None = Query(None, description="Rows with this phase"),
    capability_id: UUID | None = Query(None, description="Rows with this capability"),
    card_type_id: UUID | None = Query(None, description="Rows with this card type"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[WipLimitOut]:
    """List WIP limits, oldest first. Each filter matches the row's own value exactly."""
    rows, total = await svc.list_rows(
        session,
        WipLimit,
        tenant_id,
        limit=limit,
        offset=offset,
        order_by=(WipLimit.created_at, WipLimit.id),
        project_type_id=project_type_id,
        phase_id=phase_id,
        capability_id=capability_id,
        card_type_id=card_type_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return [WipLimitOut.model_validate(r) for r in rows]


@wip_limits_router.put(
    "/{eid}",
    response_model=WipLimitOut,
    operation_id="update_pm_wip_limit",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_pm_wip_limit(
    eid: UUID = Path(..., description="WIP limit ID"),
    payload: WipLimitIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> WipLimitOut:
    """Update a WIP limit (scope and limit). Omitted fields are unchanged; an explicit null sets a field to not set."""
    row = await svc.update_row(session, WipLimit, eid, _sent(payload), tenant_id)
    return WipLimitOut.model_validate(row)


@wip_limits_router.delete(
    "/{eid}",
    operation_id="delete_pm_wip_limit",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def delete_pm_wip_limit(
    eid: UUID = Path(..., description="WIP limit ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a WIP limit."""
    return await svc.delete_row(session, WipLimit, eid, tenant_id)


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------


@priority_levels_router.post(
    "",
    response_model=PriorityLevelOut,
    operation_id="create_pm_priority_level",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_pm_priority_level(
    payload: PriorityLevelIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PriorityLevelOut:
    """Create a priority level. 409 if its rank is taken, or if it would be a second default."""
    row = await svc.create_row(session, PriorityLevel, payload.model_dump(), tenant_id, subject_id)
    return PriorityLevelOut.model_validate(row)


@priority_levels_router.get(
    "/{eid}",
    response_model=PriorityLevelOut,
    operation_id="get_pm_priority_level",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_pm_priority_level(
    eid: UUID = Path(..., description="Priority level ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PriorityLevelOut:
    """Get a priority level."""
    return PriorityLevelOut.model_validate(
        await svc.get_row(session, PriorityLevel, eid, tenant_id)
    )


@priority_levels_router.get(
    "",
    response_model=list[PriorityLevelOut],
    operation_id="list_pm_priority_levels",
    responses=AUTH_RESPONSES,
)
async def list_pm_priority_levels(
    response: Response,
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[PriorityLevelOut]:
    """List priority levels by rank, highest priority (rank 0) first."""
    rows, total = await svc.list_rows(
        session,
        PriorityLevel,
        tenant_id,
        limit=limit,
        offset=offset,
        order_by=(PriorityLevel.rank, PriorityLevel.id),
    )
    response.headers["X-Total-Count"] = str(total)
    return [PriorityLevelOut.model_validate(r) for r in rows]


@priority_levels_router.put(
    "/{eid}",
    response_model=PriorityLevelOut,
    operation_id="update_pm_priority_level",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_pm_priority_level(
    eid: UUID = Path(..., description="Priority level ID"),
    payload: PriorityLevelIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PriorityLevelOut:
    """Update a priority level. Omitted fields are unchanged; an explicit null sets a field to not set."""
    row = await svc.update_row(session, PriorityLevel, eid, _sent(payload), tenant_id)
    return PriorityLevelOut.model_validate(row)


@priority_levels_router.delete(
    "/{eid}",
    operation_id="delete_pm_priority_level",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def delete_pm_priority_level(
    eid: UUID = Path(..., description="Priority level ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a priority level."""
    return await svc.delete_row(session, PriorityLevel, eid, tenant_id)


# ---------------------------------------------------------------------------
# Deliverable kinds
# ---------------------------------------------------------------------------


@deliverable_kinds_router.post(
    "",
    response_model=DeliverableKindOut,
    operation_id="create_pm_deliverable_kind",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_pm_deliverable_kind(
    payload: DeliverableKindIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeliverableKindOut:
    """Create a deliverable kind."""
    row = await svc.create_row(
        session, DeliverableKind, payload.model_dump(), tenant_id, subject_id
    )
    return DeliverableKindOut.model_validate(row)


@deliverable_kinds_router.get(
    "/{eid}",
    response_model=DeliverableKindOut,
    operation_id="get_pm_deliverable_kind",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_pm_deliverable_kind(
    eid: UUID = Path(..., description="Deliverable kind ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeliverableKindOut:
    """Get a deliverable kind."""
    return DeliverableKindOut.model_validate(
        await svc.get_row(session, DeliverableKind, eid, tenant_id)
    )


@deliverable_kinds_router.get(
    "",
    response_model=list[DeliverableKindOut],
    operation_id="list_pm_deliverable_kinds",
    responses=AUTH_RESPONSES,
)
async def list_pm_deliverable_kinds(
    response: Response,
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[DeliverableKindOut]:
    """List deliverable kinds, by name."""
    rows, total = await svc.list_rows(
        session,
        DeliverableKind,
        tenant_id,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return [DeliverableKindOut.model_validate(r) for r in rows]


@deliverable_kinds_router.put(
    "/{eid}",
    response_model=DeliverableKindOut,
    operation_id="update_pm_deliverable_kind",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_pm_deliverable_kind(
    eid: UUID = Path(..., description="Deliverable kind ID"),
    payload: DeliverableKindIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeliverableKindOut:
    """Update a deliverable kind. Omitted fields are unchanged; an explicit null sets a field to not set."""
    row = await svc.update_row(session, DeliverableKind, eid, _sent(payload), tenant_id)
    return DeliverableKindOut.model_validate(row)


@deliverable_kinds_router.delete(
    "/{eid}",
    operation_id="delete_pm_deliverable_kind",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def delete_pm_deliverable_kind(
    eid: UUID = Path(..., description="Deliverable kind ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a deliverable kind."""
    return await svc.delete_row(session, DeliverableKind, eid, tenant_id)


# ---------------------------------------------------------------------------
# Milestone kinds
# ---------------------------------------------------------------------------


@milestone_kinds_router.post(
    "",
    response_model=MilestoneKindOut,
    operation_id="create_pm_milestone_kind",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_pm_milestone_kind(
    payload: MilestoneKindIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> MilestoneKindOut:
    """Create a milestone kind, optionally marking a phase's exit."""
    row = await svc.create_row(session, MilestoneKind, payload.model_dump(), tenant_id, subject_id)
    return MilestoneKindOut.model_validate(row)


@milestone_kinds_router.get(
    "/{eid}",
    response_model=MilestoneKindOut,
    operation_id="get_pm_milestone_kind",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_pm_milestone_kind(
    eid: UUID = Path(..., description="Milestone kind ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> MilestoneKindOut:
    """Get a milestone kind."""
    return MilestoneKindOut.model_validate(
        await svc.get_row(session, MilestoneKind, eid, tenant_id)
    )


@milestone_kinds_router.get(
    "",
    response_model=list[MilestoneKindOut],
    operation_id="list_pm_milestone_kinds",
    responses=AUTH_RESPONSES,
)
async def list_pm_milestone_kinds(
    response: Response,
    phase_id: UUID | None = Query(None, description="Filter by phase"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[MilestoneKindOut]:
    """List milestone kinds, by name."""
    rows, total = await svc.list_rows(
        session,
        MilestoneKind,
        tenant_id,
        limit=limit,
        offset=offset,
        phase_id=phase_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return [MilestoneKindOut.model_validate(r) for r in rows]


@milestone_kinds_router.put(
    "/{eid}",
    response_model=MilestoneKindOut,
    operation_id="update_pm_milestone_kind",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_pm_milestone_kind(
    eid: UUID = Path(..., description="Milestone kind ID"),
    payload: MilestoneKindIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> MilestoneKindOut:
    """Update a milestone kind. Omitted fields are unchanged; an explicit null sets a field to not set."""
    row = await svc.update_row(session, MilestoneKind, eid, _sent(payload), tenant_id)
    return MilestoneKindOut.model_validate(row)


@milestone_kinds_router.delete(
    "/{eid}",
    operation_id="delete_pm_milestone_kind",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def delete_pm_milestone_kind(
    eid: UUID = Path(..., description="Milestone kind ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a milestone kind."""
    return await svc.delete_row(session, MilestoneKind, eid, tenant_id)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


@gates_router.post(
    "",
    response_model=GateOut,
    operation_id="create_pm_gate",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_pm_gate(
    payload: GateIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> GateOut:
    """Create a gate on a phase's exit."""
    row = await svc.create_row(session, Gate, payload.model_dump(), tenant_id, subject_id)
    return GateOut.model_validate(row)


@gates_router.get(
    "/{eid}",
    response_model=GateOut,
    operation_id="get_pm_gate",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_pm_gate(
    eid: UUID = Path(..., description="Gate ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> GateOut:
    """Get a gate."""
    return GateOut.model_validate(await svc.get_row(session, Gate, eid, tenant_id))


@gates_router.get(
    "",
    response_model=list[GateOut],
    operation_id="list_pm_gates",
    responses=AUTH_RESPONSES,
)
async def list_pm_gates(
    response: Response,
    phase_id: UUID | None = Query(None, description="Filter by phase"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[GateOut]:
    """List gates, by name."""
    rows, total = await svc.list_rows(
        session,
        Gate,
        tenant_id,
        limit=limit,
        offset=offset,
        phase_id=phase_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return [GateOut.model_validate(r) for r in rows]


@gates_router.put(
    "/{eid}",
    response_model=GateOut,
    operation_id="update_pm_gate",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_pm_gate(
    eid: UUID = Path(..., description="Gate ID"),
    payload: GateIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> GateOut:
    """Update a gate. Omitted fields are unchanged; an explicit null sets a field to not set."""
    row = await svc.update_row(session, Gate, eid, _sent(payload), tenant_id)
    return GateOut.model_validate(row)


@gates_router.delete(
    "/{eid}",
    operation_id="delete_pm_gate",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def delete_pm_gate(
    eid: UUID = Path(..., description="Gate ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a gate."""
    return await svc.delete_row(session, Gate, eid, tenant_id)


# ---------------------------------------------------------------------------
# Board templates
# ---------------------------------------------------------------------------


@board_templates_router.post(
    "",
    response_model=BoardTemplateOut,
    operation_id="create_pm_board_template",
    responses={**AUTH_RESPONSES, **_CONFLICT, **_INVALID},
)
async def create_pm_board_template(
    payload: BoardTemplateIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> BoardTemplateOut:
    """Create a board template."""
    row = await svc.create_row(session, BoardTemplate, payload.model_dump(), tenant_id, subject_id)
    return BoardTemplateOut.model_validate(row)


@board_templates_router.get(
    "/{eid}",
    response_model=BoardTemplateOut,
    operation_id="get_pm_board_template",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_pm_board_template(
    eid: UUID = Path(..., description="Board template ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> BoardTemplateOut:
    """Get a board template."""
    return BoardTemplateOut.model_validate(
        await svc.get_row(session, BoardTemplate, eid, tenant_id)
    )


@board_templates_router.get(
    "",
    response_model=list[BoardTemplateOut],
    operation_id="list_pm_board_templates",
    responses=AUTH_RESPONSES,
)
async def list_pm_board_templates(
    response: Response,
    project_type_id: UUID | None = Query(None, description="Filter by project type"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[BoardTemplateOut]:
    """List board templates, by name."""
    rows, total = await svc.list_rows(
        session,
        BoardTemplate,
        tenant_id,
        limit=limit,
        offset=offset,
        project_type_id=project_type_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return [BoardTemplateOut.model_validate(r) for r in rows]


@board_templates_router.put(
    "/{eid}",
    response_model=BoardTemplateOut,
    operation_id="update_pm_board_template",
    responses={**AUTH_RESPONSES, **_NOT_FOUND, **_CONFLICT, **_INVALID},
)
async def update_pm_board_template(
    eid: UUID = Path(..., description="Board template ID"),
    payload: BoardTemplateIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> BoardTemplateOut:
    """Update a board template. Omitted fields are unchanged; an explicit null sets a field to not set."""
    row = await svc.update_row(session, BoardTemplate, eid, _sent(payload), tenant_id)
    return BoardTemplateOut.model_validate(row)


@board_templates_router.delete(
    "/{eid}",
    operation_id="delete_pm_board_template",
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def delete_pm_board_template(
    eid: UUID = Path(..., description="Board template ID"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete a board template."""
    return await svc.delete_row(session, BoardTemplate, eid, tenant_id)
