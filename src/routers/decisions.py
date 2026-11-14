"""Decisions CRUD and version endpoints."""

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from auth.governed_scope import (
    SCOPE_REQUIRED_422,
    body_project_scope,
    require_conveyor_project_scope,
)
from db import get_session
from quality_arch import decision_quality_check
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
from routers._quality_history import (
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    quality_history_response,
)
from routers.responses import AUTH_RESPONSES
from schemas.common import DeleteResult, ErrorResponse, VersionComparison, VersionInfo
from schemas.decisions import DecisionIn, DecisionOut
from schemas.requirements import QualityHistory, QualityReport
from services import decision_service as svc
from services import version_service
from services.quality_service import QualityResult, persist_quality

router = APIRouter()


@router.post(
    "",
    response_model=DecisionOut,
    operation_id="create_decision",
    responses={**AUTH_RESPONSES, **SCOPE_REQUIRED_422},
)
async def create_decision(
    payload: DecisionIn,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DecisionOut:
    """Create a new architecture decision record.

    When ``project_id`` is set, this is a get-or-create keyed on
    ``(tenant_id, project_id, title)`` so an AA-intake BLOCKED→DOING re-run
    updates the existing decision in place instead of minting a duplicate.
    Without this, ``EntityService._generate_unique_slug`` auto-increments the
    slug (title, title-1, title-2…) and every re-run inserts a fresh row.
    The gate is deliberate: a falsy ``project_id`` (non-project callers) keeps
    pure create semantics. Best-effort only — an LLM-drifted title on re-run
    won't match the key and will still create a new row.
    """
    require_conveyor_project_scope(
        request, kind="decision", op="create", project_scope=body_project_scope(payload)
    )
    if payload.stable_key and payload.project_id:
        # Deterministic AA dedup key (cardId + kind + ordinal); backed by the
        # disjoint partial index uq_decisions_tenant_project_stable_key.
        decision, _created = await svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            identity_fields=("project_id", "stable_key"),
        )
        return decision
    if payload.project_id:
        decision, _created = await svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            identity_fields=("project_id", "title"),
            # The title index uq_decisions_tenant_project_title is PARTIAL on
            # ``stable_key IS NULL``; without this filter the recovery re-SELECT
            # (identity + tenant/visibility only) could match — and update — an
            # OLDER same-(project,title) row that carries a NON-null stable_key,
            # a row the title index never covered. Mirror the index predicate.
            extra_filters={"stable_key": None},
        )
        return decision
    return await svc.create(session, payload, tenant_id, subject_id, visibility=visibility)


@router.get(
    "/{did}",
    response_model=DecisionOut,
    operation_id="get_decision",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Decision not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_decision(
    did: UUID = Path(..., description="Decision ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DecisionOut:
    """Retrieve decision by ID."""
    return await svc.get(session, did, tenant_id, subject_id, include_system=True)


@router.get(
    "", response_model=list[DecisionOut], operation_id="list_decisions", responses=AUTH_RESPONSES
)
async def list_decisions(
    response: Response,
    project_id: str | None = None,
    status: str | None = None,
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
) -> list[DecisionOut]:
    """List decisions filtered by tenant_id.

    ``origin_kind`` is new here and is applied, not merely accepted — fr's AG-UI
    list action already sent it and FastAPI dropped it silently, returning the
    whole tenant with a 200. The column exists (``src/models/decisions.py:57``)
    and its domain is closed by the DB CHECK ``ck_decisions_origin_kind``, so the
    pattern mirrors ``OriginFieldsInMixin`` and an out-of-domain value is a 422
    rather than a silent no-op. ``project_id`` was already declared and applied;
    it is unchanged.

    ⛔ ``card_id``, ``bound_to`` and ``visible_in`` stay undeclared on purpose —
    see ``routers/requirements.py::list_requirements`` for the reasoning. In
    short: the first two are filtered against fr's own ``asset_bindings`` table,
    which lives in fr and not here, and ``card_id`` exists on no model in this
    estate.

    Filters narrow WITHIN the tenant boundary — ``EntityService.list_all`` starts
    from ``scope(...)`` and every filter is an additional ``.where``.
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
        status=status,
        origin_kind=origin_kind,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{did}",
    response_model=DecisionOut,
    operation_id="update_decision",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Decision not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        **PATCH_409,
        **SCOPE_REQUIRED_422,
    },
)
async def update_decision(
    request: Request,
    did: UUID = Path(..., description="Decision ID"),
    payload: DecisionIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    scope_project_id: UUID | None = scope_project_query(for_list=False),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DecisionOut:
    """Update decision."""
    require_conveyor_project_scope(
        request, kind="decision", op="update", project_scope=scope_project_id
    )
    if scope_project_id is not None:
        await svc.require_owned(session, did, tenant_id, subject_id, scope_project_id)
    return await svc.update(session, did, payload, tenant_id, subject_id)


# The PATCH body model is built at import time from ``DecisionIn`` (all fields optional,
# extra forbidden). Type checkers cannot use a runtime-built class in an
# annotation, so they are shown the full schema instead; runtime uses the partial.
if TYPE_CHECKING:
    _DecisionPatch = DecisionIn
    _DecisionProjectCopy = ProjectCopyStatic
else:
    _DecisionPatch = governed_patch_model(DecisionIn, svc.patch_server_managed)
    _DecisionProjectCopy = project_copy_model(_DecisionPatch, "Decision")


@router.patch(
    "/{did}",
    response_model=DecisionOut,
    operation_id="patch_decision",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Decision not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        **PATCH_409,
        422: {"description": "Validation error", "model": ErrorResponse},
        **SCOPE_REQUIRED_422,
    },
)
async def patch_decision(
    request: Request,
    did: UUID = Path(..., description="Decision ID"),
    payload: _DecisionPatch = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    scope_project_id: UUID | None = scope_project_query(for_list=False),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DecisionOut:
    """Partially update a decision: fields omitted from the body keep their stored value.

    PUT (``update_decision``) is unchanged and still replaces the whole record, so
    existing callers are unaffected. Send only the fields to change; an explicit
    ``null`` clears an optional field.
    """
    require_conveyor_project_scope(
        request, kind="decision", op="patch", project_scope=scope_project_id
    )
    return await svc.patch(
        session,
        did,
        payload.model_dump(mode="json", exclude_unset=True),
        DecisionIn,
        tenant_id,
        subject_id,
        scope_project_id=scope_project_id,
    )


@router.post(
    "/{did}/project-copy",
    response_model=DecisionOut,
    operation_id="project_copy_decision",
    responses={**AUTH_RESPONSES, **project_copy_responses("Decision")},
)
async def project_copy_decision(
    response: Response,
    did: UUID = Path(..., description="Decision ID (the original)"),
    payload: _DecisionProjectCopy = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DecisionOut:
    """Edit an inherited decision FOR a project: copy-on-write (G8 / R313).

    Creates the project's copy (``origin_kind='project'``, ``origin_project_id``,
    ``supersedes`` = the original's slug, ``version_status='DRAFT'``) with ``changes``
    applied, or — when the project already has a copy of this original — patches that copy.
    A copy promoted earlier is revived from the original's current content. The original is
    never written. ``X-Project-Copy`` says which: ``created`` / ``existing`` / ``revived``.
    """
    changes = payload.changes.model_dump(mode="json", exclude_unset=True) if payload.changes else {}
    out, state = await svc.project_copy(
        session, did, payload.project_id, changes, tenant_id, subject_id
    )
    response.headers["X-Project-Copy"] = state
    return out


@router.post(
    "/{did}/promote",
    response_model=DecisionOut,
    operation_id="promote_decision",
    dependencies=[Depends(require_fr_delegated)],
    responses={**AUTH_RESPONSES, **promote_responses("Decision")},
)
async def promote_decision(
    did: UUID = Path(..., description="Decision ID (the project copy)"),
    payload: PromoteIn | None = Body(None),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DecisionOut:
    """Promote a project copy into its tenant original (fr, after a tenant_admin approval).

    The original takes the copy's content as a new minor ``version`` (history snapshot
    ``promoted``); the copy becomes ``SUPERSEDED`` with ``superseded_by`` = the original's
    slug (snapshot ``superseded``). One transaction. Returns the promoted original.
    """
    return await svc.promote(
        session,
        did,
        tenant_id,
        subject_id,
        approved_by=payload.approved_by if payload else None,
    )


@router.delete(
    "/{did}",
    operation_id="delete_decision",
    response_model=DeleteResult,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Decision not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        **PATCH_409,
        **SCOPE_REQUIRED_422,
    },
)
async def delete_decision(
    request: Request,
    did: UUID = Path(..., description="Decision ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    scope_project_id: UUID | None = scope_project_query(for_list=False),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete decision."""
    require_conveyor_project_scope(
        request, kind="decision", op="delete", project_scope=scope_project_id
    )
    if scope_project_id is not None:
        await svc.require_owned(session, did, tenant_id, subject_id, scope_project_id)
    return DeleteResult.model_validate(await svc.delete(session, did, tenant_id, subject_id))


@router.post(
    "/{did}/quality",
    response_model=QualityReport,
    operation_id="get_decision_quality",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Decision not found", "model": ErrorResponse},
    },
)
async def decision_quality(
    did: UUID = Path(..., description="Decision ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityReport:
    """Run heuristic quality checks and persist score onto the decision."""
    obj = await svc._get_owned(session, did, tenant_id, subject_id)
    score, issues, passes = decision_quality_check(obj)
    await persist_quality(
        session,
        obj,
        QualityResult(score=score, issues=issues, suggestions=[], agent_name=None),
    )
    await session.commit()
    return QualityReport(score=score, issues=issues, passes=passes)


@router.get(
    "/{did}/quality/history",
    response_model=QualityHistory,
    operation_id="get_decision_quality_history",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Decision not found", "model": ErrorResponse},
    },
)
async def decision_quality_history(
    did: UUID = Path(..., description="Decision ID"),
    limit: int = Query(HISTORY_LIMIT_DEFAULT, ge=1, le=HISTORY_LIMIT_MAX),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityHistory:
    """Every persisted quality evaluation of this decision, newest first.

    Append-only history behind the entity's ``quality_score`` (which stays the
    latest). Advisory data only — nothing gates on it.
    """
    obj = await svc._get_owned(session, did, tenant_id, subject_id)
    return await quality_history_response(session, obj, tenant_id, limit)


# -- Version endpoints --


@router.get(
    "/{did}/version",
    response_model=VersionInfo,
    operation_id="get_decision_version",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Decision not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_decision_version(
    did: UUID = Path(..., description="Decision ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionInfo:
    """Get decision version info."""
    obj = await svc._get_owned(session, did, tenant_id, subject_id)
    return await version_service.get_info(obj)


@router.get(
    "/{did}/versions",
    response_model=list[VersionInfo],
    operation_id="list_decision_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Decision not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def list_decision_versions(
    did: UUID = Path(..., description="Decision ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[VersionInfo]:
    """List decision version history."""
    obj = await svc._get_owned(session, did, tenant_id, subject_id)
    return await version_service.list_versions(session, "decision", did, obj)


@router.get(
    "/{did}/versions/{v1}/compare/{v2}",
    response_model=VersionComparison,
    operation_id="compare_decision_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Decision or version not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        422: {"description": "Invalid version format", "model": ErrorResponse},
    },
)
async def compare_decision_versions(
    did: UUID = Path(..., description="Decision ID"),
    v1: str = Path(..., description="First version"),
    v2: str = Path(..., description="Second version"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionComparison:
    """Compare decision versions using stored snapshots."""
    await svc._get_owned(session, did, tenant_id, subject_id)
    return await version_service.compare(session, "decision", did, v1, v2)
