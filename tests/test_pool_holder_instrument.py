"""The pool-holder instrument must actually name the holder — and must let go of it.

`src/pool_watch.py` is the only thing in this estate that can attribute a SQLAlchemy
checkout. pgbouncer says `cl_waiting = 0` and Postgres says `state = idle`; both are true and
neither can see a checkout, because a checkout never leaves the process. So the instrument's
own failure modes are un-noticeable from outside: a dropped `checkout` listener reads exactly
like "no holders", and a dropped `checkin` listener reads exactly like "everything is a
holder" while quietly growing an unbounded dict inside the service it is diagnosing.

Every test below therefore names the mutation it kills, and the engines are built LOCALLY
(`pool_size=1, max_overflow=0`) rather than borrowed from `db.engine` or the session-scoped
`test_engine`, which the rest of the suite depends on.
"""

import asyncio
import gc
import os
import time
import uuid
import warnings
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import pool_watch


class _Recordish:
    """Stand-in for a `_ConnectionRecord`: identity-hashed and weakly referenceable.

    `_ConnectionRecord.__slots__` declares `__weakref__` (verified against SQLAlchemy
    2.0.51), which is what makes the weak-keyed registry possible at all. A bare class
    instance reproduces both properties without needing a live connection, so the
    cap/filter/tally tests below can build a deterministic population.
    """


@pytest_asyncio.fixture
async def watched_engine() -> AsyncGenerator[tuple[AsyncEngine, str], None]:
    """A private engine with the instrument installed, plus its unique label.

    DATABASE_URL is what tests/conftest.py resolves and exports at import time (archetype CI
    value, else TEST_DATABASE_URL, else the local docker DSN — rewritten to a per-worker
    schema under xdist), so this reuses the suite's one source of truth for the DSN.

    pool_size=1/max_overflow=0 makes the checkout sequence deterministic: exactly one
    connection can be out at a time, so a listener firing twice is visible as a count.
    """
    pool_watch.clear()
    label = f"unit-{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    pool_watch.install(engine, label)
    try:
        yield engine, label
    finally:
        await engine.dispose()
        pool_watch.clear()


def _mine(label: str) -> list[dict[str, Any]]:
    """Holders belonging to this test's engine only (the registry is process-wide)."""
    return [h for h in pool_watch.holders(limit=10_000) if h["engine"] == label]


# ── the instrument records ───────────────────────────────────────────────────────────────────
# KILLS: deleting the `checkout` listener (or attaching it to engine.pool, which dispose()
# replaces), and dropping the caller/label/age fields from the record. Without this, the
# instrument's "no holders" answer would be indistinguishable from a genuine refutation of the
# leak hypothesis — the exact false negative the module exists to avoid.
@pytest.mark.asyncio
async def test_a_checked_out_connection_is_recorded_with_its_caller_and_a_real_age(
    watched_engine: tuple[AsyncEngine, str],
) -> None:
    engine, label = watched_engine
    pool_watch.caller_var.set("POST /principles/{id}/evaluate")

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        await asyncio.sleep(0.01)

        mine = _mine(label)
        assert len(mine) == 1, f"the checkout was not recorded at all: {pool_watch.holders(50)}"
        assert mine[0]["caller"] == "POST /principles/{id}/evaluate", (
            "the holder was recorded without its caller — an age with no name cannot "
            "attribute a leak to a code path"
        )
        assert mine[0]["age_s"] > 0.0, "age_s must be a live measurement, not a constant"
        assert "task" in mine[0]


# ── the instrument lets go: THIS IS THE MEMORY-LEAK TEST ─────────────────────────────────────
# KILLS: deleting the `checkin` listener. A missing checkin makes every returned connection
# look like a permanent holder AND grows _holders without bound inside the service being
# diagnosed — an instrument that becomes the outage.
@pytest.mark.asyncio
async def test_the_registry_is_empty_once_the_connection_is_returned(
    watched_engine: tuple[AsyncEngine, str],
) -> None:
    engine, label = watched_engine

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        assert _mine(label), "premise broken: nothing was recorded, so release proves nothing"

    assert _mine(label) == [], (
        "the connection went back to the pool but the instrument still calls it a holder — "
        "checkin is not being observed"
    )


# ── a leaked (garbage-collected) checkout must STOP being a holder ───────────────────────────
# KILLS: reverting the registry to `id(connection_record)` keying, and deleting the `detach`
# listener from install().
#
# This is the one the instrument got wrong. Under an async dialect a checkout that is
# garbage-collected instead of closed is DETACHED, not checked in: `_finalize_fairy` sets
# `detach=True`, `_ConnectionFairy.detach()` clears `fairy_ref` and returns the record to the
# pool, and the trailing `if fairy_ref is not None: checkin()` is therefore skipped. The slot
# comes back; the `checkin` event never fires. With `id()` keys and no `detach` listener the
# entry was immortal, so the warning printed an ever-ageing holder next to
# `pool_checkedout=0` — and the module docstring tells the operator that an old holder means
# a leak, i.e. it pointed them at a code path that had already released. A phantom holder is
# worse than no holder: it is a confident, false accusation.
@pytest.mark.asyncio
async def test_a_garbage_collected_checkout_stops_being_a_holder(
    watched_engine: tuple[AsyncEngine, str],
) -> None:
    engine, label = watched_engine
    pool_watch.caller_var.set("POST /leaks-its-connection")

    async def _leak_one() -> None:
        """Check a connection out and drop it on the floor: no close(), no context manager.

        Done in its own frame deliberately. Leaking from the test body leaves `conn` bound
        in a frame that outlives the `del`, so the connection is never collected and this
        test would pass on its own premise check rather than on the fix.
        """
        conn = await engine.connect()
        await conn.execute(text("SELECT 1"))

    await _leak_one()
    assert len(_mine(label)) == 1, "premise broken: the checkout was never recorded"

    with warnings.catch_warnings():
        # SQLAlchemy warns loudly about a gc-collected, non-checked-in connection. That
        # warning IS the condition under test; it is not a defect in this test.
        warnings.simplefilter("ignore")
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0)

    # `Pool` is typed without the QueuePool counters; the runtime object has them.
    pool: Any = engine.pool
    assert pool.checkedout() == 0, (
        "premise broken: the pool still reports the slot as checked out, so this test is "
        "not exercising the gc/detach path at all"
    )
    assert _mine(label) == [], (
        f"the slot was reclaimed but the instrument still names a holder: {_mine(label)}. "
        "The saturation warning would print an ageing holder beside pool_checkedout=0 and "
        "send the investigation after a path that had already released"
    )


# ── the registry is bounded even when check-ins never arrive ─────────────────────────────────
# KILLS: removing the _MAX_TRACKED cap. Calling _record() with no matching _forget() IS the
# suppressed-checkin case, reproduced deterministically instead of hoping a real pool leaks.
#
# NOTE ON `<=`: the cap used to be a `clear()`, so the registry dropped to ~1 and a strict
# `<` held trivially. It is now "spend the unverifiable entries, then refuse the incoming
# one", which parks the registry AT the ceiling instead of below it — see the test below for
# why that trade is the right way round. Boundedness, the property this test exists for, is
# unchanged and still asserted.
def test_the_registry_never_exceeds_its_cap_when_checkins_are_suppressed() -> None:
    pool_watch.clear()
    try:
        high_water = 0
        for i in range(pool_watch._MAX_TRACKED * 2 + 7):
            pool_watch._record(i, "cap-test")  # no _forget: the leaked-checkin failure mode
            high_water = max(high_water, len(pool_watch.holders(limit=10_000)))

        assert high_water <= pool_watch._MAX_TRACKED, (
            f"registry grew to {high_water}, past its {pool_watch._MAX_TRACKED} cap — a lost "
            "checkin would become an unbounded dict in a live service"
        )
        assert len(pool_watch.holders(limit=10_000)) <= pool_watch._MAX_TRACKED
    finally:
        pool_watch.clear()


# ── THE ANTI-FALSE-NEGATIVE TEST: the cap must not erase a live holder ───────────────────────
# KILLS: restoring `_holders.clear()` in _record()'s cap branch, and any LRU/evict-oldest
# policy that can spend a weak-keyed (provably live) entry.
#
# This is the module's own documented false negative in executable form. An ALREADY
# checked-out connection fires no second `checkout` event, so anything the cap removes is
# never re-recorded — it is gone for the whole life of that connection. Measured on the old
# code: 15 real connections checked out and recorded, cap reached, registry size 1, and
# `holders()` for those 15 → `[]` while the pool was genuinely exhausted. The instrument
# whose entire purpose is to name the holder during exhaustion returned nothing, at exactly
# the moment it was needed, and "nothing" is the module's documented REFUTATION of the leak
# hypothesis — so it did not fail to answer, it answered wrongly.
def test_holders_still_names_the_live_holders_after_the_cap_is_reached() -> None:
    pool_watch.clear()
    try:
        # 15 = POOL_SIZE + MAX_OVERFLOW: a genuinely exhausted runtime pool.
        live = [_Recordish() for _ in range(15)]
        pool_watch.caller_var.set("POST /principles/{id}/evaluate")
        for record in live:
            pool_watch._record(record, "live-runtime")

        # Now flood past the cap with entries the registry cannot verify (non-weak keys).
        pool_watch.caller_var.set("phantom")
        for i in range(pool_watch._MAX_TRACKED * 2):
            pool_watch._record(f"phantom-{i}", "phantom")

        assert len(pool_watch.holders(limit=10_000)) <= pool_watch._MAX_TRACKED, (
            "premise broken: the cap did not bind, so this proves nothing about it"
        )
        survivors = [h for h in pool_watch.holders(limit=10_000) if h["engine"] == "live-runtime"]
        assert len(survivors) == 15, (
            f"{len(survivors)} of 15 still-checked-out connections survived the cap. A "
            "connection already checked out fires no second `checkout`, so every one the "
            "cap dropped is unrecoverable — holders() would come back empty during genuine "
            "exhaustion, which reads as 'nobody is holding' and refutes the leak hypothesis "
            "the pool is actually demonstrating"
        )
        assert {h["caller"] for h in survivors} == {"POST /principles/{id}/evaluate"}, (
            "the survivors lost their attribution — an age with no name cannot accuse a code path"
        )
        assert pool_watch.tally()["live-runtime"] == 15
    finally:
        pool_watch.caller_var.set("?")
        pool_watch.clear()


# ── the report must answer the question the predicate asked ──────────────────────────────────
# KILLS: deleting the `engine=` parameter from holders() (or ignoring it), and deleting
# tally().
#
# `lifespan._pool_saturated()` reads the RUNTIME engine's counters only, while the registry
# is process-wide across runtime + owner + health. Unfiltered and oldest-first, the owner
# engine's long-running reconcile sweep is reliably the oldest entry, so it crowds the
# runtime holders out of the top `limit` — measured, 1 of 15 runtime holders survived into
# the report. A report that names a different pool than the one that tripped is worse than
# no report. The tally is the other half: filtering must not make owner/health pressure
# invisible, only secondary.
def test_holders_filters_by_engine_and_the_tally_counts_every_engine() -> None:
    pool_watch.clear()
    try:
        pool_watch.caller_var.set("conflict_reconcile.cycle")
        owner = _Recordish()
        pool_watch._record(owner, "owner")  # deliberately the OLDEST entry
        time.sleep(0.01)

        pool_watch.caller_var.set("POST /principles/{id}/evaluate")
        runtime = [_Recordish() for _ in range(3)]
        for record in runtime:
            pool_watch._record(record, "runtime")

        # The crowding-out, demonstrated: unfiltered, the owner sweep owns the top slot.
        assert pool_watch.holders(limit=1)[0]["engine"] == "owner", (
            "premise broken: the owner entry is not the oldest, so the filter below is not "
            "being tested against the condition it exists for"
        )

        filtered = pool_watch.holders(limit=5, engine="runtime")
        assert [h["engine"] for h in filtered] == ["runtime"] * 3, (
            f"holders(engine='runtime') returned {filtered} — the report still names a pool "
            "other than the one the saturation predicate measured"
        )
        assert pool_watch.holders(limit=5, engine="health") == [], (
            "an engine with no holders must filter to [], not fall through to every engine"
        )
        assert pool_watch.tally() == {"owner": 1, "runtime": 3}, (
            f"tally() reported {pool_watch.tally()} — filtering the report to the runtime "
            "pool silently drops owner-engine pressure unless the tally carries it"
        )
    finally:
        pool_watch.caller_var.set("?")
        pool_watch.clear()


# ── the saturation report is wired to the FILTERED view ──────────────────────────────────────
# KILLS: reverting src/lifespan.py's `_pool_holders()` to an unfiltered `holders(5)`, and
# deleting `holders_by_engine=_pool_holder_tally()` from the db_pool.saturated warning.
# tests/test_event_loop_lag_probe.py proves the holders kwarg REACHES the log line; nothing
# there can see WHICH engine's holders it carries, which is the defect.
def test_the_saturation_report_prefers_runtime_holders_and_keeps_the_others_in_a_tally() -> None:
    import lifespan as lifespan_module

    pool_watch.clear()
    try:
        pool_watch.caller_var.set("conflict_reconcile.cycle")
        owner = _Recordish()
        pool_watch._record(owner, "owner")
        time.sleep(0.01)
        pool_watch.caller_var.set("POST /principles/{id}/evaluate")
        runtime = _Recordish()
        pool_watch._record(runtime, "runtime")

        assert [h["engine"] for h in lifespan_module._pool_holders()] == ["runtime"], (
            "the warning fired on the runtime pool's counters and reported the owner "
            "engine's holder — a confident pointer at the wrong pool"
        )
        assert lifespan_module._pool_holders_by_engine() == {"owner": 1, "runtime": 1}, (
            "owner-engine pressure vanished from the warning entirely once the holder list "
            "was filtered; the tally exists so it is demoted, not dropped"
        )

        # And the fallback: with nothing recorded for runtime, an empty list would read as
        # the module's documented negative result ("nobody is holding"). Report what there
        # is instead — every entry carries its own engine label.
        pool_watch._forget(runtime)
        assert [h["engine"] for h in lifespan_module._pool_holders()] == ["owner"], (
            "no runtime holder is recorded and the report degraded to [] — that is read as "
            "a refutation of the leak hypothesis, not as 'nothing to say about runtime'"
        )
    finally:
        pool_watch.caller_var.set("?")
        pool_watch.clear()


# ── a broken instrument degrades to no instrument, never to a failed query ───────────────────
# KILLS: removing the try/except around either listener body. SQLAlchemy propagates an
# exception raised in a checkout/checkin listener straight into the caller's connection
# acquisition, so an unguarded diagnostic turns every query in the service into an error.
@pytest.mark.asyncio
async def test_a_raising_listener_body_does_not_break_the_query(
    watched_engine: tuple[AsyncEngine, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, label = watched_engine

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the diagnostic exploded")

    monkeypatch.setattr(pool_watch, "_record", _boom)
    monkeypatch.setattr(pool_watch, "_forget", _boom)

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar_one() == 1

    assert _mine(label) == []


# ── holders() ordering and limit ─────────────────────────────────────────────────────────────
# KILLS: dropping the sort, reversing it (newest-first hides the oldest holder, which is the
# one that matters), ignoring `limit`, and blowing up on an empty registry.
def test_holders_is_sorted_oldest_first_and_honours_limit() -> None:
    pool_watch.clear()
    try:
        assert pool_watch.holders() == [], "holders() must be safe to call with nothing held"

        for name in ("oldest", "middle", "newest"):
            pool_watch.caller_var.set(name)
            pool_watch._record(hash(name), "sort-test")
            time.sleep(0.01)

        every = pool_watch.holders(limit=10)
        assert [h["caller"] for h in every] == ["oldest", "middle", "newest"]
        assert every[0]["age_s"] > every[-1]["age_s"], "age must fall as recency rises"

        assert [h["caller"] for h in pool_watch.holders(limit=2)] == ["oldest", "middle"]
        assert [h["caller"] for h in pool_watch.holders(limit=1)] == ["oldest"]
    finally:
        pool_watch.caller_var.set("?")
        pool_watch.clear()


# ── install() is idempotent per engine ───────────────────────────────────────────────────────
# KILLS: removing the id(engine) guard. A caller with an alias/fallback engine (the since-removed
# `owner_engine = engine`) can pass the same engine twice; a second registration fires every listener twice, which no
# assertion on the registry CONTENTS can see (same key, same value) — only a call count can.
@pytest.mark.asyncio
async def test_install_twice_on_the_same_engine_registers_the_hooks_once(
    watched_engine: tuple[AsyncEngine, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, label = watched_engine
    real_record = pool_watch._record
    calls: list[Any] = []

    def _counting(key: Any, engine_label: str) -> None:
        calls.append(key)
        real_record(key, engine_label)

    monkeypatch.setattr(pool_watch, "_record", _counting)

    pool_watch.install(engine, label)  # second install on the SAME object — must be a no-op

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    assert len(calls) == 1, (
        f"one checkout fired the checkout listener {len(calls)} times — install() registered "
        "the hooks more than once"
    )


# ── THE INTEGRATION LEG ──────────────────────────────────────────────────────────────────────
#
# Everything above tests a UNIT of the instrument. Nothing above drives an ACTUAL saturation
# episode and then asks the service's own reporting helpers what they would print — which is
# the one leg production exercises first, and the leg the two known defects (a cleared registry
# answering `[]` while 15 real connections were checked out) would have shown up in.
#
# TWO SLOTS, NOT ONE. `db.pool_is_saturated()` computes its ceiling from `db.POOL_SIZE +
# db.MAX_OVERFLOW` and asks for `headroom=1`, so a one-slot pool makes the predicate DEGENERATE:
# the threshold becomes `checkedout >= 0`, which is true with nothing held at all, and the
# "not saturated once it is returned" half could then never be false. Two slots is the smallest
# pool the real predicate can answer BOTH ways about, so that is what these two legs build —
# and `db.POOL_SIZE`/`db.MAX_OVERFLOW` are pinned to the engine's ACTUAL configuration so the
# predicate's ceiling is the engine's ceiling and not a fiction.
_SATURATING_POOL_SIZE = 2

# Long enough to be a real wait on the pool, short enough that proving exhaustion (a checkout
# that genuinely cannot be served) costs one second rather than the SQLAlchemy default 30.
_POOL_TIMEOUT_S = 1.0


@pytest_asyncio.fixture
async def runtime_engine() -> AsyncGenerator[AsyncEngine, None]:
    """A REAL engine under the production label ``"runtime"``, plus an OLDER ``"owner"`` holder.

    The label is not decorative: `lifespan._pool_holders()` calls
    `holders(5, engine="runtime")` and falls back to an unfiltered `holders(5)` when that comes
    back empty, so an engine registered under any other label would exercise the FALLBACK and
    quietly prove nothing about the filter that the report actually depends on.

    ⚠️ **And the label alone is not enough** — this fixture used to install only the runtime
    engine, and an adversarial mutation review proved the resulting leg was blind: with a
    single-label registry, `holders(engine=...)` ignoring its filter and `_pool_holders()`
    hard-wired to the unfiltered fallback BOTH survived, because filtered and unfiltered return a
    byte-identical list when only one label exists. The docstring asserted the filter was
    load-bearing while nothing here could observe it.

    So a second engine is installed as ``"owner"`` and its connection is taken FIRST, making it
    the OLDEST entry in the registry. That is not a contrivance — it is the production shape the
    filter was written for: `_pool_holders()`'s own docstring records that the owner engine's
    long-running reconcile sweep crowds runtime holders out of the top 5 (measured, 1 of 15
    survived). Now an unfiltered report would put the owner entry at index 0, and both mutants
    die on this leg rather than only on its synthetic sibling.

    Same DSN and same `pool_watch.clear()` bracketing as `watched_engine` above — the registry
    is process-wide, so a leaked holder here is a phantom in a sibling test.
    """
    pool_watch.clear()
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        pool_size=_SATURATING_POOL_SIZE,
        max_overflow=0,
        pool_timeout=_POOL_TIMEOUT_S,
    )
    pool_watch.install(engine, "runtime")
    decoy = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    pool_watch.install(decoy, "owner")
    pool_watch.caller_var.set("conflict_reconcile.sweep (owner)")
    owner_conn = await decoy.connect()
    # Taken first and held for the whole test, so it is unambiguously the oldest
    # entry: `holders()` sorts oldest-first, so an unfiltered report starts HERE.
    await asyncio.sleep(0.01)
    try:
        yield engine
    finally:
        await owner_conn.close()
        await decoy.dispose()
        await engine.dispose()
        pool_watch.clear()


# ── under GENUINE saturation the report names a holder ───────────────────────────────────────
# KILL-MUTATION, stated exactly: make `lifespan._pool_holders()` `return []` unconditionally.
# THIS leg goes RED — it asserts a non-empty report, an oldest-first `caller`, and a live
# `age_s` at the same instant the predicate says the pool is saturated. Every unit test above
# that does not itself call `_pool_holders()` stays GREEN under that mutation: they assert on
# `pool_watch.holders()`/`tally()` directly, one layer below the mutation.
#
# One honest exception, because a kill claim that overstates itself is worth nothing:
# `test_the_saturation_report_prefers_runtime_holders_and_keeps_the_others_in_a_tally` also
# calls `_pool_holders()` and also goes red. It is the SYNTHETIC half of the same contract —
# it populates the registry by calling `pool_watch._record()` by hand, so it can never observe
# whether a real `checkout` event ever reaches the registry, nor whether the pool was actually
# saturated when the report was taken. That gap is exactly what this leg closes.
#
# ALSO KILLED, and killed by nothing else in the repo: `_pool_saturated()` degraded to a
# constant (`return False` hides every episode; `return True` alarms forever) — no other test
# calls it at all, and `tests/test_event_loop_lag_probe.py` fakes `_pool_stats` outright, so
# no other test ever puts real pool counters through the real predicate.
async def test_a_saturated_runtime_pool_reports_a_named_holder_with_a_live_age(
    runtime_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db as db_module
    import lifespan as lifespan_module

    # `_pool_stats()` reads `db.engine`; point it at the small engine so the counters the
    # predicate reads are this pool's own. (Same monkeypatch seam the lag-probe tests use.)
    monkeypatch.setattr(db_module, "engine", runtime_engine)
    monkeypatch.setattr(db_module, "POOL_SIZE", _SATURATING_POOL_SIZE)
    monkeypatch.setattr(db_module, "MAX_OVERFLOW", 0)

    held: list[Any] = []
    try:
        # Two DIFFERENT callers so the oldest-first ordering of the report is observable, and
        # so the caller assertion cannot pass on a constant.
        pool_watch.caller_var.set("POST /principles/{id}/evaluate")
        held.append(await runtime_engine.connect())
        await held[0].execute(text("SELECT 1"))
        await asyncio.sleep(0.01)

        pool_watch.caller_var.set("conflict_reconcile.cycle")
        held.append(await runtime_engine.connect())
        await held[1].execute(text("SELECT 1"))
        await asyncio.sleep(0.01)

        # GENUINE exhaustion, not a computed condition: with both slots out and no overflow,
        # a third checkout cannot be served and times out on the pool itself.
        with pytest.raises(sqlalchemy.exc.TimeoutError):
            await runtime_engine.connect()

        stats = lifespan_module._pool_stats()
        assert stats.get("pool_checkedout") == _SATURATING_POOL_SIZE, (
            f"premise broken: the pool reports {stats} while both of its slots are held — "
            "nothing below is a measurement of a saturated pool"
        )
        assert lifespan_module._pool_saturated(stats) is True, (
            "the pool cannot serve another checkout and the saturation predicate says it is "
            f"fine ({stats}) — the warning that carries the holder report never fires"
        )

        report = lifespan_module._pool_holders()
        assert report, (
            "the pool is exhausted and the report names NOBODY. `[]` is the module's "
            "documented REFUTATION of the leak hypothesis, so this does not read as 'the "
            "instrument failed' — it reads as 'nobody is holding', at the one moment the "
            "instrument exists to answer"
        )
        assert [h["engine"] for h in report] == ["runtime"] * _SATURATING_POOL_SIZE, (
            f"the report carries {report} — the saturation fired on the runtime pool's "
            "counters, so a non-runtime entry here points the investigation at another engine"
        )
        assert report[0]["caller"] == "POST /principles/{id}/evaluate", (
            f"the oldest holder is reported as {report[0]['caller']!r}: either the checkout "
            "was recorded with no attribution, or the report is not oldest-first — and the "
            "oldest holder is the one a leak investigation starts from"
        )
        assert {h["caller"] for h in report} == {
            "POST /principles/{id}/evaluate",
            "conflict_reconcile.cycle",
        }, f"both holders must be named, got {report}"

        age = report[0]["age_s"]
        assert isinstance(age, float), f"age_s must be a real number, got {age!r}"
        assert 0.0 < age < 60.0, (
            f"age_s={age} is not a live, non-negative measurement of this checkout — a "
            "constant age cannot separate a leaked connection from fast churn, and those "
            "two have opposite fixes"
        )
        assert report[0]["age_s"] >= report[-1]["age_s"], "ages must fall as recency rises"

        # Both engines, not just runtime: `.get("runtime")` alone passes under a tally that has
        # silently lost the owner engine, which is the pressure the filter deliberately hides
        # from the report and the tally exists to keep visible.
        assert lifespan_module._pool_holders_by_engine() == {
            "runtime": _SATURATING_POOL_SIZE,
            "owner": 1,
        }, (
            f"the tally reports {lifespan_module._pool_holders_by_engine()} while "
            f"{_SATURATING_POOL_SIZE} runtime connections are checked out — the warning would "
            "understate the pressure it is reporting"
        )
    finally:
        for conn in held:
            await conn.close()
        pool_watch.caller_var.set("?")


# ── and it lets go: the counterpart, so neither half is a constant ───────────────────────────
# KILLS: any change that makes the saturation report STICK — a dropped `checkin` listener, or
# a `_pool_holders()` hard-wired to a non-empty answer. Without this leg the test above passes
# just as well against an instrument that reports a holder unconditionally, which in a live
# service is a permanent false accusation against whatever code path was named last.
#
# The premise is asserted first: if the pool were never saturated here, "not saturated now"
# would prove nothing.
async def test_the_runtime_holder_is_gone_once_the_checkouts_are_returned(
    runtime_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db as db_module
    import lifespan as lifespan_module

    monkeypatch.setattr(db_module, "engine", runtime_engine)
    monkeypatch.setattr(db_module, "POOL_SIZE", _SATURATING_POOL_SIZE)
    monkeypatch.setattr(db_module, "MAX_OVERFLOW", 0)

    held: list[Any] = []
    try:
        pool_watch.caller_var.set("POST /principles/{id}/evaluate")
        for _ in range(_SATURATING_POOL_SIZE):
            conn = await runtime_engine.connect()
            held.append(conn)
            await conn.execute(text("SELECT 1"))

        assert lifespan_module._pool_saturated(lifespan_module._pool_stats()) is True, (
            "premise broken: the pool never reached saturation, so releasing it proves nothing"
        )
        assert _mine("runtime"), "premise broken: no runtime holder was recorded to release"

        while held:
            await held.pop().close()

        assert lifespan_module._pool_saturated(lifespan_module._pool_stats()) is False, (
            "every connection is back in the pool and the predicate still calls it saturated "
            f"({lifespan_module._pool_stats()}) — the warning would fire forever"
        )
        assert _mine("runtime") == [], (
            "the connections were returned but the instrument still names them: "
            f"{_mine('runtime')}. An ageing holder printed beside an idle pool sends the "
            "investigation after a code path that had already released"
        )
        # POSITIVE, deliberately: `"runtime" not in {}` is satisfied by ANY degradation of
        # tally(), so an empty-dict mutant would pass a bare absence check. Naming what MUST
        # still be there makes the absence of the other thing mean something.
        assert lifespan_module._pool_holders_by_engine() == {"owner": 1}, (
            f"the tally still counts a runtime holder: {lifespan_module._pool_holders_by_engine()}"
        )
    finally:
        for conn in held:
            await conn.close()
        pool_watch.caller_var.set("?")
