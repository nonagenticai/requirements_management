"""`lifespan._cancel_tasks` — no single task may abort the shutdown sweep.

Why this file exists: the shutdown change shipped with ZERO coverage. An
adversarial audit reverted `src/lifespan.py` to its pre-change version and ran
the whole suite — `1939 passed, 1 skipped`, bit-identical to baseline. A change
no test can see is a change that can be silently reverted, and this one is on the
SIGTERM path of a single-replica service, where the failure is a pod that will
not terminate.

Each test below states the mutant it kills.
"""

import asyncio
import logging

import pytest

import lifespan as lifespan_module


async def _dies_with(exc: BaseException) -> None:
    raise exc


async def _swallows_cancellation(rounds: int = 1) -> None:
    """A task that genuinely refuses to die — for a bounded number of rounds.

    ⚠️ TWO mistakes were made building this, both caught by running the mutant
    rather than by reading the code, and both left the test VACUOUS:

    1. A plain ``except asyncio.CancelledError: continue`` is not enough. The
       Task's ``_must_cancel`` flag survives, so the next suspension re-delivers
       the cancellation and the task dies anyway. ``uncancel()`` is what actually
       clears the request. (Measured: with ``uncancel`` a bare ``await task``
       never returns at all.)
    2. Far worse — the test cancelled the task **before it had ever run**. A Task
       cancelled before its first step never executes a line of its coroutine, so
       ``await task`` raised ``CancelledError`` instantly and the stubborn path
       was never entered. The mutant survived and the test looked green. The
       caller MUST yield (``await asyncio.sleep(0)``) so the task reaches its
       first await first.

    ``rounds`` is bounded so the test can still reap it afterwards: it survives
    exactly ONE cancellation — enough to outlive the shutdown bound under test —
    and dies on the next, so nothing is left pending at teardown. An unreapable
    task here hangs the whole suite, which is how this file first went from
    "vacuous" to "times out at 120s".
    """
    survived = 0
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            if survived >= rounds:
                raise
            survived += 1
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            continue


# KILLS: narrowing `_cancel_tasks`'s `except Exception` back to `CancelledError`
# only. Awaiting a task that already died re-raises ITS exception, which escaped
# the loop and left every LATER task un-cancelled.
async def test_a_task_that_already_died_does_not_prevent_the_others_being_cancelled() -> None:
    dead = asyncio.create_task(_dies_with(ValueError("boom")))
    await asyncio.sleep(0)  # let it fail
    live = asyncio.create_task(asyncio.sleep(3600))

    await lifespan_module._cancel_tasks((dead, live))

    assert live.cancelled() or live.done(), (
        "the second task was never cancelled — the first task's exception escaped the sweep"
    )


# KILLS: removing the `asyncio.wait` bound (reverting to a bare `await task`).
# ⚠️ It is `asyncio.wait`, NOT `wait_for` — `wait_for` cancels and then WAITS for the
# cancellation to land, which never happens against a task that `uncancel()`s, so it is
# not a bound at all. See src/lifespan.py for the measurement.
# Without the bound this hangs forever, and because cancellation now runs BEFORE
# the HTTP clients are closed, it would hang holding them open.
async def test_a_task_that_swallows_cancellation_cannot_hang_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(lifespan_module, "_SHUTDOWN_TASK_TIMEOUT_S", 0.2)
    stubborn = asyncio.create_task(_swallows_cancellation())
    # ⛔ Load-bearing: let it REACH its first await. Cancelling a task that has
    # never been stepped kills it instantly without running a line, which is
    # exactly how the first version of this test went vacuously green.
    await asyncio.sleep(0.05)
    assert not stubborn.done(), "premise broken: the task never started"

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(lifespan_module._cancel_tasks((stubborn,)), timeout=5.0)
    elapsed = loop.time() - started

    assert elapsed < 3.0, f"shutdown took {elapsed:.2f}s — the await is unbounded"

    # Reap it, and PROVE it was reaped — a task still pending here would be left
    # for the event-loop teardown and hang the suite.
    stubborn.cancel()
    reaped, _still = await asyncio.wait({stubborn}, timeout=2.0)
    assert reaped, "the stubborn task outlived the test"


# KILLS: letting `_cancel_tasks` raise. A shutdown path that can fail turns a
# clean SIGTERM into a stuck pod, so it must absorb everything.
@pytest.mark.parametrize("exc", [ValueError("x"), RuntimeError("y"), OSError("z")])
async def test_no_exception_type_escapes_the_sweep(exc: BaseException) -> None:
    dead = asyncio.create_task(_dies_with(exc))
    await asyncio.sleep(0)

    await lifespan_module._cancel_tasks((dead,))  # must not raise

    # An explicit assertion, not just "it did not raise": the repo's own
    # vacuous-test guard counted this test as assertion-free, and it was right to.
    # "Did not raise" is the claim, but a reader cannot see it and a mutation that
    # skips the task entirely would also not raise.
    assert dead.done(), "the failed task was not reaped"
    assert dead.exception() is exc, "the sweep swallowed a DIFFERENT exception than the one raised"


# KILLS: dropping the `if task is None: continue` guard — the real call site
# passes tasks that are None when their `create_task` failed at startup.
async def test_none_entries_are_skipped() -> None:
    live = asyncio.create_task(asyncio.sleep(3600))
    await lifespan_module._cancel_tasks((None, live, None))
    assert live.cancelled() or live.done()


# KILLS: deleting the `if not done: log; continue` guard in `_cancel_tasks`.
# An adversarial mutation review found that mutant SURVIVED: without the guard,
# `task.result()` on a still-pending task raises `InvalidStateError`, which IS an
# `Exception`, so it lands in the generic handler and is logged as
# `shutdown.task_failed {'error': 'Result is not set.'}`. Bounded and non-raising
# either way — so nothing broke, but the operator is told the wrong thing about
# the wrong failure. A task that REFUSED to cancel and a task that CRASHED need
# different responses, and only this leg keeps them apart.
async def test_a_task_that_refuses_to_cancel_is_named_as_such(monkeypatch, caplog) -> None:
    monkeypatch.setattr(lifespan_module, "_SHUTDOWN_TASK_TIMEOUT_S", 0.2)
    stubborn = asyncio.create_task(_swallows_cancellation())
    stubborn.set_name("reconcile-loop")
    await asyncio.sleep(0.05)
    assert not stubborn.done(), "premise broken: the task never started"

    with caplog.at_level(logging.ERROR):
        await lifespan_module._cancel_tasks((stubborn,))

    text = caplog.text
    assert "shutdown.task_would_not_cancel" in text, (
        f"a task that outlived the bound was not reported as such; log was: {text!r}"
    )
    assert "shutdown.task_failed" not in text, (
        "a refusal to cancel was misreported as a task crash — those need different responses"
    )
    assert "reconcile-loop" in text, "the log line does not say WHICH task would not cancel"

    stubborn.cancel()
    reaped, _still = await asyncio.wait({stubborn}, timeout=2.0)
    assert reaped, "the stubborn task outlived the test"
