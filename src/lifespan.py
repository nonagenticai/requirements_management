"""Application lifespan management with startup/shutdown handlers."""

import asyncio
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from typing import Any

import sqlalchemy.exc
from fastapi import FastAPI

from logging_config import get_logger
from telemetry import setup_telemetry

log = get_logger(__name__)


def _pool_saturated(stats: dict[str, int]) -> bool:
    """True when the runtime pool has one slot or fewer left.

    Delegates to ``db.pool_is_saturated`` for the same non-divergence reason as
    ``_pool_stats``. Returns False when the counters are unavailable — a probe
    that cannot read the pool must not manufacture an alarm about it.
    """
    try:
        from db import pool_is_saturated

        return pool_is_saturated(stats)
    except Exception:  # noqa: BLE001 - diagnostics must not mask the warning
        return False


_SHUTDOWN_TASK_TIMEOUT_S = 5.0

# Pools whose capacity is at or below this are saturated during ORDINARY use, so
# they must dwell for two consecutive samples before warning. Anything larger
# warns on the first sample, exactly as it did before the per-engine sweep.
_DWELL_REQUIRED_AT_OR_BELOW = 1


async def _cancel_tasks(tasks: Iterable["asyncio.Task[Any] | None"]) -> None:
    """Cancel every task, and let NO single one abort the sweep.

    Three failure modes, each measured rather than imagined:

    * **A task that already died of something other than cancellation.** Awaiting
      it re-raises that exception. With only ``CancelledError`` caught, it escaped
      the loop and every LATER task was never cancelled at all — verified by
      execution: the loop escaped with ``ValueError`` and never reached the second
      task. Caught and logged here instead.
    * **A task that swallows its own cancellation** (a bare ``except Exception``
      around an await inside the loop body will do it). ``await task`` is then
      unbounded and shutdown hangs forever — and because this now runs BEFORE the
      HTTP clients are closed, it would hang holding them open. Bounded by ``_SHUTDOWN_TASK_TIMEOUT_S`` via
      ``asyncio.wait`` — NOT ``wait_for``, which cannot bound it (see below).
    * **A crash nobody ever sees.** Neither background task carries a
      done-callback, so a loop can be dead for days while ``/health/ready`` keeps
      answering 200. This log line is the only place that surfaces.

    Deliberately never raises: a shutdown path that can itself fail turns a clean
    SIGTERM into a stuck pod.
    """
    for task in tasks:
        if task is None:
            continue
        task.cancel()
        name = "?"
        try:
            name = task.get_name()
        except Exception:  # noqa: BLE001 - naming must not break shutdown
            pass
        # ⛔ `asyncio.wait_for` CANNOT be used here, and the first version of this
        # function used it. `wait_for` cancels the awaitable and then WAITS for
        # that cancellation to land — against a task that calls `uncancel()` and
        # keeps going, it never lands, and the "bound" hangs forever. Measured:
        # the test for this case timed out at 120s with `wait_for` in place.
        #
        # `asyncio.wait` is the real bound: it neither cancels nor waits for
        # completion, it just returns after the timeout and tells us whether the
        # task finished. A task that will not die is then LEFT running and named
        # in the log, which is the honest outcome — shutdown continues, and the
        # process exit reaps it.
        done, _pending = await asyncio.wait({task}, timeout=_SHUTDOWN_TASK_TIMEOUT_S)
        if not done:
            log.error(
                "shutdown.task_would_not_cancel",
                task=name,
                timeout_s=_SHUTDOWN_TASK_TIMEOUT_S,
            )
            continue
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - shutdown must not be abortable
            log.error(
                "shutdown.task_failed",
                task=name,
                error=str(exc),
                error_type=type(exc).__name__,
            )


def _pool_holders(engine: str = "runtime") -> list[dict[str, Any]]:
    """Who is holding a connection on the ``engine`` pool now, oldest first, or `[]`.

    ⚠️ **The filter is the point.** The holder registry is process-wide across
    every watched engine (runtime + health), while a saturation warning is about
    ONE of them. Unfiltered, a long-lived holder on another engine crowds the
    tripped pool's holders out of the top 5 (measured once, with the since-removed
    owner engine: 1 of 15 runtime holders survived into the report). A
    report that names a different pool than the one that tripped is worse than no
    report: it is a confident pointer at the wrong engine. So the caller passes
    the label that actually tripped, and logs ``holders_by_engine`` alongside so
    the other pools' pressure is visible rather than silently dropped.

    The default stays ``"runtime"`` so the zero-argument call sites — and the
    tests that pin them — keep their existing meaning.

    The unfiltered fallback is deliberate: if the runtime engine has no recorded
    holder at all, an empty list would read as the documented negative result
    (see below) when what we really have is nothing to say about runtime. Showing
    every engine's holders — each entry carries its own ``engine`` label — is
    honest; `[]` in that situation would not be.

    Wrapped in the same defensive try/except as ``_pool_stats`` for the same
    reason: a diagnostic must never be able to break the probe loop that carries
    it. An unreadable holder table degrades to `[]`, exactly as unreadable
    counters degrade to `{}`.

    ⚠️ **A SHORT / YOUNG LIST IS ITSELF A RESULT — do not read it as a failed
    measurement.** No instrument outside this process can see a SQLAlchemy
    checkout: during the exhaustion episodes pgbouncer reported
    ``client_waiting=0`` and Postgres reported ``state=idle`` with ``active≈1``
    while this pool was at its limit. So the ages below are the only evidence of
    WHO holds. If saturation is reported and **no holder is older than ~1s**,
    that REFUTES the slow-holder / leaked-connection explanation and points at
    ARRIVAL RATE instead — many short checkouts turning over faster than the
    pool can serve them. The two have opposite fixes (find the leak vs. bound
    the concurrency), and nothing else in this process can tell them apart.
    """
    try:
        from pool_watch import holders

        scoped = holders(5, engine=engine)
        return scoped if scoped else holders(5)
    except Exception:  # noqa: BLE001 - diagnostics must not mask the warning
        return []


def _pool_holders_by_engine() -> dict[str, int]:
    """Holder count per engine label, e.g. ``{"health": 1, "runtime": 15}``, or `{}`.

    The companion to ``_pool_holders()``'s runtime filter. Filtering answers the
    predicate's question; this keeps the answer from hiding the other pools —
    pressure that the filter drops out of the top 5 still shows up here as a
    number. Degrades to `{}` on any failure, for the same reason
    ``_pool_stats`` does.
    """
    try:
        from pool_watch import tally

        return tally()
    except Exception:  # noqa: BLE001 - diagnostics must not mask the warning
        return {}


def _pool_stats(target: Any = None) -> dict[str, int]:
    """SQLAlchemy pool counters for ``target`` (default: runtime), or `{}`.

    Delegates to ``db.pool_stats`` so this diagnostic and the one the readiness
    probe reports can never drift apart — two implementations of "how full is the
    pool" is how a saturation gauge ends up disagreeing with the outage it is
    supposed to explain.

    The ``target`` passthrough exists so the saturation sweep below can ask each
    engine about ITSELF. The zero-argument call still means the runtime engine,
    which is what the ``event_loop.blocked`` line wants: that line is about the
    loop, and the runtime pool is the one the loop's requests draw from.
    """
    try:
        from db import pool_stats

        return pool_stats(target)
    except Exception:  # noqa: BLE001 - diagnostics must not mask the warning
        return {}


def _saturated_engines() -> list[tuple[str, dict[str, int]]] | None:
    """``(label, stats)`` for every watched engine that is currently saturated.

    ⚠️ **This replaced a single-engine read, and the reason is a false negative
    that could not be argued away.** Until 2026-09-04 the trigger evaluated only
    the runtime pool, so another engine's exhaustion could emit `QueuePool limit
    ... reached` while `db_pool.saturated` stayed silent — and a reader then
    concluded "the instrument is deployed and quiet, so nothing saturated". The
    engine this genuinely unblocks is ``health`` — capacity 1 — which the old
    hard-wired ceiling of 15 made structurally unmeasurable.

    ⚠️ Returns ``None`` — NOT ``[]`` — when the engines cannot be read. The caller
    uses "absent from this list" to reset each label's dwell counter, and an
    empty list on a READ FAILURE would silently re-arm that gate for every
    engine, so a pool that is in fact still saturated would have to start
    counting again. `[]` must mean "nothing is saturated"; it must not double as
    "I could not tell".
    """
    try:
        from db import pool_is_saturated, watched_engines

        out: list[tuple[str, dict[str, int]]] = []
        for label, eng in watched_engines():
            st = _pool_stats(eng)
            if st and pool_is_saturated(st):
                out.append((label, st))
        return out
    except Exception:  # noqa: BLE001 - diagnostics must not mask the warning
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifecycle.

    Startup:
        - Initialize OpenTelemetry
        - Create database tables
        - Start the event-loop-lag background task

    Shutdown:
        - Close shared HTTP clients
        - Cancel the background task
    """
    shutdown_event = asyncio.Event()
    lag_task: asyncio.Task | None = None

    # Startup
    otel_enabled = setup_telemetry(app, "requirements-api")
    log.info("startup.otel", enabled=otel_enabled)

    # The DEDICATED Prometheus listener, on its OWN port (settings.metrics_port,
    # env METRICS_PORT, default 9921) — never a /metrics route on the API port;
    # see src/metrics_server.py. Bound BEFORE the DB check on purpose: a pod that
    # cannot reach Postgres is the pod whose metrics an operator most wants, and
    # the call is fail-soft so it can never itself be the reason startup dies.
    # Importing `metrics` registers its counters (and zero series) on REGISTRY.
    import metrics  # noqa: F401
    from metrics_server import start_metrics_server
    from settings import get_settings as _get_settings

    start_metrics_server(app, _get_settings().metrics_port)

    # Say out loud which environment this pod resolved to, and whether the
    # production fail-fast therefore ran. ⚠️ This exists because the answer was
    # "development" and "no" on the live production pod for months and nothing
    # said so: `ENVIRONMENT` is absent from the Deployment, `settings.environment`
    # defaults to "development", and `_REQUIRED_IN_PRODUCTION` early-returns for
    # anything that is not exactly "production". A guard that silently declines
    # to run is indistinguishable from a guard that ran and passed — unless it
    # announces itself, which is what this line is for.
    try:
        from settings import get_settings

        _env = get_settings().environment
        log.info(
            "startup.environment",
            environment=_env,
            required_settings_enforced=(_env == "production"),
        )
    except Exception as exc:  # noqa: BLE001 - never let a diagnostic stop startup
        log.warning("startup.environment", status="unreadable", error=str(exc))

    # NOTE: Database tables are created by Alembic migrations via K8s initContainer
    # Do NOT use Base.metadata.create_all() as it bypasses Alembic versioning
    log.info("startup.migrations", source="initContainer")

    # Register the synchronous quality heuristics.
    from services.eval_registrations import register_all

    register_all()
    log.info("startup.eval_registrations", status="ok")

    # Verify database connectivity — fail fast if DB is unreachable
    from sqlalchemy import text as sa_text

    from db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("SELECT 1"))
        log.info("startup.db_check", status="ok")
    except (OSError, sqlalchemy.exc.SQLAlchemyError) as exc:
        log.critical("startup.db_check", status="failed", error=str(exc))
        raise RuntimeError(f"Failed to connect to database at startup: {exc}") from exc

    # ── A `daily_backup_loop` + `utils/backups.py` lived here. DELETED — do not re-add.
    # It never produced a byte and could not: `pg_dump` is absent from the image
    # (`command -v pg_dump` → MISSING in the running pod), and PGHOST/PGPORT/PGDATABASE/
    # PGUSER/PGPASSWORD/BACKUP_DIR are in neither the Deployment env nor the
    # `requirements-api-env` secret, so it fell back to `PGHOST=db` and skipped. PVC
    # `requirements-backups` (5Gi, mounted /backups, PVC created 2026-07-25) is EMPTY; zero
    # `backup.*` log lines ever. It is also redundant: worker4's `pg-basebackup.timer`
    # takes cluster-wide PITR base backups to MinIO (18 committed bases, 14-day retention,
    # WAL archiving caught up), which covers this database. And it was a live hazard: a
    # SYNCHRONOUS `subprocess.check_call(["pg_dump", ..., "-Z", "9"])` on the asyncio loop
    # of a SINGLE-replica pod whose readiness probe is timeoutSeconds 5 / failureThreshold
    # 3 — adding pg_dump to the image would have dropped the pod from Service endpoints
    # for the length of every dump. "Just point it at our own DSN" does NOT fix it: that
    # DSN is pgbouncer in TRANSACTION mode, where pg_dump cannot run.

    async def event_loop_lag_loop() -> None:
        """Measure event-loop blocking directly, and say who was holding the DB.

        Why this exists (issue #111): the pod intermittently served
        `/health/ready` in 60-87s while returning 200. Readiness is
        `timeoutSeconds: 5, failureThreshold: 3` on a SINGLE replica, so that is
        long enough to drop the pod out of Service endpoints — with no restart
        and no error log. Two hypotheses were investigated and BOTH refuted:
        CPU-bound spaCy/sklearn on the loop (that code ran zero times in the
        window) and connection-pool starvation (pool timeout is 30s; the stalls
        were 87s and returned 200). The middleware's `duration_s` spans the whole
        request, so it cannot distinguish "this handler was slow" from "the loop
        was busy elsewhere" — which is precisely why the cause is still unknown.

        The measurement: sleep a known interval, then report the EXCESS over it.
        On a healthy loop that excess is sub-millisecond. If a coroutine
        monopolises the loop — or a sync call blocks it — every other task is
        delayed by exactly this much, including the readiness handler. So the
        number reported here IS the delay the kubelet's probe would have seen.

        Pool counters are sampled at the SAME instant on purpose. The two
        surviving explanations (loop blocked vs. all DB connections busy) are
        distinguishable only by which signal moves together with the stall, and
        sampling them separately after the fact cannot do that. `checkedout`
        approaching `size + overflow` points at the pool; a large `lag_s` with an
        idle pool points at the loop.
        """
        from settings import get_settings

        settings = get_settings()
        interval = settings.event_loop_lag_interval_s
        warn_at = settings.event_loop_lag_warn_s
        if interval <= 0:
            log.info("startup.event_loop_lag", status="disabled")
            return
        log.info("startup.event_loop_lag", status="started", interval_s=interval, warn_s=warn_at)
        loop = asyncio.get_running_loop()
        # Rate-limit the saturation warning: the probe samples twice a second and
        # an exhaustion episode lasts tens of seconds, so an unthrottled warning
        # would emit ~60 identical lines per episode and bury the thing it names.
        saturation_warn_every_s = 10.0
        # Per-LABEL, not a single scalar. With one scalar the first engine to
        # warn suppresses every other engine for the whole window — which would
        # reintroduce, one layer up, exactly the false negative that making the
        # sweep engine-complete was meant to remove.
        last_saturation_warn: dict[str, float] = {}
        # A pool of capacity 1 (`health_engine`) is legitimately "saturated" for
        # the few milliseconds of every `SELECT 1`, sampled twice a second. Two
        # CONSECUTIVE saturated samples (~0.5s dwell) is the difference between a
        # probe doing its job and a probe that is stuck. Reset on any clear
        # sample, so this cannot accumulate across unrelated blips.
        consecutive_saturated: dict[str, int] = {}
        try:
            while not shutdown_event.is_set():
                started = loop.time()
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
                if shutdown_event.is_set():
                    break
                # Excess over the interval we asked for. `loop.time()` is
                # monotonic, so this is immune to wall-clock adjustment.
                lag = loop.time() - started - interval
                stats = _pool_stats()
                if lag >= warn_at:
                    log.warning("event_loop.blocked", lag_s=round(lag, 3), **stats)
                # ── The other half of the #111 discriminator ──────────────────
                # The pool counters used to be attached ONLY to the line above,
                # i.e. they were emitted only when the OTHER hypothesis was true.
                # So when the pool actually did exhaust (measured 2026-09-03:
                # `QueuePool limit of size 5 overflow 10 reached, timeout 30.00`,
                # while loop lag never once crossed the warn threshold) the
                # instrument built to attribute it stayed silent, and the only
                # evidence was a stack trace raised after the damage was done.
                # This samples saturation on its own terms, so the next episode
                # is attributable while it is happening.
                sweep = _saturated_engines()
                saturated = {} if sweep is None else dict(sweep)
                # Only reset dwell counters on a sweep that actually READ the
                # engines. `sweep is None` means the read failed; treating that
                # as "nothing saturated" would re-arm the gate below.
                if sweep is not None:
                    for label in [k for k in consecutive_saturated if k not in saturated]:
                        consecutive_saturated[label] = 0
                for label, engine_stats in saturated.items():
                    seen = consecutive_saturated.get(label, 0) + 1
                    consecutive_saturated[label] = seen
                    # ⛔ THE DWELL GATE IS FOR TINY POOLS ONLY, AND APPLYING IT TO
                    # EVERY ENGINE WAS A REAL BUG — caught by adversarial review
                    # before this shipped. It exists because `health_engine` has
                    # capacity 1 and is therefore legitimately "saturated" for the
                    # few milliseconds of every `SELECT 1`, sampled twice a second.
                    # A 15-slot pool at 15/15 is never routine. Measured under the
                    # unconditional version: a 15-slot pool reading 15/15 on 20 of
                    # 40 samples emitted ZERO warnings, because it never happened
                    # to be saturated on two consecutive reads. That is precisely
                    # the ARRIVAL-RATE regime this incident's surviving hypothesis
                    # describes ("no holder older than ~1s" = many short checkouts
                    # turning over fast), i.e. the gate blinded the gauge in the
                    # one case it most needed to see.
                    limit = engine_stats.get("pool_limit", 0)
                    if 0 < limit <= _DWELL_REQUIRED_AT_OR_BELOW and seen < 2:
                        continue
                    now = loop.time()
                    if (
                        now - last_saturation_warn.get(label, -saturation_warn_every_s)
                        >= saturation_warn_every_s
                    ):
                        last_saturation_warn[label] = now
                        log.warning(
                            "db_pool.saturated",
                            engine=label,
                            lag_s=round(lag, 3),
                            holders=_pool_holders(label),
                            holders_by_engine=_pool_holders_by_engine(),
                            **engine_stats,
                        )
        except asyncio.CancelledError:
            pass

    try:
        lag_task = asyncio.create_task(event_loop_lag_loop())
        log.info("startup.lag_task", status="started")
    except RuntimeError as exc:
        log.warning("startup.lag_task", status="failed", error=str(exc))

    yield

    # Shutdown — close shared HTTP clients
    from auth.tenant_resolution import close_licenses_client
    from clients.coding_analysis import close_coding_analysis_client
    from fleet_auth import close_license_introspect_client

    # Stop the background task BEFORE closing the HTTP clients.
    #
    # ⚠️ The reason is NOT that the loops "observe the event and unwind" — an
    # earlier version of this comment claimed that and it is false: `set()` is
    # followed immediately by `cancel()` with no await in between, so the loops
    # are never scheduled to see the flag and in fact unwind via
    # `CancelledError`. `shutdown_event` is set anyway because it is the
    # documented stop condition and a loop MAY reach its `wait_for` first.
    #
    # The real reason is ordering: cancelling first means the clients outlive
    # the only background callers that can still be using them.
    shutdown_event.set()
    from services.modeling_validation import background_tasks

    # In-flight background validations: cancelled runs are marked failed (or, if that
    # write cannot land, reaped as orphaned by the next boot).
    await _cancel_tasks((lag_task, *background_tasks()))

    from metrics_server import stop_metrics_server

    stop_metrics_server(app)

    await close_coding_analysis_client()
    await close_license_introspect_client()
    await close_licenses_client()
