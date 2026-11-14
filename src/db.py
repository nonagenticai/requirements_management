"""
Database configuration and session management.

This module defines a global SQLAlchemy ``engine`` using an asynchronous
driver. The ``get_session`` function yields database sessions to FastAPI
dependencies using an async context manager. The database URL is pulled
from the ``DATABASE_URL`` environment variable.
"""

import logging
import math
import os
from collections.abc import AsyncGenerator
from contextvars import ContextVar
from typing import Any

from sqlalchemy import Connection, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

import pool_watch
from fleet_auth import register_tenant_guc

# Request-scoped tenant for the RLS GUC. AuthMiddleware sets it (in
# _apply_principal) from the resolved principal; register_tenant_guc reads it at
# every transaction-begin and binds ``app.current_tenant``. A client-asserted
# value is never used here — only the middleware-resolved tenant.
tenant_id_var: ContextVar[str | None] = ContextVar("requirements_tenant_id", default=None)

# The database URL comes from the environment: the gitops Deployment in the
# cluster, tests/conftest.py under pytest. No default — an unset URL fails closed.
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("TEST_DATABASE_URL") or ""

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

# Convert postgresql:// to postgresql+psycopg:// for async psycopg3 driver
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)


# ── Pool sizing, chosen rather than inherited ────────────────────────────────
# These were SQLAlchemy's unconfigured defaults until 2026-09-03, which meant the
# ceiling that decides whether this pod stays in its own Service EndpointSlice was
# a number nobody had ever looked at. They are stated here, at today's values, so
# a change to them is a reviewable diff rather than an upstream default drifting.
#
# ⛔ DO NOT WIDEN THESE AS A FIX FOR SATURATION. Measured 2026-09-03: when this
# pool exhausted, pgbouncer showed `cl_waiting 0` / `maxwait 0` and Postgres saw
# avg 3.7-20ms queries with ZERO `idle in transaction` sessions. The connections
# were checked out by THIS PROCESS while idle at the server — i.e. held across a
# slow non-DB await. Widening a client pool whose server tier never waits moves
# the queue, it does not shorten it; it also raises the number of connections
# that can sit idle-in-checkout. Find the holder instead.
#
# `_env_num` rather than a bare `int(os.getenv(...))`: these are read at IMPORT,
# so a malformed value is not a bad setting, it is a pod that never starts. An
# empty string is the ordinary outcome of a Helm value or ConfigMap key that is
# present but unset, and `int("")` raises — turning a typo in a ConfigMap into a
# CrashLoopBackOff on a service whose whole problem is availability.
def _env_num(name: str, default: float, cast: Any = int, *, allow_zero: bool = False) -> Any:
    """Parse a positive, finite pool setting, falling back LOUDLY.

    ``allow_zero`` exists for the MAX_OVERFLOW settings only. Rejecting ``0``
    outright was wrong for them (found by an adversarial review, 2026-09-04): ``max_overflow=0``
    is a legitimate, BOUNDED setting — "this pool may not grow" — and it leaves
    ``pool_is_saturated``'s ceiling at ``pool_size``, which is correct. It is
    still refused for POOL_SIZE, where ``0`` means an UNBOUNDED pool and would
    give a gauge that thinks it is always full; that rejection is deliberate and
    stays.

    Three failures an adversarial review found in the first version, each of which
    defeats the point of stating these values at all:
      * `"nan"` / `"inf"` parse clean through `float()` — and
        `create_async_engine(pool_timeout=nan)` is ACCEPTED, so the setting is live
        and meaningless. (`int()` already rejected them; the float settings did not.)
      * `"5.7"` / `"0x10"` fell back with NO log line, so a typo'd ConfigMap
        silently reverted the value this block insists must be a reviewable diff.
      * `"0"` / `"-1"` were accepted outright. `pool_size=0` is an UNBOUNDED pool,
        while `pool_is_saturated` would still compute its ceiling from the 0 —
        a pool that can never be full and a gauge that thinks it always is.
    """
    raw = os.getenv(name, "")
    if not raw.strip():
        return cast(default)
    try:
        value = cast(raw)
        if not math.isfinite(float(value)):
            raise ValueError(f"not finite: {value!r}")
        floor = 0 if allow_zero else 1
        if float(value) < floor:
            raise ValueError(
                f"must be {'non-negative' if allow_zero else 'positive'}, got {value!r}"
            )
    except (TypeError, ValueError) as exc:
        logging.getLogger(__name__).warning(
            "db.pool_setting_ignored name=%s raw=%r reason=%s falling_back_to=%s",
            name,
            raw,
            exc,
            default,
        )
        return cast(default)
    return value


POOL_SIZE = _env_num("DB_POOL_SIZE", 5, int)
MAX_OVERFLOW = _env_num("DB_MAX_OVERFLOW", 10, int, allow_zero=True)
POOL_TIMEOUT_S = _env_num("DB_POOL_TIMEOUT_S", 30, float)

# ── A ceiling on how long a query may hold its connection ────────────────────
# ⛔ THIS IS THE SETTING THAT MAKES `pool_timeout` SURVIVABLE. Without it there
# is no upper bound at all on a checkout: a statement that never finishes never
# returns its connection, so the pool loses that slot permanently and the pod —
# `replicas: 1`, `limits.memory 512Mi` — walks into the exact readiness-eviction
# cascade `091f684:docs/incidents/2026-09-03-pool-exhaustion.md` records. That is not a
# hypothetical shape here: `GET /architecture/elements/{eid}/ancestors` runs a
# recursive CTE with no cycle break, so ONE `parent_id` cycle spins forever and
# burns a slot per request until all 15 are gone.
#
# 10s is chosen against two measured numbers, not picked for roundness.
#
# ⚠️ CORRECTED 2026-09-04 — an earlier version of this comment claimed "the p99
# statement on this service is 3.7-20 **ms**, so 10s is ~500x the slowest normal
# query". That evidence was wrong and an adversarial audit killed it against
# `pg_stat_statements` (window opening 2026-08-30): the `search_light` CTE runs
# **mean 215.7 ms over 6,845 calls, max 912 ms**, and the heaviest element
# SELECT peaks at **1,050 ms**. Real headroom is **~9.5x**, not ~500x. The
# CONCLUSION survives — 10s still cannot abort legitimate work — but anyone
# tuning `DB_STATEMENT_TIMEOUT_MS` must tune it against 1.05s, not against 20ms.
# Second number, unchanged: 10s is well under `pool_timeout` (30s), so a runaway
# hands its connection back BEFORE a checkout queue can form behind it.
# Ordering the two the other way round — a statement bound above the pool
# wait — would leave the queue forming first and the abort arriving after the
# damage, i.e. the timeout would be decorative.
STATEMENT_TIMEOUT_MS = _env_num("DB_STATEMENT_TIMEOUT_MS", 10000, int)


# ⛔ MECHANISM IS `SET LOCAL`, VIA A `begin` LISTENER. DO NOT "SIMPLIFY" IT.
#
# Two shapes look equivalent and are both wrong against THIS deployment, whose
# DSN terminates at `pgbouncer.example.com:6432` in **transaction**
# pool mode:
#
#   * `connect_args={"options": "-c statement_timeout=..."}` — a libpq STARTUP
#     parameter. pgbouncer negotiates startup parameters per client connection;
#     it either rejects an untracked one or partitions the server pool by its
#     value. Either way the setting is not reliably on the server that runs the
#     statement.
#   * a bare session-level `SET statement_timeout` — survives the transaction. In
#     transaction mode the server connection goes back to the pooler at COMMIT and
#     is handed to the NEXT client, carrying our value with it. That is a setting
#     leak across tenants of the pooler, not a timeout.
#
# `SET LOCAL` (spelled `set_config(..., is_local=true)`) is scoped to the
# transaction and unwound at its end, so it is PgBouncer-safe by construction.
# This mirrors `fleet_auth.register_tenant_guc` (vendored from kag_common), which
# binds `app.current_tenant` the same way on the same event for the same reason;
# the two listeners run back to back at every transaction begin.
def register_statement_timeout(engine: AsyncEngine, timeout_ms: int, *, label: str) -> None:
    """Bound every statement on ``engine`` at ``timeout_ms``, per transaction.

    Registers ONE ``begin`` listener on ``engine.sync_engine``, so the bound is
    correct-by-construction for every session path — ``get_session``, any direct
    ``sessionmaker()`` use, and raw ``engine.begin()`` — rather than something a
    new call site has to remember. ``label`` names the pool in the debug line so
    a wrong value is attributable to an engine rather than to "the database".

    The listener is attached to the ENGINE, not to ``engine.pool``: a pool object
    is replaced wholesale by ``dispose()`` and its listeners go with it silently
    (the hazard ``pool_watch.install`` documents at length). An engine outlives
    every pool it owns.

    Exceptions are deliberately NOT swallowed here, unlike in ``pool_watch``. A
    diagnostic that fails should degrade to no diagnostic; a SAFETY BOUND that
    fails must not degrade to an unbounded query that silently keeps the old
    behaviour back.
    """

    @event.listens_for(engine.sync_engine, "begin")
    def _set_statement_timeout(conn: Connection) -> None:
        # `set_config(..., true)` IS `SET LOCAL`, and is the parameterisable
        # spelling — `SET LOCAL statement_timeout = %s` takes no bind parameter.
        conn.exec_driver_sql(
            "SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_ms)),)
        )

    logging.getLogger(__name__).debug(
        "db.statement_timeout_registered engine=%s timeout_ms=%s", label, timeout_ms
    )


# ⛔ DELIBERATELY ABSENT: `idle_in_transaction_session_timeout`.
# `statement_timeout` bounds server-side statement EXECUTION only. If an
# idle-in-transaction holder is ever measured, fix the holder rather than adding
# a timeout that aborts work at random.

# ── ONE pool spec, applied to EVERY long-lived engine ────────────────────────
# This dict exists because a second engine (the since-removed owner engine)
# once took SQLAlchemy's unconfigured defaults (pool_size=5 + max_overflow=10)
# and silently doubled the ceiling against the same pgbouncer entry. Any new
# long-lived engine takes these arguments.
#
# `pool_pre_ping` / `pool_recycle` are STANDARDISATION AND HYGIENE, **NOT THE
# CAUSE AND NOT THE FIX**. ⛔ Do not read them as the explanation for the
# saturation: the stale-socket hypothesis — a pgbouncer roll poisoning every
# pooled connection — was REFUTED BY MEASUREMENT TODAY (2026-09-03). Nine
# QueuePool checkout-timeout episodes occurred inside a 13h window in which
# pgbouncer rolled ZERO times. The holders are found by `pool_watch`, not here.
# They are set because this repo was the fleet OUTLIER: `secrets/models.py` uses
# exactly `pool_pre_ping=True, pool_recycle=3600` (its comment naming PgBouncer
# restarts), and `skill_builder` and `improvement-loop-service` do the same.
# 3600 is a sane ceiling for the recycle: pgbouncer's `server_lifetime` is unset
# (so the 3600s default) and `server_idle_timeout=240`, i.e. the server side
# retires a connection no later than we do.
_POOL_KWARGS: dict[str, Any] = {
    "pool_size": POOL_SIZE,
    "max_overflow": MAX_OVERFLOW,
    "pool_timeout": POOL_TIMEOUT_S,
    "pool_pre_ping": True,
    "pool_recycle": 3600,
}

# Create the async engine. ``future=True`` enables SQLAlchemy 2.x style
# behaviour. ``echo=False`` suppresses SQL logging; set to True for debug.
engine = create_async_engine(
    DATABASE_URL,
    future=True,
    echo=False,
    **_POOL_KWARGS,
)

# Name every checkout's holder. Without this a QueuePool timeout says only that
# the pool was full, never WHICH await was sitting on the 15 connections.
pool_watch.install(engine, "runtime")

# Bind the RLS tenant GUC (``app.current_tenant``) on every transaction of the
# engine — one listener covers get_session + any direct AsyncSessionLocal() use,
# correct-by-construction. Inert until TENANT_RLS_MODE=enforce (+ the restricted
# non-owner role cutover); the owner bypasses ENABLE'd RLS today, so no-op now.
register_tenant_guc(engine, lambda: tenant_id_var.get(None))

# The request path's ceiling on a single statement. See STATEMENT_TIMEOUT_MS.
register_statement_timeout(engine, STATEMENT_TIMEOUT_MS, label="runtime")

# Factory for creating new async sessions
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# Base class for declarative models
class Base(DeclarativeBase):
    pass


class TenantSubjectMixin:
    """Per-user-within-tenant ownership type stub.

    Declares the ownership columns (``tenant_id``, ``tenant_name``,
    ``subject_id``, ``visibility``) that every owned model carries. No __init__
    logic — SQLAlchemy's default kwargs constructor handles assignment.

    ``tenant_id`` is annotated ``str`` but the COLUMN is ``sa.Uuid(as_uuid=False)``
    on all 22 owned tables — the annotation is the Python-side type, not a licence
    to store a name. A tenant name is an alias resolved at the request boundary
    (``src/auth/tenant_resolution.py``) and never written; the label lives in
    ``tenant_name``. Pinned by ``tests/test_tenant_id_is_always_a_uuid.py``.
    """

    tenant_id: str
    tenant_name: str
    subject_id: str
    visibility: str


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session for dependency injection.

    Routers bind this with ``Depends(get_session, scope="function")`` so the
    connection is returned to the pool the moment the path-operation function
    returns — before the response body is pumped and before any
    ``BackgroundTasks`` run. See ``091f684:docs/incidents/2026-09-03-pool-exhaustion.md``
    §10.6 and ``tests/test_session_released_before_response.py``.

    ⛔ WHY THE CLOSE IS NOT ``async with``, and why its errors are suppressed.

    Under function scope the teardown runs INSIDE the response path, before
    ``await response(scope, receive, send)``. So an exception raised by
    ``close()`` no longer happens after the body was sent and swallowed — it
    propagates into ``wrap_app_handling_exceptions`` and becomes a **500 on a
    request whose work already committed**.

    MEASURED on this branch before this guard existed: with the backend
    connection killed mid-request, the request-scoped route answered ``200``
    and the function-scoped route answered ``500`` — and BOTH rows were durably
    committed. The blast radius is every handler still holding an open
    transaction at return, i.e. most read-only GETs, and the trigger is any
    mid-request backend loss: a pgbouncer roll, a Postgres restart, an admin
    terminate. ``pool_pre_ping`` does not cover it — it pings at CHECKOUT.

    The harm is not the 500 itself. It is that a retrying caller (the AA
    conveyor does retry) re-sends a write that already landed.

    So a failure to hand the connection back is logged and dropped: the
    connection is being discarded either way, and the response the handler
    already produced is the truthful one. Errors raised by the HANDLER are
    untouched — they propagate through the ``yield`` as before.
    """
    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001 — see the docstring; never mask the handler
            logging.getLogger(__name__).warning("db.session_close_failed", exc_info=True)


# ── The readiness probe's own connection ─────────────────────────────────────
# ⛔ WHY THIS ENGINE EXISTS, and why it must not be merged back into ``engine``.
#
# `/health/ready` used to take `session: AsyncSession = Depends(get_session)`,
# i.e. it competed for the SAME 15 slots as request traffic. That makes the probe
# a function of LOAD rather than of HEALTH, and on this Deployment the
# consequence is a total outage rather than a slow patch:
#
#   pool saturates -> the probe cannot be served -> it exceeds `timeoutSeconds: 5`
#   -> 3 misses at `periodSeconds: 10` -> Kubernetes removes the pod from its own
#   EndpointSlice -> and `replicas: 1` means the Service now has NO endpoint, so
#   every in-flight caller gets `read ECONNRESET` / `socket hang up` and every
#   retry those resets provoke arrives at the same saturated pool.
#
# MEASURED 2026-09-03 on `requirements-c4f5cdd7d-fcgmw`: 10 readiness probes over
# 5s in two episodes, THREE of them returning 503 at exactly 30.00s — which is
# `pool_timeout`, not a database failure. The `Readiness probe failed ... context
# deadline exceeded` event carried count 302. Downstream, `AA Activity -
# Expansion` recorded 31 `socket hang up` + 22 `read ECONNRESET` in 24h.
#
# A dedicated single-connection pool makes the probe's independence from request
# load STRUCTURAL rather than tuned: no amount of request concurrency can occupy
# it, because nothing else uses it. It costs exactly one extra pooled connection
# per pod, against a pgbouncer pool measured at `cl_waiting 0` / `maxwait 0`.
#
# The RLS GUC listener is deliberately NOT registered on this engine: the probe
# carries no tenant, runs `SELECT 1`, and must not depend on request context.
# ⛔ THIS MUST EXCEED `routers/health.READY_BUDGET_S` (3.0s). It used to be 2.0,
# which inverted the two on a ONE-SLOT pool and manufactured the exact false 503
# this whole incident is about.
#
# The mechanism, measured from the code: a probe may legitimately occupy the
# single slot for its full 3.0s budget. A second probe overlapping it waited only
# 2.0s for the checkout, raised `SAPoolTimeout`, and `probe_database()` mapped
# that to DB_DOWN with the comment "the database is not answering fast enough" —
# on a perfectly healthy database whose only sin was that another PROBE held the
# slot. With `replicas: 1` and `failureThreshold: 3`, three such overlaps evict
# the only pod: a total outage caused by the readiness check itself.
#
# The fix is ordering, not sizing: keep the 1-slot pool (its isolation is the
# whole point) and make the checkout wait OUTLAST the budget, so the outer
# `asyncio.timeout(READY_BUDGET_S)` is the single authority on when the probe
# gives up. A give-up then means "could not answer in budget" — which is true —
# instead of "the database is down", which was not.
#
# The two constants cannot import each other (`routers/health` imports `db`), so
# the invariant is pinned behaviourally instead:
# tests/test_readiness_survives_pool_saturation.py.
HEALTH_POOL_TIMEOUT_S = _env_num("DB_HEALTH_POOL_TIMEOUT_S", 5, float)

# ⛔ THIS ONE IS DELIBERATELY *UNDER* `routers/health.READY_BUDGET_S` (3.0s), and
# that is the OPPOSITE ordering to `HEALTH_POOL_TIMEOUT_S` above — read both
# before changing either.
#
# The two settings answer different questions. The POOL timeout must OUTLAST the
# budget so that a probe waiting on the single slot gives up via the handler's own
# `asyncio.timeout` and reports "could not answer in budget" rather than the false
# "the database is down". The STATEMENT timeout must land INSIDE the budget so
# that a `SELECT 1` the server has accepted but is not answering is aborted while
# the handler is still there to see it — the connection goes back to the one-slot
# pool immediately and the NEXT probe, 10s later, starts from a clean pool instead
# of queueing behind a statement nobody is waiting for any more.
# 2500ms leaves 500ms of the 3.0s budget for the handler to render the verdict.
HEALTH_STATEMENT_TIMEOUT_MS = _env_num("DB_HEALTH_STATEMENT_TIMEOUT_MS", 2500, int)

# The sizing here is deliberately NOT ``**_POOL_KWARGS`` — 1 + 0 is the whole
# point of this engine. The stale-connection settings ARE shared with it, for the
# same standardisation reason stated above (hygiene, not the cause): a probe that
# hands out a dead socket answers 503 on a perfectly healthy database.
health_engine = create_async_engine(
    DATABASE_URL,
    future=True,
    echo=False,
    pool_size=1,
    max_overflow=0,
    pool_timeout=HEALTH_POOL_TIMEOUT_S,
    pool_pre_ping=True,
    pool_recycle=3600,
)

register_statement_timeout(health_engine, HEALTH_STATEMENT_TIMEOUT_MS, label="health")

pool_watch.install(health_engine, "health")


def watched_engines() -> list[tuple[str, Any]]:
    """``(label, engine)`` for every engine ``pool_watch`` tracks, deduped by identity.

    The dedup guards against one pool being reported twice under two labels if
    two labels ever alias the same engine (a test that monkeypatches one onto the
    other). ``id()`` is the right key here because engine objects are
    module-level and long-lived, so the recycling hazard ``pool_watch.clear()``
    documents does not apply.

    Built at CALL time from the module globals rather than frozen into a list at
    import, so a test that monkeypatches ``db.engine`` is actually observed.
    """
    out: list[tuple[str, Any]] = []
    seen: set[int] = set()
    for label, eng in (
        ("runtime", engine),
        ("health", health_engine),
    ):
        if eng is None or id(eng) in seen:
            continue
        seen.add(id(eng))
        out.append((label, eng))
    return out


def pool_stats(target: Any = None) -> dict[str, int]:
    """SQLAlchemy pool counters for ``target`` (default: the runtime engine).

    Best-effort by design: these are diagnostic context attached to a warning or
    a health body, and must never be the reason the caller fails to answer.
    Also emits ``pool_max_overflow`` and ``pool_limit`` (= size + max_overflow),
    so a caller can ask about a pool whose capacity is not the runtime one's.

    ``QueuePool`` exposes ``checkedout``/``checkedin``/``size``/``overflow``, but
    a pool without them (``NullPool``/``StaticPool``, as tests may configure) is a
    real possibility, so every accessor is probed rather than assumed.
    """
    try:
        pool = (target or engine).pool
        stats: dict[str, int] = {}
        for name in ("checkedout", "checkedin", "size", "overflow"):
            accessor = getattr(pool, name, None)
            if callable(accessor):
                stats[f"pool_{name}"] = int(accessor())
        # The CEILING, carried explicitly. `size()` is the configured pool size,
        # not the ceiling (see `pool_is_saturated` for the measured table), so a
        # caller handed these counters for a NON-runtime engine previously had no
        # way to know that engine's real capacity and fell back to the runtime
        # module constants. `_max_overflow` is a private attribute rather than an
        # accessor, hence `getattr` + an isinstance check rather than `callable`.
        max_overflow = getattr(pool, "_max_overflow", None)
        if isinstance(max_overflow, int) and "pool_size" in stats:
            stats["pool_max_overflow"] = max_overflow
            stats["pool_limit"] = stats["pool_size"] + max_overflow
        return stats
    except Exception:  # noqa: BLE001 - diagnostics must not mask the caller
        return {}


def pool_is_saturated(stats: dict[str, int] | None = None, headroom: int = 1) -> bool:
    """True when the pool described by ``stats`` has ``headroom`` or fewer slots left.

    ``stats`` may describe ANY engine — pass ``pool_stats(some_engine)``. The
    ceiling is read from ``stats["pool_limit"]`` when present, so the answer is
    about the pool that was measured rather than about the runtime one.

    MEASURED against SQLAlchemy 2.0.51 rather than assumed, because every one of
    these three counters means something other than what its name suggests, and
    getting it wrong is how a saturation gauge ends up unable to fire:

        pool_size=3, max_overflow=2      size()  checkedout()  overflow()
          nothing open                      3         0           -3
          3 open (pool full, no overflow)   3         3            0
          5 open (the ceiling)              3         5            2

    So ``size()`` is the CONFIGURED pool size, not the ceiling; ``overflow()``
    counts from ``-pool_size`` upward and only reaches ``max_overflow`` at the
    ceiling; and ``checkedout()`` is the only one that counts everything in use.
    The ceiling is therefore ``POOL_SIZE + MAX_OVERFLOW`` and the in-use count is
    ``checkedout`` — which is also what licenses the discriminator in
    ``routers/health.py``: a checkout timeout while ``checkedout`` is LOW cannot
    be contention, because contention is exactly what this number counts.
    """
    s = stats if stats is not None else pool_stats()
    if "pool_checkedout" not in s:
        return False
    # ⛔ THE CEILING COMES FROM THE POOL, NOT FROM THE MODULE. Until 2026-09-04
    # this line read `(POOL_SIZE + MAX_OVERFLOW) - headroom`, i.e. it applied the
    # RUNTIME engine's 15 to whatever engine's counters it was handed. The gauge
    # was therefore structurally incapable of firing for `health_engine`, whose
    # capacity is 1: it would have needed 14 checkouts of a one-slot pool. That
    # pool is the one #140 created precisely BECAUSE one-slot contention is the
    # condition worth reporting. `pool_limit` is absent from hand-built stats
    # dicts (every existing caller and test), which is why the fallback stays.
    limit = s.get("pool_limit", POOL_SIZE + MAX_OVERFLOW)
    if limit <= 0:
        return False
    # A pool of capacity 1 with the default headroom of 1 would be "saturated"
    # while idle (`checkedout >= 0`). Clamping keeps the question meaningful for
    # small pools: at capacity 1 it asks `checkedout >= 1`.
    room = min(headroom, limit - 1)
    return s["pool_checkedout"] >= limit - room
