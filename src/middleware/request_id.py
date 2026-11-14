"""Request ID middleware.

Mints a uuid7 request ID on every request (if the caller did not supply one
via ``X-Request-ID``) and pins it to ``request.state.request_id`` so every
downstream middleware, log line, and exception envelope can reference the same
identifier. Echoes the value back on the response as ``X-Request-ID``.

Runs before ``RequestLoggingMiddleware`` and the auth middleware so that any
401/403/500 emitted from the auth layer carries a stable request ID.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

import pool_watch

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.responses import Response


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Set ``request.state.request_id`` and echo ``X-Request-ID``."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        incoming = request.headers.get("x-request-id", "").strip()
        request_id = incoming or str(uuid.uuid7())
        request.state.request_id = request_id

        # Attribute every DB connection this request checks out (see `pool_watch`
        # and `lifespan._pool_holders`). This middleware is registered LAST in
        # main.py and is therefore the OUTERMOST wrapper on the request path — it
        # runs before AuthMiddleware and before the request logger, and it is
        # registered unconditionally (AuthMiddleware is not: `ENABLE_AUTH=false`
        # removes it entirely). That matters here: the auth-exempt probes
        # `/health`, `/health/live` and `/health/ready` never reach the request
        # logger at all (it returns early on `/health`), and a probe's own
        # checkout is precisely one of the holders we need to see when the pool
        # is at its limit.
        #
        # ⚠️ This is the RAW path, not the route template. Starlette matches the
        # route inside the router, downstream of every middleware, so
        # `scope["route"]` does not exist yet at this point — the template is
        # genuinely unavailable here, and moving this call downstream to get it
        # would lose the auth-exempt and 401 requests. The value is read ONLY
        # inside the already-throttled `db_pool.saturated` warning and is never
        # returned to a client, so a path parameter costs bounded log
        # cardinality, not an exposure.
        pool_watch.caller_var.set(f"http {request.method} {request.url.path}")

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
