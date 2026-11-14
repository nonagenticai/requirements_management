"""Framework-neutral HTTP conventions of the render routes: ETag, If-None-Match, headers.

Shared by the services that load a render target (``services/model_render.py``) and the
route helpers (``routers/_render_common.py``), so no service imports a router module.
See ``routers/_render_common.py`` for the full contract.
"""

from __future__ import annotations

import hashlib

CSP = "default-src 'none'; style-src 'unsafe-inline'"
MEDIA_TYPES = {"svg": "image/svg+xml", "png": "image/png", "pdf": "application/pdf"}
# Cacheable, but revalidated with the ETag every time (a draft render changes per write).
CACHE_CONTROL = "private, no-cache"


def render_etag(
    content_hash: str, target_id: str, renderer_version: str, fmt: str, report: bool
) -> str:
    """Strong ETag over the renderer's input, target, version and representation."""
    representation = "report" if report else fmt
    raw = "\n".join([content_hash, target_id, renderer_version, representation])
    return '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest() + '"'


def render_headers(etag: str, renderer_version: str) -> dict[str, str]:
    return {
        "ETag": etag,
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "X-Renderer-Version": renderer_version,
        "Cache-Control": CACHE_CONTROL,
    }


def matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 weak comparison of ``If-None-Match`` against ``etag``."""
    if if_none_match is None:
        return False
    tags = [t.strip() for t in if_none_match.split(",")]
    return any(t == "*" or t.removeprefix("W/") == etag for t in tags)
