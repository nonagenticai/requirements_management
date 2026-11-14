"""HTTP request logging middleware for FastAPI.

Logs incoming HTTP requests with method, URL, client IP, and response times.
Excludes health check endpoints from logging to reduce noise.
"""

import time
import uuid

from fastapi import FastAPI, Request

from logging_config import bind_request_context, get_logger

log = get_logger(__name__)


def add_request_logging_middleware(app: FastAPI) -> None:
    """Register request logging middleware on FastAPI app."""

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """Log incoming requests and response times."""
        if request.url.path == "/health":
            return await call_next(request)

        # Prefer the request_id pinned by RequestIdMiddleware (outermost in the
        # stack — runs first on the request path). Fall back to an upstream
        # X-Request-Id header, then to a fresh uuid7.
        request_id = (
            getattr(request.state, "request_id", "")
            or request.headers.get("X-Request-Id")
            or str(uuid.uuid7())
        )
        request.state.request_id = request_id
        bind_request_context(request_id=request_id)

        start_time = time.time()
        client_host = request.client.host if request.client else "unknown"
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            query=request.url.query or None,
            client=client_host,
        )

        # ⛔ AN UNHANDLED EXCEPTION USED TO LEAVE NO `http.response` AT ALL.
        #
        # `@app.exception_handler(Exception)` is run by Starlette's
        # ServerErrorMiddleware, which sits OUTSIDE this middleware. So when a
        # handler raises, the exception propagates straight past the line below
        # and the 500 envelope is minted above us — the request logs an
        # `http.request` and then nothing, forever.
        #
        # MEASURED 2026-09-03, a 24m22s window on the live pod: n8n received
        # HTTP 500s from `POST /architecture/elements` and
        # `POST /architecture/relationships`, and this log contained **zero**
        # lines with `"status": 500`. The only way to find them was to pair
        # 1,103 `http.request` against 1,117 `http.response` records and chase
        # the three orphans — each of which turned out to sit exactly 30.000s
        # before an `unhandled_error: QueuePool limit of size 5 overflow 10
        # reached, timeout 30.00`. That 30.0s IS the diagnosis, and it was
        # visible nowhere in the request stream.
        #
        # The exception is re-raised unchanged, so the envelope, the status and
        # `unhandled_error` all behave exactly as before; this only stops the
        # failure from being the one request shape that leaves no trace.
        #
        # ⚠️ WHAT THIS DOES NOT COVER, stated so the record is not overclaimed:
        # only exceptions raised BEFORE the response is returned. A
        # `StreamingResponse` whose generator raises mid-body, and a failing
        # `BackgroundTask`, both still log `status: 200` with no `unhandled`
        # marker — verified, and pre-existing. `CancelledError` (a client
        # disconnect) is deliberately NOT caught: it derives from
        # `BaseException`, and logging a disconnect as a 500 would invent a
        # server fault out of a client hanging up.
        try:
            response = await call_next(request)
        except Exception:
            log.info(
                "http.response",
                method=request.method,
                path=request.url.path,
                status=500,
                duration_s=round(time.time() - start_time, 3),
                unhandled=True,
            )
            raise

        response.headers["X-Request-Id"] = request_id

        process_time = time.time() - start_time
        log.info(
            "http.response",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_s=round(process_time, 3),
        )

        return response
