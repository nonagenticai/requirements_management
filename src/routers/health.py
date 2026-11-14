"""Health check endpoints.

⛔ READ THIS BEFORE TOUCHING `/health/ready`.

Readiness answers ONE question: *should Kubernetes route traffic to this pod?*
It is not a load gauge, and on this Deployment the difference is an outage.

Until 2026-09-03 this handler was `async def readiness(session = Depends(get_session))`,
so it drew a connection from the SAME pool as request traffic. That made the probe a
function of load: when the pool saturated, the probe could not be served inside its
`timeoutSeconds: 5`, three misses at `periodSeconds: 10` removed the pod from its own
EndpointSlice — and with `replicas: 1` the Service then had NO endpoint at all, so every
in-flight caller got `read ECONNRESET` / `socket hang up` and every retry those resets
provoked arrived at the same saturated pool. Saturation was converted into a total outage.

MEASURED, pod `requirements-c4f5cdd7d-fcgmw`, 2026-09-03: ten readiness probes over 5s in
two episodes, THREE returning 503 at exactly **30.00s** — that is SQLAlchemy's `pool_timeout`,
not a database failure. `Readiness probe failed ... context deadline exceeded` carried
count 302. Downstream, `AA Activity - Expansion` logged 31 `socket hang up` and 22
`read ECONNRESET` in the same 24h.

So the probe now distinguishes two conditions the old code collapsed into one 503:

  * **unreachable** — the database does not answer on a connection nothing else can
    occupy. Genuine unreadiness -> 503, exactly as before.
  * **saturated**  — the RUNTIME pool is full. The pod is BUSY, not BROKEN. It stays
    READY (200) and says so in the body, because removing the only replica helps nobody:
    there is no peer to take the traffic, and the removal itself manufactures the resets.

⚠️ Those two are measured SEPARATELY and must stay that way. Reachability is a real
`SELECT 1` on a dedicated connection; saturation is read from the runtime pool's counters.
An earlier draft folded them together by treating a health-pool checkout timeout as
"busy -> ready", which let a pod with an unreachable database answer 200 forever — see
`probe_database` for the failing case an adversarial review produced.

⚠️ The DB check is NOT removed, and must not be. A readiness probe that never touches the
database would route traffic to a pod that cannot serve it. The distinction introduced here
is *unreachable vs saturated*, never *checked vs unchecked* — and the negative leg is pinned
by `tests/test_readiness_survives_pool_saturation.py`, which fails if this degenerates into
"always ready".
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SAPoolTimeout

from db import health_engine, pool_is_saturated, pool_stats

router = APIRouter()

logger = logging.getLogger(__name__)

# The whole probe must answer well inside the kubelet's `timeoutSeconds: 5`. The
# dedicated health pool cannot be occupied by request traffic, so a `SELECT 1`
# that still cannot complete in this budget means the DATABASE is not answering —
# which is exactly the condition readiness exists to report.
READY_BUDGET_S = float(3.0)

DB_OK = "ok"
DB_DOWN = "down"


async def probe_database() -> str:
    """Return ``DB_OK`` or ``DB_DOWN`` within ``READY_BUDGET_S``.

    ⛔ THERE IS DELIBERATELY NO "BUSY" VERDICT HERE, and the first version of this
    change had one. It was wrong in the dangerous direction, and an adversarial
    review produced the failing case: `health_engine` is `pool_size=1,
    max_overflow=0` and NOTHING ELSE USES IT, so request traffic cannot make its
    checkout time out. The only way to get a `SAPoolTimeout` on this pool is a
    PREVIOUS PROBE still holding the single slot — which is evidence that the
    database is hung, not that the service is busy. Mapping it to READY meant a
    pod with a blackholed DSN could answer 200 `{"database": true}` forever:

        probeB: verdict='busy' in 2.00s  -> HTTP 200 ready, database=true
        probeA: verdict='down' in 3.00s  -> HTTP 503

    `/health/ready` is authz-exempt and ingress-reachable, so any second poller
    interleaving with the kubelet would reset `failureThreshold: 3` indefinitely.
    That is strictly worse than the 503 this change set out to remove.

    Request-pool SATURATION — the condition that must not remove the pod — is a
    different measurement entirely, taken from the runtime pool's own counters in
    `readiness()` below. Keeping the two apart is the whole design: this function
    answers "can I reach the database?", the counters answer "am I busy?".

    ⛔ WHAT THE GAUGE DOES **NOT** DO — retracted 2026-09-03, same day it was
    written. An earlier version of this docstring claimed `db_pool.saturated` could
    separate the two surviving explanations for the exhaustion: "timeouts WITH the
    warning = connections HELD; timeouts WITHOUT it = `checkedout` is low, so
    connection ESTABLISHMENT is stalling". **That is backwards and the second
    branch is unreachable.** Measured against SQLAlchemy 2.0.51:
    `QueuePool._inc_overflow` runs BEFORE the creator, so a connection that is
    mid-establishment already counts as checked out —

        MID-ESTABLISHMENT: checkedout=15 size=5 overflow=10 checkedin=0

    — i.e. establishment stalling produces the SAME saturated reading as held
    connections. The gauge cannot tell them apart, and anyone reading the old
    claim would have concluded the opposite of the truth from a correct
    measurement.

    What it IS worth: it names saturation WHILE it is happening, on its own
    schedule, instead of only afterwards via a stack trace raised 30s late. That
    was the actual gap — the pool counters existed but were attached only to
    `event_loop.blocked`, i.e. emitted only when the OTHER hypothesis was true.
    Separating held-vs-establishing needs a different instrument (connect latency,
    or `SHOW POOLS` sampled in the same second).

    ⚠️ HONEST LIMIT OF "UNCONTENDED". The dedication is structural at the
    SQLAlchemy layer only. This DSN points at pgbouncer (`requirements_api`,
    `pool_mode = transaction`, `pool_size 12`), so if an episode ever holds
    SERVER-side transactions the probe queues there instead and hits
    `READY_BUDGET_S` — reported as DOWN, correctly, but for a reason one layer
    below the one this docstring describes. Measured 2026-09-03 that layer was
    idle (`cl_waiting 0`, `maxwait 0`, zero `idle in transaction`), which is what
    makes the inference sound TODAY; `db_pool.saturated` plus `SHOW POOLS` is how
    to re-check it rather than assume it.

    Exception ordering is load-bearing: `sqlalchemy.exc.TimeoutError` subclasses
    `SQLAlchemyError` (and is NOT an `OSError`), while the builtin `TimeoutError`
    that `asyncio.timeout` raises IS an `OSError` — so it must be caught before
    one. All three land on ``DB_DOWN``, but for stated and different reasons.
    """
    try:
        async with asyncio.timeout(READY_BUDGET_S):
            async with health_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except SAPoolTimeout:
        # A previous probe still holds the only slot: the database is not
        # answering fast enough for even a `SELECT 1` to clear between probes.
        return DB_DOWN
    except TimeoutError:
        return DB_DOWN
    except SQLAlchemyError, OSError:
        return DB_DOWN
    return DB_OK


@router.get("/health", operation_id="health_check", tags=["Health"])
async def health() -> dict[str, Any]:
    """Health check endpoint with basic diagnostics.

    Carries ``tenant_refusals`` — process-lifetime counts of requests this pod
    REFUSED because ``X-Tenant-Id`` named a tenant licences does not know, split
    by failure class (see ``auth.tenant_resolution.record_tenant_refusal``).

    ⛔ IT IS HERE BECAUSE THE BURN-DOWN MUST BE READABLE FROM THE POD. This
    service's Prometheus counters live on a separate metrics-only port (METRICS_PORT,
    default 9921; see ``src/metrics_server.py``), which an operator on the pod does not
    reach first; the alternative was a
    log query, over a stream nobody retains, to answer "is anything still being
    refused?". The previous answer to that question was reconstructed from the
    BILLING ledger weeks after the fact.

    ⚠️ COUNTS ONLY — never the offending tenant values. ``/health`` is a PUBLIC
    endpoint (``PUBLIC_PATHS`` in ``auth.middleware``), so anything put here
    is unauthenticated. Three integers say whether the boundary is refusing
    anyone; the value that was refused stays in the WARNING log, behind cluster
    access, where a per-tenant question belongs.

    Best-effort, like every other diagnostic in this module: if the counter
    cannot be read the key is omitted rather than failing the check. ``/health``
    is what a human curls when something is wrong, and it must not be the thing
    that is wrong.
    """
    payload: dict[str, Any] = {
        "status": "ok",
        "service": "requirements-api",
        "version": "0.2.0",
    }
    try:
        from auth.tenant_resolution import tenant_refusal_counts

        payload["tenant_refusals"] = tenant_refusal_counts()
    except Exception:  # pragma: no cover - diagnostics must never break /health
        logger.debug("health.tenant_refusals_unavailable", exc_info=True)
    return payload


@router.get("/health/live", operation_id="liveness_check", tags=["Health"])
async def liveness() -> dict[str, Any]:
    """Kubernetes liveness probe."""
    return {"status": "alive"}


@router.get("/health/ready", operation_id="readiness_check", tags=["Health"])
async def readiness() -> dict[str, Any]:
    """Kubernetes readiness probe — database REACHABILITY, not database load."""
    reachable = await probe_database() == DB_OK
    stats = pool_stats()

    checks: dict[str, Any] = {"database": reachable}

    # Per-engine occupancy. `stats` above describes the runtime pool alone; this
    # says how many connections each engine is actually holding right now —
    # {"runtime": 2, "health": 1}. Best-effort like every other diagnostic here: an unreadable tally omits the
    # key rather than failing the probe.
    try:
        from pool_watch import tally

        pools = tally()
        if pools:
            checks["pools"] = pools
    except Exception:  # noqa: BLE001 - a diagnostic must never fail the probe
        pass

    if pool_is_saturated(stats):
        # Reported, never fatal — and read from the RUNTIME pool, which is the
        # one request traffic actually contends for. This is the signal an
        # operator needs and the one thing the old handler destroyed by turning
        # it into a 503.
        checks["database_busy"] = True
        logger.warning(
            "readiness.pool_saturated %s",
            " ".join(f"{k}={v}" for k, v in sorted(stats.items())),
        )

    if not reachable:
        raise HTTPException(status_code=503, detail={"status": "not ready", "checks": checks})

    return {"status": "ready", "checks": checks}
