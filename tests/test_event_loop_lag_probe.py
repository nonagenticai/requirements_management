"""The event-loop lag probe must actually detect a blocked loop (issue #111).

Why this file exists
--------------------
The single-replica pod intermittently served ``/health/ready`` in 60-87s while
still returning 200. Readiness is ``timeoutSeconds: 5, failureThreshold: 3``, so
that silently drops the pod out of the Service's endpoints — no restart, no error
log. Two hypotheses were REFUTED: CPU-bound spaCy on the loop (that code ran zero
times in the window) and connection-pool starvation (pool timeout is 30s; the
stalls were 87s and returned 200). ``event_loop_lag_loop`` in ``src/lifespan.py``
measures the remaining candidate directly: it sleeps a known interval and reports
the EXCESS, which is exactly the delay the kubelet's probe would have seen.

A probe that only *starts* is worthless. The tests below therefore drive the real
loop function and genuinely block the event loop with a synchronous
``time.sleep`` scheduled via ``loop.call_soon`` — a callback scheduled that way
runs ON the loop thread, so nothing else (including the probe's own timer
callback) can run until it returns. Deleting the probe, or breaking its
measurement, fails ``test_probe_warns_when_the_loop_is_genuinely_blocked``.

Driving the closure
-------------------
``event_loop_lag_loop`` is a closure inside ``lifespan()`` and cannot be imported.
Every test therefore drives the whole ``lifespan()`` context manager; its startup
``SELECT 1`` runs against the real test database, and only telemetry is stubbed.

Timing margins are deliberately wide (a 0.5s block asserted only as ">= 0.3s").
This box runs heavily loaded and tight margins have produced false failures
before (issue #106). The property being pinned is "a multi-hundred-millisecond
stall is reported at roughly its true size", which does not need precision.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
import types
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import lifespan as _lifespan_import  # noqa: F401  (registers it in sys.modules)
import pool_watch

# ⚠️ Fetch modules out of sys.modules rather than using monkeypatch's dotted
# strings — see tests/test_cpu_bound_nlp_is_off_the_event_loop.py for the case
# where a package __init__ binds an instance over a submodule name and the
# dotted form silently patches the wrong object. sys.modules is keyed by the
# real dotted path and cannot be shadowed.
lifespan_module = sys.modules["lifespan"]
settings_module = sys.modules["settings"]
db_module = sys.modules["db"]

lifespan = lifespan_module.lifespan

# asyncio_mode=auto (pyproject) runs async tests without an explicit mark.

# Long enough to be unmistakable against a 0.05s probe interval, short enough to
# keep the suite quick.
_BLOCK_S = 0.5
_FAST_INTERVAL_S = 0.05
_FAST_WARN_S = 0.1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fake_settings(*, interval: float, warn: float) -> types.SimpleNamespace:
    """Settings stand-in covering every attribute the lifespan loops read.

    ⚠️ ``environment`` is NOT optional padding. ``logging_config.add_service_context``
    is a structlog processor that runs on EVERY record and does
    ``get_settings().environment``; once ``get_settings`` is stubbed here, a stub
    missing that attribute raises ``AttributeError`` from inside the logging
    pipeline, i.e. the probe's own ``log.warning`` call blows up. Measured
    2026-09-04 on untouched ``main``: 6 of the 12 tests in this file — every one
    that drives ``lifespan()`` — failed with
    ``AttributeError: 'types.SimpleNamespace' object has no attribute 'environment'``
    the moment that processor started reading settings. A settings stub must
    therefore cover what the LOGGER reads, not only what the code under test reads.
    """
    return types.SimpleNamespace(
        event_loop_lag_interval_s=interval,
        event_loop_lag_warn_s=warn,
        environment="test",
        # lifespan() starts the dedicated metrics listener from this value.
        metrics_port=int(os.environ["METRICS_PORT"]),
    )


def _patch_settings(monkeypatch, *, interval: float, warn: float) -> None:
    """Point ``from settings import get_settings`` (done inside the loops) at a stub."""
    monkeypatch.setattr(
        settings_module, "get_settings", lambda: _fake_settings(interval=interval, warn=warn)
    )


def _lifespan_patches():
    """Telemetry (external OTEL collector) is stubbed; the startup ``SELECT 1`` runs
    against the real test database, so the second element is a no-op context."""
    return (
        patch.object(lifespan_module, "setup_telemetry", return_value=False),
        contextlib.nullcontext(),
    )


def _events(caplog, name: str) -> list[str]:
    """Rendered log lines whose structlog ``event`` is ``name``.

    Matching is on the rendered message because the renderer differs by
    LOG_FORMAT (JSONRenderer vs ConsoleRenderer); both embed the event name
    verbatim.
    """
    return [r.getMessage() for r in caplog.records if name in r.getMessage()]


def _blocked_lags(caplog) -> list[float]:
    """Every ``lag_s`` reported by an ``event_loop.blocked`` warning."""
    lags: list[float] = []
    for message in _events(caplog, "event_loop.blocked"):
        try:
            payload = json.loads(message)
        except ValueError, TypeError:
            payload = None
        if isinstance(payload, dict) and "lag_s" in payload:
            lags.append(float(payload["lag_s"]))
            continue
        match = re.search(r"lag_s[=:]\s*'?([0-9.]+)", message)
        if match is not None:
            lags.append(float(match.group(1)))
    return lags


def _find_lag_task() -> asyncio.Task | None:
    """The running ``event_loop_lag_loop`` task, located by its code object name."""
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        code = getattr(coro, "cr_code", None)
        if code is not None and code.co_name == "event_loop_lag_loop":
            return task
    return None


def _block_the_loop(seconds: float) -> None:
    """Genuinely block the event loop for ``seconds``.

    ``call_soon`` runs the callback on the loop thread from the ready queue, so a
    synchronous ``time.sleep`` inside it stops the loop dead: no timer fires, no
    task resumes. This is the real defect shape from issue #111, not a simulation
    of it.
    """
    asyncio.get_running_loop().call_soon(time.sleep, seconds)


# ---------------------------------------------------------------------------
# 1. _pool_stats — the diagnostic context sampled alongside the warning.
# ---------------------------------------------------------------------------


def test_pool_stats_never_raises_when_the_engine_blows_up(monkeypatch):
    """Pins that diagnostics can never be the reason the warning fails to emit."""

    class ExplodingEngine:
        @property
        def pool(self):
            raise RuntimeError("engine is disposed")

    monkeypatch.setattr(db_module, "engine", ExplodingEngine())

    assert lifespan_module._pool_stats() == {}


# ---------------------------------------------------------------------------
# 2. THE CRITICAL TEST: the probe detects a loop that is actually blocked.
# ---------------------------------------------------------------------------
async def test_probe_warns_when_the_loop_is_genuinely_blocked(monkeypatch, caplog):
    """Pins the whole point: a real loop block produces event_loop.blocked with a matching lag_s."""
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=_FAST_WARN_S)
    # Sentinel pool counters prove they are sampled at the same instant as the
    # warning, which is what distinguishes "loop blocked" from "pool exhausted".
    monkeypatch.setattr(lifespan_module, "_pool_stats", lambda: {"pool_checkedout": 41})

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            await asyncio.sleep(_FAST_INTERVAL_S * 4)  # let the probe settle
            _block_the_loop(_BLOCK_S)
            await asyncio.sleep(_BLOCK_S + _FAST_INTERVAL_S * 8)

    lags = _blocked_lags(caplog)
    assert lags, (
        "the event loop was blocked for "
        f"{_BLOCK_S}s and no event_loop.blocked warning was emitted — the probe "
        "is gone or no longer measures loop blocking (issue #111)"
    )
    worst = max(lags)
    assert worst >= _FAST_WARN_S, f"lag_s {worst} is below its own warn threshold {_FAST_WARN_S}"
    assert worst >= _BLOCK_S - _FAST_INTERVAL_S * 2 - 0.1, (
        f"reported lag_s {worst} is far below the {_BLOCK_S}s block actually "
        "imposed — the measurement under-reports the stall a probe is meant to size"
    )
    # Very loose upper bound: only catches a wildly wrong unit/arithmetic, never
    # a slow box (issue #106).
    assert worst < _BLOCK_S * 10, (
        f"reported lag_s {worst} is implausibly large for a {_BLOCK_S}s block"
    )
    assert any("pool_checkedout" in m for m in _events(caplog, "event_loop.blocked")), (
        "pool counters were not attached to the warning — the two surviving "
        "explanations (blocked loop vs. exhausted pool) become indistinguishable"
    )


# ---------------------------------------------------------------------------
# 3. The converse: a healthy loop must stay silent (no alert fatigue).
# ---------------------------------------------------------------------------
async def test_no_warning_on_a_healthy_loop(monkeypatch, caplog):
    """Pins that an unblocked loop emits zero event_loop.blocked records."""
    # warn=1.0s so only a full second of stall could trip it; a loaded box
    # (issue #106) will not fabricate that out of an idle await.
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=1.0)

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            await asyncio.sleep(_FAST_INTERVAL_S * 10)

    assert _blocked_lags(caplog) == [], (
        "event_loop.blocked fired on an idle loop — the probe reports its own "
        "sleep as lag and every warning it ever emits is noise"
    )


# ---------------------------------------------------------------------------
# 4. The kill switch.
# ---------------------------------------------------------------------------
async def test_probe_is_disabled_when_the_interval_is_not_positive(monkeypatch, caplog):
    """Pins that interval <= 0 turns the probe off (logged) instead of spinning."""
    _patch_settings(monkeypatch, interval=0.0, warn=_FAST_WARN_S)

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.INFO), telemetry, session_local:
        async with lifespan(FastAPI()):
            await asyncio.sleep(_FAST_INTERVAL_S * 4)

    startup_lines = _events(caplog, "startup.event_loop_lag")
    assert startup_lines, "the probe never logged its startup status"
    assert any("disabled" in line for line in startup_lines), (
        f"expected a disabled status in {startup_lines}"
    )
    assert _blocked_lags(caplog) == []


# ---------------------------------------------------------------------------
# 5. Shutdown must not have to wait out a whole interval.
# ---------------------------------------------------------------------------
async def test_the_probe_does_not_outlive_the_lifespan_context(monkeypatch, caplog):
    """Pins that the probe cannot hold pod termination open, even with a 30s interval.

    ⚠️ Honest scope, established by mutation rather than assumed. This pins the
    OUTCOME (prompt exit), and deliberately claims nothing about the mechanism,
    because prompt exit is guaranteed TWICE over:

      1. the cooperative `wait_for(shutdown_event.wait(), timeout=interval)`
         returns the instant the event is set, so the loop self-exits; and
      2. lifespan's shutdown also `cancel()`s the task and awaits it.

    Either alone is sufficient. Mutating away the cooperative wait (plain
    `asyncio.sleep`) leaves all seven green because (2) covers it; mutating away
    the cancel-tuple entry leaves them green because (1) covers it. That is
    defence in depth, not a gap — but it does mean no single mutation can turn
    this test red, so do not read it as pinning either mechanism individually.
    """
    long_interval = 30.0
    _patch_settings(monkeypatch, interval=long_interval, warn=1.0)

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.INFO), telemetry, session_local:
        # Exiting the lifespan context IS the production shutdown path: it sets
        # the shutdown event, then cancels and awaits every task. Timing that
        # exit tests the real sequence, rather than reaching into the closure's
        # frame for its free variables — which pinned CPython internals and was
        # not even type-safe (`get_coro()` is Coroutine | None).
        async with lifespan(FastAPI()):
            await asyncio.sleep(_FAST_INTERVAL_S * 2)
            task = _find_lag_task()
            assert task is not None, "the event_loop_lag_loop task is not running"
            started = time.monotonic()
        elapsed = time.monotonic() - started
        assert task.done(), "the lag task outlived the lifespan context"

    assert elapsed < long_interval / 2, (
        f"shutdown took {elapsed:.2f}s with a {long_interval}s probe interval — "
        "the lag task is no longer cancelled and awaited on lifespan exit, so it "
        "would hold pod termination open for up to a full interval"
    )


# ---------------------------------------------------------------------------
# 4. db_pool.saturated — the OTHER half of the #111 discriminator (2026-09-03).
#
# The pool counters were attached ONLY to `event_loop.blocked`, i.e. they were
# emitted only when the loop hypothesis was true. So when the pool actually did
# exhaust — measured 2026-09-03, `QueuePool limit of size 5 overflow 10 reached,
# timeout 30.00`, while loop lag never once crossed its warn threshold — the
# instrument built to attribute #111 stayed completely silent, and the only
# evidence was a stack trace raised 30s after the damage was done.
#
# The two tests below are a matched pair on purpose: a gauge that always fires is
# exactly as useless as one that never does, and only the negative leg can tell
# them apart.
# ---------------------------------------------------------------------------
def _saturation_events(caplog) -> list[str]:
    return _events(caplog, "db_pool.saturated")


class _FakePoolAt:
    """A pool reporting a fixed `checkedout`; the other counters are constants."""

    def __init__(self, checkedout: int):
        self._checkedout = checkedout

    def checkedout(self):
        return self._checkedout

    def checkedin(self):
        return 0

    def size(self):
        return db_module.POOL_SIZE

    def overflow(self):
        return db_module.MAX_OVERFLOW


async def test_saturation_warning_fires_when_every_slot_is_checked_out(monkeypatch, caplog):
    """A full pool must announce itself WHILE it is full, not via a later traceback."""
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=_FAST_WARN_S)
    ceiling = db_module.POOL_SIZE + db_module.MAX_OVERFLOW
    monkeypatch.setattr(db_module, "engine", types.SimpleNamespace(pool=_FakePoolAt(ceiling)))

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            await asyncio.sleep(_FAST_INTERVAL_S * 6)

    events = _saturation_events(caplog)
    assert events, (
        f"the pool reported {ceiling}/{ceiling} connections checked out and no "
        "db_pool.saturated warning was emitted — saturation is once again "
        "invisible until it throws"
    )
    assert any("pool_checkedout" in m for m in events), (
        "the warning must carry the counters; without them it names a condition "
        "but not its size, which is what made #111 unanswerable"
    )


async def test_no_saturation_warning_when_the_pool_has_headroom(monkeypatch, caplog):
    """The vacuity control: a gauge that always fires would pass the test above."""
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=_FAST_WARN_S)
    monkeypatch.setattr(db_module, "engine", types.SimpleNamespace(pool=_FakePoolAt(0)))

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            await asyncio.sleep(_FAST_INTERVAL_S * 6)

    assert not _saturation_events(caplog), (
        "an idle pool produced a saturation warning — the predicate cannot "
        "distinguish the condition it exists to report"
    )


# ---------------------------------------------------------------------------
# 5. holders= on db_pool.saturated — does the instrument REACH the log line?
#
# `src/pool_watch.py` has its own unit tests (tests/test_pool_holder_instrument.py):
# they prove the registry records and releases. They prove NOTHING about the wire
# between it and the warning. `_pool_holders()` swallows every exception, so a
# mis-wired import, a renamed symbol or a genuinely broken registry all degrade to
# `[]` — and in a pool-exhaustion investigation `holders: []` reads identically to
# "nobody is holding a connection", which is the module's documented *negative
# result* (it refutes the leak hypothesis and points at arrival rate). A silently
# dead instrument would therefore not merely fail to help; it would fabricate a
# refutation. These tests make the wire itself observable.
# ---------------------------------------------------------------------------
def _saturation_lines_carrying_holders(caplog: pytest.LogCaptureFixture) -> list[str]:
    """`db_pool.saturated` lines that actually carry a ``holders`` key.

    Substring matching on the rendered line, for the same reason ``_events`` does
    it: the renderer differs by LOG_FORMAT (JSONRenderer emits ``"holders": [...]``,
    ConsoleRenderer emits ``holders=[...]``) and both embed the key name and the
    holder's ``caller`` string verbatim.
    """
    return [m for m in _saturation_events(caplog) if "holders" in m]


def _exploding_holders(limit: int = 5) -> list[dict[str, Any]]:
    """A `pool_watch.holders` that fails the way a broken registry would."""
    raise RuntimeError("the holder registry exploded")


# KILLS: deleting `holders=_pool_holders()` from the `db_pool.saturated` warning
# call in src/lifespan.py, and any rewiring of `_pool_holders` that stops it
# reaching the real registry (wrong import path, wrong symbol, a stubbed-out
# body) — all of which the bare `except Exception: return []` would otherwise
# turn into a silent, permanent, and *plausible-looking* empty list.
async def test_the_saturation_warning_names_who_is_holding_a_connection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A real checkout, recorded by the real instrument, must appear on the warning.

    ⚠️ This deliberately does NOT monkeypatch `pool_watch.holders`. Patching the
    function under test would only prove that whatever it returns is forwarded as
    a kwarg — it could not distinguish a live instrument from a dead one, which is
    the entire failure mode. So the holder is created through the production
    recording path: a locally-created engine with `pool_watch.install()` on it, a
    `caller_var` set the way a route sets it, and a genuine connection checked out
    and held open across the probe's sampling window.

    The engine is private (`pool_size=1, max_overflow=0`) and built from
    `os.environ["DATABASE_URL"]`, following tests/test_pool_holder_instrument.py,
    so it never touches `db.engine` or the session-scoped `test_engine`. The
    *counters* are still faked — an honest 15/15 exhaustion would need 15 real
    connections and would starve the rest of the suite — but the holder is real.

    Ordering is load-bearing: the connection is checked out BEFORE `lifespan()`
    starts, because the saturation warning is rate-limited to one per 10s. If the
    probe's first sample landed before the checkout was recorded, the only warning
    of the test would carry an empty list and the assertion would fail for a
    timing reason rather than a real one.
    """
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=_FAST_WARN_S)
    ceiling = db_module.POOL_SIZE + db_module.MAX_OVERFLOW
    monkeypatch.setattr(db_module, "engine", types.SimpleNamespace(pool=_FakePoolAt(ceiling)))

    # The registry is process-wide; clear it so the assertion cannot be satisfied
    # by another test's leftover holder, and so ours is inside `holders(5)`.
    pool_watch.clear()
    label = f"lag-probe-{uuid.uuid4().hex[:8]}"
    caller = f"POST /pool-holder-reaches-the-log/{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    pool_watch.install(engine, label)
    token = pool_watch.caller_var.set(caller)

    try:
        telemetry, session_local = _lifespan_patches()
        with caplog.at_level(logging.WARNING), telemetry, session_local:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                async with lifespan(FastAPI()):
                    await asyncio.sleep(_FAST_INTERVAL_S * 6)
    finally:
        pool_watch.caller_var.reset(token)
        await engine.dispose()
        pool_watch.clear()

    events = _saturation_events(caplog)
    assert events, (
        "premise broken: the pool reported "
        f"{ceiling}/{ceiling} checked out and no db_pool.saturated warning was "
        "emitted, so this test proves nothing about holders"
    )
    with_holders = _saturation_lines_carrying_holders(caplog)
    assert with_holders, (
        "db_pool.saturated was emitted with no `holders` key at all — the "
        "saturation gauge names a condition but not a single connection holder, "
        "which is exactly the evidence gap it was added to close"
    )
    assert any(caller in m for m in with_holders), (
        f"a connection was genuinely checked out under caller {caller!r} while "
        "the pool reported saturation, and it does not appear in the warning's "
        f"holders list: {with_holders}. An empty/stale holders list is NOT a "
        "harmless omission — it is read as 'no one is holding', i.e. as a "
        "refutation of the leak hypothesis, so a dead instrument fabricates a "
        "negative result instead of producing none"
    )


# KILLS: removing the `try/except Exception: return []` from `_pool_holders()` in
# src/lifespan.py. Without it, a raising registry propagates out of the kwargs
# evaluation of `log.warning(...)`, which `event_loop_lag_loop` only guards
# against `CancelledError` — so the probe task dies, the saturation warning is
# never emitted, and BOTH halves of the #111 discriminator go dark permanently
# because a *diagnostic* failed.
async def test_a_raising_holder_instrument_does_not_take_the_probe_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken instrument must degrade to `holders: []`, never to a dead probe."""
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=_FAST_WARN_S)
    ceiling = db_module.POOL_SIZE + db_module.MAX_OVERFLOW
    monkeypatch.setattr(db_module, "engine", types.SimpleNamespace(pool=_FakePoolAt(ceiling)))
    # Patched on the module OBJECT, not via a dotted string — see the note at the
    # top of this file. `_pool_holders` does `from pool_watch import holders`
    # INSIDE the function, so the lookup happens per call and sees this.
    monkeypatch.setattr(pool_watch, "holders", _exploding_holders)

    # The degradation shape, asserted directly: `[]`, not a propagated RuntimeError.
    assert lifespan_module._pool_holders() == [], (
        "_pool_holders() let a broken registry escape — a diagnostic is about to "
        "become the outage it was added to explain"
    )

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            await asyncio.sleep(_FAST_INTERVAL_S * 6)
            task = _find_lag_task()
            assert task is not None and not task.done(), (
                "the event_loop_lag_loop task is gone after sampling a saturated "
                "pool with a raising holder instrument — one broken diagnostic "
                "silenced the whole probe, including event_loop.blocked"
            )

    events = _saturation_events(caplog)
    assert events, (
        "the pool was saturated and no db_pool.saturated warning survived a "
        "raising pool_watch.holders — the instrument's failure suppressed the "
        "very alarm it was meant to enrich"
    )
    assert any(("holders=[]" in m) or ('"holders": []' in m) for m in events), (
        "the warning was emitted but its holders field is neither absent nor an "
        f"empty list: {events}. The documented degradation is `[]`"
    )


# KILLS: making the saturation branch unconditional (e.g. dropping the
# `if _pool_saturated(stats):` guard). Without this leg, the two tests above
# would both pass on a gauge that fires on every single sample, which would be
# exactly as useless as one that never fires.
#
# tests/test_no_saturation_warning_when_the_pool_has_headroom above is the same
# control for the *counters*; this one closes it for the *holder* path
# specifically — the presence of a recorded holder must not by itself be enough
# to trip the warning, or "someone is using the database" becomes an alarm.
async def test_a_recorded_holder_alone_does_not_trip_the_saturation_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The non-vacuity control: a holder on a pool with headroom stays silent."""
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=_FAST_WARN_S)
    monkeypatch.setattr(db_module, "engine", types.SimpleNamespace(pool=_FakePoolAt(0)))

    pool_watch.clear()
    caller = f"GET /headroom-control/{uuid.uuid4().hex[:8]}"
    token = pool_watch.caller_var.set(caller)
    try:
        # Recorded through pool_watch's own bookkeeping rather than a real
        # checkout: this leg only needs a non-empty registry, and a second live
        # engine would be cost with no added guarantee.
        pool_watch._record(id(caplog), "headroom-control")
        assert pool_watch.holders(5), "premise broken: no holder recorded"

        telemetry, session_local = _lifespan_patches()
        with caplog.at_level(logging.WARNING), telemetry, session_local:
            async with lifespan(FastAPI()):
                await asyncio.sleep(_FAST_INTERVAL_S * 6)
    finally:
        pool_watch.caller_var.reset(token)
        pool_watch.clear()

    assert not _saturation_events(caplog), (
        "a pool with every slot free emitted db_pool.saturated while a holder was "
        "recorded — the warning is tracking 'is anyone using the database' rather "
        "than 'is the pool full', so the two tests above prove nothing"
    )


# ---------------------------------------------------------------------------
# 6. The saturation sweep is ENGINE-COMPLETE, per-label throttled, and
#    noise-bounded (2026-09-04).
#
# Three defects were closed at once, and each needs its own leg because each can
# regress without touching the others:
#
#   (a) THE CEILING CAME FROM THE MODULE, NOT THE POOL. `pool_is_saturated`
#       compared `checkedout` against the RUNTIME constants
#       (POOL_SIZE + MAX_OVERFLOW = 15) whatever engine's counters it was handed.
#       `health_engine` has capacity 1 — the pool #140 created *precisely
#       because* one-slot contention is the condition worth reporting — so the
#       gauge would have needed 14 checkouts of a one-slot pool to fire. Not
#       "unlikely to fire": structurally incapable of it.
#
#   (b) ONE THROTTLE FOR ALL ENGINES. The sweep now visits every watched engine,
#       but a single `last_saturation_warn` float means the first engine to warn
#       suppresses every other engine for the whole 10s window — reintroducing,
#       one layer up, exactly the false negative that making the sweep
#       engine-complete was meant to remove.
#
#   (c) NO DWELL GATE. `health_engine` is legitimately "saturated" for the few
#       milliseconds of every `SELECT 1`, and the probe samples twice a second.
#       Without the two-consecutive-sample gate the log floods with true-but-
#       worthless lines, and the operator learns to scroll past the field that
#       was added to attribute a production outage.
#
# ⚠️ TIMING. These legs never `sleep` then assert. This box runs ~30 concurrent
# sessions at load ~80, and a fixed sleep is a coin-flip there. Every leg polls a
# CONDITION (a log record appeared / the fake pool has been sampled N times)
# against a generous deadline, and every assertion is about a log FIELD or a
# COUNT — never about elapsed time.
# ---------------------------------------------------------------------------
_POLL_DEADLINE_S = 20.0
_POLL_TICK_S = 0.01


class _FixedPool:
    """A pool that reports a FIXED capacity of its own, unrelated to the runtime one.

    ``_max_overflow`` is a private attribute rather than an accessor, which is how
    ``db.pool_stats`` reads it — so it is set as an attribute here rather than
    exposed as a method, or ``pool_limit`` would never be emitted and the whole
    point of these tests (a per-pool ceiling) would silently fall back to 15.
    """

    def __init__(self, checkedout: int, size: int, max_overflow: int = 0):
        self._checkedout = checkedout
        self._size = size
        self._max_overflow = max_overflow

    def checkedout(self):
        return self._checkedout

    def checkedin(self):
        return 0

    def size(self):
        return self._size

    def overflow(self):
        return self._checkedout - self._size


class _CountingPool(_FixedPool):
    """A one-slot pool that is saturated for exactly its first ``saturated_for`` samples.

    Counts its own ``checkedout()`` calls so a test can wait on SAMPLES TAKEN
    rather than on the clock: ``_pool_stats`` reads ``checkedout`` exactly once
    per engine per probe tick, so ``.samples`` IS the probe's sample count for
    this engine. That is what makes the negative leg below deterministic on a
    loaded box — "no warning after six samples" is a statement about the probe,
    "no warning after 300ms" is a statement about the scheduler.
    """

    def __init__(self, saturated_for: int):
        super().__init__(checkedout=0, size=1, max_overflow=0)
        self._saturated_for = saturated_for
        self.samples = 0

    def checkedout(self):
        self.samples += 1
        return 1 if self.samples <= self._saturated_for else 0


def _fake_engine(pool) -> types.SimpleNamespace:
    """The minimum an object needs to be a `db.pool_stats` target: a `.pool`."""
    return types.SimpleNamespace(pool=pool)


def _saturation_engine_labels(caplog) -> list[str]:
    """The ``engine=`` field of every ``db_pool.saturated`` record, in order.

    Parsed from the rendered line for the same reason ``_events`` matches on it:
    the renderer differs by LOG_FORMAT (JSONRenderer emits ``"engine": "x"``,
    ConsoleRenderer emits ``engine=x``), and a test that only worked under one of
    them would pass or fail on an environment variable.
    """
    labels: list[str] = []
    for message in _saturation_events(caplog):
        try:
            payload = json.loads(message)
        except ValueError, TypeError:
            payload = None
        if isinstance(payload, dict) and "engine" in payload:
            labels.append(str(payload["engine"]))
            continue
        match = re.search(r"engine[=:]\s*'?\"?([A-Za-z0-9_.\-]+)", message)
        if match is not None:
            labels.append(match.group(1))
    return labels


async def _poll_until(predicate, *, deadline_s: float = _POLL_DEADLINE_S) -> bool:
    """Await ``predicate()`` becoming true, or the deadline. Returns whether it did.

    Deliberately returns rather than asserting, so each caller can raise its own
    diagnostic message. The deadline is generous on purpose: it exists to stop a
    genuinely broken build from hanging the suite, NOT to measure anything. No
    assertion in this section is ever made about how long this took.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_POLL_TICK_S)
    return predicate()


# KILLS: reverting the ceiling in `db.pool_is_saturated` to
# `(POOL_SIZE + MAX_OVERFLOW) - headroom`. Under that revert the capacity-1 leg
# asks `1 >= 14` and goes red, which is the exact arithmetic that made
# `health_engine` unmeasurable in production.
def test_the_saturation_ceiling_comes_from_the_pool_not_the_module():
    """A one-slot pool with its one slot taken IS saturated — 1 of 1, not 1 of 15.

    ``health_engine`` is ``pool_size=1, max_overflow=0``. Before 2026-09-04 the
    predicate compared its ``checkedout`` against the RUNTIME engine's ceiling of
    15 (``POOL_SIZE=5 + MAX_OVERFLOW=10``), so reporting saturation on it required
    14 simultaneous checkouts of a pool that can hold one. The readiness probe's
    dedicated pool — added by #140 specifically so that one-slot contention would
    be visible — was therefore the one pool the gauge could never describe.

    The headroom clamp is the other half: with the default ``headroom=1`` and a
    limit of 1, an unclamped ``checkedout >= limit - headroom`` is
    ``checkedout >= 0``, i.e. "saturated while idle". ``room = min(headroom,
    limit - 1)`` collapses to 0 here, so the question asked of a one-slot pool is
    the only meaningful one: is its single slot taken?

    The backwards-compatibility leg is not padding. Every pre-existing caller and
    test in this repo hands in a hand-built dict with only ``pool_checkedout``
    (see ``_FakePoolAt`` above, whose fake pool exposes no ``_max_overflow`` and
    therefore yields no ``pool_limit``). If the absent-key fallback stopped
    resolving to 15, those callers would silently change meaning rather than
    fail.
    """
    saturated = db_module.pool_is_saturated
    limit_1 = {"pool_checkedout": 1, "pool_size": 1, "pool_limit": 1}
    idle_1 = {"pool_checkedout": 0, "pool_size": 1, "pool_limit": 1}

    assert saturated(limit_1) is True, (
        "a pool of capacity 1 with its only slot checked out was not reported as "
        "saturated — the ceiling is being taken from the runtime module constants "
        f"({db_module.POOL_SIZE} + {db_module.MAX_OVERFLOW} = "
        f"{db_module.POOL_SIZE + db_module.MAX_OVERFLOW}) instead of from "
        "stats['pool_limit'], which makes health_engine structurally unmeasurable"
    )
    assert saturated(idle_1) is False, (
        "an IDLE pool of capacity 1 was reported as saturated — the headroom "
        "clamp (room = min(headroom, limit - 1)) is gone, so the gauge fires "
        "continuously on every small pool and means nothing"
    )

    ceiling = db_module.POOL_SIZE + db_module.MAX_OVERFLOW
    assert saturated({"pool_checkedout": ceiling - 1}) is True, (
        "a stats dict carrying no pool_limit must fall back to the runtime "
        f"ceiling of {ceiling}; {ceiling - 1} checked out leaves one slot and is "
        "the documented trip point for every pre-existing caller"
    )
    assert saturated({"pool_checkedout": ceiling - 2}) is False, (
        f"{ceiling - 2} of {ceiling} checked out leaves two slots free and must "
        "not trip the no-pool_limit fallback path"
    )


# KILLS: replacing the per-label `last_saturation_warn: dict[str, float]` in
# `event_loop_lag_loop` with a single float scalar (the pre-2026-09-04 throttle).
# The first engine to warn then suppresses the second for the whole 10s window,
# only one line is emitted, and the second engine's exhaustion is invisible.
async def test_two_saturated_engines_each_get_their_own_warning(monkeypatch, caplog):
    """Two pools full at once must produce two lines, each naming its own engine.

    The sweep became engine-complete because a second engine with settings
    identical to the runtime engine's (the since-removed owner engine) emits the byte-identical
    ``QueuePool limit of size 5 overflow 10 reached`` while ``db_pool.saturated``
    stayed silent — letting a reader conclude "the instrument is deployed and
    quiet, so nothing saturated". Sweeping every engine fixes that only if the
    THROTTLE is also per-engine: the warning is rate-limited to one per 10s
    (the probe samples twice a second and an episode lasts tens of seconds, so
    unthrottled it would emit ~60 identical lines and bury what it names), and a
    single shared timestamp means engine #2 is suppressed by engine #1 for the
    entire episode. That is the same false negative moved up one layer.

    The two fakes carry DIFFERENT ``checkedout`` values so the assertion can also
    show each line carries that engine's OWN counters — a sweep that reported
    every engine using the runtime engine's stats would name the right pool and
    then describe the wrong one.
    """
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=_FAST_WARN_S)

    alpha = _fake_engine(_FixedPool(checkedout=15, size=5, max_overflow=10))
    beta = _fake_engine(_FixedPool(checkedout=1, size=1, max_overflow=0))
    monkeypatch.setattr(db_module, "watched_engines", lambda: [("alpha", alpha), ("beta", beta)])

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            got_both = await _poll_until(lambda: len(set(_saturation_engine_labels(caplog))) >= 2)

    labels = _saturation_engine_labels(caplog)
    assert got_both, (
        "two engines reported themselves saturated on every sample and only "
        f"{sorted(set(labels))} produced a db_pool.saturated warning — the "
        "per-label throttle has collapsed back to a single scalar, so the first "
        "engine to warn silences every other engine for the whole 10s window"
    )
    assert set(labels) == {"alpha", "beta"}, (
        f"expected one warning per saturated engine, got engine labels {labels}"
    )
    # The 10s throttle means each label may warn at most once in this window; a
    # duplicate would prove the throttle is not keyed on the label at all.
    assert labels.count("alpha") == 1 and labels.count("beta") == 1, (
        f"an engine warned more than once inside the 10s throttle window: {labels}"
    )
    lines = _saturation_events(caplog)
    assert any("15" in m and "alpha" in m for m in lines), (
        "the alpha line does not carry alpha's own pool_checkedout of 15 — each "
        f"warning must describe the engine it names: {lines}"
    )


# KILLS: deleting the `if seen < 2: continue` dwell gate from the saturation
# sweep in `event_loop_lag_loop`. Without it the single-sample leg below emits a
# db_pool.saturated record and goes red.
async def test_one_saturated_sample_is_not_enough_to_warn(monkeypatch, caplog):
    """A blip must stay silent; two consecutive samples must warn exactly once.

    This is the noise bound, and it is not hypothetical. ``health_engine`` has
    capacity 1, so it is genuinely saturated for the few milliseconds of every
    ``SELECT 1`` the readiness probe runs — and with the ceiling fix above, that
    momentary truth is now VISIBLE to the gauge for the first time. Sampled twice
    a second, an ungated predicate would emit a stream of true-but-worthless
    warnings about a pool doing exactly its job, and the field added to attribute
    a production outage becomes the field operators filter out.

    Two consecutive saturated samples (~0.5s of dwell at the production interval)
    is the line between "a probe doing its job" and "a probe that is stuck".

    Both legs wait on the fake pool's own SAMPLE COUNTER rather than on a sleep:
    ``db.pool_stats`` reads ``checkedout()`` exactly once per engine per tick, so
    ``pool.samples`` is the probe's sample count for this engine. The negative leg
    is meaningless without that — "no warning yet" after an untimed sleep on a
    loaded box would pass even if the probe had never sampled at all.
    """
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=_FAST_WARN_S)

    # ── Leg 1: saturated on ONE sample, clear on every sample after it.
    blip = _CountingPool(saturated_for=1)
    monkeypatch.setattr(db_module, "watched_engines", lambda: [("blip", _fake_engine(blip))])

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            sampled = await _poll_until(lambda: blip.samples >= 6)

    assert sampled, (
        f"premise broken: the probe took only {blip.samples} samples of the fake "
        "pool, so 'no warning' proves nothing about the dwell gate"
    )
    assert not _saturation_events(caplog), (
        "a pool that was saturated on exactly ONE of "
        f"{blip.samples} samples produced a db_pool.saturated warning — the "
        "two-consecutive-sample gate is gone, so health_engine's normal "
        "millisecond-long SELECT 1 checkout will now flood the log twice a second"
    )

    # ── Leg 2: the non-vacuity control. The gate must not simply never fire.
    caplog.clear()
    dwelling = _CountingPool(saturated_for=2)
    monkeypatch.setattr(
        db_module, "watched_engines", lambda: [("dwelling", _fake_engine(dwelling))]
    )

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            warned = await _poll_until(lambda: bool(_saturation_events(caplog)))
            # Keep sampling past the saturated window so a gate that fires on
            # EVERY subsequent sample would show up as more than one record.
            await _poll_until(lambda: dwelling.samples >= 6)

    events = _saturation_events(caplog)
    assert warned and events, (
        "a pool saturated on two CONSECUTIVE samples emitted no db_pool.saturated "
        f"warning after {dwelling.samples} samples — the dwell gate no longer "
        "lets anything through, which is exactly as useless as no gate at all"
    )
    assert len(events) == 1, f"expected exactly one warning for a two-sample episode, got {events}"
    assert _saturation_engine_labels(caplog) == ["dwelling"], (
        f"the warning does not name the engine that dwelled: {_saturation_engine_labels(caplog)}"
    )


class _AlternatingPool(_FixedPool):
    """A pool of stated capacity that is saturated on every OTHER sample.

    ``saturated, clear, saturated, clear`` — i.e. it is FULL half the time and
    never full on two consecutive reads. That is not a contrived shape: it is the
    ARRIVAL-RATE regime ("no holder older than ~1s", many short checkouts turning
    over faster than the pool serves them) which is the surviving hypothesis for
    the 2026-09-03 exhaustion, sampled by a probe that reads twice a second.

    Like ``_CountingPool`` it counts its own ``checkedout()`` calls so a test can
    wait on SAMPLES TAKEN rather than on the clock — ``db.pool_stats`` reads
    ``checkedout`` exactly once per engine per tick, so ``.samples`` IS the
    probe's sample count for this engine.

    ⚠️ ``overflow()`` is inherited unchanged and deliberately does NOT delegate to
    ``checkedout()``: a second call per tick would advance this counter, and the
    counter is this test's only clock. Nothing in ``pool_is_saturated`` reads
    ``overflow`` — the ceiling comes from ``pool_size + _max_overflow`` and the
    occupancy from ``checkedout`` — so the inherited value is inert here.
    """

    def __init__(self, *, size: int, max_overflow: int = 0):
        super().__init__(checkedout=0, size=size, max_overflow=max_overflow)
        self.samples = 0

    def checkedout(self):
        self.samples += 1
        return (self._size + self._max_overflow) if self.samples % 2 == 1 else 0


# KILLS: reverting the dwell gate in `event_loop_lag_loop` to the unconditional
# `if seen < 2: continue` (i.e. dropping the `0 < limit <=
# _DWELL_REQUIRED_AT_OR_BELOW` guard). Leg 1 then goes red: the 15-slot pool
# alternates, never reaches two consecutive saturated samples, and emits nothing.
async def test_the_dwell_gate_applies_only_to_pools_that_are_tiny(monkeypatch, caplog):
    """A 15-slot pool at 15/15 must warn on the FIRST sample; a 1-slot pool must not.

    The gate above (`test_one_saturated_sample_is_not_enough_to_warn`) exists
    because ``health_engine`` has capacity 1 and is therefore legitimately
    "saturated" for the few milliseconds of every ``SELECT 1``, sampled twice a
    second. It was first written to apply to EVERY engine, and an adversarial
    review measured the consequence: a 15-slot fake pool reading **15/15 on 20 of
    40 samples emitted ZERO** ``db_pool.saturated`` lines, because it never
    happened to be saturated on two CONSECUTIVE reads.

    That is not an edge case, it is the case. A 15-slot pool at its ceiling is
    never routine — and the regime that produces the alternating shape (many
    short checkouts turning over fast, "no holder older than ~1s") is exactly the
    ARRIVAL-RATE explanation that survived the 2026-09-03 investigation, after
    the slow-holder and stale-socket hypotheses were refuted. So the gate blinded
    the gauge in the one situation it most needed to see, while remaining green
    against every test that existed.

    The fix is a capacity predicate, not a removal:
    ``0 < pool_limit <= _DWELL_REQUIRED_AT_OR_BELOW`` (= 1). Both halves are
    asserted here, because either one alone is satisfiable by a mutant:

    * **Leg 1** — a 15-slot pool alternating full/empty produces at least one
      warning naming it. Fails under the unconditional gate.
    * **Leg 2** — a 1-slot pool alternating full/empty produces NONE. Fails if
      the gate is deleted rather than scoped, which would put the log back to a
      true-but-worthless line twice a second about a probe doing its job.
    * **Leg 3** — the vacuity control for leg 2: the same 1-slot engine, saturated
      on two CONSECUTIVE samples, DOES warn. Without it, "the tiny pool stayed
      silent" would also pass a mutant that silenced small pools entirely.

    Every leg waits on the fake pool's own sample counter or on a log record, never
    on elapsed time.
    """
    _patch_settings(monkeypatch, interval=_FAST_INTERVAL_S, warn=_FAST_WARN_S)

    # ── Leg 1: the runtime-shaped pool. 5 + 10 = 15, full on alternate samples.
    big = _AlternatingPool(size=5, max_overflow=10)
    monkeypatch.setattr(db_module, "watched_engines", lambda: [("big", _fake_engine(big))])

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            warned = await _poll_until(lambda: "big" in _saturation_engine_labels(caplog))
            # Keep going past the first alternation so "it never saw two in a row"
            # is a measured fact about this run, not an assumption.
            await _poll_until(lambda: big.samples >= 6)

    assert warned, (
        "a 15-slot pool reported 15 of 15 checked out on alternating samples "
        f"({big.samples} samples taken) and produced NO db_pool.saturated warning "
        "— the two-consecutive-sample dwell gate is being applied to every engine "
        "again. That gate exists only for capacity-1 pools; on a large pool it "
        "silences the arrival-rate regime (many short checkouts turning over "
        "fast) which is the surviving explanation for the exhaustion this "
        "instrument was built to attribute. Measured under the unconditional "
        "version: 20 saturated samples out of 40, ZERO warnings"
    )
    assert _saturation_engine_labels(caplog).count("big") == 1, (
        "expected exactly one warning inside the 10s throttle window, got "
        f"{_saturation_engine_labels(caplog)}"
    )

    # ── Leg 2: the capacity-1 pool. Same alternating shape, must stay silent.
    caplog.clear()
    tiny = _AlternatingPool(size=1, max_overflow=0)
    monkeypatch.setattr(db_module, "watched_engines", lambda: [("tiny", _fake_engine(tiny))])

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            sampled = await _poll_until(lambda: tiny.samples >= 6)

    assert sampled, (
        f"premise broken: the probe took only {tiny.samples} samples of the "
        "one-slot pool, so 'no warning' proves nothing about the dwell gate"
    )
    assert not _saturation_events(caplog), (
        "a capacity-1 pool that was saturated on alternating samples "
        f"({tiny.samples} samples, never two in a row) produced a "
        "db_pool.saturated warning — the dwell gate has been deleted rather than "
        "scoped to small pools, so health_engine's normal millisecond-long "
        "SELECT 1 checkout now floods the log twice a second"
    )

    # ── Leg 3: vacuity control. The capacity-1 path must still be able to warn.
    caplog.clear()
    dwelling = _CountingPool(saturated_for=2)
    monkeypatch.setattr(
        db_module, "watched_engines", lambda: [("dwelling", _fake_engine(dwelling))]
    )

    telemetry, session_local = _lifespan_patches()
    with caplog.at_level(logging.WARNING), telemetry, session_local:
        async with lifespan(FastAPI()):
            dwelt = await _poll_until(lambda: "dwelling" in _saturation_engine_labels(caplog))

    assert dwelt, (
        "the capacity-1 engine did not warn even after two CONSECUTIVE saturated "
        f"samples ({dwelling.samples} samples taken) — leg 2 above is then "
        "vacuous, because a gauge that can never describe a small pool would pass "
        "it. Two consecutive samples (~0.5s of dwell at the production interval) "
        "is the line between a probe doing its job and a probe that is stuck"
    )
