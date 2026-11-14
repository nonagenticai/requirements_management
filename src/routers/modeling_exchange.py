"""Exchange-format export/import and view rendering routes (target spec §8.1).

``exchange_router`` mounts under ``/modeling/models``; ``view_render_router`` under
``/architecture/views``. Services: ``services/model_exchange.py``,
``services/model_render.py``; the render HTTP conventions are ``routers/_render_common.py``
(shared with the UML render routes).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from routers._render_common import (
    RENDER_RESPONSES,
    FormatParam,
    IfNoneMatch,
    RenderFormat,
    ReportParam,
    not_modified,
    render_response,
)
from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.common import ErrorResponse
from schemas.modeling_exchange import (
    ExchangeExportReport,
    ExchangeImportOut,
    OrganizationsIn,
    OrganizationsOut,
)
from services import model_exchange as xsvc
from services import model_organizations as osvc
from services import model_render as rsvc
from services import modeling_models as models_svc
from services.idempotency import IDEMPOTENCY_409, Idempotency, idempotency_key_header
from settings import get_settings
from tools.rendering import ARCHIMATE_RENDERER_VERSION

exchange_router = APIRouter(route_class=ErrorEnvelopeRoute)
view_render_router = APIRouter(route_class=ErrorEnvelopeRoute)

_404 = {"description": "Model, revision or view not found", "model": ErrorResponse}
XML_TYPES = ("application/xml", "text/xml")
UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"

_EXPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    200: {
        "description": (
            "Open Group exchange XML (attachment; `X-Exchange-Loss-Count`), or with "
            "`?report=true` JSON `{xml, losses[]}`"
        ),
        "content": {
            "application/xml": {"schema": {"type": "string"}},
        },
        "model": ExchangeExportReport,
    },
    404: _404,
    409: {"description": f"`{xsvc.UNSUPPORTED_LANGUAGE}`: not an ArchiMate model"},
    422: {"description": "`input_too_large` above MODEL_SYNC_MAX_CONCEPTS"},
}

_IMPORT_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "description": "An Open Group ArchiMate Model Exchange File (3.1) document",
        "content": {
            "application/xml": {"schema": {"type": "string", "format": "binary"}},
            "text/xml": {"schema": {"type": "string", "format": "binary"}},
        },
    }
}

_IMPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    409: IDEMPOTENCY_409,
    413: {"description": f"`{xsvc.EXCHANGE_TOO_LARGE}`: body above EXCHANGE_IMPORT_MAX_BYTES"},
    415: {"description": f"`{UNSUPPORTED_MEDIA_TYPE}`: Content-Type is not application/xml"},
    422: {
        "description": (
            f"`{xsvc.EXCHANGE_INVALID}` (malformed, DOCTYPE/entities, wrong root) or "
            "`input_too_large`"
        )
    },
}

_RENDER_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    **RENDER_RESPONSES,
    404: _404,
}


async def read_xml_body(request: Request) -> bytes:
    """The request body, refused BEFORE buffering more than the configured cap."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in XML_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "code": UNSUPPORTED_MEDIA_TYPE,
                "message": "send the exchange document as Content-Type: application/xml",
            },
        )
    limit = get_settings().exchange_import_max_bytes
    too_large = HTTPException(
        status_code=413,
        detail={
            "code": xsvc.EXCHANGE_TOO_LARGE,
            "message": f"the exchange document is larger than EXCHANGE_IMPORT_MAX_BYTES={limit}",
        },
    )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


def _xml_response(result: Any, filename: str, report: bool, extra: dict[str, str]) -> Response:
    losses = [xsvc.loss_out(loss) for loss in result.losses]
    if report:
        body = ExchangeExportReport(xml=result.data.decode("utf-8"), losses=losses)
        return JSONResponse(body.model_dump(mode="json"), headers=extra)
    return Response(
        content=result.data,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exchange-Loss-Count": str(len(losses)),
            "X-Content-Type-Options": "nosniff",
            **extra,
        },
    )


# -- Export --


@exchange_router.get(
    "/{model_id}/revisions/{rev_id}/exchange",
    operation_id="export_model_revision_exchange",
    response_class=Response,
    responses=_EXPORT_RESPONSES,
)
async def export_model_revision_exchange(
    model_id: UUID = Path(..., description="Model ID"),
    rev_id: UUID = Path(..., description="Revision ID"),
    report: bool = Query(False, description="Return JSON {xml, losses[]} instead of raw XML"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Export a sealed revision's immutable manifest as Open Group exchange XML."""
    manifest, _hash, seq = await xsvc.revision_manifest(
        session, model_id, rev_id, tenant_id, subject_id
    )
    result = await run_in_threadpool(xsvc.export_manifest, manifest)
    return _xml_response(result, f"model-{model_id}-rev-{seq}.xml", report, {})


@exchange_router.get(
    "/{model_id}/exchange",
    operation_id="export_model_exchange",
    response_class=Response,
    responses=_EXPORT_RESPONSES,
)
async def export_model_exchange(
    model_id: UUID = Path(..., description="Model ID"),
    report: bool = Query(False, description="Return JSON {xml, losses[]} instead of raw XML"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Export the draft head as Open Group exchange XML; ``ETag`` is the head exported."""
    manifest, head = await xsvc.draft_manifest(session, model_id, tenant_id, subject_id)
    result = await run_in_threadpool(xsvc.export_manifest, manifest)
    return _xml_response(result, f"model-{model_id}-head-{head}.xml", report, {"ETag": f'"{head}"'})


# -- Import (staged as a change set) --


def _replay(replay: Any) -> JSONResponse:
    return JSONResponse(
        ExchangeImportOut.model_validate(replay.body).model_dump(mode="json"),
        status_code=replay.status_code,
    )


@exchange_router.post(
    "/exchange-imports",
    response_model=ExchangeImportOut,
    status_code=201,
    operation_id="import_exchange_as_new_model",
    responses=_IMPORT_RESPONSES,
    openapi_extra=_IMPORT_BODY,
)
async def import_exchange_as_new_model(
    request: Request,
    filename: str | None = Query(None, max_length=255, description="Source file name (recorded)"),
    project_id: str | None = Query(None, max_length=100, description="Project scope of the model"),
    idempotency_key: str | None = Depends(idempotency_key_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Create an ArchiMate model from an exchange document and stage its content (201).

    The model is created empty (title = the document's model name); the content is a
    change set (``evaluator_ref=exchange-import``), ``proposed`` or ``invalid``, applied
    with ``POST /modeling/change-sets/{change_set_id}/apply``.
    """
    source = xsvc.Source(data=await read_xml_body(request), filename=filename)
    idem = Idempotency(
        session,
        route="/modeling/models/exchange-imports",
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        key=idempotency_key,
        body={"sha256": source.sha256, "filename": filename, "project_id": project_id},
    )
    async with idem.guard():
        out, replay = await xsvc.import_as_new_model(
            session,
            source,
            idem,
            tenant_id=tenant_id,
            subject_id=subject_id,
            visibility=visibility,
            project_id=project_id,
        )
    if replay is not None:
        return _replay(replay)
    return out


@exchange_router.post(
    "/{model_id}/exchange-imports",
    response_model=ExchangeImportOut,
    status_code=201,
    operation_id="import_model_exchange",
    responses={
        **_IMPORT_RESPONSES,
        404: _404,
        409: {
            "description": (
                f"`{xsvc.UNSUPPORTED_LANGUAGE}`: not an ArchiMate model; "
                + IDEMPOTENCY_409["description"]
            )
        },
    },
    openapi_extra=_IMPORT_BODY,
)
async def import_model_exchange(
    request: Request,
    model_id: UUID = Path(..., description="Model ID"),
    filename: str | None = Query(None, max_length=255, description="Source file name (recorded)"),
    idempotency_key: str | None = Depends(idempotency_key_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Any:
    """Stage an exchange document against the draft head as a change set (201).

    Ops create or update every element, relationship, view and occurrence relative to
    the head; nothing is written to the model until the change set is applied.
    """
    source = xsvc.Source(data=await read_xml_body(request), filename=filename)
    idem = Idempotency(
        session,
        route="/modeling/models/{model_id}/exchange-imports",
        tenant_id=tenant_id,
        subject_id=subject_id,
        visibility=visibility,
        key=idempotency_key,
        body={"model_id": str(model_id), "sha256": source.sha256, "filename": filename},
    )
    async with idem.guard():
        out, replay = await xsvc.import_into_model(
            session, model_id, source, idem, tenant_id=tenant_id, subject_id=subject_id
        )
    if replay is not None:
        return _replay(replay)
    return out


# -- Rendering --


async def _render_response(
    target: rsvc.Target, fmt: RenderFormat, report: bool, filename: str
) -> Response:
    if target.manifest is None:
        return not_modified(target.tag, ARCHIMATE_RENDERER_VERSION)
    manifest, view_id = target.manifest, target.view_id
    return await render_response(
        lambda: rsvc.render_view(manifest, view_id),
        fmt=fmt,
        report=report,
        etag=target.tag,
        renderer_version=ARCHIMATE_RENDERER_VERSION,
        filename=filename,
    )


@view_render_router.get(
    "/{vid}/render",
    operation_id="render_architecture_view",
    response_class=Response,
    responses=_RENDER_RESPONSES,
)
async def render_architecture_view(
    vid: UUID = Path(..., description="Architecture view ID"),
    format: FormatParam = "svg",  # noqa: A002
    report: ReportParam = False,
    if_none_match: IfNoneMatch = None,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Render the view as it is on the draft head (synchronous: sanitized SVG, PNG or PDF)."""
    target = await rsvc.draft_target(
        session,
        vid,
        tenant_id=tenant_id,
        subject_id=subject_id,
        fmt=format,
        report=report,
        if_none_match=if_none_match,
    )
    return await _render_response(target, format, report, f"view-{vid}")


@exchange_router.get(
    "/{model_id}/revisions/{rev_id}/views/{view_id}/render",
    operation_id="render_model_revision_view",
    response_class=Response,
    responses=_RENDER_RESPONSES,
)
async def render_model_revision_view(
    model_id: UUID = Path(..., description="Model ID"),
    rev_id: UUID = Path(..., description="Revision ID"),
    view_id: UUID = Path(..., description="View ID in the revision's manifest"),
    format: FormatParam = "svg",  # noqa: A002
    report: ReportParam = False,
    if_none_match: IfNoneMatch = None,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Render a view of a sealed revision (immutable: unaffected by later draft edits)."""
    target = await rsvc.revision_target(
        session,
        model_id,
        rev_id,
        view_id,
        tenant_id=tenant_id,
        subject_id=subject_id,
        fmt=format,
        report=report,
        if_none_match=if_none_match,
    )
    return await _render_response(target, format, report, f"view-{view_id}-rev-{rev_id}")


# -- Folder tree (organizations) --


@exchange_router.get(
    "/{model_id}/organizations",
    response_model=OrganizationsOut,
    operation_id="get_model_organizations",
    responses={
        **AUTH_RESPONSES,
        404: _404,
        409: {"description": f"`{osvc.UNSUPPORTED_LANGUAGE}`: not an ArchiMate model"},
    },
)
async def get_model_organizations(
    response: Response,
    model_id: UUID = Path(..., description="Model ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> OrganizationsOut:
    """The model's folder tree (items of deleted rows are left out); ``ETag`` = head."""
    tree, head = await osvc.read_tree(session, model_id, tenant_id, subject_id)
    response.headers["ETag"] = f'"{head}"'
    return OrganizationsOut.model_validate(
        {"model_id": model_id, "head": head, "organizations": tree}
    )


@exchange_router.put(
    "/{model_id}/organizations",
    response_model=OrganizationsOut,
    operation_id="replace_model_organizations",
    responses={
        **AUTH_RESPONSES,
        404: _404,
        409: {"description": f"`{osvc.UNSUPPORTED_LANGUAGE}`: not an ArchiMate model"},
        412: models_svc.STALE_HEAD_412,
        422: {"description": "`model_integrity` (AM-REF-001): an item is not in the model"},
    },
)
async def replace_model_organizations(
    response: Response,
    payload: OrganizationsIn,
    model_id: UUID = Path(..., description="Model ID"),
    if_match: int | None = Depends(models_svc.if_match_header),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> OrganizationsOut:
    """Replace the whole folder tree; bumps the head (``ETag``). If-Match is the head."""
    raw = (
        None
        if payload.organizations is None
        else [f.model_dump(mode="json") for f in payload.organizations]
    )
    tree, head = await osvc.replace_tree(
        session, model_id, raw, tenant_id=tenant_id, subject_id=subject_id, if_match=if_match
    )
    response.headers["ETag"] = f'"{head}"'
    return OrganizationsOut.model_validate(
        {"model_id": model_id, "head": head, "organizations": tree}
    )
