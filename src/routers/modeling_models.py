"""Modeling models and their sealed revisions (target spec §7.1–7.2)."""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.common import DeleteResult, ErrorResponse
from schemas.modeling_lifecycle import (
    ChangeSetIn,
    ChangeSetOut,
    ChangeSetReject,
    ModelDetail,
    ModelIn,
    ModelOut,
    ModelPatch,
    PublicationOut,
    ReviewIn,
    ReviewOut,
    RevisionDetail,
    RevisionIn,
    RevisionOut,
    TraceLinkIn,
    TraceLinkOut,
    ValidationFindingOut,
    ValidationRunIn,
    ValidationRunOut,
)
from services import change_sets as cs_svc
from services import model_reviews as review_svc
from services import modeling_models as svc
from services import modeling_validation as run_svc
from services import trace_links as link_svc
from services.idempotency import IDEMPOTENCY_409, Idempotency, idempotency_key_header

router = APIRouter(route_class=ErrorEnvelopeRoute)
validation_runs_router = APIRouter(route_class=ErrorEnvelopeRoute)
change_sets_router = APIRouter(route_class=ErrorEnvelopeRoute)
trace_links_router = APIRouter(route_class=ErrorEnvelopeRoute)

_404 = {"description": "Model not found", "model": ErrorResponse}


async def _detail(session: AsyncSession, model: Any) -> ModelDetail:
    out = ModelDetail.model_validate(model, from_attributes=True)
    out.latest_revision_id = await svc.latest_revision_id(session, model.id)
    out.latest_validation_run = await run_svc.latest_run_summary(session, model.id)
    return out


def _revision_out(row: Any) -> RevisionOut:
    return RevisionOut(
        revision_id=row.id,
        model_id=row.model_id,
        seq=row.seq,
        label=row.label,
        head=row.head,
        content_hash=row.content_hash,
        sealed_by=row.sealed_by,
        sealed_at=row.sealed_at,
    )


@router.get(
    "",
    response_model=list[ModelOut],
    operation_id="list_models",
    responses=AUTH_RESPONSES,
)
async def list_models(
    response: Response,
    language: Literal["archimate", "uml"] | None = Query(None, description="archimate | uml"),
    project_id: str | None = Query(None, description="Filter by project"),
    q: str | None = Query(None, max_length=200, description="Title contains (case-insensitive)"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ModelOut]:
    """Models visible to the caller, newest first."""
    rows, total = await svc.list_models(
        session,
        tenant_id,
        subject_id,
        language=language,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return [ModelOut.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=ModelOut,
    status_code=201,
    operation_id="create_model",
    responses={
        **AUTH_RESPONSES,
        409: IDEMPOTENCY_409,
        422: {"description": "Validation error", "model": ErrorResponse},
    },
)
async def create_model(
    response: Response,
    payload: ModelIn,
    idempotency_key: str | None = Depends(idempotency_key_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    header_visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ModelOut:
    """Create a model (201). Retry-safe with the optional ``Idempotency-Key`` header."""
    visibility = payload.visibility or header_visibility
    idem = Idempotency(
        session,
        route="/modeling/models",
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        key=idempotency_key,
        body=payload,
    )
    if (replay := await idem.begin()) is not None:
        response.status_code = replay.status_code
        return ModelOut.model_validate(replay.body)
    stage = idem.stager(201, ModelOut)
    async with idem.guard():
        model = await svc.create_model(
            session,
            payload,
            tenant_id=tenant_id,
            subject_id=subject_id,
            visibility=visibility,
            before_commit=stage,
        )
    response.headers["ETag"] = f'"{model.head}"'
    return ModelOut.model_validate(model)


@router.get(
    "/{model_id}",
    response_model=ModelDetail,
    operation_id="get_model",
    responses={**AUTH_RESPONSES, 404: _404},
)
async def get_model(
    response: Response,
    model_id: UUID = Path(..., description="Model ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ModelDetail:
    """The model with its draft ``head`` (also the ``ETag``) and latest revision id."""
    model = await svc.get_model(session, model_id, tenant_id, subject_id)
    response.headers["ETag"] = f'"{model.head}"'
    return await _detail(session, model)


@router.patch(
    "/{model_id}",
    response_model=ModelDetail,
    operation_id="patch_model",
    responses={
        **AUTH_RESPONSES,
        403: {"description": f"`{svc.MODEL_VISIBILITY_OWNER_ONLY}`: visibility is owner-only"},
        404: _404,
        409: {
            "description": (
                f"`{svc.VISIBILITY_CHANGE_FOREIGN_ROWS}`: another subject's rows or trace "
                "links keep the model shared"
            )
        },
        412: svc.STALE_HEAD_412,
        422: {"description": "`default_model_visibility_fixed`"},
    },
)
async def patch_model(
    response: Response,
    model_id: UUID = Path(..., description="Model ID"),
    payload: ModelPatch = Body(...),
    if_match: int | None = Depends(svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ModelDetail:
    """Update title, description and visibility; language and its version are immutable.

    Any change increments ``head`` (they are manifest header fields). Only the owner may
    change visibility; rows and trace links move with it.
    """
    model = await svc.patch_model(
        session,
        model_id,
        payload,
        tenant_id=tenant_id,
        subject_id=subject_id,
        if_match=if_match,
    )
    response.headers["ETag"] = f'"{model.head}"'
    return await _detail(session, model)


@router.delete(
    "/{model_id}",
    operation_id="delete_model",
    response_model=DeleteResult,
    responses={
        **AUTH_RESPONSES,
        404: _404,
        409: {
            "description": (
                "`model_has_revisions` without ?force=true, or `model_has_foreign_rows`: "
                "rows of another tenant carry the model's id and its owner must remove "
                "them first (they are never deleted across tenants)"
            )
        },
    },
)
async def delete_model(
    model_id: UUID = Path(..., description="Model ID"),
    force: bool = Query(False, description="Also delete a model that has sealed revisions"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete the model with all its concepts, relationships, views and revisions."""
    return DeleteResult.model_validate(
        await svc.delete_model(
            session, model_id, tenant_id=tenant_id, subject_id=subject_id, force=force
        )
    )


@router.get(
    "/{model_id}/revisions",
    response_model=list[RevisionOut],
    operation_id="list_model_revisions",
    responses={**AUTH_RESPONSES, 404: _404},
)
async def list_model_revisions(
    response: Response,
    model_id: UUID = Path(..., description="Model ID"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[RevisionOut]:
    """Sealed revisions, newest first."""
    await svc.get_model(session, model_id, tenant_id, subject_id)
    rows, total = await svc.list_revisions(session, model_id, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return [_revision_out(r) for r in rows]


@router.post(
    "/{model_id}/revisions",
    response_model=RevisionOut,
    status_code=201,
    operation_id="seal_model_revision",
    responses={
        **AUTH_RESPONSES,
        404: _404,
        412: svc.STALE_HEAD_412,
        422: {"description": "`input_too_large` above MODEL_ASYNC_MAX_CONCEPTS"},
        503: {
            "description": (
                "`seal_timeout`: a statement of the seal exceeded "
                "MODEL_SEAL_STATEMENT_TIMEOUT_MS (e.g. waiting for the model's row lock)"
            )
        },
    },
)
async def seal_model_revision(
    model_id: UUID = Path(..., description="Model ID"),
    payload: RevisionIn | None = Body(None),
    if_match: int | None = Depends(svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RevisionOut:
    """Seal the draft head into an immutable revision (canonical JSON + sha256).

    ``sealed_by`` is the authenticated principal. The expected head may be sent as
    ``If-Match`` or as ``head`` in the body; a mismatch is ``412 stale_head``.
    """
    body = payload or RevisionIn()
    expected = if_match if if_match is not None else body.head
    row = await svc.seal_revision(
        session,
        model_id,
        tenant_id=tenant_id,
        subject_id=subject_id,
        label=body.label,
        expected_head=expected,
    )
    return _revision_out(row)


@router.get(
    "/{model_id}/revisions/{rev_id}",
    response_model=RevisionDetail,
    operation_id="get_model_revision",
    responses={**AUTH_RESPONSES, 404: _404},
)
async def get_model_revision(
    model_id: UUID = Path(..., description="Model ID"),
    rev_id: UUID = Path(..., description="Revision ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> RevisionDetail:
    """Revision metadata plus its full manifest."""
    await svc.get_model(session, model_id, tenant_id, subject_id)
    row = await svc.get_revision(session, model_id, rev_id)
    return RevisionDetail(**_revision_out(row).model_dump(), manifest=row.manifest)


# -- Validation runs (spec §7.3) --


@router.post(
    "/{model_id}/validation-runs",
    response_model=ValidationRunOut,
    status_code=201,
    operation_id="run_model_validation",
    responses={
        **AUTH_RESPONSES,
        202: {
            "model": ValidationRunOut,
            "description": (
                "A sealed revision above MODEL_SYNC_MAX_CONCEPTS: the run is `queued` and "
                "validated in the background; poll `GET /modeling/validation-runs/{run_id}`"
            ),
        },
        404: _404,
        422: {
            "description": (
                "`input_too_large` (draft head above MODEL_SYNC_MAX_CONCEPTS, revision "
                "above MODEL_ASYNC_MAX_CONCEPTS), `unknown_ruleset`, `unknown_rule`"
            )
        },
        503: {
            "description": (
                "`validation_queue_full`: MODEL_ASYNC_MAX_QUEUED background runs are "
                "already queued or running; retry after `Retry-After` seconds"
            )
        },
    },
)
async def run_model_validation(
    response: Response,
    model_id: UUID = Path(..., description="Model ID"),
    payload: ValidationRunIn | None = Body(None),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ValidationRunOut:
    """Validate a revision or the draft head with the model language's ruleset (201).

    A sealed revision above MODEL_SYNC_MAX_CONCEPTS is validated in the background: 202
    with a `queued` run that ends `complete`, `incomplete` or `failed`.
    """
    run = await run_svc.run_validation(
        session, model_id, payload or ValidationRunIn(), tenant_id=tenant_id, subject_id=subject_id
    )
    if run.status in run_svc.ACTIVE:
        response.status_code = 202
    return run_svc.run_out(run)


@router.get(
    "/{model_id}/validation-runs",
    response_model=list[ValidationRunOut],
    operation_id="list_model_validation_runs",
    responses={**AUTH_RESPONSES, 404: _404},
)
async def list_model_validation_runs(
    response: Response,
    model_id: UUID = Path(..., description="Model ID"),
    target: Literal["revision", "draft_head"] | None = Query(None, description="Filter by target"),
    clean: bool | None = Query(None, description="Filter by clean verdict"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ValidationRunOut]:
    """The model's validation runs, newest first."""
    await svc.get_model(session, model_id, tenant_id, subject_id)
    rows, total = await run_svc.list_runs(
        session, model_id, target=target, clean=clean, limit=limit, offset=offset
    )
    response.headers["X-Total-Count"] = str(total)
    return [run_svc.run_out(r) for r in rows]


@validation_runs_router.get(
    "/{run_id}",
    response_model=ValidationRunOut,
    operation_id="get_validation_run",
    responses={**AUTH_RESPONSES, 404: {"description": "Run not found", "model": ErrorResponse}},
)
async def get_validation_run(
    run_id: UUID = Path(..., description="Validation run ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ValidationRunOut:
    """One validation run."""
    return run_svc.run_out(await run_svc.get_run(session, run_id, tenant_id, subject_id))


@validation_runs_router.get(
    "/{run_id}/findings",
    response_model=list[ValidationFindingOut],
    operation_id="list_validation_findings",
    responses={**AUTH_RESPONSES, 404: {"description": "Run not found", "model": ErrorResponse}},
)
async def list_validation_findings(
    response: Response,
    run_id: UUID = Path(..., description="Validation run ID"),
    severity: Literal["error", "warning", "advice"] | None = Query(None),
    rule_id: str | None = Query(None, description="Only this rule"),
    target_id: str | None = Query(None, description="Only findings naming this id"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ValidationFindingOut]:
    """Findings ordered by (severity desc, rule_id, first target id)."""
    run = await run_svc.get_run(session, run_id, tenant_id, subject_id)
    rows, total = await run_svc.list_findings(
        session,
        run.id,
        severity=severity,
        rule_id=rule_id,
        target_id=target_id,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return [
        ValidationFindingOut(
            rule_id=f.rule_id,
            ruleset_id=f.ruleset_id,
            severity=f.severity,  # ty: ignore[invalid-argument-type]
            targets=list(f.targets),
            path=f.path,
            message=f.message,
            evidence=f.evidence,
        )
        for f in rows
    ]


# -- Change sets (spec §9.5) --


@router.post(
    "/{model_id}/change-sets",
    response_model=ChangeSetOut,
    status_code=201,
    operation_id="create_change_set",
    responses={
        **AUTH_RESPONSES,
        404: _404,
        412: svc.STALE_HEAD_412,
        422: {
            "description": (
                "`VALIDATION_ERROR`: the body does not validate (shape, bounds). Ops that "
                "do not apply are not refused: the set is stored `invalid` with findings"
            ),
            "model": ErrorResponse,
        },
    },
)
async def create_change_set(
    model_id: UUID = Path(..., description="Model ID"),
    payload: ChangeSetIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ChangeSetOut:
    """Propose changes: validated in memory against the head, stored proposed|invalid (201)."""
    row = await cs_svc.create_change_set(
        session, model_id, payload, tenant_id=tenant_id, subject_id=subject_id
    )
    return ChangeSetOut.model_validate(row)


@router.get(
    "/{model_id}/change-sets",
    response_model=list[ChangeSetOut],
    operation_id="list_change_sets",
    responses={**AUTH_RESPONSES, 404: _404},
)
async def list_change_sets(
    response: Response,
    model_id: UUID = Path(..., description="Model ID"),
    status: Literal["proposed", "invalid", "applied", "rejected"] | None = Query(None),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ChangeSetOut]:
    """The model's change sets, newest first."""
    await svc.get_model(session, model_id, tenant_id, subject_id)
    rows, total = await cs_svc.list_change_sets(
        session, model_id, status=status, limit=limit, offset=offset
    )
    response.headers["X-Total-Count"] = str(total)
    return [ChangeSetOut.model_validate(r) for r in rows]


@change_sets_router.get(
    "/{change_set_id}",
    response_model=ChangeSetOut,
    operation_id="get_change_set",
    responses={**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}},
)
async def get_change_set(
    change_set_id: UUID = Path(..., description="Change set ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ChangeSetOut:
    """One change set with its findings."""
    row = await cs_svc.get_change_set(session, change_set_id, tenant_id, subject_id)
    return ChangeSetOut.model_validate(row)


@change_sets_router.post(
    "/{change_set_id}/apply",
    response_model=ChangeSetOut,
    operation_id="apply_change_set",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Not found", "model": ErrorResponse},
        409: {"description": "`change_set_not_proposed`"},
        412: svc.STALE_HEAD_412,
        422: {"description": "`model_integrity` from a write gate; nothing applied"},
    },
)
async def apply_change_set(
    response: Response,
    change_set_id: UUID = Path(..., description="Change set ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ChangeSetOut:
    """Apply a proposed set in ONE transaction; the head must still equal ``base_head``."""
    row = await cs_svc.apply_change_set(
        session, change_set_id, tenant_id=tenant_id, subject_id=subject_id
    )
    if row.applied_head is not None:
        response.headers["ETag"] = f'"{row.applied_head}"'
    return ChangeSetOut.model_validate(row)


@change_sets_router.post(
    "/{change_set_id}/reject",
    response_model=ChangeSetOut,
    operation_id="reject_change_set",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Not found", "model": ErrorResponse},
        409: {"description": "`change_set_not_open`"},
    },
)
async def reject_change_set(
    change_set_id: UUID = Path(..., description="Change set ID"),
    payload: ChangeSetReject = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ChangeSetOut:
    """Reject a proposed or invalid set; ``rejected_by`` is the caller."""
    row = await cs_svc.reject_change_set(
        session, change_set_id, payload.reason, tenant_id=tenant_id, subject_id=subject_id
    )
    return ChangeSetOut.model_validate(row)


# -- Review and publication (plan §7.3) --


@router.post(
    "/{model_id}/revisions/{rev_id}/reviews",
    response_model=ReviewOut,
    status_code=201,
    operation_id="review_model_revision",
    responses={
        **AUTH_RESPONSES,
        404: _404,
        422: {"description": "`review_reason_required`"},
    },
)
async def review_model_revision(
    model_id: UUID = Path(..., description="Model ID"),
    rev_id: UUID = Path(..., description="Revision ID"),
    payload: ReviewIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ReviewOut:
    """Approve or reject a sealed revision; the reviewer is the caller (201)."""
    row = await review_svc.review_revision(
        session, model_id, rev_id, payload, tenant_id=tenant_id, subject_id=subject_id
    )
    return ReviewOut.model_validate(row)


@router.get(
    "/{model_id}/revisions/{rev_id}/reviews",
    response_model=list[ReviewOut],
    operation_id="list_model_revision_reviews",
    responses={**AUTH_RESPONSES, 404: _404},
)
async def list_model_revision_reviews(
    model_id: UUID = Path(..., description="Model ID"),
    rev_id: UUID = Path(..., description="Revision ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ReviewOut]:
    """Reviews of a revision, newest first (the newest decides)."""
    await svc.get_model(session, model_id, tenant_id, subject_id)
    await svc.get_revision(session, model_id, rev_id)
    return [
        ReviewOut.model_validate(r)
        for r in await review_svc.list_reviews(session, model_id, rev_id)
    ]


@router.post(
    "/{model_id}/revisions/{rev_id}/publish",
    response_model=PublicationOut,
    status_code=201,
    operation_id="publish_model_revision",
    responses={
        **AUTH_RESPONSES,
        404: _404,
        409: {
            "description": (
                "`revision_not_approved`, `revision_not_validated`, `revision_already_published`"
            )
        },
    },
)
async def publish_model_revision(
    model_id: UUID = Path(..., description="Model ID"),
    rev_id: UUID = Path(..., description="Revision ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PublicationOut:
    """Publish an approved revision with a clean, complete run on its content (201)."""
    row = await review_svc.publish_revision(
        session, model_id, rev_id, tenant_id=tenant_id, subject_id=subject_id
    )
    return PublicationOut.model_validate(row)


@router.get(
    "/{model_id}/publications",
    response_model=list[PublicationOut],
    operation_id="list_model_publications",
    responses={**AUTH_RESPONSES, 404: _404},
)
async def list_model_publications(
    model_id: UUID = Path(..., description="Model ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[PublicationOut]:
    """Publication history, newest first; each names the revision it superseded."""
    await svc.get_model(session, model_id, tenant_id, subject_id)
    return [
        PublicationOut.model_validate(r)
        for r in await review_svc.list_publications(session, model_id)
    ]


# -- Trace links (spec §7.4) --

_LINK_404 = {"description": "Trace link not found", "model": ErrorResponse}


@trace_links_router.get(
    "",
    response_model=list[TraceLinkOut],
    operation_id="list_trace_links",
    responses=AUTH_RESPONSES,
)
async def list_trace_links(
    response: Response,
    source_id: UUID | None = Query(None, description="Filter by source id"),
    target_id: UUID | None = Query(None, description="Filter by target id"),
    link_type: Literal["realizes", "refines", "traces_to", "satisfies", "implements", "depends_on"]
    | None = Query(None, description="Filter by link type"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[TraceLinkOut]:
    """Trace links visible to the caller, newest first."""
    rows, total = await link_svc.list_links(
        session,
        tenant_id,
        subject_id,
        source_id=source_id,
        target_id=target_id,
        link_type=link_type,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return [link_svc.link_out(r) for r in rows]


@trace_links_router.post(
    "",
    response_model=TraceLinkOut,
    status_code=201,
    operation_id="create_trace_link",
    responses={**AUTH_RESPONSES, 422: {"description": "`model_integrity` (TRC-REF-001)"}},
)
async def create_trace_link(
    response: Response,
    payload: TraceLinkIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TraceLinkOut:
    """Create an immutable link; both ends must resolve and be visible (201)."""
    row = await link_svc.create_link(
        session, payload, tenant_id=tenant_id, subject_id=subject_id, visibility=visibility
    )
    svc.set_etag(response, session)
    return link_svc.link_out(row)


@trace_links_router.get(
    "/{link_id}",
    response_model=TraceLinkOut,
    operation_id="get_trace_link",
    responses={**AUTH_RESPONSES, 404: _LINK_404},
)
async def get_trace_link(
    link_id: UUID = Path(..., description="Trace link ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TraceLinkOut:
    """One trace link."""
    return link_svc.link_out(await link_svc.get_link(session, link_id, tenant_id, subject_id))


@trace_links_router.delete(
    "/{link_id}",
    operation_id="delete_trace_link",
    response_model=DeleteResult,
    responses={**AUTH_RESPONSES, 404: _LINK_404},
)
async def delete_trace_link(
    response: Response,
    link_id: UUID = Path(..., description="Trace link ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete a trace link (links are immutable: delete and re-create to change one)."""
    out = await link_svc.delete_link(session, link_id, tenant_id, subject_id)
    svc.set_etag(response, session)
    return DeleteResult.model_validate(out)
