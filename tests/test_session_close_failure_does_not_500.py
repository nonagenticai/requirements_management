"""A failure to hand the connection back must not turn a 200 into a 500.

Function-scoped teardown runs INSIDE the response path — before
``await response(scope, receive, send)`` (``fastapi/routing.py:129-130``).
So unlike request scope, an exception from ``session.close()`` is no longer
raised after the body was already sent and discarded: it propagates into
``wrap_app_handling_exceptions`` and becomes a 500.

MEASURED on this branch before ``get_session`` suppressed it: with the backend
killed mid-request, the request-scoped route answered 200 and the
function-scoped route answered 500 — and BOTH rows were durably committed.

The harm is not the 500. It is that a retrying caller re-sends a write that
already landed. This test pins the guard; deleting the ``try/except`` around
``await session.close()`` in ``src/db.py`` turns it RED.

Trigger in production is any mid-request backend loss: a pgbouncer roll, a
Postgres restart, an admin terminate. ``pool_pre_ping`` does NOT cover it —
it pings at checkout, not at close.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session

pytestmark = pytest.mark.anyio if False else []

_TABLE = "session_close_probe"


async def _body(session: AsyncSession, killer) -> dict:
    """Commit a durable row, reopen a transaction, then kill THIS backend.

    The kill happens INSIDE the handler, before it returns, so teardown is
    guaranteed to meet a dead connection. Killing after the response instead
    would let the pool reconnect and the test would prove nothing.
    """
    key = uuid.uuid4().hex[:8]
    await session.execute(text(f"CREATE TABLE IF NOT EXISTS {_TABLE} (k text primary key)"))
    await session.execute(text(f"INSERT INTO {_TABLE} (k) VALUES (:k)"), {"k": key})
    await session.commit()  # the write is now durable
    # Re-open a transaction so the session has something to roll back at close.
    await session.execute(text(f"SELECT count(*) FROM {_TABLE}"))
    pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar()
    async with killer.connect() as conn:
        killed = (await conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})).scalar()
        await conn.commit()
    # Report whether the backend was ACTUALLY killed. Without this the test
    # self-disarms: an adversarial review commented the terminate out entirely
    # and the file still reported 2 passed, so if the kill ever silently stops
    # working the regression it exists to catch would ship green.
    return {"key": key, "pid": pid, "killed": bool(killed)}


def _build(scope_kwargs: dict, killer) -> FastAPI:
    app = FastAPI()

    @app.post("/probe")
    async def probe(session: AsyncSession = Depends(get_session, **scope_kwargs)):  # noqa: B008
        return await _body(session, killer)

    return app


async def _status(scope_kwargs: dict, killer) -> tuple[int, str]:
    app = _build(scope_kwargs, killer)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
        response = await client.post("/probe")
    return response.status_code, response.text


async def test_function_scope_does_not_500_when_the_backend_dies_before_teardown(test_engine):
    """THE load-bearing assertion. RED if src/db.py stops suppressing close errors.

    Request scope is the control: it has always answered 200 here, because its
    teardown runs after the body was already sent.
    """
    request_scope, _ = await _status({}, test_engine)
    function_scope, body = await _status({"scope": "function"}, test_engine)

    assert request_scope == 200, "control changed; the experiment is not comparable"
    # THE PRECONDITION. If the backend was not actually terminated, this test
    # proves nothing about a failing close and must say so rather than pass.
    assert json.loads(body)["killed"] is True, (
        "pg_terminate_backend did not report killing the request's own backend, "
        "so teardown never met a dead connection and this test is vacuous."
    )
    assert function_scope == 200, (
        "a function-scoped route 500s when the backend dies before teardown. "
        "The close error is reaching wrap_app_handling_exceptions instead of "
        f"being logged and dropped in get_session. body={body[:200]}"
    )


async def test_the_write_lands_either_way_which_is_why_the_500_mattered(test_engine):
    """A 500 produced at teardown still commits. That is the retry hazard."""
    status, body = await _status({"scope": "function"}, test_engine)
    assert status == 200, body
    key = json.loads(body)["key"]
    async with test_engine.connect() as conn:
        found = (
            await conn.execute(text(f"SELECT count(*) FROM {_TABLE} WHERE k = :k"), {"k": key})
        ).scalar()
    assert found == 1, "the handler's write did not land, so this test proves nothing"
