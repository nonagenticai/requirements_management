"""A `statement_timeout` abort must surface as HTTP 504, and NOTHING ELSE may.

Postgres aborts a statement that exceeds ``statement_timeout`` with SQLSTATE
``57014`` (``query_canceled``). psycopg raises ``psycopg.errors.QueryCanceled``;
SQLAlchemy wraps it in ``sqlalchemy.exc.OperationalError`` with the psycopg
exception on ``.orig``. ``src/main.py`` maps exactly that — and only that — to a
504 ``STATEMENT_TIMEOUT`` envelope, on two separate paths:

* bare -> ``operational_error_handler`` (registered on the concrete class, so
  Starlette's ``ExceptionMiddleware`` serves it and does NOT re-raise);
* wrapped in an ``ExceptionGroup`` (a handler using a ``TaskGroup`` delivers its
  failure that way) -> no concrete handler matches an ``ExceptionGroup``, so it
  falls through to ``global_exception_handler``'s group branch.

Before this file the whole mapping had ZERO coverage:
``grep -rn "504\\|STATEMENT_TIMEOUT\\|operational_error_handler" tests/`` returned
nothing. It worked; nothing proved it kept working.

The apps here are built the way ``tests/test_error_envelope.py`` builds them: a
tiny FastAPI app with main.py's real handler functions attached, so the
assertions are not tied to the full app boot. The envelope shape asserted is
that file's ``_assert_envelope``, unchanged.

⚠️ ``raise_app_exceptions=False`` on the transport is REQUIRED for the paths that
reach ``global_exception_handler``: Starlette routes the ``Exception`` key to
``ServerErrorMiddleware``, which sends the response and then RE-RAISES. That
re-raise is what ``operational_error_handler`` exists to avoid for the bare case
(see its docstring), but the group-wrapped case genuinely goes through it. The
flag is set uniformly so the tests differ only in the exception they raise, and
the RESPONSE is what is asserted in every arm.
"""

from __future__ import annotations

import re

import httpx
import psycopg.errors
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

UUID7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture(autouse=True)
def _clean_tables():
    """Override tests/conftest.py's autouse truncate fixture for THIS module only.

    That fixture requests the session-scoped ``test_engine``, i.e. every test in
    the suite pays a DROP-all / ``create_all`` / TRUNCATE cycle against Postgres.
    Nothing in this file reads or writes a single row: the exceptions are
    constructed in-process and the route raises them before any DB call, so the
    engine is pure cost — and, measured while writing this, pure flakiness: under
    a concurrent run the fixture ERRORed out (``IntegrityError`` on the extension
    index, ``DROP TABLE … CASCADE`` hitting ``statement_timeout``) and turned a
    clean RED/GREEN signal about the 504 mapping into noise about the database.
    Overriding by name keeps the isolation contract intact for every other module
    while making these five tests deterministic and DB-free.
    """
    yield


def _assert_envelope(body: dict, *, expected_status: int) -> None:
    """Same shape assertion as tests/test_error_envelope.py — do not diverge."""
    assert set(body.keys()) == {"error"}, body
    err = body["error"]
    assert isinstance(err.get("code"), str) and err["code"], err
    assert isinstance(err.get("message"), str) and err["message"], err
    assert err.get("status") == expected_status, err
    assert UUID7_RE.match(err.get("request_id", "")), err
    assert TIMESTAMP_RE.match(err.get("timestamp", "")), err


def _timeout_error() -> OperationalError:
    """The exact object a statement-timeout abort produces (SQLSTATE 57014)."""
    orig = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    assert orig.sqlstate == "57014", orig.sqlstate
    return OperationalError("SELECT pg_sleep(99)", {}, orig)


def _non_timeout_error() -> OperationalError:
    """A DB failure that is NOT a timeout: 57P01 admin_shutdown."""
    orig = psycopg.errors.AdminShutdown("terminating connection due to administrator command")
    assert orig.sqlstate == "57P01", orig.sqlstate
    assert orig.sqlstate != "57014", "control must not be the timeout SQLSTATE"
    return OperationalError("SELECT 1", {}, orig)


def _app_raising(exc: BaseException) -> FastAPI:
    """A one-route app wired with main.py's real handlers, mirroring main.py's order."""
    from middleware.request_id import RequestIdMiddleware

    app = FastAPI()
    from main import (
        global_exception_handler,
        http_exception_handler,
        operational_error_handler,
    )

    app.exception_handler(StarletteHTTPException)(http_exception_handler)
    app.exception_handler(OperationalError)(operational_error_handler)
    app.exception_handler(Exception)(global_exception_handler)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/boom")
    async def _boom() -> None:
        raise exc

    return app


async def _get_boom(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get("/boom")


async def test_bare_statement_timeout_returns_504() -> None:
    """A bare OperationalError wrapping QueryCanceled (57014) renders 504 STATEMENT_TIMEOUT.

    KILLING MUTATION: in src/main.py set ``QUERY_CANCELED_SQLSTATE = "57015"``
    (any value other than 57014). ``_find_statement_timeout`` then returns None,
    ``operational_error_handler`` re-raises, and the response becomes a 500
    INTERNAL_SERVER_ERROR envelope -> this test goes RED.
    """
    resp = await _get_boom(_app_raising(_timeout_error()))

    assert resp.status_code == 504, resp.text
    body = resp.json()
    _assert_envelope(body, expected_status=504)
    assert body["error"]["code"] == "STATEMENT_TIMEOUT", body


async def test_non_timeout_operational_error_is_still_500() -> None:
    """⛔ THE CONTROL. A NON-timeout OperationalError must NOT become 504.

    This is the most important test in the file. If a mutation widened the
    mapping so that EVERY ``OperationalError`` rendered a 504 — e.g. dropping the
    ``getattr(exc.orig, "sqlstate", None) == QUERY_CANCELED_SQLSTATE`` condition
    in ``_find_statement_timeout`` and returning ``exc`` for any
    ``OperationalError``, or deleting the ``if timed_out is None: raise exc``
    guard in ``operational_error_handler`` — the 504 test above would STILL PASS.
    Only this arm catches it. Mislabelling a dead/shutdown connection as "the
    query exceeded its time budget" is exactly the mistake the 504 exists to
    prevent: it tells the AA conveyor to retry a request that failed for a reason
    retrying will not fix.

    KILLING MUTATION: make ``_find_statement_timeout`` return ``exc`` for any
    ``OperationalError`` (ignore the SQLSTATE) -> this test goes RED at 504,
    while test_bare_statement_timeout_returns_504 stays green.
    """
    resp = await _get_boom(_app_raising(_non_timeout_error()))

    assert resp.status_code == 500, resp.text
    body = resp.json()
    _assert_envelope(body, expected_status=500)
    assert body["error"]["code"] == "INTERNAL_SERVER_ERROR", body
    assert body["error"]["code"] != "STATEMENT_TIMEOUT", body


async def test_group_wrapped_statement_timeout_returns_504() -> None:
    """A 57014 OperationalError raised inside an ExceptionGroup still renders 504.

    This is the TaskGroup shape: ``ExceptionMiddleware`` matches handlers on the
    exception's MRO, and an ``ExceptionGroup`` is not an ``OperationalError``, so
    the concrete handler never sees it. It reaches
    ``global_exception_handler``, whose group branch calls
    ``_find_statement_timeout`` recursively. Observable as a RESPONSE only
    because the transport is built with ``raise_app_exceptions=False``;
    ``ServerErrorMiddleware`` re-raises after sending.

    KILLING MUTATION: delete the ``timed_out = _find_statement_timeout(exc)`` /
    ``if timed_out is not None: return _statement_timeout_to_response(...)``
    branch from ``global_exception_handler`` -> the group falls to the generic
    arm and answers 500 INTERNAL_SERVER_ERROR -> this test goes RED.
    """
    group = ExceptionGroup("task group failed", [_timeout_error()])
    resp = await _get_boom(_app_raising(group))

    assert resp.status_code == 504, resp.text
    body = resp.json()
    _assert_envelope(body, expected_status=504)
    assert body["error"]["code"] == "STATEMENT_TIMEOUT", body


async def test_nested_group_wrapped_statement_timeout_returns_504() -> None:
    """Nesting is walked, not just the first level — a group inside a group still 504s.

    ``_find_statement_timeout`` recurses; a single-level check would satisfy the
    test above but not this one.

    KILLING MUTATION: replace the recursive ``_find_statement_timeout(sub)`` call
    inside the ``BaseExceptionGroup`` loop with a flat
    ``isinstance(sub, OperationalError)`` check -> the inner group is skipped,
    the response is 500 -> this test goes RED while the flat group test above
    stays green.
    """
    inner = ExceptionGroup("inner", [_timeout_error()])
    outer = ExceptionGroup("outer", [inner])
    resp = await _get_boom(_app_raising(outer))

    assert resp.status_code == 504, resp.text
    assert resp.json()["error"]["code"] == "STATEMENT_TIMEOUT", resp.text


async def test_504_envelope_request_id_matches_response_header() -> None:
    """The 504 envelope carries the SAME request_id the middleware echoed.

    Covers requirement 4 end-to-end: the timeout response is not a bespoke body,
    it is the standard envelope keyed to this request. ``_request_id_from``
    falls back to a freshly minted uuid7 when ``request.state.request_id`` is
    absent, so a shape-only assertion would pass even if the handler ignored the
    request entirely — comparing against ``X-Request-ID`` is what makes it bind.

    KILLING MUTATION: in ``_request_id_from`` return ``str(uuid.uuid7())``
    unconditionally -> the envelope id no longer matches the header -> RED.
    """
    resp = await _get_boom(_app_raising(_timeout_error()))

    assert resp.status_code == 504, resp.text
    header_id = resp.headers.get("X-Request-ID", "")
    assert UUID7_RE.match(header_id), header_id
    assert resp.json()["error"]["request_id"] == header_id, resp.text
