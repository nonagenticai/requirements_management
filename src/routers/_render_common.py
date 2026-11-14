"""Shared HTTP conventions of the synchronous render routes (SVG / PNG / PDF / JSON report).

* The SVG is sanitized at the boundary (``tools.rendering.sanitize_svg``) whatever the
  renderer produced; a PNG is rasterised from, and a vector PDF drawn from, that sanitized
  SVG. A PDF is an attachment (``Content-Disposition``) named after the view or diagram.
* Every response (``304`` included) carries ``Content-Security-Policy: default-src 'none';
  style-src 'unsafe-inline'``, ``X-Content-Type-Options: nosniff``, ``X-Renderer-Version``
  and ``Cache-Control: private, no-cache`` (a cache may keep it but must revalidate with
  the ``ETag``: a draft render changes with every write).
* ``report=true`` returns ``200`` JSON ``{svg, diagnostics, renderer_version}`` whatever
  ``format`` says.
* The ``ETag`` is a sha256 over the hash of the renderer's INPUT (not of its output), the
  diagram, the renderer version and the representation (``report``, or else the format).
  It is a strong validator because the pinned renderer is deterministic: equal inputs give
  byte-identical bodies. A JSON report carries the SVG whatever ``format`` says, so
  ``format`` is not part of a report's ETag. ``If-None-Match`` naming it (or ``*``, weak
  comparison as RFC 9110 prescribes for it) is ``304`` with no body, decided BEFORE
  anything is rendered.
* The renderer runs in a worker thread with no database session or transaction open.

The framework-neutral parts (ETag, ``If-None-Match``, headers) live in
``services/render_http.py`` so the target loaders need no router import.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Header, Query, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from schemas.common import ErrorResponse
from schemas.modeling_exchange import RenderReport
from services.render_http import MEDIA_TYPES, render_headers
from tools.rendering import RenderResult, sanitize_svg
from tools.rendering.pdf import render_pdf
from tools.rendering.png import render_png

RenderFormat = Literal["svg", "png", "pdf"]

RENDER_RESPONSES: dict[int | str, dict] = {
    200: {
        "description": (
            "The rendered diagram (SVG, PNG, vector PDF as an attachment, or with "
            "`report=true` a JSON report)"
        ),
        "content": {
            "image/svg+xml": {"schema": {"type": "string"}},
            "image/png": {"schema": {"type": "string", "format": "binary"}},
            "application/pdf": {"schema": {"type": "string", "format": "binary"}},
        },
        "model": RenderReport,
    },
    304: {"description": "Not modified: `If-None-Match` names the current ETag"},
    422: {
        "description": "`input_too_large` above MODEL_SYNC_MAX_CONCEPTS",
        "model": ErrorResponse,
    },
}


FormatParam = Annotated[
    RenderFormat, Query(alias="format", description="svg (default), png or pdf (vector)")
]
ReportParam = Annotated[
    bool,
    Query(description="true: JSON {svg, diagnostics, renderer_version} instead of the image"),
]
IfNoneMatch = Annotated[
    str | None,
    Header(alias="If-None-Match", description="An ETag of an earlier render: 304 if unchanged"),
]


def not_modified(etag: str, renderer_version: str) -> Response:
    return Response(status_code=304, headers=render_headers(etag, renderer_version))


def _encode(
    result: RenderResult, fmt: RenderFormat, report: bool
) -> tuple[bytes | None, str, dict]:
    svg = sanitize_svg(result.svg)
    if report:
        return (
            None,
            "",
            {
                "svg": svg,
                "diagnostics": [d.model_dump(mode="json") for d in result.diagnostics],
                "renderer_version": result.renderer_version,
            },
        )
    if fmt == "png":
        return render_png(svg), MEDIA_TYPES["png"], {}
    if fmt == "pdf":
        return render_pdf(svg), MEDIA_TYPES["pdf"], {}
    return svg.encode("utf-8"), MEDIA_TYPES["svg"], {}


async def render_response(
    render: Callable[[], RenderResult],
    *,
    fmt: RenderFormat,
    report: bool,
    etag: str,
    renderer_version: str,
    filename: str,
) -> Response:
    """Run ``render()`` (→ ``RenderResult``) off the event loop and encode the response.

    ``filename`` (no extension) names the PDF attachment.
    """

    def work() -> tuple[bytes | None, str, dict]:
        return _encode(render(), fmt, report)

    body, media_type, report_body = await run_in_threadpool(work)
    headers = render_headers(etag, renderer_version)
    if body is None:
        return JSONResponse(report_body, headers=headers)
    if fmt == "pdf":
        headers["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    return Response(content=body, media_type=media_type, headers=headers)
