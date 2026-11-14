"""A query that will not finish must give its connection BACK, not keep it.

Why this file exists
--------------------
Until 2026-09-04 no engine in ``src/db.py`` set ``statement_timeout``. There was
therefore no upper bound of any kind on how long a checkout could last: a
statement that never completes never returns its pooled connection, and the pool
loses that slot for the life of the process. ``091f684:docs/incidents/2026-09-03-pool-exhaustion.md``
records what that costs on this Deployment — ``replicas: 1``, so once the pool is
gone the readiness probe misses three times, the pod is removed from its own
EndpointSlice and the Service has NO endpoint at all.

The trigger is not hypothetical. ``GET /architecture/elements/{eid}/ancestors``
runs a recursive CTE with no cycle break, so a single ``parent_id`` cycle spins
in Postgres forever; each such request burns one of the 15 slots permanently.

What is asserted, and what each leg can catch
---------------------------------------------
* ``test_a_runaway_statement_is_aborted`` — the bound exists at all and is the
  server aborting the statement (SQLSTATE 57014), not the client hanging.
* ``test_the_connection_comes_back_after_the_abort`` — **the load-bearing one.**
  The point of the timeout is not the error; it is that the slot is released. A
  timeout that raised while leaking the checkout would fix nothing and would pass
  the first test.
* ``test_every_watched_engine_carries_its_own_timeout`` — the bound is registered
  on every engine and each one carries ITS OWN value. A single global value —
  the obvious "simplification" — is a regression: it would either lift the
  health probe's bound above ``routers/health.READY_BUDGET_S`` (3.0s) or cut the
  request path's bound to the probe's.

Assertions are on SQLSTATE, on ``pool.checkedout()`` and on ``pg_settings`` —
never on elapsed time. This box runs ~30 concurrent agent sessions and any
timing threshold flakes.

Needs a reachable Postgres, like every other DB-backed file here: the repo's
autouse ``_clean_tables`` fixture already requires one.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import db
from db import register_statement_timeout

# Postgres `query_canceled` — the abort `statement_timeout` produces. Asserting on
# this rather than on `pytest.raises(OperationalError)` alone is what stops an
# unrelated failure (a dropped connection, a syntax error) from passing as if the
# timeout had fired.
QUERY_CANCELED = "57014"

# Short enough that the whole file costs well under a second of real waiting,
# while `SLEEP_S` stays an order of magnitude above it so the difference between
# "aborted" and "ran to completion" is unmissable.
SHORT_TIMEOUT_MS = 500
SLEEP_S = 5

# The values `src/db.py` states, per engine. Written out here rather than imported
# from `db`, deliberately: importing the constants would make this test agree with
# whatever the module says, including a wrong value. These are the numbers the
# comments in `db.py` argue for, and 2500 in particular must stay under
# `routers/health.READY_BUDGET_S` (3.0s).
EXPECTED_TIMEOUT_MS = {
    "runtime": 10000,
    "health": 2500,
}


@pytest_asyncio.fixture
async def bounded_engine():
    """An engine with a deliberately tiny statement bound and a ONE-slot pool.

    ``pool_size=1, max_overflow=0`` is what makes the release assertion mean
    something: with a single slot, a leaked checkout is not a statistic, it is
    the whole pool.
    """
    engine = create_async_engine(
        db.DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
    )
    register_statement_timeout(engine, SHORT_TIMEOUT_MS, label="test")
    yield engine
    await engine.dispose()


async def test_a_runaway_statement_is_aborted(bounded_engine):
    """A statement past the bound is cancelled by the server, not awaited.

    KILLING MUTATION: ``remove-set-local`` — delete the
    ``@event.listens_for(engine.sync_engine, "begin")`` registration inside
    ``db.register_statement_timeout`` (or the ``set_config`` call in its body).
    ``pg_sleep`` then runs to completion, no exception is raised, and
    ``pytest.raises`` fails. Nothing else in the suite goes red for that mutation,
    which is exactly why this leg is here.
    """
    with pytest.raises(OperationalError) as caught:
        async with bounded_engine.connect() as conn:
            await conn.execute(text(f"SELECT pg_sleep({SLEEP_S})"))

    assert getattr(caught.value.orig, "sqlstate", None) == QUERY_CANCELED


async def test_the_connection_comes_back_after_the_abort(bounded_engine):
    """The slot is RELEASED by the abort — the reason the timeout exists at all.

    A `statement_timeout` that raised while the checkout leaked would satisfy the
    test above and still lose the pool one connection per runaway query, which is
    the defect verbatim. So the oracle is the pool counter, plus a following query
    that can only succeed if the single slot really came back.
    """
    pool = bounded_engine.pool

    # Warm the pool first, so `before` is a steady-state number rather than the
    # zero of a pool that has never opened a connection.
    async with bounded_engine.connect() as warm:
        await warm.execute(text("SELECT 1"))
    before = pool.checkedout()

    with pytest.raises(OperationalError):
        async with bounded_engine.connect() as conn:
            await conn.execute(text(f"SELECT pg_sleep({SLEEP_S})"))

    assert pool.checkedout() == before

    async with bounded_engine.connect() as after:
        assert (await after.execute(text("SELECT 1"))).scalar_one() == 1


async def test_every_watched_engine_carries_its_own_timeout():
    """Each engine is bound, and bound to ITS OWN value.

    Read back from ``pg_settings`` inside a real transaction on each engine, which
    is the only evidence that the listener actually fires: a registration that is
    present but never reached, or reached on the wrong connection, reports 0 here.
    ``pg_settings.setting`` for ``statement_timeout`` is in milliseconds, so the
    comparison is exact and needs no unit parsing.

    The distinctness assertion at the end is the anti-regression leg: collapsing
    the values into one global ``statement_timeout`` would keep every other
    assertion in this file passing.
    """
    watched = db.watched_engines()
    assert {label for label, _ in watched} == {"runtime", "health"}

    measured: dict[str, str] = {}
    for label, engine in watched:
        async with engine.begin() as conn:
            result = await conn.execute(
                text("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
            )
            measured[label] = result.scalar_one()

    assert measured == {label: str(EXPECTED_TIMEOUT_MS[label]) for label, _ in watched}
    assert measured["runtime"] != measured["health"]


# A value nothing else in this repo, in Postgres' defaults, or in any engine
# `db.py` builds could produce. If it is still on the connection after COMMIT it
# came from OUR listener and from nowhere else, which is what makes the readback
# below attributable rather than circumstantial.
LEAK_PROBE_TIMEOUT_MS = 777


async def test_the_bound_is_unwound_at_commit_and_does_not_ride_the_connection_on():
    """After COMMIT the connection carries the server default, NOT our value.

    KILLING MUTATION: ``is_local-false`` — flip the third argument of
    ``set_config('statement_timeout', %s, true)`` in ``db.register_statement_timeout``
    to ``false``. That is a session-level ``SET`` instead of ``SET LOCAL``: the
    value survives the transaction, and because this service's DSN terminates at
    ``pgbouncer.example.com:6432`` in **transaction** pool mode the
    server connection is handed to the NEXT client at COMMIT still carrying it. The
    victim is some other tenant's query, aborted at OUR timeout — a setting leak,
    not a timeout. Measured both ways on this file: ``true`` -> ``30s`` after the
    commit, ``false`` -> ``777ms``.

    ⛔ THE READBACK MUST BYPASS SQLAlchemy. ``conn.execute(text("SHOW ..."))`` after
    the commit AUTOBEGINS a new transaction, which re-fires the very ``begin``
    listener under test and re-binds 777ms — so it reports ``777ms`` under BOTH arms
    and is worth nothing as an oracle (measured, not assumed). The question "what is
    on this connection between transactions" can only be asked of the DBAPI
    connection directly, which is what ``get_raw_connection().driver_connection``
    reaches.

    This is the leg the existing ``remove-set-local`` mutation cannot reach: deleting
    the registration outright is a strictly WEAKER mutant. It makes the timeout
    ABSENT, which the two tests above catch; it does not make the timeout LEAK,
    which is the hazard the 18-line "⛔ DO NOT SIMPLIFY IT" comment in ``db.py`` is
    about, and which nothing in this file saw until now.
    """
    leaked = f"{LEAK_PROBE_TIMEOUT_MS}ms"

    # What an UNBOUND connection to this same database carries — measured, not
    # hardcoded, because the server default is a property of the deployment (30s
    # here, `0` on a stock postgres) and asserting `!= ''` would pass under the
    # mutant, which is the whole failure this test exists to avoid.
    plain = create_async_engine(db.DATABASE_URL, poolclass=NullPool)
    try:
        async with plain.connect() as conn:
            server_default = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
    finally:
        await plain.dispose()

    # Self-check: the probe value must be distinguishable from the default, or the
    # assertion at the end would be vacuous rather than strict.
    assert server_default != leaked

    engine = create_async_engine(db.DATABASE_URL, pool_size=1, max_overflow=0, pool_timeout=5)
    register_statement_timeout(engine, LEAK_PROBE_TIMEOUT_MS, label="leak-probe")
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                bound = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
            # The bound really was applied inside the transaction — otherwise the
            # readback below would be trivially satisfied by a listener that never
            # ran, and this test would pass for the wrong reason.
            assert bound == leaked

            # Same physical connection, now between transactions, asked through the
            # driver so no `begin` listener can fire. In transaction pool mode this
            # is the exact state pgbouncer hands to the next client.
            driver_conn = (await conn.get_raw_connection()).driver_connection
            # Not defensive padding: `driver_connection` is Optional, and a None
            # here would mean the readback never happened at all.
            assert driver_conn is not None
            cursor = driver_conn.cursor()
            await cursor.execute("SHOW statement_timeout")
            after_commit = (await cursor.fetchone())[0]
            await driver_conn.rollback()
    finally:
        await engine.dispose()

    assert after_commit != leaked
    assert after_commit == server_default
