"""Synchronous SVG/PNG/PDF rendering of ArchiMate views (plan §10, target spec §8.1).

The snapshot comes from the manifest builder sealing uses — the draft head's
(``modeling_models.build_manifest`` under the model's ``FOR SHARE`` lock) or a revision's
immutable manifest — so a render shows exactly what a seal would store. The database
transaction is closed BEFORE rendering (the connection goes back to the pool).

The HTTP conventions — boundary sanitizing, headers, ``ETag``, ``304``, the JSON report and
running the renderer in a worker thread — are the ones every render route shares
(``routers/_render_common.py``, framework-neutral parts in ``services/render_http.py``);
this module only loads the target. The ETag's content
basis is the revision ``content_hash``, or the draft's model id + head, and a matching
``If-None-Match`` is decided before the snapshot is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from services import model_exchange as xsvc
from services import modeling_models as models_svc
from services import view_occurrences as occ
from services.render_http import matches, render_etag
from settings import get_settings
from tools.rendering import (
    ARCHIMATE_RENDERER_VERSION,
    RenderResult,
    RenderTargetNotFoundError,
    render_archimate_view,
)


def _tag(basis: str, view_id: str, fmt: str, report: bool) -> str:
    return render_etag(basis, view_id, ARCHIMATE_RENDERER_VERSION, fmt, report)


@dataclass
class Target:
    """What to render, loaded with the transaction already closed."""

    tag: str
    manifest: dict[str, Any] | None  # None when the caller's ETag matched (304)
    view_id: str


async def draft_target(
    session: AsyncSession,
    view_id: UUID,
    *,
    tenant_id: str,
    subject_id: str,
    fmt: str,
    report: bool,
    if_none_match: str | None,
) -> Target:
    view = await occ.get_view(session, view_id, tenant_id, subject_id, mutable=False)
    model = await models_svc.get_model(session, view.model_id, tenant_id, subject_id, lock="share")
    tag = _tag(f"draft:{model.id}:{model.head}", str(view.id), fmt, report)
    if matches(if_none_match, tag):
        await session.commit()
        return Target(tag=tag, manifest=None, view_id=str(view.id))
    total = await models_svc.count_concepts(session, model.id)
    if total > get_settings().model_sync_max_concepts:
        raise xsvc.too_large(total, "the model")
    manifest = await models_svc.canonical_manifest(session, model)
    await session.commit()
    return Target(tag=tag, manifest=manifest, view_id=str(view.id))


async def revision_target(
    session: AsyncSession,
    model_id: UUID,
    rev_id: UUID,
    view_id: UUID,
    *,
    tenant_id: str,
    subject_id: str,
    fmt: str,
    report: bool,
    if_none_match: str | None,
) -> Target:
    await models_svc.get_model(session, model_id, tenant_id, subject_id)
    row = await models_svc.get_revision(session, model_id, rev_id)
    manifest, content_hash = row.manifest, row.content_hash
    await session.commit()
    vid = str(view_id)
    # A UML revision has no ArchiMate views: 404, as the UML route answers for an
    # ArchiMate revision.
    if manifest.get("model", {}).get("language") != "archimate" or not any(
        v["id"] == vid for v in manifest.get("views", [])
    ):
        raise HTTPException(status_code=404, detail="view not found in this revision")
    tag = _tag(f"revision:{content_hash}", vid, fmt, report)
    if matches(if_none_match, tag):
        return Target(tag=tag, manifest=None, view_id=vid)
    xsvc.check_manifest_size(manifest, "the revision")
    return Target(tag=tag, manifest=manifest, view_id=vid)


def render_view(manifest: dict[str, Any], view_id: str) -> RenderResult:
    """The pure render of one view of a manifest (runs in a worker thread)."""
    try:
        return render_archimate_view(models_svc.archimate_snapshot(manifest), view_id)
    except RenderTargetNotFoundError as exc:  # pragma: no cover — checked by the loaders
        raise HTTPException(status_code=404, detail="view not found") from exc
