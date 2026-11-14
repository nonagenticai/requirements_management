"""A pooled DB connection must be released BEFORE the response is sent.

WHY THIS FILE EXISTS
--------------------
FastAPI 0.137.1 runs a path operation inside two nested exit stacks
(``fastapi/routing.py:125-130``)::

    async with AsyncExitStack() as request_stack:          # "request" scope
        scope["fastapi_inner_astack"] = request_stack
        async with AsyncExitStack() as function_stack:     # "function" scope
            scope["fastapi_function_astack"] = function_stack
            response = await f(request)
        await response(scope, receive, send)               # <-- body pumped HERE

A ``yield`` dependency is torn down on whichever stack its ``Depends`` marker
names: ``fastapi/dependencies/utils.py:670`` picks ``function_astack`` only when
``sub_dependant.scope == "function"``, and ``dependencies/models.py:188-192``
makes a generator dependency default to ``"request"`` when no scope is given.

So with the default (``Depends(get_session)``) the session is closed on the
REQUEST stack — i.e. after ``await response(...)`` returns. And
``starlette/responses.py:163-170`` runs ``await self.background()`` *inside*
``Response.__call__``, after the body messages. The pooled connection is
therefore still checked out while the response body is written AND for the whole
duration of every ``BackgroundTasks`` job. Measured in production at up to
176.7s for a background LLM eval, which pins all 15 pool slots and turns into
30s ``pool_timeout`` errors and HTTP 500s for unrelated traffic.

``Depends(get_session, scope="function")`` moves the teardown onto the function
stack, which closes before the body is pumped and before background tasks run.

WHAT THIS FILE PROVES — AND WHAT IT DOES NOT
--------------------------------------------
Both tests measure the SAME quantity — ``engine.pool.checkedout()`` on a
dedicated, otherwise-idle engine — at the two moments that matter, and both are
parametrised over BOTH scopes. Each asserts the request-scope value is **1**
(connection still held) and the function-scope value is **0** (already
released). There is no ``sleep``-then-assert anywhere; the only evidence is the
recorded integer.

PROVES: FastAPI's *mechanism*. At the background-task moment and at the
``http.response.body`` moment, a request-scoped session still holds its pooled
connection and a function-scoped one does not. The request-scope leg is the
control that keeps the function-scope 0 from being a broken probe.

DOES NOT PROVE: that this repo's routers actually use function scope. This file
builds its own hermetic one-route app and hardcodes BOTH scopes itself, so it
never reads a single ``src/`` call site. ⛔ Reverting ``scope="function"`` from
all 176 ``src/`` sites leaves this file 4/4 GREEN (measured). It must therefore
NEVER be cited as the guard against that regression. The guards that do cover it
are:

* ``tests/test_session_scope_is_function.py`` — every ``Depends(get_session)``
  call site in the SOURCE TEXT under ``src/`` (AST).
* ``tests/test_session_scope_effective_on_every_route.py`` — every session
  dependant on the BUILT app object, i.e. what FastAPI actually parsed.
"""

import os

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db import get_session

# ---------------------------------------------------------------------------
# The two scopes, and what each one MUST record
# ---------------------------------------------------------------------------
# ``{}`` is today's production shape: a bare ``Depends(get_session)``, whose
# computed scope is "request". ``{"scope": "function"}`` is the fix. Every test
# below runs both, so a single run demonstrates the mechanism in both
# directions — the request-scope case is the control that proves the
# function-scope 0 is a real release and not a broken probe.
SCOPE_CASES = [
    pytest.param({}, 1, id="request_scope_still_holds_the_connection"),
    pytest.param({"scope": "function"}, 0, id="function_scope_already_released_it"),
]


@pytest_asyncio.fixture
async def probe_engine(test_engine):
    """A dedicated, otherwise-idle engine so ``checkedout()`` is unambiguous.

    Deliberately NOT ``db.engine``: the module-level engine is shared with the
    real app and with anything else in-process, so a count taken from it would
    measure the whole process rather than this one request.

    The DSN comes from the suite's own resolution — ``tests/conftest.py`` picks
    ``TEST_DATABASE_URL`` -> ``DATABASE_URL`` -> an ephemeral docker postgres at
    import time (applying the xdist per-worker ``search_path`` rewrite) and
    writes the winner back into ``os.environ``. Reading it here reuses that
    exact resolution without re-importing conftest (whose import has side
    effects: RSA keygen and the spaCy warm-up).

    Depends on ``test_engine`` only to inherit its "postgres unreachable ->
    FAIL" gate.
    """
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        pool_size=2,
        max_overflow=0,
        # Fail fast rather than hang if a leak means the slot never comes back.
        # This is a guard, not a measurement — nothing is asserted on time.
        pool_timeout=5,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


def _build_app(engine, scope_kwargs: dict, bg_recorder: list[int]) -> FastAPI:
    """Build a hermetic one-route app whose session dependency uses ``engine``.

    The route declares the REAL ``db.get_session`` symbol in its ``Depends``
    marker (so the scope semantics under test are the production ones) while
    ``dependency_overrides`` swaps the callable for one bound to the dedicated
    engine. FastAPI carries ``scope`` from the original marker across the
    override (``dependencies/utils.py:640-646`` re-derives the dependant with
    ``scope=sub_dependant.scope``), so the override does not weaken the test.
    """
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_session():
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.dependency_overrides[get_session] = _override_get_session

    session_dep = Depends(get_session, **scope_kwargs)

    @app.get("/probe")
    async def probe(bg: BackgroundTasks, session: AsyncSession = session_dep):
        # Actually use the session, or no connection is ever checked out and
        # both scopes would trivially record 0.
        await session.execute(text("SELECT 1"))

        async def _record_checkedout_at_background_time() -> None:
            # FIRST statement: the count must describe the moment the background
            # job starts, before it does anything of its own.
            bg_recorder.append(engine.pool.checkedout())

        bg.add_task(_record_checkedout_at_background_time)
        return {"ok": True}

    return app


def _recording_send_wrapper(app, engine, body_recorder: list[int]):
    """Wrap an ASGI app so each ``http.response.body`` records ``checkedout()``."""

    async def wrapped(scope, receive, send):
        async def _send(message):
            if message["type"] == "http.response.body":
                body_recorder.append(engine.pool.checkedout())
            await send(message)

        await app(scope, receive, _send)

    return wrapped


@pytest.mark.parametrize(("scope_kwargs", "expected_checkedout"), SCOPE_CASES)
async def test_session_is_released_before_background_tasks_run(
    probe_engine, scope_kwargs, expected_checkedout
):
    """C1 — the background-task limb.

    ``BackgroundTasks`` run inside ``Response.__call__``, which is inside the
    request exit stack. A request-scoped session is therefore still holding its
    pooled connection for the entire background job; a function-scoped one is
    not.
    """
    bg_recorder: list[int] = []
    app = _build_app(probe_engine, scope_kwargs, bg_recorder)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://probe") as client:
        response = await client.get("/probe")

    # If the route had errored, the recorder could be empty or read 0 and the
    # function-scope case would pass vacuously.
    assert response.status_code == 200, response.text
    assert len(bg_recorder) == 1, (
        f"the background task did not run exactly once: {bg_recorder!r}. "
        "ASGITransport awaits the full ASGI call, so background tasks have "
        "completed by the time the client returns — an empty list means the "
        "task never ran, not that it is still pending."
    )

    assert bg_recorder[0] == expected_checkedout, (
        f"with Depends(get_session, {scope_kwargs or 'no scope'}) the pool showed "
        f"{bg_recorder[0]} connection(s) checked out at the START of the background "
        f"task, expected {expected_checkedout}. "
        "1 = the connection is pinned for the whole background job (the production "
        "defect: a 176.7s LLM eval holds a pool slot). "
        "0 = teardown ran on the function exit stack, before the response was sent."
    )


@pytest.mark.parametrize(("scope_kwargs", "expected_checkedout"), SCOPE_CASES)
async def test_session_is_released_before_the_response_body_is_pumped(
    probe_engine, scope_kwargs, expected_checkedout
):
    """C2 — the response-pump limb.

    ``routing.py:125-130`` sends the body via ``await response(scope, receive,
    send)`` *after* the function stack has closed but *inside* the request
    stack. Measuring at the first ``http.response.body`` message therefore
    separates the two scopes exactly.
    """
    body_recorder: list[int] = []
    bg_recorder: list[int] = []
    app = _build_app(probe_engine, scope_kwargs, bg_recorder)

    transport = ASGITransport(app=_recording_send_wrapper(app, probe_engine, body_recorder))
    async with AsyncClient(transport=transport, base_url="http://probe") as client:
        response = await client.get("/probe")

    assert response.status_code == 200, response.text
    assert body_recorder, (
        "no http.response.body message was observed — the send wrapper never "
        "fired, so nothing was measured."
    )

    assert body_recorder[0] == expected_checkedout, (
        f"with Depends(get_session, {scope_kwargs or 'no scope'}) the pool showed "
        f"{body_recorder[0]} connection(s) checked out while the FIRST response body "
        f"message was written, expected {expected_checkedout}. "
        "1 = the session outlives the response (request exit stack). "
        "0 = the session closed on the function exit stack, before the body."
    )
