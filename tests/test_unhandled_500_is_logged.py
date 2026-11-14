"""An unhandled exception must still leave an `http.response` record.

Why this file exists
--------------------
`@app.exception_handler(Exception)` is run by Starlette's `ServerErrorMiddleware`,
which sits OUTSIDE `middleware/request_logging.py`. So when a handler raised, the
exception propagated straight past the response-logging call and the 500 envelope
was minted above it: the request stream contained an `http.request` line and then
nothing at all. A 500 was the one request shape that left no trace.

MEASURED 2026-09-03, a 24m22s window on the live `requirements` pod: n8n's
`AA Activity - Expansion` received HTTP 500 from `POST /architecture/elements` and
`POST /architecture/relationships`, and the pod's log contained **zero** lines with
`"status": 500`. The only way to find them was to pair 1,103 `http.request` records
against 1,117 `http.response` records and chase the three orphans — each of which
sat exactly 30.000s before an `unhandled_error: QueuePool limit of size 5 overflow
10 reached, timeout 30.00`.

That 30.0s IS the diagnosis (it is SQLAlchemy's `pool_timeout`, not a database
failure), and it appeared nowhere in the request stream. Hence the duration is
asserted below, not just the status: a record that says "500" without saying "after
30 seconds" would not have shortened this investigation by a day.
"""

import json
import logging
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from middleware.request_logging import add_request_logging_middleware  # noqa: E402


def _build_app() -> FastAPI:
    app = FastAPI()
    add_request_logging_middleware(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("QueuePool limit of size 5 overflow 10 reached")

    @app.get("/fine")
    async def fine():
        return {"ok": True}

    return app


def _responses(caplog) -> list[dict]:
    """Every `http.response` record, parsed from whichever renderer is active."""
    out = []
    for record in caplog.records:
        message = record.getMessage()
        if "http.response" not in message:
            continue
        try:
            payload = json.loads(message)
        except ValueError, TypeError:
            status = re.search(r"status[=:]\s*'?(\d{3})", message)
            unhandled = "unhandled" in message
            payload = {"status": int(status.group(1)) if status else None, "unhandled": unhandled}
        out.append(payload)
    return out


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_a_raising_handler_still_logs_an_http_response(caplog):
    with caplog.at_level(logging.INFO):
        await _get(_build_app(), "/boom")

    responses = [r for r in _responses(caplog) if r.get("status") == 500]
    assert responses, (
        "a handler raised and no http.response record was emitted — a 500 is once "
        "again the only request shape that leaves no trace in the request stream"
    )
    assert any(r.get("unhandled") for r in responses), (
        "the record must mark itself as the unhandled path, so it is not confused "
        "with a 500 an application deliberately returned"
    )


async def test_the_exception_is_re_raised_unchanged(caplog):
    """Logging must not swallow. The envelope and `unhandled_error` depend on it."""
    app = FastAPI()
    add_request_logging_middleware(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("propagate me")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with pytest.raises(RuntimeError, match="propagate me"):
                await client.get("/boom")


async def test_a_normal_response_is_not_marked_unhandled(caplog):
    """Vacuity control: a marker on every response would carry no information."""
    with caplog.at_level(logging.INFO):
        response = await _get(_build_app(), "/fine")

    assert response.status_code == 200
    ok = [r for r in _responses(caplog) if r.get("status") == 200]
    assert ok, "the healthy path must still log its response"
    assert not any(r.get("unhandled") for r in ok)


async def test_the_duration_of_a_failing_request_is_recorded(caplog):
    """The 30.0s was the diagnosis; a 500 record without it would not have helped."""
    with caplog.at_level(logging.INFO):
        await _get(_build_app(), "/boom")

    failed = [r for r in _responses(caplog) if r.get("status") == 500]
    assert failed, "no 500 record to inspect"
    assert any("duration_s" in r for r in failed), (
        "the unhandled record carries no duration_s — the live diagnosis was that "
        "the failing requests took exactly 30.000s, which is SQLAlchemy's "
        "pool_timeout and not a database failure"
    )
