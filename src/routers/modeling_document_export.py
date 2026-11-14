"""Whole-model document exports, mounted under ``/modeling/models``.

* ``GET /{model_id}/export`` and ``GET /{model_id}/revisions/{rev_id}/export`` —
  ``format=pdf``: one titled vector PDF page per view (ArchiMate) or diagram (UML);
  ``format=svg``: a zip with one sanitized SVG per view / diagram.
* ``GET /{model_id}/xmi`` and ``GET /{model_id}/revisions/{rev_id}/xmi`` — OMG XMI 2.5.1 /
  UML 2.5.1 of a UML model (an ArchiMate model is ``409``: use ``…/exchange``).

Service: ``services/model_document_export.py``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from auth import get_subject_id, get_tenant_id
from db import get_session
from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.common import ErrorResponse
from services import model_document_export as dsvc

router = APIRouter(route_class=ErrorEnvelopeRoute)

ExportFormat = Literal["pdf", "svg"]
FormatQuery = Annotated[
    ExportFormat,
    Query(
        alias="format",
        description=(
            "pdf (default): one titled vector page per view / diagram; svg: a zip with one "
            "sanitized SVG per view / diagram"
        ),
    ),
]

_404 = {
    "description": (
        "Model or revision not found, or `no_diagrams`: the model has no view (ArchiMate) "
        "or diagram (UML) to export"
    ),
    "model": ErrorResponse,
}
_422 = {"description": "`input_too_large` above MODEL_SYNC_MAX_CONCEPTS", "model": ErrorResponse}

_EXPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    200: {
        "description": (
            "Attachment: `application/pdf` (one page per view / diagram, with an outline "
            "entry each) or `application/zip` of `NNN-<name>.svg` files, in page order"
        ),
        "content": {
            "application/pdf": {"schema": {"type": "string", "format": "binary"}},
            "application/zip": {"schema": {"type": "string", "format": "binary"}},
        },
    },
    404: _404,
    422: _422,
}

_XMI_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    200: {
        "description": "OMG XMI 2.5.1 / UML 2.5.1 document (attachment)",
        "content": {"application/xml": {"schema": {"type": "string"}}},
    },
    404: {"description": "Model or revision not found", "model": ErrorResponse},
    409: {
        "description": (
            f"`{dsvc.UNSUPPORTED_LANGUAGE}`: not a UML model — an ArchiMate model exports as "
            "the Open Group exchange format (`GET /modeling/models/{model_id}/exchange`)"
        ),
        "model": ErrorResponse,
    },
    422: _422,
}


def _attachment(body: bytes, media_type: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-cache",
        },
    )


async def _export(doc: dsvc.Document, fmt: ExportFormat) -> Response:
    if fmt == "svg":
        body = await run_in_threadpool(dsvc.export_svg_zip, doc)
        return _attachment(body, "application/zip", f"{doc.basename}-svg.zip")
    body = await run_in_threadpool(dsvc.export_pdf, doc)
    return _attachment(body, "application/pdf", f"{doc.basename}.pdf")


async def _xmi(doc: dsvc.Document) -> Response:
    body = await run_in_threadpool(dsvc.export_uml_xmi, doc)
    return _attachment(body, "application/xml", f"{doc.basename}.xmi")


@router.get(
    "/{model_id}/export",
    operation_id="export_model_diagrams",
    response_class=Response,
    responses=_EXPORT_RESPONSES,
)
async def export_model_diagrams(
    model_id: UUID = Path(..., description="Model ID"),
    format: FormatQuery = "pdf",  # noqa: A002
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Export every view (ArchiMate) or diagram (UML) of the draft head as one document.

    PDF: one vector page per view / diagram, titled with its name, in id order. SVG: a zip
    of the same pages as sanitized SVG files. A model with no view / diagram is
    ``404 no_diagrams``.
    """
    doc = await dsvc.draft_document(session, model_id, tenant_id, subject_id)
    return await _export(doc, format)


@router.get(
    "/{model_id}/revisions/{rev_id}/export",
    operation_id="export_model_revision_diagrams",
    response_class=Response,
    responses=_EXPORT_RESPONSES,
)
async def export_model_revision_diagrams(
    model_id: UUID = Path(..., description="Model ID"),
    rev_id: UUID = Path(..., description="Revision ID"),
    format: FormatQuery = "pdf",  # noqa: A002
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Export every view / diagram of a sealed revision (immutable) as one document.

    Same shape as ``GET /modeling/models/{model_id}/export``.
    """
    doc = await dsvc.revision_document(session, model_id, rev_id, tenant_id, subject_id)
    return await _export(doc, format)


@router.get(
    "/{model_id}/xmi",
    operation_id="export_model_xmi",
    response_class=Response,
    responses=_XMI_RESPONSES,
)
async def export_model_xmi(
    model_id: UUID = Path(..., description="Model ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Export a UML model's draft head as OMG XMI 2.5.1 / UML 2.5.1.

    Elements, features, relationships and interactions are UML; each diagram's kind and
    the concepts it shows are in an ``xmi:Extension extender="requirements-api"``.
    """
    doc = await dsvc.draft_document(session, model_id, tenant_id, subject_id, uml_only=True)
    return await _xmi(doc)


@router.get(
    "/{model_id}/revisions/{rev_id}/xmi",
    operation_id="export_model_revision_xmi",
    response_class=Response,
    responses=_XMI_RESPONSES,
)
async def export_model_revision_xmi(
    model_id: UUID = Path(..., description="Model ID"),
    rev_id: UUID = Path(..., description="Revision ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> Response:
    """Export a sealed revision of a UML model as OMG XMI 2.5.1 / UML 2.5.1."""
    doc = await dsvc.revision_document(
        session, model_id, rev_id, tenant_id, subject_id, uml_only=True
    )
    return await _xmi(doc)
