"""Principles CRUD and version endpoints."""

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
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
from quality_arch import principle_quality_check
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
from schemas.principles import PrincipleIn, PrincipleOut
from schemas.requirements import QualityHistory, QualityReport
from services import principle_service as svc
from services import version_service
from services.quality_service import QualityResult, persist_quality

router = APIRouter()


@router.post(
    "",
    response_model=PrincipleOut,
    operation_id="create_principle",
    responses={**AUTH_RESPONSES, **SCOPE_REQUIRED_422},
)
async def create_principle(
    payload: PrincipleIn,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PrincipleOut:
    """Create a new architecture principle.

    **Project-scoped creates are get-or-create**, keyed on
    ``(tenant_id, origin_project_id, title)``, so an AA-intake BLOCKED→DOING
    re-run updates the principle in place instead of minting a duplicate.
    Without this, ``EntityService._generate_unique_slug`` auto-increments the
    slug (title, title-1, title-2…) so the ``(tenant_id, slug)`` unique
    constraint never fires and every re-run inserts a fresh row.

    Mirrors the requirement/decision/architecture-element siblings (PRs
    #63/#64), which gate their dedup on a truthy ``project_id``. ``Principle``
    has no ``project_id`` column — it is tenant-wide by design ("architecture
    guidance statements that apply across projects") — so the equivalent gate
    here is ``origin_project_id``, which the AA-intake ``Create Principle`` node
    always sets. **An un-scoped create (no ``origin_project_id``, i.e. the
    general API) keeps its existing behaviour and still mints a distinct
    slug** — the dedup must not change semantics for hand-authored principles
    that legitimately share a title.

    Best-effort, as with the siblings: an LLM-drifted title on re-run won't
    match the key and will still create a new row.
    """
    require_conveyor_project_scope(
        request, kind="principle", op="create", project_scope=body_project_scope(payload)
    )
    if payload.origin_project_id:
        principle, _created = await svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            identity_fields=("origin_project_id", "title"),
        )
        return principle
    return await svc.create(session, payload, tenant_id, subject_id, visibility=visibility)


@router.get(
    "/{pid}",
    response_model=PrincipleOut,
    operation_id="get_principle",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Principle not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_principle(
    pid: UUID = Path(..., description="Principle ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PrincipleOut:
    """Retrieve principle by ID."""
    return await svc.get(session, pid, tenant_id, subject_id, include_system=True)


@router.get(
    "", response_model=list[PrincipleOut], operation_id="list_principles", responses=AUTH_RESPONSES
)
async def list_principles(
    response: Response,
    active_only: bool = False,
    origin_kind: str | None = Query(
        None,
        pattern="^(tenant|project|workflow)$",
        description="Provenance filter: tenant | project | workflow",
    ),
    project_id: UUID | None = Query(
        None,
        description=(
            "Provenance filter on `origin_project_id`: only the principles MINTED by this "
            "project (its intake created them). Not a scoping filter: a principle authored "
            "under another project, or tenant-wide, may still apply to this project and is "
            "not returned."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    scope_project_id: UUID | None = scope_project_query(for_list=True),
    include_shadowed: bool = include_shadowed_query(),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[PrincipleOut]:
    """List principles filtered by tenant_id.

    ``origin_kind`` is declared here because it is applied here. fr's AG-UI list
    action already sent it and FastAPI dropped it without a word, so the caller
    got the whole tenant back with a 200 and no way to tell. The column exists
    (``src/models/principles.py:51``) and its domain is closed by the DB CHECK
    ``ck_principles_origin_kind``, so the pattern mirrors ``OriginFieldsInMixin``
    and an out-of-domain value is a 422 rather than a silent no-op.

    ``project_id`` filters on ``origin_project_id`` — PROVENANCE ("which
    project's intake minted this principle"), NOT scoping ("the principles that
    apply to this project"). ``Principle`` has no ``project_id`` column; it is
    tenant-wide by design ("architecture guidance statements that apply across
    projects"). A principle authored under project A that governs the whole
    tenant is therefore NOT in project B's filtered list. The parameter's
    published description says exactly that, so a caller is never answered with
    a predicate it did not ask for (fr asked for the provenance read, 2026-10-12).
    Project-scoped listing still needs the fr-side bindings (below) or a real
    scoping column.

    ⛔ ``card_id``, ``bound_to`` and ``visible_in`` stay undeclared on purpose —
    see ``routers/requirements.py::list_requirements``. ``bound_to``/
    ``visible_in`` are resolved against fr's own ``asset_bindings`` table, which
    is not in this service; ``card_id`` exists on no model in this estate.

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
        active_only=active_only,
        origin_kind=origin_kind,
        origin_project_id=project_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{pid}",
    response_model=PrincipleOut,
    operation_id="update_principle",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Principle not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        **PATCH_409,
        **SCOPE_REQUIRED_422,
    },
)
async def update_principle(
    request: Request,
    pid: UUID = Path(..., description="Principle ID"),
    payload: PrincipleIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    scope_project_id: UUID | None = scope_project_query(for_list=False),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PrincipleOut:
    """Update principle."""
    require_conveyor_project_scope(
        request, kind="principle", op="update", project_scope=scope_project_id
    )
    if scope_project_id is not None:
        await svc.require_owned(session, pid, tenant_id, subject_id, scope_project_id)
    return await svc.update(session, pid, payload, tenant_id, subject_id)


# The PATCH body model is built at import time from ``PrincipleIn`` (all fields optional,
# extra forbidden). Type checkers cannot use a runtime-built class in an
# annotation, so they are shown the full schema instead; runtime uses the partial.
if TYPE_CHECKING:
    _PrinciplePatch = PrincipleIn
    _PrincipleProjectCopy = ProjectCopyStatic
else:
    _PrinciplePatch = governed_patch_model(PrincipleIn, svc.patch_server_managed)
    _PrincipleProjectCopy = project_copy_model(_PrinciplePatch, "Principle")


@router.patch(
    "/{pid}",
    response_model=PrincipleOut,
    operation_id="patch_principle",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Principle not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        **PATCH_409,
        422: {"description": "Validation error", "model": ErrorResponse},
        **SCOPE_REQUIRED_422,
    },
)
async def patch_principle(
    request: Request,
    pid: UUID = Path(..., description="Principle ID"),
    payload: _PrinciplePatch = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    scope_project_id: UUID | None = scope_project_query(for_list=False),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PrincipleOut:
    """Partially update a principle: fields omitted from the body keep their stored value.

    PUT (``update_principle``) is unchanged and still replaces the whole record, so
    existing callers are unaffected. Send only the fields to change; an explicit
    ``null`` clears an optional field.
    """
    require_conveyor_project_scope(
        request, kind="principle", op="patch", project_scope=scope_project_id
    )
    return await svc.patch(
        session,
        pid,
        payload.model_dump(mode="json", exclude_unset=True),
        PrincipleIn,
        tenant_id,
        subject_id,
        scope_project_id=scope_project_id,
    )


@router.post(
    "/{pid}/project-copy",
    response_model=PrincipleOut,
    operation_id="project_copy_principle",
    responses={**AUTH_RESPONSES, **project_copy_responses("Principle")},
)
async def project_copy_principle(
    response: Response,
    pid: UUID = Path(..., description="Principle ID (the original)"),
    payload: _PrincipleProjectCopy = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PrincipleOut:
    """Edit an inherited principle FOR a project: copy-on-write (G8 / R313).

    Creates the project's copy (``origin_kind='project'``, ``origin_project_id``,
    ``supersedes`` = the original's slug, ``version_status='DRAFT'``) with ``changes``
    applied, or — when the project already has a copy of this original — patches that copy.
    A copy promoted earlier is revived from the original's current content. The original is
    never written. ``X-Project-Copy`` says which: ``created`` / ``existing`` / ``revived``.
    """
    changes = payload.changes.model_dump(mode="json", exclude_unset=True) if payload.changes else {}
    out, state = await svc.project_copy(
        session, pid, payload.project_id, changes, tenant_id, subject_id
    )
    response.headers["X-Project-Copy"] = state
    return out


@router.post(
    "/{pid}/promote",
    response_model=PrincipleOut,
    operation_id="promote_principle",
    dependencies=[Depends(require_fr_delegated)],
    responses={**AUTH_RESPONSES, **promote_responses("Principle")},
)
async def promote_principle(
    pid: UUID = Path(..., description="Principle ID (the project copy)"),
    payload: PromoteIn | None = Body(None),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PrincipleOut:
    """Promote a project copy into its tenant original (fr, after a tenant_admin approval).

    The original takes the copy's content as a new minor ``version`` (history snapshot
    ``promoted``); the copy becomes ``SUPERSEDED`` with ``superseded_by`` = the original's
    slug (snapshot ``superseded``). One transaction. Returns the promoted original.
    """
    return await svc.promote(
        session,
        pid,
        tenant_id,
        subject_id,
        approved_by=payload.approved_by if payload else None,
    )


@router.delete(
    "/{pid}",
    operation_id="delete_principle",
    response_model=DeleteResult,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Principle not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        **PATCH_409,
        **SCOPE_REQUIRED_422,
    },
)
async def delete_principle(
    request: Request,
    pid: UUID = Path(..., description="Principle ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    scope_project_id: UUID | None = scope_project_query(for_list=False),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete principle."""
    require_conveyor_project_scope(
        request, kind="principle", op="delete", project_scope=scope_project_id
    )
    if scope_project_id is not None:
        await svc.require_owned(session, pid, tenant_id, subject_id, scope_project_id)
    return DeleteResult.model_validate(await svc.delete(session, pid, tenant_id, subject_id))


@router.post(
    "/{pid}/quality",
    response_model=QualityReport,
    operation_id="get_principle_quality",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Principle not found", "model": ErrorResponse},
    },
)
async def principle_quality(
    pid: UUID = Path(..., description="Principle ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityReport:
    """Run heuristic quality checks and persist score onto the principle."""
    obj = await svc._get_owned(session, pid, tenant_id, subject_id)
    score, issues, passes = principle_quality_check(obj)
    await persist_quality(
        session,
        obj,
        QualityResult(score=score, issues=issues, suggestions=[], agent_name=None),
    )
    await session.commit()
    return QualityReport(score=score, issues=issues, passes=passes)


@router.get(
    "/{pid}/quality/history",
    response_model=QualityHistory,
    operation_id="get_principle_quality_history",
    tags=["Quality"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Principle not found", "model": ErrorResponse},
    },
)
async def principle_quality_history(
    pid: UUID = Path(..., description="Principle ID"),
    limit: int = Query(HISTORY_LIMIT_DEFAULT, ge=1, le=HISTORY_LIMIT_MAX),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> QualityHistory:
    """Every persisted quality evaluation of this principle, newest first.

    Append-only history behind the entity's ``quality_score`` (which stays the
    latest). Advisory data only — nothing gates on it.
    """
    obj = await svc._get_owned(session, pid, tenant_id, subject_id)
    return await quality_history_response(session, obj, tenant_id, limit)


# -- Version endpoints --


@router.get(
    "/{pid}/version",
    response_model=VersionInfo,
    operation_id="get_principle_version",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Principle not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_principle_version(
    pid: UUID = Path(..., description="Principle ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionInfo:
    """Get principle version info."""
    obj = await svc._get_owned(session, pid, tenant_id, subject_id)
    return await version_service.get_info(obj)


@router.get(
    "/{pid}/versions",
    response_model=list[VersionInfo],
    operation_id="list_principle_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Principle not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def list_principle_versions(
    pid: UUID = Path(..., description="Principle ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[VersionInfo]:
    """List principle version history."""
    obj = await svc._get_owned(session, pid, tenant_id, subject_id)
    return await version_service.list_versions(session, "principle", pid, obj)


@router.get(
    "/{pid}/versions/{v1}/compare/{v2}",
    response_model=VersionComparison,
    operation_id="compare_principle_versions",
    tags=["Versions"],
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Principle or version not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
        422: {"description": "Invalid version format", "model": ErrorResponse},
    },
)
async def compare_principle_versions(
    pid: UUID = Path(..., description="Principle ID"),
    v1: str = Path(..., description="First version"),
    v2: str = Path(..., description="Second version"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> VersionComparison:
    """Compare principle versions using stored snapshots."""
    await svc._get_owned(session, pid, tenant_id, subject_id)
    return await version_service.compare(session, "principle", pid, v1, v2)
