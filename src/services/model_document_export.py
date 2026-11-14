"""Whole-model exports: every view / diagram as one PDF or a zip of SVGs, and UML XMI.

The content comes from the same manifest a seal stores — the draft head's canonical
manifest (read under the model's ``FOR SHARE`` lock) or a sealed revision's immutable one —
and the transaction is closed BEFORE anything is rendered. Every page is the
per-view / per-diagram render route's sanitized SVG, so an export page and a single render
of the same target draw the same thing.

* ArchiMate: one page / file per view, in the manifest's (canonical, id) order.
* UML: one page / file per diagram, same order.
* A model with no view or diagram is ``404 no_diagrams`` (there is nothing to draw; an
  empty PDF would look like a broken export).
* XMI is UML only; an ArchiMate model is ``409 unsupported_language`` pointing at the Open
  Group exchange route.
* Size: the synchronous cap (``MODEL_SYNC_MAX_CONCEPTS``) applies as on the render routes
  (``422 input_too_large``).
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from services import model_exchange as xsvc
from services import modeling_models as models_svc
from services.uml_draft import UmlDraft
from settings import get_settings
from tools.rendering import render_archimate_view, render_uml_diagram, sanitize_svg
from tools.rendering.pdf import render_pdf_document
from tools.uml.xmi import export_xmi, manifest_header

NO_DIAGRAMS = "no_diagrams"
UNSUPPORTED_LANGUAGE = "xmi_unsupported_language"


@dataclass
class Document:
    """A loaded model: its manifest and what to call the export file."""

    manifest: dict[str, Any]
    language: str
    basename: str  # no extension

    @property
    def title(self) -> str:
        return (self.manifest.get("model") or {}).get("title") or self.basename


def _check_size(manifest: dict[str, Any], language: str, what: str) -> None:
    if language == "uml":
        total = UmlDraft.from_manifest(manifest).size()
        if total > get_settings().model_sync_max_concepts:
            raise xsvc.too_large(total, what)
    else:
        xsvc.check_manifest_size(manifest, what)


def require_uml(language: str) -> None:
    """``409 unsupported_language`` unless ``language`` is UML (the XMI routes)."""
    if language != "uml":
        raise HTTPException(
            status_code=409,
            detail={
                "code": UNSUPPORTED_LANGUAGE,
                "message": (
                    f"XMI carries UML models; this is a {language} model — export it as the "
                    "Open Group exchange format with GET /modeling/models/{model_id}/exchange"
                ),
            },
        )


async def draft_document(
    session: AsyncSession,
    model_id: UUID,
    tenant_id: str,
    subject_id: str,
    *,
    uml_only: bool = False,
) -> Document:
    """The draft head as one state (``FOR SHARE``); ends the transaction."""
    model = await models_svc.get_model(session, model_id, tenant_id, subject_id, lock="share")
    if uml_only:
        require_uml(model.language)
    total = await models_svc.count_concepts(session, model.id)
    if total > get_settings().model_sync_max_concepts:
        raise xsvc.too_large(total, "the model")
    manifest = await models_svc.canonical_manifest(session, model)
    language, head = model.language, model.head
    await session.commit()
    return Document(manifest=manifest, language=language, basename=f"model-{model_id}-head-{head}")


async def revision_document(
    session: AsyncSession,
    model_id: UUID,
    rev_id: UUID,
    tenant_id: str,
    subject_id: str,
    *,
    uml_only: bool = False,
) -> Document:
    """A sealed revision's immutable manifest; ends the transaction."""
    model = await models_svc.get_model(session, model_id, tenant_id, subject_id)
    if uml_only:
        require_uml(model.language)
    row = await models_svc.get_revision(session, model.id, rev_id)
    manifest, seq, language = row.manifest, row.seq, model.language
    await session.commit()
    _check_size(manifest, language, "the revision")
    return Document(manifest=manifest, language=language, basename=f"model-{model_id}-rev-{seq}")


def _pages(doc: Document) -> list[tuple[str, str, Callable[[], str]]]:
    """``(id, title, render → raw svg)`` per view (ArchiMate) or diagram (UML)."""
    if doc.language == "uml":
        snapshot = UmlDraft.from_manifest(doc.manifest).snapshot()
        return [
            (d.id, d.name or d.id, lambda did=d.id: render_uml_diagram(snapshot, did).svg)
            for d in snapshot.diagrams or []
        ]
    snap = models_svc.archimate_snapshot(doc.manifest)
    return [
        (v.id, v.name or v.id, lambda vid=v.id: render_archimate_view(snap, vid).svg)
        for v in snap.views or []
    ]


def _rendered(doc: Document) -> list[tuple[str, str, str]]:
    pages = _pages(doc)
    if not pages:
        noun = "diagram" if doc.language == "uml" else "view"
        raise HTTPException(
            status_code=404,
            detail={"code": NO_DIAGRAMS, "message": f"the model has no {noun} to export"},
        )
    return [(pid, title, sanitize_svg(render())) for pid, title, render in pages]


def export_pdf(doc: Document) -> bytes:
    """One titled vector PDF page per view / diagram (runs in a worker thread)."""
    return render_pdf_document(
        [(title, svg) for _pid, title, svg in _rendered(doc)], title=doc.title
    )


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:60] or "diagram"


def export_svg_zip(doc: Document) -> bytes:
    """A zip with one sanitized SVG per view / diagram, ``NNN-<name>.svg`` in page order."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for index, (_pid, title, svg) in enumerate(_rendered(doc), start=1):
            info = zipfile.ZipInfo(
                f"{index:03d}-{_slug(title)}.svg", date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, svg.encode("utf-8"))
    return out.getvalue()


def export_uml_xmi(doc: Document) -> bytes:
    """XMI 2.5.1 of a UML model (runs in a worker thread)."""
    require_uml(doc.language)
    model_id, name, documentation = manifest_header(doc.manifest)
    snapshot = UmlDraft.from_manifest(doc.manifest).snapshot()
    return export_xmi(snapshot, model_id=model_id, name=name, documentation=documentation)
