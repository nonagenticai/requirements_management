"""Architecture relationships CRUD endpoints with metamodel validation."""

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from auth.pm_only import PM_ONLY_403, is_delegated, require_pm_writer_for_entity_types
from db import get_session
from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.architecture import (
    ArchitectureRelationshipIn,
    ArchitectureRelationshipOut,
    ArchitectureRelationshipUpdate,
)
from schemas.common import DeleteResult, ErrorResponse
from services import architecture_relationship_service as svc
from services.archimate_integrity import ARCHIMATE_ENDPOINT_TYPES
from services.idempotency import (
    IDEMPOTENCY_409,
    Idempotency,
    IdempotentReplay,
    idempotency_key_header,
)
from services.modeling_models import (
    STALE_HEAD_412,
    check_edge_if_match,
    check_if_match,
    if_match_header,
    model_visible,
    relationship_model_id,
    set_etag,
)

router = APIRouter(route_class=ErrorEnvelopeRoute)

# The refusal is the error envelope with ``findings`` (``model_integrity``), not a
# MetamodelValidationResult body.
_METAMODEL_422 = {
    "description": (
        "`model_integrity`: the relationship is not legal in the metamodel, or the model it "
        "would join (its SOURCE end's) is not one the caller may write into — another "
        "tenant's (e.g. the system tenant's), not writable, or of another visibility "
        "(AM-REF-001, `evidence.reason` `model_not_writable` / `model_visibility_mismatch`; "
        "an update is checked only when it moves the edge to a different model)"
    ),
    "model": ErrorResponse,
}


@router.post(
    "",
    response_model=ArchitectureRelationshipOut,
    operation_id="create_architecture_relationship",
    responses={
        **AUTH_RESPONSES,
        403: PM_ONLY_403,
        409: IDEMPOTENCY_409,
        400: {"description": "`if_match_not_applicable`: the edge joins no model"},
        412: STALE_HEAD_412,
        422: _METAMODEL_422,
    },
)
async def create_architecture_relationship(
    request: Request,
    response: Response,
    payload: ArchitectureRelationshipIn,
    idempotency_key: str | None = Depends(idempotency_key_header),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureRelationshipOut:
    """Create a relationship, validating against the ArchiMate metamodel when both endpoints are architecture elements.

    Metamodel validation lives in ``RelationshipService.create`` (and
    ``update``) so it covers every write path — the get-or-create AA-intake
    surface and any non-router caller — not just this route.
    """
    # A relationship with a PM runtime end is a PM write: fr may not make it (``auth/pm_only.py``).
    await require_pm_writer_for_entity_types(
        request, payload.source_entity_type, payload.target_entity_type
    )
    idem = Idempotency(
        session,
        route="/architecture/relationships",
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        key=idempotency_key,
        body=payload,
    )
    if (replay := await idem.begin()) is not None:
        response.status_code = replay.status_code
        return ArchitectureRelationshipOut.model_validate(replay.body)
    # An ArchiMate edge joins its source end's model (flush hook): If-Match is that head.
    await check_if_match(
        session,
        await relationship_model_id(
            session,
            payload.source_entity_type,
            payload.source_id,
            tenant_id,
            subject_id=subject_id,
            target_entity_type=payload.target_entity_type,
            if_match=if_match,
        ),
        if_match,
    )
    # Claim, edge and stored response commit in ONE transaction (``before_commit``).
    stage = idem.stager(200, ArchitectureRelationshipOut)
    # Get-or-create keyed on the edge identity — the endpoint tuple, not a
    # title (relationships have no meaningful title). Gated on ``project_id`` so
    # an AA-intake re-run updates the same edge in place instead of minting a
    # duplicate; ``RelationshipService._generate_slug`` would otherwise
    # auto-increment the slug and insert a fresh row every re-run. Non-project
    # callers keep pure-create semantics.
    try:
        async with idem.guard():
            if payload.project_id:
                # The race recovery rolls back (dropping the claim): ``reclaim`` re-takes it
                # before the survivor is touched, or replays a same-key request that won.
                relationship, _created = await svc.get_or_create(
                    session,
                    payload,
                    tenant_id,
                    subject_id,
                    visibility=visibility,
                    identity_fields=(
                        "project_id",
                        "source_entity_type",
                        "source_id",
                        "target_entity_type",
                        "target_id",
                        "relationship_type",
                    ),
                    before_commit=stage,
                    reclaim=idem.reclaimer(),
                )
            else:
                relationship = await svc.create(
                    session,
                    payload,
                    tenant_id,
                    subject_id,
                    visibility=visibility,
                    before_commit=stage,
                )
    except IdempotentReplay as replayed:
        response.status_code = replayed.replay.status_code
        return ArchitectureRelationshipOut.model_validate(replayed.replay.body)
    set_etag(response, session)
    return relationship


@router.get(
    "/{rid}",
    response_model=ArchitectureRelationshipOut,
    operation_id="get_architecture_relationship",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Relationship not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_architecture_relationship(
    rid: UUID = Path(..., description="Relationship ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureRelationshipOut:
    """Retrieve relationship by ID."""
    return await svc.get(session, rid, tenant_id, subject_id)


@router.get(
    "",
    response_model=list[ArchitectureRelationshipOut],
    operation_id="list_architecture_relationships",
    responses=AUTH_RESPONSES,
)
async def list_architecture_relationships(
    response: Response,
    source_entity_type: str | None = Query(
        None,
        description=(
            "Filter by source entity type. Naming either end's type lifts the default "
            "ArchiMate-only restriction (e.g. `toc_nodes` lists TOC edges)"
        ),
    ),
    source_id: UUID | None = Query(None, description="Filter by source ID"),
    target_entity_type: str | None = Query(
        None,
        description="Filter by target entity type (lifts the default restriction, as above)",
    ),
    target_id: UUID | None = Query(None, description="Filter by target ID"),
    relationship_type: str | None = Query(None, description="Filter by relationship type"),
    project_id: str | None = Query(None, description="Filter by project"),
    model_id: UUID | None = Query(
        None,
        description=(
            "Filter to the relationships of this ArchiMate model: the ArchiMate edges whose "
            "SOURCE end belongs to it (stored as the relationship's `model_id`, re-derived "
            "when an end moves). A relationship joins only a model of the caller's own "
            "tenant that it may write, with the model's visibility (otherwise the write is "
            "422 `model_not_writable` / `model_visibility_mismatch`). Non-ArchiMate links "
            "(PM / TOC / governance) belong to no model and never match. Only a model the "
            "caller may read (`GET /modeling/models/{id}`) filters to anything: another "
            "tenant's model or another subject's private model returns an empty list."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ArchitectureRelationshipOut]:
    """List ArchiMate relationships (both ends an architecture element or relationship).

    The table also stores TOC links and governance links; those are listed only when
    ``source_entity_type`` or
    ``target_entity_type`` names their type explicitly.
    """
    explicit_types = bool(source_entity_type) or bool(target_entity_type)
    if model_id is not None and not await model_visible(session, model_id, tenant_id, subject_id):
        # Rows are listed under a model only for a caller who may read the model itself.
        response.headers["X-Total-Count"] = "0"
        return []
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        limit=limit,
        offset=offset,
        endpoint_types=None if explicit_types else ARCHIMATE_ENDPOINT_TYPES,
        source_entity_type=source_entity_type,
        source_id=source_id,
        target_entity_type=target_entity_type,
        target_id=target_id,
        relationship_type=relationship_type,
        project_id=project_id,
        model_id=model_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{rid}",
    response_model=ArchitectureRelationshipOut,
    operation_id="update_architecture_relationship",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Relationship not found", "model": ErrorResponse},
        403: PM_ONLY_403,
        400: {"description": "`if_match_not_applicable`: the edge joins no model"},
        412: STALE_HEAD_412,
        422: _METAMODEL_422,
    },
)
async def update_architecture_relationship(
    request: Request,
    response: Response,
    rid: UUID = Path(..., description="Relationship ID"),
    payload: ArchitectureRelationshipUpdate = Body(...),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureRelationshipOut:
    """Update relationship metadata or endpoints; re-validates the metamodel."""
    changes = payload.model_dump(exclude_unset=True)
    if is_delegated(request):
        # Both ends, before AND after: fr may neither edit a PM edge nor turn one into
        # (or out of) a PM edge.
        before = await svc.get(session, rid, tenant_id, subject_id)
        await require_pm_writer_for_entity_types(
            request,
            before.source_entity_type,
            before.target_entity_type,
            changes.get("source_entity_type"),
            changes.get("target_entity_type"),
        )
    await check_edge_if_match(session, rid, tenant_id, if_match, subject_id=subject_id)
    obj = await svc.update(
        session,
        rid,
        changes,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )
    await session.commit()
    set_etag(response, session)
    return obj


@router.delete(
    "/{rid}",
    operation_id="delete_architecture_relationship",
    response_model=DeleteResult,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Relationship not found", "model": ErrorResponse},
        403: PM_ONLY_403,
        400: {"description": "`if_match_not_applicable`: the edge joins no model"},
        412: STALE_HEAD_412,
    },
)
async def delete_architecture_relationship(
    request: Request,
    response: Response,
    rid: UUID = Path(..., description="Relationship ID"),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete relationship."""
    if is_delegated(request):
        row = await svc.get(session, rid, tenant_id, subject_id)
        await require_pm_writer_for_entity_types(
            request, row.source_entity_type, row.target_entity_type
        )
    await check_edge_if_match(session, rid, tenant_id, if_match, subject_id=subject_id)
    out = await svc.delete(session, rid, tenant_id, subject_id)
    set_etag(response, session)
    return DeleteResult.model_validate(out)


@router.get(
    "/graph/{entity_type}/{entity_id}",
    response_model=list[ArchitectureRelationshipOut],
    operation_id="get_entity_graph",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Entity not found", "model": ErrorResponse},
    },
)
async def get_entity_graph(
    entity_type: str = Path(..., description="Entity type"),
    entity_id: UUID = Path(..., description="Entity ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ArchitectureRelationshipOut]:
    """Get all relationships where an entity appears as source or target."""
    return await svc.get_graph(session, entity_type, entity_id, tenant_id, subject_id)
