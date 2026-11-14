"""Architecture elements CRUD endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from models.architecture import ArchitectureElement
from quality_arch import element_quality_check
from repositories import hierarchy
from repositories.bounded import fetch_bounded
from repositories.hierarchy import assert_parent_acyclic
from routers._quality_history import (
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    quality_history_response,
)
from routers.architecture_tools import BoundedInputRoute
from routers.responses import AUTH_RESPONSES
from schemas.architecture import (
    ArchitectureElementIn,
    ArchitectureElementOut,
    ArchitectureElementRealisationBindingIn,
    ArchitectureElementRealisationBindingList,
    ArchitectureElementRealisationBindingOut,
)
from schemas.common import DeleteResult, ErrorResponse, VersionComparison, VersionInfo
from schemas.requirements import QualityHistory, QualityReport
from services import architecture_element_service as svc
from services import version_service
from services.archimate_integrity import (
    assert_parent_resolves,
    assert_retype_keeps_relationships_valid,
)
from services.architecture_upsert import (
    code_binding_upsert_key,
    upsert_synthetic_by_code_binding,
)
from services.idempotency import (
    IDEMPOTENCY_409,
    Idempotency,
    IdempotentReplay,
    idempotency_key_header,
)
from services.modeling_models import (
    STALE_HEAD_412,
    check_if_match,
    if_match_header,
    model_visible,
    resolve_create_model,
    row_model_id,
    set_etag,
)
from services.quality_service import QualityResult, persist_quality
from services.realisation_bindings import (
    attach_binding,
    detach_binding,
    extract_code_binding_from_payload,
    list_bindings_for_element,
    write_coding_component_binding,
)


def _strip_code_binding_from_properties(payload: ArchitectureElementIn) -> None:
    """Drop ``code_binding`` from ``payload.properties`` so it is not persisted
    on the element JSONB column. The wire payload's ``code_binding`` is a
    request-side convenience; the structured
    ``architecture_element_realisation_bindings`` table is the canonical store.
    """
    props = payload.properties
    if isinstance(props, dict) and "code_binding" in props:
        props.pop("code_binding", None)


# Same `route_class` as `architecture_tools`, for the
# same reason: `batch-delete`'s filter branch calls `fetch_bounded`, which raises
# `InputTooLarge` (a plain `ValueError`). Without this the refusal escapes the
# router unhandled and answers 500 — "this service is broken" — instead of the
# 422 that names the total and the limit the caller has to act on. The mapping is
# per-ROUTER, so it does not arrive from `architecture_tools` by inheritance.
router = APIRouter(route_class=BoundedInputRoute)


_ALLOWED_FILTER_KEYS = frozenset({"layer", "status", "project_id", "is_synthetic"})
_BATCH_DELETE_MAX = 500


class BatchDeleteRequest(BaseModel):
    """Body for ``POST /architecture/elements/batch-delete``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    ids: list[UUID] | None = Field(
        None, description="Explicit element IDs (max 500); mutually-exclusive with filter"
    )
    filter: dict[str, Any] | None = Field(
        None,
        description=("Filter dict with allowed keys: layer, status, project_id, is_synthetic"),
    )

    @field_validator("ids")
    @classmethod
    def _cap_ids(cls, v: list[UUID] | None) -> list[UUID] | None:
        if v and len(v) > _BATCH_DELETE_MAX:
            raise ValueError(f"max {_BATCH_DELETE_MAX} ids per call")
        return v

    @field_validator("filter")
    @classmethod
    def _whitelist_filter(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        unknown = set(v.keys()) - _ALLOWED_FILTER_KEYS
        if unknown:
            raise ValueError(
                f"Unknown filter keys: {sorted(unknown)}. Allowed: {sorted(_ALLOWED_FILTER_KEYS)}"
            )
        return v


@router.post(
    "",
    response_model=ArchitectureElementOut,
    operation_id="create_architecture_element",
    responses={
        **AUTH_RESPONSES,
        201: {
            "description": "Synthetic upsert inserted a new element",
            "model": ArchitectureElementOut,
        },
        409: IDEMPOTENCY_409,
        412: STALE_HEAD_412,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def create_architecture_element(
    payload: ArchitectureElementIn,
    response: Response,
    idempotency_key: str | None = Depends(idempotency_key_header),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureElementOut:
    """Create a new architecture element (200).

    Elements are identified by id only (target spec A12): two elements may share a
    title and type — the ArchiMate duplicate checker reports that as a WARNING
    (``AM-DUP-001``), it never merges them. Make a retry safe with the optional
    ``Idempotency-Key`` header (``services/idempotency.py``).

    When ``is_synthetic=true`` AND
    ``properties.code_binding.{container, component_id}`` are both present,
    the call becomes an upsert keyed on
    ``(tenant_id, project_id, code_binding.container,
    code_binding.component_id)``: existing rows are updated (200), new rows
    are inserted (201). This lets fr's Architecture Governance review queue
    (P4.4) accept the same proposal twice without creating duplicate Artifact
    rows. Contract: ``docs/ARCHITECTURE_PROPOSALS_CONTRACT.md``.
    """
    # Hash the body BEFORE ``code_binding`` is stripped from it below.
    idem = Idempotency(
        session,
        route="/architecture/elements",
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        key=idempotency_key,
        body={**payload.model_dump(mode="json"), "model_id": payload.model_id},
    )
    if (replay := await idem.begin()) is not None:
        response.status_code = replay.status_code
        return ArchitectureElementOut.model_validate(replay.body)
    model_id = await resolve_create_model(
        session,
        model_id=payload.model_id,
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        project_id=payload.project_id,
        if_match=if_match,
    )

    binding = code_binding_upsert_key(payload)
    binding_meta = extract_code_binding_from_payload(payload.properties)
    # Strip the request's ``code_binding`` from properties before persisting
    # the element — the structured bindings table is the canonical store.
    _strip_code_binding_from_properties(payload)

    # WRITE-TIME ACYCLICITY (guard 3 of 3). This POST is not always a create:
    # the synthetic upsert resolves to an EXISTING row, and both branches
    # persist payload.parent_id. The check runs once here, before the branch,
    # with element_id=None — which catches a parent whose own ancestor chain is
    # already cyclic. A brand-new row cannot be its own ancestor. The synthetic
    # upsert's update branches re-check with the resolved row's id.
    await assert_parent_acyclic(
        session,
        model=ArchitectureElement,
        tenant_id=tenant_id,
        element_id=None,
        parent_id=payload.parent_id,
    )
    await assert_parent_resolves(
        session, parent_id=payload.parent_id, tenant_id=tenant_id, subject_id=subject_id
    )

    # With an Idempotency-Key the binding row and the stored response are written inside
    # the winning write's own transaction (``before_commit``), so claim, element, binding
    # and response commit together — a crash can never leave a created element behind an
    # in-flight key.
    async def stage_status(out: ArchitectureElementOut, status: int) -> None:
        if binding_meta is not None:
            m_container, m_component_id, m_extra = binding_meta
            await write_coding_component_binding(
                session,
                element_id=out.id,
                tenant_id=tenant_id,
                container=m_container,
                component_id=m_component_id,
                extra_properties=m_extra,
            )
        await idem.stage(status, out, ArchitectureElementOut)

    async def stage(out: ArchitectureElementOut, created: bool) -> None:
        # Synthetic upsert: 201 inserted / 200 updated.
        await stage_status(out, 201 if created else 200)

    keyed = idem.key is not None

    async def stage_plain(out: ArchitectureElementOut) -> None:
        await stage_status(out, 200)

    try:
        async with idem.guard():
            if binding is not None:
                container, component_id = binding
                # Idempotent synthetic upsert with a DB-race backstop: the
                # uq_arch_elements_synthetic_binding_* partial unique indexes make the
                # concurrent double-mint impossible; the helper recovers the survivor on
                # the unique violation. Identity here is the CODE UNIT, not a title.
                result, created = await upsert_synthetic_by_code_binding(
                    session,
                    svc=svc,
                    payload=payload,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    visibility=visibility,
                    project_id=payload.project_id,
                    container=container,
                    component_id=component_id,
                    before_commit=stage if keyed else None,
                    # The race recovery rolls back (dropping the claim): ``reclaim``
                    # re-takes it before the survivor is touched, or replays a winner.
                    reclaim=idem.reclaimer(),
                    model_id=model_id,
                )
                status_code = 201 if created else 200
            else:
                # Plain create: no title identity (A12). Same-title elements are two rows.
                # 200, like views / standards / relationships (the pre-A12 contract).
                result = await svc.create(
                    session,
                    payload,
                    tenant_id,
                    subject_id,
                    visibility=visibility,
                    extra_columns={"model_id": model_id} if model_id is not None else None,
                    before_commit=stage_plain if keyed else None,
                )
                status_code = 200
    except IdempotentReplay as replayed:
        response.status_code = replayed.replay.status_code
        return ArchitectureElementOut.model_validate(replayed.replay.body)
    response.status_code = status_code

    if binding_meta is not None and not keyed:
        m_container, m_component_id, m_extra = binding_meta
        await write_coding_component_binding(
            session,
            element_id=result.id,
            tenant_id=tenant_id,
            container=m_container,
            component_id=m_component_id,
            extra_properties=m_extra,
        )
        await session.commit()
    set_etag(response, session)
    return result


@router.post(
    "/batch-delete",
    operation_id="batch_delete_architecture_elements",
    status_code=200,
    responses={
        **AUTH_RESPONSES,
        400: {"description": "Bad request", "model": ErrorResponse},
    },
)
async def batch_delete_architecture_elements(
    req: BatchDeleteRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Bulk delete elements by explicit ids OR by allow-listed filter keys.

    Always tenant-scoped; relationships referencing each deleted element are
    cleaned up as a side-effect via the standard ``EntityService.delete``
    pathway. ``ids`` and ``filter`` are mutually exclusive — exactly one of
    them must be provided.
    """
    if (req.ids is None and req.filter is None) or (req.ids and req.filter):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of `ids` or `filter`",
        )

    # An EMPTY filter is a malformed intent, in the same class as providing both
    # or neither — hence 400 here rather than the 422 the over-cap bound below
    # returns. `_whitelist_filter` rejects an unknown key but never requires a
    # present one, so `{"filter": {}}` used to select every element in the tenant
    # and delete it. This endpoint's contract is that the caller states WHAT to
    # delete; `{}` states nothing, and "no criteria" must not silently mean "all
    # rows" on a destructive path. Nothing is lost: "delete everything for this
    # tenant" already has its own deliberate, separately-named path in
    # `EntityService.bulk_delete`. And `{}` is the likeliest ACCIDENT — the n8n
    # node exposes Filter as a free-form JSON text field
    # (`architecture-elements.resource.ts:359-365`, `required: false`), where an
    # empty object is exactly what a workflow author leaves behind.
    #
    # Deliberately NOT symmetric with `{"ids": []}`, which keeps returning 200
    # `deleted: 0` (see the long comment on that branch below). An explicit empty
    # list is "delete these zero things" — the caller enumerated the set and it
    # was empty. An empty filter is "delete, without saying what". The first is a
    # complete instruction with nothing in it; the second is an incomplete one.
    if req.filter is not None and not req.filter:
        raise HTTPException(
            status_code=400,
            detail=(
                "`filter` must name at least one criterion; an empty filter would "
                "match every element in the tenant. Allowed keys: "
                f"{sorted(_ALLOWED_FILTER_KEYS)}."
            ),
        )

    deleted_ids: list[UUID] = []

    # ``is not None``, NOT truthiness. ``{"ids": []}`` passes the mutual-exclusion
    # guard above (``[]`` is not None, and ``[] and None`` is falsy), then a bare
    # ``if req.ids:`` skipped this branch and fell through to the ``assert`` on
    # the filter path — an AssertionError, i.e. HTTP 500, for a request that is
    # perfectly well-formed. Deleting an empty set is a no-op, not a fault: this
    # returns ``deleted: 0`` and keeps the endpoint idempotent, which is the same
    # posture as the "ignore unknowns silently" loop below.
    if req.ids is not None:
        # Resolve existing visible+mutable rows; ignore unknowns silently.
        for entity_id in req.ids:
            try:
                await svc.delete(session, entity_id, tenant_id, subject_id)
                deleted_ids.append(entity_id)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
        return {"deleted": len(deleted_ids), "ids": [str(i) for i in deleted_ids]}

    # filter path. Reachable only when `ids` is absent, which the guard above and
    # the `is not None` branch together guarantee — so this assert documents an
    # invariant rather than carrying one.
    assert req.filter is not None
    query = select(ArchitectureElement.id).where(ArchitectureElement.tenant_id == tenant_id)
    for key, value in req.filter.items():
        # `getattr` is safe here WITHOUT a router-side allowlist: the request
        # model already rejects unknown keys with a 422 naming the allowed set
        # (measured: `{"nope": 1}` -> 422 "Unknown filter keys: ['nope']. Allowed:
        # ['is_synthetic', 'layer', 'project_id', 'status']"). A second check here
        # would be dead code, and a WIDER one -- every mapped column -- would be
        # worse than none, because it would read as protection while admitting 34
        # fields the schema admits 4 of.
        column = getattr(ArchitectureElement, key)
        query = query.where(column == value)

    # Bounded by `_BATCH_DELETE_MAX` — the SAME constant the `ids` branch already
    # declares (line 60), deliberately NOT one of the `DEFAULT_MAX_INPUT_*`
    # ceilings. Those are 6000/24000, sized ~2x the largest tenant for READ-ONLY
    # graph analysis where loading the whole model is the point. This path
    # DELETES, one committed transaction per element, and the endpoint has
    # already told every caller that 500 is the most it will accept in one call.
    # `{"filter": {}}` is a well-formed request that matches every element in the
    # tenant (2,703 on the largest live one); before this bound the two branches
    # of the same endpoint disagreed by three orders of magnitude, and the
    # unbounded one was the one that could not be undone.
    #
    # `query` is `select(ArchitectureElement.id)` — a COLUMN select — so the rows
    # are SQLAlchemy `Row` objects, read as `r.id`. Do NOT `.scalars()` them.
    bounded = await fetch_bounded(
        session,
        query,
        cap=_BATCH_DELETE_MAX,
        order_by=ArchitectureElement.id,
        label="architecture elements matching filter",
    )
    bounded.raise_if_truncated()
    candidate_ids = [r.id for r in bounded.rows]
    for entity_id in candidate_ids:
        try:
            await svc.delete(session, entity_id, tenant_id, subject_id)
            deleted_ids.append(entity_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise

    return {"deleted": len(deleted_ids), "ids": [str(i) for i in deleted_ids]}


@router.get(
    "/{eid}",
    response_model=ArchitectureElementOut,
    operation_id="get_architecture_element",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_architecture_element(
    eid: UUID = Path(..., description="Architecture element ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureElementOut:
    """Retrieve architecture element by ID."""
    return await svc.get(session, eid, tenant_id, subject_id, include_system=True)


@router.get(
    "",
    response_model=list[ArchitectureElementOut],
    operation_id="list_architecture_elements",
    responses=AUTH_RESPONSES,
)
async def list_architecture_elements(
    response: Response,
    layer: str | None = Query(None, description="Filter by ArchiMate layer"),
    element_type: str | None = Query(None, description="Filter by element type"),
    status: str | None = Query(None, description="Filter by status"),
    architecture_state: str | None = Query(None, description="Filter by architecture state"),
    project_id: str | None = Query(None, description="Filter by project"),
    model_id: UUID | None = Query(
        None,
        description=(
            "Filter to the elements of this ArchiMate model (the element's `model_id`). "
            "Only a model the caller may read (`GET /modeling/models/{id}`) filters to "
            "anything: another tenant's model (the system tenant's included) or another "
            "subject's private model returns an empty list."
        ),
    ),
    synthetic: bool | None = Query(
        None,
        description=(
            "Filter by synthetic flag. Default (None) hides synthetic rows; "
            "pass true to retrieve only synthetic rows."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ArchitectureElementOut]:
    """List architecture elements with optional filters.

    Synthetic rows (``is_synthetic=true``) are tool-generated scaffolding and
    are excluded by default. Pass ``?synthetic=true`` to retrieve them.
    """
    if model_id is not None and not await model_visible(session, model_id, tenant_id, subject_id):
        # Rows are listed under a model only for a caller who may read the model itself.
        response.headers["X-Total-Count"] = "0"
        return []
    is_synthetic_filter = False if synthetic is None else synthetic
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        layer=layer,
        element_type=element_type,
        status=status,
        architecture_state=architecture_state,
        project_id=project_id,
        model_id=model_id,
        is_synthetic=is_synthetic_filter,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{eid}",
    response_model=ArchitectureElementOut,
    operation_id="update_architecture_element",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        412: STALE_HEAD_412,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def update_architecture_element(
    response: Response,
    eid: UUID = Path(..., description="Architecture element ID"),
    payload: ArchitectureElementIn = Body(...),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureElementOut:
    """Update architecture element.

    When the request payload carries a complete ``code_binding.{container,
    component_id}`` pair, the matching row in
    ``architecture_element_realisation_bindings`` (kind ``coding_component``)
    is refreshed and the ``code_binding`` block is stripped from the element's
    JSONB ``properties`` before persistence — the structured bindings table
    is the canonical store.
    """
    binding_meta = extract_code_binding_from_payload(payload.properties)
    _strip_code_binding_from_properties(payload)
    # ``model_id`` is fixed at create; If-Match is checked under the model's lock first.
    await check_if_match(
        session,
        await row_model_id(session, ArchitectureElement, eid, tenant_id, subject_id=subject_id),
        if_match,
    )
    # Write-time acyclicity BEFORE the update — this is the route that turns a
    # healthy tree into a cycle (PUT root {parent_id: <own descendant>}), and it
    # must refuse rather than persist and let the read guards mop up.
    await assert_parent_acyclic(
        session,
        model=ArchitectureElement,
        tenant_id=tenant_id,
        element_id=eid,
        parent_id=payload.parent_id,
    )
    await assert_parent_resolves(
        session, parent_id=payload.parent_id, tenant_id=tenant_id, subject_id=subject_id
    )
    await assert_retype_keeps_relationships_valid(
        session,
        element_id=eid,
        new_type=payload.element_type,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )
    result = await svc.update(session, eid, payload, tenant_id, subject_id)
    if binding_meta is not None:
        m_container, m_component_id, m_extra = binding_meta
        await write_coding_component_binding(
            session,
            element_id=result.id,
            tenant_id=tenant_id,
            container=m_container,
            component_id=m_component_id,
            extra_properties=m_extra,
        )
        await session.commit()
    set_etag(response, session)
    return result


@router.get(
    "/{eid}/children",
    response_model=list[ArchitectureElementOut],
    operation_id="list_architecture_element_children",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
    },
)
async def list_architecture_element_children(
    response: Response,
    eid: UUID = Path(..., description="Parent element ID"),
    limit: int = Query(1000, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ArchitectureElementOut]:
    """List direct children of an architecture element.

    Bounded with the sibling's CEILING (``GET /architecture/elements``,
    ``le=1000``) but NOT its default: this endpoint declared no ``limit`` at all
    and its query had no cap, so one fan-out parent returned the whole child set
    into a ``512Mi`` / ``replicas: 1`` pod.

    ⚠️ ``default=1000``, deliberately NOT the sibling's ``default=100``.
    The sibling is a list endpoint that has offered paging since it was written,
    so its callers pass a ``limit`` and expect a page. This one is an EXISTING
    UNPAGINATED endpoint acquiring a bound, and it has a real caller that sends
    no query string at all and has no pagination-follow loop:
    ``n8n-nodes-requirements`` op ``listArchitectureElementChildren``
    (``nodes/Requirements/operations/index.ts:1046``), dispatchable from AA
    conveyor cards. Defaulting to 100 would silently hand that caller a truncated
    child set the moment any element exceeds 100 children — trading an
    availability defect for exactly the "confidently wrong answer" this PR exists
    to remove. Defaulting to the ceiling keeps every caller's present behaviour
    while still making the query bounded.
    (Latent either way today: ``count(parent_id)`` over ``architecture_elements``
    is 0 of 3,680 on production, so no element currently has any children.)

    ``offset`` comes with the cap on purpose — a bound with no cursor makes
    every child past the first page permanently unreachable, which trades an
    availability defect for a correctness one. Ordering is by ``id``, which is
    uuid7 and therefore creation-ordered, so paging is stable rather than
    whatever order the heap hands back. The total is reported in
    ``X-Total-Count`` (the house form for every list endpoint here) so a caller
    can see that it has been truncated; the body stays a bare
    ``list[ArchitectureElementOut]``.
    """
    parent = await session.get(ArchitectureElement, eid)
    if not parent or parent.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    query = select(ArchitectureElement).where(
        ArchitectureElement.tenant_id == tenant_id,
        ArchitectureElement.parent_id == eid,
    )
    count_query = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_query)).scalar() or 0

    query = query.order_by(ArchitectureElement.id).offset(offset).limit(limit)
    rows = (await session.execute(query)).scalars().all()

    response.headers["X-Total-Count"] = str(total)
    return [ArchitectureElementOut.model_validate(r) for r in rows]


@router.get(
    "/{eid}/ancestors",
    response_model=list[ArchitectureElementOut],
    operation_id="list_architecture_element_ancestors",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        409: {"description": "Cycle detected in the element hierarchy", "model": ErrorResponse},
    },
)
async def list_architecture_element_ancestors(
    response: Response,
    eid: UUID = Path(..., description="Element ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ArchitectureElementOut]:
    """Return ancestor chain (nearest first), excluding the element itself.

    Terminates on cyclic ``parent_id`` data (409) and on pathologically deep but
    acyclic data (200 + ``X-Ancestors-Truncated``). The response body stays a
    bare ``list[ArchitectureElementOut]`` in BOTH the normal and the truncated
    case — the AA conveyor consumes this list positionally, so the truncation
    signal goes in headers and must never become a body envelope.
    """
    el = await session.get(ArchitectureElement, eid)
    if not el or el.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="not found")

    # Read at call time (module attribute, not a from-import) so the cap is one
    # knob rather than a value frozen into this module's namespace at import.
    max_depth = hierarchy.ANCESTORS_MAX_DEPTH

    # TWO INDEPENDENT TERMINATION GUARDS. Neither is sufficient alone:
    #
    #   1. ``NOT (e.id = ANY(up.path))`` terminates EXACTLY on a cycle, and —
    #      because it stops at the repeat rather than at an arbitrary limit —
    #      it lets us tell a cycle apart from a merely-long chain and answer
    #      409 instead of silently truncating. It does nothing for acyclic data.
    #
    #   2. ``up.depth < :max_depth`` bounds acyclic-but-pathological data (a
    #      100k-deep chain is not a cycle, and guard 1 would happily walk all of
    #      it). It is also the guard that still holds if guard 1 is ever
    #      mis-edited — an ``= ANY`` predicate is easy to break silently, and a
    #      broken guard 1 with no depth cap is an unbounded recursion on a
    #      ``replicas: 1`` service, i.e. the original defect back again.
    #
    # This was previously ``UNION ALL`` with a ``depth`` column that was
    # computed and then never filtered on, and no path column at all. Two
    # ordinary writes (A.parent=B, B.parent=A) plus one GET spun forever and
    # never returned the connection. Do not remove either predicate; do not
    # "simplify" this to ``UNION`` (dedupe would terminate, but it would report
    # a cycle as an ordinary short chain and the 409 would become a silent
    # wrong answer).
    cte = text(
        """
        WITH RECURSIVE up AS (
          SELECT id, parent_id, 1 AS depth, ARRAY[id] AS path
            FROM architecture_elements
            WHERE id = :eid AND tenant_id = :tid
          UNION ALL
          SELECT e.id, e.parent_id, up.depth + 1, up.path || e.id
            FROM architecture_elements e
            JOIN up ON e.id = up.parent_id
            WHERE e.tenant_id = :tid
              AND NOT (e.id = ANY(up.path))
              AND up.depth < :max_depth
        )
        SELECT id, parent_id, depth FROM up ORDER BY depth ASC
        """
    )
    rows = (
        await session.execute(cte, {"eid": eid, "tid": tenant_id, "max_depth": max_depth})
    ).all()
    if not rows:
        return []

    # rows[0] is the anchor (the element itself, depth 1); the rest are its
    # ancestors. The anchor is kept in the result set rather than filtered out
    # in SQL because a SELF-LOOP (A.parent = A) produces the anchor and nothing
    # else — the old ``WHERE id <> :eid`` made that case indistinguishable from
    # "no ancestors" and would have answered 200 [] to a genuine cycle.
    visited = {r[0] for r in rows}
    deepest_id, deepest_parent, deepest_depth = rows[-1]

    if deepest_parent is not None and deepest_parent in visited:
        # The walk stopped because the next hop was a node already on the path.
        # That is a cycle, not a short chain.
        raise HTTPException(
            status_code=409,
            detail=(
                f"cycle detected in the element hierarchy: element {deepest_id} "
                f"has parent {deepest_parent}, which is already in its ancestor chain"
            ),
        )

    if deepest_parent is not None and deepest_depth >= max_depth:
        # Acyclic but deeper than the cap: return what we walked, and SAY SO.
        # A truncated list that claims to be complete is worse than an error.
        response.headers["X-Ancestors-Truncated"] = "true"
        response.headers["X-Ancestors-Depth-Limit"] = str(max_depth)

    ancestor_ids = [r[0] for r in rows[1:]]
    if not ancestor_ids:
        return []

    elements = (
        (
            await session.execute(
                select(ArchitectureElement).where(ArchitectureElement.id.in_(ancestor_ids))
            )
        )
        .scalars()
        .all()
    )
    by_id = {e.id: e for e in elements}
    ordered = [by_id[aid] for aid in ancestor_ids if aid in by_id]
    return [ArchitectureElementOut.model_validate(r) for r in ordered]


@router.post(
    "/{eid}/quality",
    response_model=QualityReport,
    operation_id="get_architecture_element_quality",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
    },
)
async def architecture_element_quality(
    eid: UUID = Path(..., description="Architecture element ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityReport:
    """Run heuristic quality checks and persist score onto the element."""
    obj = await svc._get_owned(session, eid, tenant_id, subject_id)
    score, issues, passes = element_quality_check(obj)
    await persist_quality(
        session,
        obj,
        QualityResult(score=score, issues=issues, suggestions=[], agent_name=None),
    )
    await session.commit()
    return QualityReport(score=score, issues=issues, passes=passes)


@router.get(
    "/{eid}/quality/history",
    response_model=QualityHistory,
    operation_id="get_architecture_element_quality_history",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Architecture element not found", "model": ErrorResponse},
    },
)
async def architecture_element_quality_history(
    eid: UUID = Path(..., description="Architecture element ID"),
    limit: int = Query(HISTORY_LIMIT_DEFAULT, ge=1, le=HISTORY_LIMIT_MAX),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityHistory:
    """Every persisted quality evaluation of this architecture element, newest first.

    Append-only history behind the entity's ``quality_score`` (which stays the
    latest). Advisory data only — nothing gates on it.
    """
    obj = await svc._get_owned(session, eid, tenant_id, subject_id)
    return await quality_history_response(session, obj, tenant_id, limit)


@router.get(
    "/{eid}/version",
    response_model=VersionInfo,
    operation_id="get_architecture_element_version_info",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
    },
)
async def get_architecture_element_version_info(
    eid: UUID = Path(..., description="Architecture element ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionInfo:
    """Get architecture element version info."""
    obj = await svc._get_owned(session, eid, tenant_id, subject_id)
    return await version_service.get_info(obj)


@router.get(
    "/{eid}/versions",
    response_model=list[VersionInfo],
    operation_id="list_architecture_element_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
    },
)
async def list_architecture_element_versions(
    eid: UUID = Path(..., description="Architecture element ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[VersionInfo]:
    """List architecture element version history."""
    obj = await svc._get_owned(session, eid, tenant_id, subject_id)
    return await version_service.list_versions(session, "architecture_element", eid, obj)


@router.get(
    "/{eid}/versions/{v1}/compare/{v2}",
    response_model=VersionComparison,
    operation_id="compare_architecture_element_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element or version not found", "model": ErrorResponse},
        422: {"description": "Invalid version format", "model": ErrorResponse},
    },
)
async def compare_architecture_element_versions(
    eid: UUID = Path(..., description="Architecture element ID"),
    v1: str = Path(..., description="First version"),
    v2: str = Path(..., description="Second version"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionComparison:
    """Compare architecture element versions using stored snapshots."""
    await svc._get_owned(session, eid, tenant_id, subject_id)
    return await version_service.compare(session, "architecture_element", eid, v1, v2)


@router.get(
    "/{eid}/bindings",
    response_model=ArchitectureElementRealisationBindingList,
    operation_id="list_architecture_element_bindings",
    tags=["Architecture Elements"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
    },
)
async def list_architecture_element_bindings(
    eid: UUID = Path(..., description="Architecture element ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureElementRealisationBindingList:
    """List all realisation bindings attached to one element.

    Returns every binding row across all three kinds (``coding_component``,
    ``coding_container``, ``external_repo``) so fr's UI can render the full
    "realised by" surface for a strategic Application Component.
    """
    el = await svc._get_owned(session, eid, tenant_id, subject_id)  # noqa: SLF001
    rows = await list_bindings_for_element(session, element_id=el.id, tenant_id=tenant_id)
    return ArchitectureElementRealisationBindingList(
        bindings=[ArchitectureElementRealisationBindingOut.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.post(
    "/{eid}/bindings",
    response_model=ArchitectureElementRealisationBindingOut,
    operation_id="create_architecture_element_binding",
    tags=["Architecture Elements"],
    status_code=201,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        412: STALE_HEAD_412,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def create_architecture_element_binding(
    response: Response,
    payload: ArchitectureElementRealisationBindingIn = Body(...),
    eid: UUID = Path(..., description="Architecture element ID"),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureElementRealisationBindingOut:
    """Attach a realisation binding to an element.

    Idempotent against the dedup unique index — re-attaching the same
    logical binding returns the pre-existing row instead of erroring.
    Discriminated by ``binding_kind``; the request body's
    ``model_post_init`` validator enforces the per-kind required-fields
    shape so mismatched payloads are rejected with HTTP 422 before they
    reach the DB check constraint.
    """
    # Tenancy gate on the element (raises 404 if it doesn't belong to caller).
    await svc._get_owned(session, eid, tenant_id, subject_id)  # noqa: SLF001
    # A binding is manifest content of the element's model: If-Match / ETag apply.
    await check_if_match(
        session,
        await row_model_id(session, ArchitectureElement, eid, tenant_id, subject_id=subject_id),
        if_match,
    )
    row = await attach_binding(
        session,
        element_id=eid,
        tenant_id=tenant_id,
        payload=payload,
    )
    await session.commit()
    await session.refresh(row)
    set_etag(response, session)
    return ArchitectureElementRealisationBindingOut.model_validate(row)


@router.delete(
    "/{eid}/bindings/{binding_id}",
    operation_id="delete_architecture_element_binding",
    tags=["Architecture Elements"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element or binding not found", "model": ErrorResponse},
        412: STALE_HEAD_412,
    },
)
async def delete_architecture_element_binding(
    response: Response,
    eid: UUID = Path(..., description="Architecture element ID"),
    binding_id: UUID = Path(..., description="Binding ID"),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Detach a realisation binding from an element.

    Cross-tenant safe: both element-id-tenancy and binding-id-tenancy are
    checked, so a caller in tenant A cannot delete tenant B's binding by
    guessing the binding UUID.
    """
    await svc._get_owned(session, eid, tenant_id, subject_id)  # noqa: SLF001
    await check_if_match(
        session,
        await row_model_id(session, ArchitectureElement, eid, tenant_id, subject_id=subject_id),
        if_match,
    )
    deleted = await detach_binding(
        session,
        element_id=eid,
        tenant_id=tenant_id,
        binding_id=binding_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="binding not found")
    await session.commit()
    set_etag(response, session)
    return {"deleted": True, "id": str(binding_id)}


@router.delete(
    "/{eid}",
    operation_id="delete_architecture_element",
    response_model=DeleteResult,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        412: STALE_HEAD_412,
    },
)
async def delete_architecture_element(
    response: Response,
    eid: UUID = Path(..., description="Architecture element ID"),
    if_match: int | None = Depends(if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete architecture element, its relationships and its view occurrences."""
    await check_if_match(
        session,
        await row_model_id(session, ArchitectureElement, eid, tenant_id, subject_id=subject_id),
        if_match,
    )
    out = await svc.delete(session, eid, tenant_id, subject_id)
    set_etag(response, session)
    return DeleteResult.model_validate(out)
