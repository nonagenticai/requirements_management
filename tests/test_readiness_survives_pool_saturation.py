"""`/health/ready` must report database REACHABILITY, never database LOAD.

Why this file exists
--------------------
Until 2026-09-03 the readiness handler was
``async def readiness(session: AsyncSession = Depends(get_session))`` — it drew a
connection from the SAME pool as request traffic. That made a load condition
indistinguishable from a failure condition, and on this Deployment
(``replicas: 1``, ``strategy: Recreate``, readiness ``timeoutSeconds: 5,
periodSeconds: 10, failureThreshold: 3``) the difference is an outage:

    pool saturates -> probe cannot be served -> 3 probe misses -> the pod is
    removed from its own EndpointSlice -> the Service now has NO endpoint ->
    in-flight callers get `read ECONNRESET` / `socket hang up` -> the retries
    those resets provoke arrive at the same saturated pool.

MEASURED on ``requirements-c4f5cdd7d-fcgmw``, 2026-09-03: ten readiness probes
over 5s in two episodes, THREE returning 503 at exactly **30.00s** — SQLAlchemy's
``pool_timeout``, not a database failure. The ``Readiness probe failed ... context
deadline exceeded`` event carried count 302. In the same 24h window
``AA Activity - Expansion`` recorded 31 ``socket hang up`` and 22
``read ECONNRESET``, and its ``Format Output`` guard threw
``[expansion] dimension(s) wholly failed on transport, 0 created``.

pgbouncer and Postgres were measured and RULED OUT as the constraint
(``cl_waiting 0``, ``maxwait 0``, avg query 3.7-20ms, ZERO ``idle in
transaction``), as were CPU throttling (3.9% of limit) and event-loop blocking
(zero ``event_loop.blocked`` warnings). The connections were checked out by this
process while idle at the server.

What these tests pin, and why each one can fail
-----------------------------------------------
The obvious way to "fix" this badly is to make readiness always succeed — delete
the DB check, or swallow every error. That would route traffic to a pod that
cannot serve it. So the negative leg is asserted as hard as the positive one:

* ``test_down_still_returns_503`` fails if the handler degenerates into
  "always ready".
* ``test_saturation_is_reported_not_fatal`` fails if saturation is turned back
  into a 503.
* ``test_readiness_does_not_take_a_request_pool_session`` fails if anyone
  reintroduces ``Depends(get_session)``, which is the defect itself.
* ``test_saturation_predicate_is_false_when_there_is_headroom`` is the vacuity
  control: a predicate that always says "saturated" would pass every other test
  here while being useless.

Every leg drives the real functions against fakes — but note the repo's autouse
`_clean_tables` fixture still TRUNCATEs, so this file needs a reachable test
database like every other. The claim is 'no PRODUCTION database', not 'no database'.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SAPoolTimeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from routers import health as health_mod  # noqa: E402


class _FakeConn:
    """Minimal async context manager standing in for `AsyncConnection`."""

    def __init__(self, on_execute=None):
        self._on_execute = on_execute

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        if self._on_execute is not None:
            return await self._on_execute()
        return None


class _FakeEngine:
    def __init__(self, connect_impl):
        self._connect_impl = connect_impl

    def connect(self):
        return self._connect_impl()


def _op_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("could not connect"))


# ── probe_database: the three verdicts, each for its own reason ───────────────


@pytest.mark.asyncio
async def test_health_pool_checkout_timeout_is_down_not_ready(monkeypatch):
    """A checkout timeout on the DEDICATED pool means the database is hung.

    Nothing but the probe uses `health_engine` (`pool_size=1, max_overflow=0`), so
    request traffic cannot cause this — only a previous probe still holding the
    single slot can, which is evidence of DB trouble. An earlier draft mapped it
    to "busy -> ready", which let a pod with a blackholed DSN answer 200
    `{"database": true}` forever whenever a second poller interleaved with the
    kubelet. This is the regression pin for that.
    """

    def _connect():
        raise SAPoolTimeout("QueuePool limit of size 1 overflow 0 reached")

    monkeypatch.setattr(health_mod, "health_engine", _FakeEngine(_connect))
    assert await health_mod.probe_database() == health_mod.DB_DOWN


@pytest.mark.asyncio
async def test_unreachable_database_is_down(monkeypatch):
    def _connect():
        raise _op_error()

    monkeypatch.setattr(health_mod, "health_engine", _FakeEngine(_connect))
    assert await health_mod.probe_database() == health_mod.DB_DOWN


@pytest.mark.asyncio
async def test_budget_expiry_is_down(monkeypatch):
    """A `SELECT 1` on an UNCONTENDED connection that cannot finish is DOWN.

    The dedicated single-slot pool is what makes that inference sound: nothing
    else can be occupying it, so slowness here is the database, not contention.
    The builtin `TimeoutError` that `asyncio.timeout` raises subclasses `OSError`,
    so this also pins the catch ordering.
    """

    async def _slow():
        await asyncio.sleep(5)

    monkeypatch.setattr(health_mod, "READY_BUDGET_S", 0.05)
    monkeypatch.setattr(health_mod, "health_engine", _FakeEngine(lambda: _FakeConn(_slow)))
    assert await health_mod.probe_database() == health_mod.DB_DOWN


# ── readiness(): the HTTP contract, both directions ───────────────────────────


@pytest.mark.asyncio
async def test_a_saturated_runtime_pool_is_reported_not_fatal(monkeypatch):
    """A full RUNTIME pool must stay 200. Removing the only replica helps nobody."""

    async def _ok():
        return health_mod.DB_OK

    monkeypatch.setattr(health_mod, "probe_database", _ok)
    monkeypatch.setattr(health_mod, "pool_stats", lambda: {"pool_checkedout": 15})
    monkeypatch.setattr(health_mod, "pool_is_saturated", lambda _s: True)

    body = await health_mod.readiness()
    assert body["status"] == "ready"
    assert body["checks"]["database"] is True
    assert body["checks"]["database_busy"] is True


@pytest.mark.asyncio
async def test_saturation_never_rescues_an_unreachable_database(monkeypatch):
    """The inversion pin, stated as its own leg.

    Saturation is reported alongside the verdict; it must never SUBSTITUTE for it.
    A saturated pool AND an unreachable database is still 503 — otherwise the
    "busy is not broken" principle becomes "busy hides broken".
    """

    async def _down():
        return health_mod.DB_DOWN

    monkeypatch.setattr(health_mod, "probe_database", _down)
    monkeypatch.setattr(health_mod, "pool_stats", lambda: {"pool_checkedout": 15})
    monkeypatch.setattr(health_mod, "pool_is_saturated", lambda _s: True)

    with pytest.raises(HTTPException) as exc:
        await health_mod.readiness()
    assert exc.value.status_code == 503
    assert exc.value.detail["checks"]["database"] is False


@pytest.mark.asyncio
async def test_down_still_returns_503(monkeypatch):
    """The mutation control: this fails if the fix becomes 'always ready'."""

    async def _down():
        return health_mod.DB_DOWN

    monkeypatch.setattr(health_mod, "probe_database", _down)
    monkeypatch.setattr(health_mod, "pool_stats", lambda: {})
    monkeypatch.setattr(health_mod, "pool_is_saturated", lambda _s: False)

    with pytest.raises(HTTPException) as exc:
        await health_mod.readiness()
    assert exc.value.status_code == 503
    assert exc.value.detail["status"] == "not ready"
    assert exc.value.detail["checks"]["database"] is False


@pytest.mark.asyncio
async def test_saturated_runtime_pool_is_flagged_even_when_the_probe_is_fast(monkeypatch):
    """The probe has its OWN pool, so it answers fast while the app is starving.

    That is the point of the split — but it would make saturation invisible if the
    handler reported only its own verdict. The runtime pool is read separately.
    """

    async def _ok():
        return health_mod.DB_OK

    monkeypatch.setattr(health_mod, "probe_database", _ok)
    monkeypatch.setattr(health_mod, "pool_stats", lambda: {"pool_checkedout": 15})
    monkeypatch.setattr(health_mod, "pool_is_saturated", lambda _s: True)

    body = await health_mod.readiness()
    assert body["status"] == "ready"
    assert body["checks"]["database_busy"] is True


# ── structural: the defect itself must not come back ──────────────────────────


def test_readiness_does_not_take_a_request_pool_session():
    """The defect WAS the signature. Pin it there.

    A behavioural test cannot see this: a handler that takes `Depends(get_session)`
    behaves identically until the pool is full, which is exactly when no test is
    watching.
    """
    import ast
    import inspect

    sig = inspect.signature(health_mod.readiness)
    assert sig.parameters == {}, (
        "readiness must not accept an injected session — that is what put the probe "
        f"in the request pool's queue. Got: {list(sig.parameters)}"
    )

    # AST, not a substring. The docstring of `health.py` explains the defect and
    # therefore CONTAINS the words `Depends(get_session)`; a grep-shaped assertion
    # fails on its own explanation. Assert on what the module actually EXECUTES:
    # no import of `get_session`, and no reference to that name anywhere in code.
    src = (Path(__file__).resolve().parents[1] / "src" / "routers" / "health.py").read_text()
    tree = ast.parse(src)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "get_session" not in imported, "routers/health.py must not import get_session"
    assert "get_session" not in referenced, "routers/health.py must not reference get_session"
    aliased_runtime_engine = [
        alias
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "engine"
    ]
    assert not aliased_runtime_engine, (
        "routers/health.py imports the RUNTIME `engine`. Aliasing it as "
        "`health_engine` reintroduces the exact defect while every behavioural "
        "test still passes — the probe is back in the request pool's queue"
    )
    assert "Depends" not in imported | referenced, (
        "readiness must not use FastAPI dependency injection for its DB handle — "
        "the checkout has to happen inside the handler's own try, or a pool timeout "
        "becomes an unhandled 500 during dependency resolution instead of a verdict"
    )


# ── the saturation predicate, both directions ─────────────────────────────────


def test_saturation_predicate_fires_at_the_ceiling():
    from db import MAX_OVERFLOW, POOL_SIZE, pool_is_saturated

    ceiling = POOL_SIZE + MAX_OVERFLOW
    assert pool_is_saturated({"pool_checkedout": ceiling}) is True
    assert pool_is_saturated({"pool_checkedout": ceiling - 1}) is True  # headroom=1


def test_saturation_predicate_is_false_when_there_is_headroom():
    """Vacuity control: a predicate that always fires would pass every test above."""
    from db import pool_is_saturated

    assert pool_is_saturated({"pool_checkedout": 0}) is False
    assert pool_is_saturated({}) is False, "unreadable counters must not manufacture an alarm"


# ── the dedicated pool must actually BE dedicated, and actually be size 1 ─────


def test_the_health_engine_is_not_the_runtime_engine():
    """`health_engine is engine` passes every behavioural test and fixes nothing.

    An adversarial review reverted the fix with a one-line `from db import engine
    as health_engine` and the whole suite stayed green. Identity is the property
    that matters, so identity is what is asserted.
    """
    import db

    assert db.health_engine is not db.engine, (
        "the readiness probe shares the runtime engine — it is back in the same "
        "queue as request traffic, which is the defect this change exists to fix"
    )


def test_the_health_pool_is_one_uncontended_slot():
    """Size is load-bearing, not decoration.

    `pool_size=1, max_overflow=0` is what makes "nothing but the probe can occupy
    this" true, and that fact is what licenses `probe_database` to read a checkout
    timeout as DB trouble rather than as request load. Delete the sizing and that
    inference silently stops holding.
    """
    import db

    pool = db.health_engine.pool
    assert pool.size() == 1, f"health pool size is {pool.size()}, expected 1"
    assert getattr(pool, "_max_overflow", 0) == 0, "health pool must not overflow"


# ── the saturation predicate across its whole range, not just its endpoints ───


def test_saturation_predicate_is_false_across_the_middle_of_the_range():
    """Endpoint-only assertions leave the ceiling unpinned.

    An adversarial mutation moved the ceiling from `POOL_SIZE + MAX_OVERFLOW` to
    `POOL_SIZE` — firing at 4/15 instead of 14/15, exactly the "gauge that cannot
    distinguish the condition" its own docstring warns about — and every test
    still passed, because only 0, 14 and 15 were ever asserted.
    """
    from db import MAX_OVERFLOW, POOL_SIZE, pool_is_saturated

    ceiling = POOL_SIZE + MAX_OVERFLOW
    assert ceiling >= 3  # the range below is empty when the env sizes the pool to 1
    for checkedout in range(0, ceiling - 1):
        assert pool_is_saturated({"pool_checkedout": checkedout}) is False, (
            f"{checkedout}/{ceiling} checked out reported as saturated — the "
            "ceiling has moved off POOL_SIZE + MAX_OVERFLOW"
        )


def test_readiness_reads_the_runtime_pool_not_its_own():
    """`pool_stats(health_engine)` passes every behavioural test and reports nothing.

    An adversarial reviewer made exactly that edit mid-session and all 19 tests
    stayed green. It is wrong quietly: `health_engine` is the probe's own
    `pool_size=1, max_overflow=0` pool, which nothing else can occupy, so
    `database_busy` would report whether the PROBE was busy — which is never —
    while the runtime pool that request traffic actually contends for went
    unreported. The flag would read healthy through the exact condition it names.

    Asserted on the call, not on behaviour, because there is no behaviour that
    distinguishes them: both pools answer, and only one of them answers about
    anything that matters.
    """
    import ast

    src = (Path(__file__).resolve().parents[1] / "src" / "routers" / "health.py").read_text()
    tree = ast.parse(src)
    readiness_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "readiness"
    )
    calls = [
        n
        for n in ast.walk(readiness_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "pool_stats"
    ]
    assert calls, "readiness no longer reads the pool counters at all"
    for call in calls:
        assert not call.args and not call.keywords, (
            "readiness passes an engine to pool_stats — it must read the DEFAULT "
            "(runtime) engine, which is the pool request traffic contends for. "
            "Passing health_engine reports the probe's own uncontended pool."
        )


# ── the checkout wait must OUTLAST the budget, not undercut it ────────────────────────────────
# KILLS: restoring `HEALTH_POOL_TIMEOUT_S`'s old default of 2 (or any value <= READY_BUDGET_S).
#
# The defect this pins, found by an adversarial review of the pool work and not by any test that
# existed: `health_engine` is `pool_size=1, max_overflow=0`, a probe may legitimately hold that
# one slot for its full `READY_BUDGET_S` (3.0s), and the checkout wait was 2.0s. So a second
# probe overlapping the first raised `SAPoolTimeout` after 2s and `probe_database()` mapped it to
# DB_DOWN — "the database is not answering" — on a database that was perfectly healthy and whose
# only sin was that another PROBE held the slot. `replicas: 1` + `failureThreshold: 3` turns three
# such overlaps into an eviction of the only pod: the readiness check causing the outage it
# exists to report. That is the same defect class the whole incident is about, one file over.
#
# ⚠️ This is asserted as an INEQUALITY, not against the literal 5. The number is free to move;
# the ordering is not, and the two constants cannot import each other (`routers/health` imports
# `db`), so nothing else can express the relationship.
def test_the_health_checkout_wait_outlasts_the_readiness_budget() -> None:
    import db as db_mod

    assert db_mod.HEALTH_POOL_TIMEOUT_S > health_mod.READY_BUDGET_S, (
        f"DB_HEALTH_POOL_TIMEOUT_S={db_mod.HEALTH_POOL_TIMEOUT_S} does not outlast "
        f"READY_BUDGET_S={health_mod.READY_BUDGET_S}. On a one-slot pool that means a probe "
        "overlapping another probe gives up on the CHECKOUT before its own budget expires, and "
        "reports the database DOWN for a queue it formed itself."
    )


# The companion property: when the connection itself never answers, the probe must spend its FULL
# budget before giving up — the outer `asyncio.timeout` is the authority, not some shorter inner
# deadline that abandons the attempt early and reports DOWN.
#
# ⚠️ HONEST SCOPE, measured rather than assumed. This test does NOT kill the timeout inversion
# above, and an earlier version of this comment claimed it did. Reverting
# `HEALTH_POOL_TIMEOUT_S` to 2 was run against it: this leg stayed GREEN (1 failed, 1 passed —
# only the inequality caught it). It cannot catch it, because it substitutes a `_FakeEngine` and
# so never touches the real pool's checkout wait at all. The inequality above is the only thing
# pinning the ordering; this leg pins that the budget is what bounds a hung connection.
async def test_an_occupied_slot_costs_the_budget_not_the_checkout_wait(monkeypatch) -> None:
    budget = 0.4
    monkeypatch.setattr(health_mod, "READY_BUDGET_S", budget)

    async def _never_returns():
        await asyncio.sleep(3600)

    monkeypatch.setattr(health_mod, "health_engine", _FakeEngine(lambda: _FakeConn(_never_returns)))

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await health_mod.probe_database()
    elapsed = loop.time() - started

    assert result == health_mod.DB_DOWN
    assert elapsed >= budget * 0.75, (
        f"the probe gave up after {elapsed:.3f}s against a {budget}s budget — it abandoned the "
        "attempt early, which is what a checkout wait shorter than the budget produces"
    )


# ── the per-engine occupancy diagnostic: useful, and never able to break the probe ──
#
# KILLS: deleting the `try/except Exception` around the `pool_watch.tally()` block in
# `routers/health.py::readiness`. The raising leg below then propagates out of the handler,
# `/health/ready` answers 500 instead of 200, and — on `replicas: 1`, `failureThreshold: 3` —
# three of those evict the only pod from its own EndpointSlice. A DIAGNOSTIC would have caused
# the outage the diagnostic exists to explain, which is the same defect class as the original
# `Depends(get_session)` probe one file over.


@pytest.mark.asyncio
async def test_the_reported_pools_come_from_the_real_holder_registry(monkeypatch):
    """`checks["pools"]` is fed by a REAL checkout, not a canned tally.

    Here an actual connection is taken from `db.engine`, whose checkout listener was
    installed as `pool_watch.install(engine, "runtime")`, and the probe is then
    asked what it sees. If the runtime engine ever stops being watched — or the
    key is fed from something other than the holder registry — its connections
    become invisible to the probe.

    Asserted as `>= 1`, never `== 1`: this suite shares one process, and another
    engine holding a connection concurrently is not this test's business.
    """
    import db as db_mod

    async def _ok():
        return health_mod.DB_OK

    monkeypatch.setattr(health_mod, "probe_database", _ok)
    monkeypatch.setattr(health_mod, "pool_stats", lambda: {"pool_checkedout": 1})
    monkeypatch.setattr(health_mod, "pool_is_saturated", lambda _s: False)

    async with db_mod.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        body = await health_mod.readiness()

    pools = body["checks"].get("pools", {})
    assert pools.get("runtime", 0) >= 1, (
        "a live connection checked out of db.engine is not reported in "
        f"checks['pools']: {pools}. Either the runtime engine is no longer registered "
        "with pool_watch, or this key is not fed by the holder registry"
    )
    assert all(isinstance(v, int) for v in pools.values()), f"non-int occupancy: {pools}"


@pytest.mark.asyncio
async def test_a_raising_tally_does_not_break_the_readiness_probe(monkeypatch):
    """A diagnostic must NEVER be able to fail the probe. This is the whole property.

    `readiness()` runs on a `replicas: 1` Deployment inside a 3s budget
    (`READY_BUDGET_S`), with `timeoutSeconds: 5, periodSeconds: 10,
    failureThreshold: 3`. An exception escaping the handler is a 500, and three of
    those remove the only pod from its own EndpointSlice — the measured 2026-09-03
    failure mode, where in-flight callers got 31 `socket hang up` and 22
    `read ECONNRESET` in 24h. `pool_watch.tally()` is a best-effort accounting of
    a weak-keyed registry mutated by pool listeners; it must be allowed to fail.

    So the contract is: the probe still ANSWERS, still answers `ready`, and simply
    omits the key it could not fill. An omitted diagnostic is an honest degraded
    reading; a 500 from a diagnostic is an outage.
    """
    import pool_watch

    async def _ok():
        return health_mod.DB_OK

    def _explode():
        raise RuntimeError("holder registry mutated during iteration")

    monkeypatch.setattr(health_mod, "probe_database", _ok)
    monkeypatch.setattr(health_mod, "pool_stats", lambda: {"pool_checkedout": 3})
    monkeypatch.setattr(health_mod, "pool_is_saturated", lambda _s: False)
    monkeypatch.setattr(pool_watch, "tally", _explode)

    body = await health_mod.readiness()

    assert body["status"] == "ready", (
        "a failure inside the per-engine tally changed the readiness VERDICT — a "
        "diagnostic is deciding whether this pod receives traffic"
    )
    assert body["checks"]["database"] is True
    assert "pools" not in body["checks"], (
        "the pools key was reported despite the tally raising: "
        f"{body['checks']}. It must be omitted, not filled with a guess"
    )
