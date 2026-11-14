"""Requirements CRUD, quality, and priority endpoints."""

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from auth.governed_scope import (
    SCOPE_REQUIRED_422,
    body_project_scope,
    require_conveyor_project_scope,
)
from db import get_session
from prio import cod_from_components, rice, wsjf
from quality import quality_check
from routers._governed import (
    PATCH_409,
    ProjectCopyStatic,
    PromoteIn,
    governed_patch_model,
    include_shadowed_query,
    project_copy_model,
    project_copy_responses,
    promote_responses,
    require_fr_delegated,
    scope_project_query,
)
from routers.responses import AUTH_RESPONSES
from schemas.common import DeleteResult, ErrorResponse, VersionComparison, VersionInfo
from schemas.requirements import PriorityOut, QualityReport, RequirementIn, RequirementOut
from services import requirement_service as svc
from services import version_service
from services.quality_service import QualityResult, persist_quality

router = APIRouter()


@router.post(
    "",
    response_model=RequirementOut,
    operation_id="create_requirement",
    responses={
        **AUTH_RESPONSES,
        422: {"description": "Validation error", "model": ErrorResponse},
        **SCOPE_REQUIRED_422,
    },
)
async def create_requirement(
    payload: RequirementIn,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RequirementOut:
    """Create requirement with unique slug derived from title.

    When ``project_id`` is set, this is a get-or-create keyed on
    ``(tenant_id, project_id, title)`` so an AA-intake BLOCKED→DOING re-run
    updates the existing requirement in place instead of minting a duplicate.
    Without this, ``EntityService._generate_unique_slug`` auto-increments the
    slug (title, title-1, title-2…) and every re-run inserts a fresh row.
    The gate is deliberate: a falsy ``project_id`` (non-project callers) keeps
    pure create semantics. Best-effort only — an LLM-drifted title on re-run
    won't match the key and will still create a new row.
    """
    require_conveyor_project_scope(
        request, kind="requirement", op="create", project_scope=body_project_scope(payload)
    )
    if payload.stable_key and payload.project_id:
        # Deterministic dedup key (cardId + kind + ordinal) forwarded by the AA
        # node — survives LLM title drift across re-runs. Backed by the disjoint
        # partial index uq_requirements_tenant_project_stable_key.
        requirement, _created = await svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            identity_fields=("project_id", "stable_key"),
        )
        return requirement
    if payload.project_id:
        requirement, _created = await svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            identity_fields=("project_id", "title"),
            # The title index uq_requirements_tenant_project_title is PARTIAL on
            # ``stable_key IS NULL``; without this filter the recovery re-SELECT
            # (identity + tenant/visibility only) could match — and update — an
            # OLDER same-(project,title) row that carries a NON-null stable_key,
            # a row the title index never covered. Mirror the index predicate.
            extra_filters={"stable_key": None},
        )
        return requirement
    return await svc.create(session, payload, tenant_id, subject_id, visibility=visibility)


@router.get(
    "/{rid}",
    response_model=RequirementOut,
    operation_id="get_requirement",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Requirement not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_requirement(
    rid: UUID = Path(..., description="Requirement ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RequirementOut:
    """Retrieve requirement by ID."""
    return await svc.get(session, rid, tenant_id, subject_id, include_system=True)


@router.get(
    "",
    response_model=list[RequirementOut],
    operation_id="list_requirements",
    responses=AUTH_RESPONSES,
)
async def list_requirements(
    response: Response,
    project_id: str | None = Query(
        None, max_length=100, description="Only requirements scoped to this project"
    ),
    origin_kind: str | None = Query(
        None,
        pattern="^(tenant|project|workflow)$",
        description="Provenance filter: tenant | project | workflow",
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    scope_project_id: UUID | None = scope_project_query(for_list=True),
    include_shadowed: bool = include_shadowed_query(),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[RequirementOut]:
    """List requirements filtered by tenant_id.

    ``project_id`` and ``origin_kind`` are DECLARED here because they are
    applied here. Both were previously sent by fr's AG-UI list actions and
    dropped on the floor: FastAPI discards an undeclared query param without a
    word, so the caller asked for one project and got a 200 carrying the whole
    tenant — believing it was scoped. Both columns exist on ``Requirement``
    (``project_id`` ``src/models/requirements.py:34``, ``origin_kind`` ``:61``),
    so the filter is a real narrowing, not a rename of something else.

    ``origin_kind``'s domain is closed by the DB CHECK
    ``ck_requirements_origin_kind`` (``tenant``/``project``/``workflow``), so the
    pattern mirrors ``OriginFieldsInMixin``: a value outside that set can never
    match a row, and 422 is the honest answer to a knob wired to nothing.

    ⛔ ``card_id``, ``bound_to`` and ``visible_in`` are deliberately NOT declared
    here, and this is not an oversight to "complete" later. ``bound_to`` and
    ``visible_in`` are **fr-local**: they are evaluated against fr's own
    ``asset_bindings`` table, which this service does not have and must not
    guess at. ``card_id`` exists on no model in this estate at all. Declaring
    any of the three here would only recreate the defect above in the other
    direction — a param that parses, narrows nothing, and reads as honoured.

    Filters narrow WITHIN the tenant boundary: ``EntityService.list_all`` starts
    from ``scope(...)``, which applies tenant + subject visibility, and every
    filter is an additional ``.where``. Nothing here can widen it.
    """
    project_view = (
        svc.project_view_where(
            scope_project_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            include_shadowed=include_shadowed,
        )
        if scope_project_id is not None
        else ()
    )
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        extra_where=project_view,
        limit=limit,
        offset=offset,
        project_id=project_id,
        origin_kind=origin_kind,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{rid}",
    response_model=RequirementOut,
    operation_id="update_requirement",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Requirement not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        422: {"description": "Validation error", "model": ErrorResponse},
        **PATCH_409,
        **SCOPE_REQUIRED_422,
    },
)
async def update_requirement(
    request: Request,
    rid: UUID = Path(..., description="Requirement ID"),
    payload: RequirementIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    scope_project_id: UUID | None = scope_project_query(for_list=False),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RequirementOut:
    """Update requirement."""
    require_conveyor_project_scope(
        request, kind="requirement", op="update", project_scope=scope_project_id
    )
    if scope_project_id is not None:
        await svc.require_owned(session, rid, tenant_id, subject_id, scope_project_id)
    return await svc.update(session, rid, payload, tenant_id, subject_id)


# The PATCH body model is built at import time from ``RequirementIn`` (all fields optional,
# extra forbidden). Type checkers cannot use a runtime-built class in an
# annotation, so they are shown the full schema instead; runtime uses the partial.
if TYPE_CHECKING:
    _RequirementPatch = RequirementIn
    _RequirementProjectCopy = ProjectCopyStatic
else:
    _RequirementPatch = governed_patch_model(RequirementIn, svc.patch_server_managed)
    _RequirementProjectCopy = project_copy_model(_RequirementPatch, "Requirement")


@router.patch(
    "/{rid}",
    response_model=RequirementOut,
    operation_id="patch_requirement",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Requirement not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        **PATCH_409,
        422: {"description": "Validation error", "model": ErrorResponse},
        **SCOPE_REQUIRED_422,
    },
)
async def patch_requirement(
    request: Request,
    rid: UUID = Path(..., description="Requirement ID"),
    payload: _RequirementPatch = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    scope_project_id: UUID | None = scope_project_query(for_list=False),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RequirementOut:
    """Partially update a requirement: fields omitted from the body keep their stored value.

    PUT (``update_requirement``) is unchanged and still replaces the whole record, so
    existing callers are unaffected. Send only the fields to change; an explicit
    ``null`` clears an optional field.
    """
    require_conveyor_project_scope(
        request, kind="requirement", op="patch", project_scope=scope_project_id
    )
    return await svc.patch(
        session,
        rid,
        payload.model_dump(mode="json", exclude_unset=True),
        RequirementIn,
        tenant_id,
        subject_id,
        scope_project_id=scope_project_id,
    )


@router.post(
    "/{rid}/project-copy",
    response_model=RequirementOut,
    operation_id="project_copy_requirement",
    responses={**AUTH_RESPONSES, **project_copy_responses("Requirement")},
)
async def project_copy_requirement(
    response: Response,
    rid: UUID = Path(..., description="Requirement ID (the original)"),
    payload: _RequirementProjectCopy = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RequirementOut:
    """Edit an inherited requirement FOR a project: copy-on-write (G8 / R313).

    Creates the project's copy (``origin_kind='project'``, ``origin_project_id``,
    ``supersedes`` = the original's slug, ``version_status='DRAFT'``) with ``changes``
    applied, or — when the project already has a copy of this original — patches that copy.
    A copy promoted earlier is revived from the original's current content. The original is
    never written. ``X-Project-Copy`` says which: ``created`` / ``existing`` / ``revived``.
    """
    changes = payload.changes.model_dump(mode="json", exclude_unset=True) if payload.changes else {}
    out, state = await svc.project_copy(
        session, rid, payload.project_id, changes, tenant_id, subject_id
    )
    response.headers["X-Project-Copy"] = state
    return out


@router.post(
    "/{rid}/promote",
    response_model=RequirementOut,
    operation_id="promote_requirement",
    dependencies=[Depends(require_fr_delegated)],
    responses={**AUTH_RESPONSES, **promote_responses("Requirement")},
)
async def promote_requirement(
    rid: UUID = Path(..., description="Requirement ID (the project copy)"),
    payload: PromoteIn | None = Body(None),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RequirementOut:
    """Promote a project copy into its tenant original (fr, after a tenant_admin approval).

    The original takes the copy's content as a new minor ``version`` (history snapshot
    ``promoted``); the copy becomes ``SUPERSEDED`` with ``superseded_by`` = the original's
    slug (snapshot ``superseded``). One transaction. Returns the promoted original.
    """
    return await svc.promote(
        session,
        rid,
        tenant_id,
        subject_id,
        approved_by=payload.approved_by if payload else None,
    )


@router.delete(
    "/{rid}",
    operation_id="delete_requirement",
    response_model=DeleteResult,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Requirement not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        **PATCH_409,
        **SCOPE_REQUIRED_422,
    },
)
async def delete_requirement(
    request: Request,
    rid: UUID = Path(..., description="Requirement ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    scope_project_id: UUID | None = scope_project_query(for_list=False),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete requirement."""
    require_conveyor_project_scope(
        request, kind="requirement", op="delete", project_scope=scope_project_id
    )
    if scope_project_id is not None:
        await svc.require_owned(session, rid, tenant_id, subject_id, scope_project_id)
    return DeleteResult.model_validate(await svc.delete(session, rid, tenant_id, subject_id))


@router.post(
    "/{rid}/quality",
    response_model=QualityReport,
    operation_id="get_requirement_quality",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Requirement not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def requirement_quality(
    rid: UUID = Path(..., description="Requirement ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityReport:
    """Run quality checks and persist score onto the requirement."""
    obj = await svc._get_owned(session, rid, tenant_id, subject_id)
    score, issues, passes = quality_check(obj.title, obj.description, obj.acceptance)
    await persist_quality(
        session,
        obj,
        QualityResult(score=score, issues=issues, suggestions=[], agent_name=None),
    )
    await session.commit()
    return QualityReport(score=score, issues=issues, passes=passes)


@router.post(
    "/{rid}/priority",
    response_model=PriorityOut,
    operation_id="calculate_requirement_priority",
    tags=["Priority"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Requirement not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        422: {"description": "Invalid parameter format", "model": ErrorResponse},
    },
)
async def requirement_priority(
    rid: UUID = Path(..., description="Requirement ID"),
    business_value: str | None = None,
    time_criticality: str | None = None,
    risk_reduction: str | None = None,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PriorityOut:
    """Calculate WSJF and RICE scores."""
    obj = await svc._get_owned(session, rid, tenant_id, subject_id)

    bv_float = tc_float = rr_float = None
    try:
        if business_value and business_value.lower() != "null":
            bv_float = float(business_value)
        if time_criticality and time_criticality.lower() != "null":
            tc_float = float(time_criticality)
        if risk_reduction and risk_reduction.lower() != "null":
            rr_float = float(risk_reduction)
    except (ValueError, TypeError, AttributeError) as _exc:
        del _exc
        raise HTTPException(status_code=422, detail="Invalid parameter format")

    cod = None
    if bv_float is not None and tc_float is not None and rr_float is not None:
        cod = cod_from_components(bv_float, tc_float, rr_float)

    wsjf_score = rice_score = None
    if obj.effort and obj.effort >= 0.001:
        wsjf_score = wsjf(cod if cod is not None else 0.0, obj.effort)
        rice_score = rice(obj.reach, obj.impact, obj.confidence, obj.effort)

    return PriorityOut(wsjf=wsjf_score, rice=rice_score)


@router.get(
    "/{rid}/version",
    response_model=VersionInfo,
    operation_id="get_requirement_version",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Requirement not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_requirement_version(
    rid: UUID = Path(..., description="Requirement ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionInfo:
    """Get requirement version info."""
    obj = await svc._get_owned(session, rid, tenant_id, subject_id)
    return await version_service.get_info(obj)


@router.get(
    "/{rid}/versions",
    response_model=list[VersionInfo],
    operation_id="list_requirement_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Requirement not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def list_requirement_versions(
    rid: UUID = Path(..., description="Requirement ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[VersionInfo]:
    """List requirement version history."""
    obj = await svc._get_owned(session, rid, tenant_id, subject_id)
    return await version_service.list_versions(session, "requirement", rid, obj)


@router.get(
    "/{rid}/versions/{v1}/compare/{v2}",
    response_model=VersionComparison,
    operation_id="compare_requirement_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Requirement or version not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        422: {"description": "Invalid version format", "model": ErrorResponse},
    },
)
async def compare_requirement_versions(
    rid: UUID = Path(..., description="Requirement ID"),
    v1: str = Path(..., description="First version"),
    v2: str = Path(..., description="Second version"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionComparison:
    """Compare requirement versions using stored snapshots."""
    await svc._get_owned(session, rid, tenant_id, subject_id)
    return await version_service.compare(session, "requirement", rid, v1, v2)
