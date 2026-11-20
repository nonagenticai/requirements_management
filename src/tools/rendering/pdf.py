"""Vector PDF of rendered (sanitized) SVG diagrams via cairosvg; multi-page via pypdf.

cairosvg (LGPL, cairo backend) draws the SVG as PDF vector operators — paths, text and
the relationship markers the renderers emit — so a PDF page is the drawing, not a bitmap
of it. It needs ``libcairo2`` at runtime (the Dockerfile's production stage installs it,
with ``fonts-dejavu-core`` so the renderers' ``DejaVu Sans`` resolves to the same family
the PNG rasteriser bundles). The input is re-sanitized first, so it carries no external
reference, and cairosvg runs with its default ``unsafe=False`` (no external files, no XML
entities). cairosvg is imported on first use, not at module import: a host without libcairo
loses PDF output only, never the whole application import.

A titled page is the sanitized diagram nested, unchanged, under a title band; a document
is one titled page per diagram, with a PDF outline entry per page.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

from lxml import etree  # ty: ignore[missing-module-member]  # C extension submodule ty cannot see
from pypdf import PdfReader, PdfWriter

from tools.rendering.fonts import FONT_FAMILY
from tools.rendering.svg import SVG_NS, clean_text, sanitize_svg

PDF_MAGIC = b"%PDF"
TITLE_BAND = 40.0  # px above the diagram on a titled page
_TITLE_SIZE = 16


def _svg2pdf(svg: str) -> bytes:
    import cairosvg  # lazy: dlopens libcairo (see module docstring)

    return bytes(cairosvg.svg2pdf(bytestring=svg.encode("utf-8")))


def render_pdf(svg: str) -> bytes:
    """One vector PDF page of ``svg`` (re-sanitized), sized to the drawing."""
    return _svg2pdf(sanitize_svg(svg))


def _number(value: str | None, default: float) -> float:
    try:
        return float(str(value).removesuffix("px"))
    except TypeError, ValueError:
        return default


def titled_svg(svg: str, title: str) -> str:
    """``svg`` (re-sanitized) under a ``TITLE_BAND`` px band carrying ``title`` as text."""
    title = clean_text(title)
    inner = etree.fromstring(sanitize_svg(svg).encode("utf-8"))
    box = (inner.get("viewBox") or "").split()
    width = _number(inner.get("width"), float(box[2]) if len(box) == 4 else 800.0)
    height = _number(inner.get("height"), float(box[3]) if len(box) == 4 else 600.0)
    # The title can be wider than a small diagram: widen the page, never squash the text.
    page_w = max(width, 0.62 * _TITLE_SIZE * len(title) + 32)
    outer = etree.Element(
        f"{{{SVG_NS}}}svg",
        # None is lxml's default-namespace key; lxml-stubs types it as str only.
        nsmap={None: SVG_NS},  # ty: ignore[invalid-argument-type]
        width=f"{page_w:g}",
        height=f"{height + TITLE_BAND:g}",
        viewBox=f"0 0 {page_w:g} {height + TITLE_BAND:g}",
    )
    etree.SubElement(
        outer,
        f"{{{SVG_NS}}}rect",
        x="0",
        y="0",
        width=f"{page_w:g}",
        height=f"{height + TITLE_BAND:g}",
        fill="#ffffff",
    )
    text = etree.SubElement(
        outer,
        f"{{{SVG_NS}}}text",
        x="16",
        y=f"{TITLE_BAND - 14:g}",
        attrib={
            "font-family": FONT_FAMILY,
            "font-size": str(_TITLE_SIZE),
            "font-weight": "bold",
            "fill": "#000000",
        },
    )
    text.text = title
    inner.set("x", "0")
    inner.set("y", f"{TITLE_BAND:g}")
    inner.set("width", f"{width:g}")
    inner.set("height", f"{height:g}")
    outer.append(inner)
    return etree.tostring(outer, encoding="unicode")


def render_pdf_document(pages: Sequence[tuple[str, str]], *, title: str) -> bytes:
    """A multi-page vector PDF: one titled page per ``(page title, svg)``, in order.

    Each page carries an outline (bookmark) entry named after it; the document's
    ``/Title`` is ``title``.
    """
    if not pages:
        raise ValueError("a PDF document needs at least one page")
    writer = PdfWriter()
    for index, (page_title, svg) in enumerate(pages):
        writer.append(PdfReader(io.BytesIO(_svg2pdf(titled_svg(svg, page_title)))))
        # pypdf 7 made these keyword-only (``def add_outline_item(self, *, title, page_number, ...)``).
        writer.add_outline_item(title=page_title or f"Page {index + 1}", page_number=index)
    writer.add_metadata({"/Title": title, "/Producer": "requirements-api"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
