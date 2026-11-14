"""Name the code that is holding a pooled SQLAlchemy connection, from inside the process.

WHY THIS EXISTS. `requirements` exhausts its 15-slot pool constantly:

    QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00

Every instrument OUTSIDE the process says the wait is not theirs. pgbouncer reports
`cl_waiting = 0`; Postgres reports `state = idle` with `active` ~= 1. Both are consistent
with the pool being full — a SQLAlchemy checkout is a purely in-process event, and there is
no metric, log line, or `pg_stat_activity` row anywhere below the ORM that can attribute it.
The process therefore has to name its own holder, which is what this module does: it hooks
the pool's `checkout`/`checkin` events, remembers who was on the way in, and forgets them on
the way out. `holders()` renders the survivors, oldest first.

WHAT A RESULT LOOKS LIKE — including the negative one. This instrument is *not* only a
culprit-finder, and reading it that way loses half its value:

  * a holder aged tens of seconds, repeatedly, names a LEAK — a connection checked out across
    something slow (an LLM call, an external HTTP hop) or never returned at all; the `caller`
    and `task` fields say which code path;
  * **"no holder is older than ~1s" is itself a result.** It REFUTES the leak hypothesis and
    moves the diagnosis to arrival rate versus concurrency: 15 slots each held briefly, but
    claimed faster than they are released, exhausts a pool exactly as visibly as one held
    forever, and the fix is a different fix (pool size, request concurrency, batching) than
    the fix for a leak. An instrument that can only accuse is an instrument that will
    manufacture an accusation; this one has to be able to come back empty-handed and have
    that mean something.

A LEAKED CONNECTION NEVER FIRES `checkin` — AND THAT NEEDS *TWO* FIXES. Measured: three
checkouts dropped without `close()` left three ever-ageing holders while `pool.checkedout()`
read 0, so the warning printed `pool_checkedout=0` beside three "old holders" and the
docstring above sent the operator after a path that had already released.

The mechanism, from `sqlalchemy/pool/base.py::_finalize_fairy`. On a garbage-collected
checkout under an ASYNC dialect (`dont_restore_gced = pool._dialect.is_async` is True),
`detach = True`, so the finaliser calls `_ConnectionFairy.detach()`. That clears
`fairy_ref`, hands the RECORD straight back to the pool via `_do_return_conn()`, and
dispatches the **`detach`** event. Control then reaches
`if connection_record.fairy_ref is not None: connection_record.checkin()` — and `fairy_ref`
is already None, so **`checkin` is skipped**. Hence:

  * `install()` listens for **`detach`** as well as `checkin`. This is the hook that
    actually covers the leak path. A weak key alone does NOT: `_do_return_conn()` puts the
    record back in the QueuePool's own queue, a strong reference, so the record object
    survives the collection of the connection that leaked it.
  * The registry is nevertheless a **`WeakKeyDictionary` over the record**, for the cases
    where the record itself does die (pool `dispose()`, an overflow record discarded on
    `sqla_queue.Full`) and, more importantly, because **`id()` is recycled**: measured, 40
    engines produced 36 distinct `id()` values, one of them reused twice. A recycled id lets
    one record's `checkin` erase a different, live record's entry. Keying on the object
    removes that hazard entirely — object identity is the thing we actually mean.

DESIGN CONSTRAINTS, each of which is load-bearing:

  * A diagnostic must never break a query. Every listener body is wrapped and every exception
    is swallowed — a broken instrument degrades to no instrument, never to a failed request.
  * The registry is bounded, but **the cap may never remove a live holder** — see `_record()`.
    The old cap was a `clear()`, and that was the module's own documented false negative in
    executable form: an already-checked-out connection fires no second `checkout`, so after a
    clear the live holders were never re-recorded. Measured: 15 real connections checked out,
    registry cleared at the cap, `holders()` for those 15 → `[]`.
  * Ages are computed on demand in `holders()`, never stored or refreshed, so the hot path is
    one dict write per checkout and one `pop` per checkin.
"""

import asyncio
import time
import weakref
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["caller_var", "clear", "holders", "install", "tally"]

# Set by whatever knows the human-meaningful name of the current unit of work (a route, a
# background job). Default "?" so an unlabelled path still records a holder rather than
# raising a LookupError inside a listener.
caller_var: ContextVar[str] = ContextVar("pool_watch_caller", default="?")

# Hard cap on tracked checkouts. The three engines together can have at most
# (POOL_SIZE + MAX_OVERFLOW) records in flight each — an order of magnitude under this
# number — so with weak keying the cap is unreachable by any amount of genuine pool
# exhaustion. See _record() for what happens if it is reached anyway.
_MAX_TRACKED = 256

# connection_record -> {"at": monotonic, "engine": label, "caller": str, "task": str|None}
#
# WEAK keys: the entry for a record that is garbage-collected (the leaked-connection case)
# vanishes with the record, so a phantom holder cannot outlive the slot it names.
_holders: "weakref.WeakKeyDictionary[Any, dict[str, Any]]" = weakref.WeakKeyDictionary()

# Fallback for keys that cannot be weakly referenced (ints, strings — only synthetic keys
# from tests reach this; a real `_ConnectionRecord` declares `__weakref__` in `__slots__`).
# These carry NO liveness guarantee, which is why the cap spends them first.
_synthetic: dict[Any, dict[str, Any]] = {}

# id(engine) of every engine already hooked; see install().
_installed: set[int] = set()


def _task_name() -> str | None:
    """Name of the running asyncio task, or None.

    Coarse, best-effort context only. It is NOT per-request attribution: measured, three
    concurrent HTTP requests produced ONE distinct task name — every one of them is the same
    `Task-N ... BaseHTTPMiddleware.__call__.<locals>.call_next.<locals>.coro`, because
    Starlette runs the downstream app in a task it names itself. The field still separates a
    holder on a background loop (the lag probe) from a holder on the
    request path, which is worth logging; it cannot tell two requests apart. Use `caller`
    for that. `current_task()` raises RuntimeError when there is no running loop, and returns
    None outside a task, so both are absorbed here.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    return task.get_name() if task is not None else None


def _tracked() -> int:
    """Live tracked checkouts. Dead weak keys are already gone, so this counts truth."""
    return len(_holders) + len(_synthetic)


def _spend_synthetic(target: int) -> None:
    """Drop the oldest non-weak entries until the registry is down to `target`.

    Only these are evictable: nothing verifies that a synthetic key still corresponds to a
    checked-out connection, so they are the entries we cannot trust — the exact opposite of
    the weak-keyed ones, whose mere presence proves their record is still alive.
    """
    overflow = _tracked() - target
    if overflow <= 0 or not _synthetic:
        return
    for key in sorted(_synthetic, key=lambda k: _synthetic[k]["at"])[:overflow]:
        _synthetic.pop(key, None)


def _record(key: Any, label: str) -> None:
    """Remember one checkout under `key` (a `_ConnectionRecord`, or a synthetic test key).

    THE CAP CANNOT REMOVE A LIVE HOLDER. It prunes unverifiable (non-weak) entries first and,
    if that is not enough, REFUSES the incoming entry rather than evicting a weak-keyed one.
    That is the inversion of the previous policy, and the previous policy's premise is what
    changed: under `id()` keying the resident entries were presumed stale garbage, so
    `clear()` looked like hygiene. Under weak keying every resident weak-keyed entry is a
    connection that is provably still alive, so evicting one — by `clear()`, LRU, or
    anything else — is destroying the only evidence of a genuine holder.

    The guarantee that survives, stated plainly: **`holders()` cannot come back empty (or
    holding only phantoms) while the pool is genuinely exhausted.** Refusing to insert is
    survivable in a way that clearing is not — refusal costs one *new* holder's name in a
    situation that already has `_MAX_TRACKED` named ones, and the pools cannot produce
    anywhere near that many records, so the branch is dead weight in production and only
    tests with synthetic keys ever reach it.
    """
    if _tracked() >= _MAX_TRACKED:
        _spend_synthetic(_MAX_TRACKED * 3 // 4)
        if _tracked() >= _MAX_TRACKED:
            return
    entry = {
        "at": time.monotonic(),
        "engine": label,
        "caller": caller_var.get(),
        "task": _task_name(),
    }
    try:
        _holders[key] = entry
    except TypeError:
        # Not weakly referenceable. Degrades to strong keying rather than to silence.
        _synthetic[key] = entry


def _forget(key: Any) -> None:
    """Forget one checkout. Tolerates a key that was never recorded (post-refusal checkin)."""
    try:
        _holders.pop(key, None)
    except TypeError:
        pass
    _synthetic.pop(key, None)


def install(engine: AsyncEngine, label: str) -> None:
    """Attach checkout/checkin listeners to `engine`. Idempotent per engine object.

    Idempotence is not decorative: a caller that hands the SAME engine object to install()
    twice (e.g. an alias/fallback engine) would otherwise double-fire every listener.
    """
    if id(engine) in _installed:
        return
    _installed.add(id(engine))

    # Listen on `engine.sync_engine`, NOT on `engine.pool`.
    #
    # The Pool OBJECT is not stable for the life of the engine: `dispose()` (and an internal
    # invalidate-all) REPLACES it with a fresh instance, and listeners attached to the old
    # pool object go with it — silently, with no error and no warning. The instrument would
    # simply stop recording, which in a pool-exhaustion investigation reads identically to
    # "there are no holders", i.e. it would fabricate the negative result described in the
    # module docstring. The Engine outlives every pool it owns, and SQLAlchemy propagates
    # pool events registered on the Engine to whatever pool it currently has.
    @event.listens_for(engine.sync_engine, "checkout")
    def _on_checkout(dbapi_conn: Any, connection_record: Any, connection_proxy: Any) -> None:
        try:
            # The RECORD, not id(record): ids are recycled (36 distinct across 40 engines,
            # one reused twice), and a weak key lets a leaked record's entry die with it.
            _record(connection_record, label)
        except Exception:
            # A diagnostic must never break a query. Deliberately silent — logging from a
            # pool listener on an exhausted pool is its own hazard.
            pass

    @event.listens_for(engine.sync_engine, "checkin")
    def _on_checkin(dbapi_conn: Any, connection_record: Any) -> None:
        try:
            _forget(connection_record)
        except Exception:
            pass

    # The LEAK path, and the only listener that sees it. A checkout collected by the garbage
    # collector under an async dialect is detached, not checked in (see the module
    # docstring): its slot returns to the pool while `checkin` never fires. Without this the
    # entry outlives the connection it names and the warning reports an ageing holder beside
    # `pool_checkedout=0` — an accusation pointed at code that had already released.
    @event.listens_for(engine.sync_engine, "detach")
    def _on_detach(dbapi_conn: Any, connection_record: Any) -> None:
        try:
            _forget(connection_record)
        except Exception:
            pass


def _entries() -> list[dict[str, Any]]:
    """Every tracked entry, both maps. Dead records have already dropped out of the weak one."""
    return [*_holders.values(), *_synthetic.values()]


def holders(limit: int = 5, engine: str | None = None) -> list[dict[str, Any]]:
    """Up to `limit` current holders, OLDEST FIRST. Safe to call when nothing is checked out.

    `engine` filters to one engine's label ("runtime", "health"). It exists because the
    registry is process-wide across all engines while the saturation predicate that calls this
    reads ONE of them: unfiltered, a long-lived holder on another engine is always the oldest
    entry and crowds the runtime holders out of the top `limit` (measured with the since-removed
    owner engine: 1 of 15 runtime holders shown). A report that names a different pool than the one that
    tripped is worse than no report — it points the investigation at the wrong engine with
    full confidence. `tally()` keeps the other engines visible instead of silently dropped.

    Ages are computed here, at call time, against a single `now` so the entries are mutually
    comparable. Returns plain dicts (never the internal records), so a caller can log or
    serialise the result without holding a reference into the registry.
    """
    now = time.monotonic()
    entries = _entries()
    if engine is not None:
        entries = [h for h in entries if h["engine"] == engine]
    snapshot = sorted(entries, key=lambda h: h["at"])
    return [
        {
            "age_s": round(now - h["at"], 3),
            "engine": h["engine"],
            "caller": h["caller"],
            "task": h["task"],
        }
        for h in snapshot[:limit]
    ]


def tally() -> dict[str, int]:
    """How many connections each engine is holding, e.g. `{"health": 0, "runtime": 15}`.

    The counterpart to `holders(engine=...)`: the filtered list answers the question the
    saturation predicate asked, and this says whether the OTHER pools are under pressure too,
    which the filter would otherwise hide. Sorted by label so the log line is stable.
    """
    counts: dict[str, int] = {}
    for h in _entries():
        counts[h["engine"]] = counts.get(h["engine"], 0) + 1
    return dict(sorted(counts.items()))


def clear() -> None:
    """Drop all state. Test helper.

    This also empties `_installed`, which matters because the guard keys on `id(engine)` and
    CPython recycles ids: a disposed engine's id can be handed to the next engine allocated,
    after which install() would skip a live engine and the tests would silently observe
    nothing. Resetting between tests removes that coupling. Do NOT call this in production to
    "re-install" on a still-live engine — the listeners are still attached and a second
    install() would double-fire them.
    """
    _holders.clear()
    _synthetic.clear()
    _installed.clear()
