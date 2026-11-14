"""Pins for the hardening of ``auth.tenant_resolution`` (two adversarial reviews).

``tests/test_tenant_is_resolved_to_a_uuid.py`` pins the *contract* — a UUID in,
a canonical UUID out, a name resolved via licences, a 400 for anything else.
This file pins the four properties that were added after that contract held but
was found exploitable or expensive, each of which is invisible to the contract
tests and would silently regress if the code were reverted:

1. **A syntax gate runs BEFORE the network.** ``quote(name, safe="")`` does not
   escape ``.`` and httpx normalises dot segments, so ``X-Tenant-Id: ..``
   measurably made this service issue ``GET {licences}/byo-mode`` — one segment
   up, off the intended route. ``_TENANT_NAME_RE`` (licences' own tenant-name
   rule) rejects such input with ZERO outbound calls, which is why the test
   below asserts on a *call count*, not just on the exception.
2. **``licenses_unreachable`` is a 503 with ``Retry-After``; everything else is
   a 400.** The docstring claimed the header before the code emitted it, so the
   header itself is asserted.
3. **Single-flight.** 50 concurrent resolves of one cold name measured 50
   outbound calls before ``_inflight`` existed.
4. **Negative caching.** An unknown name is remembered for ``_NEGATIVE_TTL_S``,
   so a stream of requests for one bad name is not a stream of licences calls.

Fake transport idiom (``httpx.MockTransport`` + a call counter, monkeypatched
over the module's shared client) is deliberately identical to
``test_tenant_is_resolved_to_a_uuid.py``. No respx.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import auth.tenant_resolution as tenant_resolution
from auth.middleware import AuthMiddleware
from auth.tenant_resolution import (
    UnresolvableTenantError,
    clear_tenant_resolution_cache,
    resolve_tenant_to_uuid,
)

# The live n8n-dev answer's shape (a UUIDv7), same value as the sibling file.
LICENSED_TENANT_UUID = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"

# Every one of these is rejected by licences' own tenant-name rule, so rejecting
# them here costs nothing and closes the URL-steering hole. ``..`` and ``.`` are
# the measured exploit; the rest are the shapes that reach this code in practice
# (an auth header, a copy-pasted label, a realm name, a path fragment).
MALFORMED_NAMES = ["..", ".", "Bearer ", "Has Space", "UPPER", "a/b"]


class _Calls:
    """Counts the licences requests a test caused, and remembers their paths."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __len__(self) -> int:
        return len(self.paths)


def _install_licenses_stub(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], object],
) -> _Calls:
    """Point the module's shared client at a MockTransport and count calls."""
    calls = _Calls()

    def _counting(request: httpx.Request) -> object:
        calls.paths.append(request.url.path)
        return handler(request)

    monkeypatch.setattr(
        tenant_resolution,
        "_client",
        httpx.AsyncClient(transport=httpx.MockTransport(_counting)),
    )
    return calls


def _known_tenant(request: httpx.Request) -> httpx.Response:
    """`/tenants/n8n-dev/byo-mode` → 200; anything else → the live 404 body."""
    if request.url.path == "/tenants/n8n-dev/byo-mode":
        return httpx.Response(
            200,
            json={
                "tenant_id": LICENSED_TENANT_UUID,
                "name": "n8n-dev",
                "byo_mode": "platform",
            },
        )
    return httpx.Response(404, json={"detail": "Tenant not found"})


def _licences_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no outbound call was allowed here: {request.url}")


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """Module-level caches leak between tests; without this the asserts lie."""
    clear_tenant_resolution_cache()
    yield
    clear_tenant_resolution_cache()


def _make_app() -> FastAPI:
    """Minimal app: one protected route that echoes the resolved tenant."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/requirements/echo")
    async def _echo(request: Request) -> JSONResponse:
        user = getattr(request.state, "user", None)
        return JSONResponse({"tenant_id": getattr(user, "tenant_id", None)})

    return app


async def _get(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get("/requirements/echo", headers=headers)


# ---------------------------------------------------------------------------
# 1. The syntax gate runs before the network
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", MALFORMED_NAMES)
async def test_malformed_name_is_refused_without_any_outbound_call(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """A name licences would reject never becomes a licences request.

    The call-count assertion is the load-bearing half. Raising
    ``UnresolvableTenantError`` *after* the round-trip would satisfy a
    reason-only test while leaving ``..`` free to steer our URL.
    """
    calls = _install_licenses_stub(monkeypatch, _explode)

    with pytest.raises(UnresolvableTenantError) as excinfo:
        await resolve_tenant_to_uuid(name)

    assert excinfo.value.reason == "malformed_tenant"
    assert len(calls) == 0, f"{name!r} reached the network: {calls.paths}"


async def test_dot_dot_does_not_walk_up_the_licences_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured defect, stated as itself: no ``GET {licences}/byo-mode``.

    ``quote('..', safe='')`` yields ``..`` unchanged and httpx then collapses
    the dot segment, so the request that used to go out was one path segment
    above the intended route.
    """
    calls = _install_licenses_stub(monkeypatch, _known_tenant)

    with pytest.raises(UnresolvableTenantError):
        await resolve_tenant_to_uuid("..")

    assert calls.paths == []


async def test_malformed_tenant_header_is_400_not_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the request boundary a malformed tenant is the CALLER's fault.

    The transport here would raise ``ConnectError`` if anything reached it, so
    a 503 in this test means the syntax gate stopped running before the network
    — exactly the regression this pins.
    """
    calls = _install_licenses_stub(monkeypatch, _licences_down)

    resp = await _get({"X-Tenant-Id": ".."})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["status"] == 400
    assert "Retry-After" not in resp.headers
    assert len(calls) == 0, calls.paths


# ---------------------------------------------------------------------------
# 2. licenses_unreachable is 503 + Retry-After; everything else is 400
# ---------------------------------------------------------------------------


async def test_valid_but_unknown_name_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Syntactically fine, genuinely unknown → the caller is wrong → 400."""
    _install_licenses_stub(monkeypatch, _known_tenant)

    resp = await _get({"X-Tenant-Id": "no-such-tenant"})

    assert resp.status_code == 400, resp.text
    assert "Retry-After" not in resp.headers


async def test_licences_unreachable_is_503_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our outage is not the caller's bad request — and a bare 503 says nothing.

    Both halves are asserted: the status (blaming the caller for our own outage
    is how an error names a cause it never checked) and the ``Retry-After``
    header, which the docstring claimed before the code emitted it.
    """
    _install_licenses_stub(monkeypatch, _licences_down)

    resp = await _get({"X-Tenant-Id": "n8n-dev"})

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["status"] == 503
    assert resp.headers["Retry-After"] == "5"


async def test_unreachable_and_unknown_are_distinct_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 400/503 split is only possible because the reasons stay distinct."""
    _install_licenses_stub(monkeypatch, _licences_down)
    with pytest.raises(UnresolvableTenantError) as down:
        await resolve_tenant_to_uuid("n8n-dev")
    assert down.value.reason == "licenses_unreachable"

    clear_tenant_resolution_cache()
    _install_licenses_stub(monkeypatch, _known_tenant)
    with pytest.raises(UnresolvableTenantError) as unknown:
        await resolve_tenant_to_uuid("no-such-tenant")
    assert unknown.value.reason == "unknown_tenant"


# ---------------------------------------------------------------------------
# 3. Single-flight: N concurrent resolves of one name = ONE outbound request
# ---------------------------------------------------------------------------


async def test_concurrent_resolutions_share_one_outbound_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured 50/50 before ``_inflight``: a cold cache multiplied the load.

    The handler sleeps so the 25 resolutions genuinely overlap — without the
    sleep the first could complete and populate the positive cache before the
    second even starts, and the test would pass on caching rather than on
    single-flight.
    """

    async def _slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return _known_tenant(request)

    calls = _install_licenses_stub(monkeypatch, _slow)

    results = await asyncio.gather(*(resolve_tenant_to_uuid("n8n-dev") for _ in range(25)))

    assert results == [LICENSED_TENANT_UUID] * 25
    assert len(calls) == 1, f"25 concurrent resolves made {len(calls)} calls: {calls.paths}"


async def test_single_flight_entry_is_cleaned_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_inflight`` is popped in a ``finally`` — a leak would wedge the name.

    A stuck entry means every later resolution of this name awaits a completed
    task forever-after instead of looking the name up again, which is a far
    worse failure than the duplicate requests single-flight removed.
    """

    async def _slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return _known_tenant(request)

    _install_licenses_stub(monkeypatch, _slow)

    await asyncio.gather(*(resolve_tenant_to_uuid("n8n-dev") for _ in range(5)))

    assert tenant_resolution._inflight == {}


async def test_single_flight_does_not_merge_different_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De-duplication is keyed by NAME — two tenants must not share an answer."""

    async def _slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return _known_tenant(request)

    calls = _install_licenses_stub(monkeypatch, _slow)

    results = await asyncio.gather(
        resolve_tenant_to_uuid("n8n-dev"),
        resolve_tenant_to_uuid("other-tenant"),
        return_exceptions=True,
    )

    assert results[0] == LICENSED_TENANT_UUID
    assert isinstance(results[1], UnresolvableTenantError)
    assert sorted(calls.paths) == [
        "/tenants/n8n-dev/byo-mode",
        "/tenants/other-tenant/byo-mode",
    ]


# ---------------------------------------------------------------------------
# 4. Negative caching: an unknown name costs one licences call, not one per hit
# ---------------------------------------------------------------------------


async def test_unknown_name_is_negatively_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any pod on the trusted network can repeat one bad name indefinitely.

    Without the negative entry that stream of requests is a stream of licences
    calls. The final leg — clear the cache, watch a second call go out — is what
    makes this a proof: it shows the suppression came from the cache and not
    from some other short-circuit that would also have swallowed the call.
    """
    calls = _install_licenses_stub(monkeypatch, _known_tenant)

    for _ in range(2):
        with pytest.raises(UnresolvableTenantError) as excinfo:
            await resolve_tenant_to_uuid("no-such-tenant")
        assert excinfo.value.reason == "unknown_tenant"

    assert len(calls) == 1, f"the second lookup was not cached: {calls.paths}"

    clear_tenant_resolution_cache()

    with pytest.raises(UnresolvableTenantError):
        await resolve_tenant_to_uuid("no-such-tenant")

    assert len(calls) == 2, "clearing the cache must let the next lookup out again"


async def test_negative_entry_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entry is a rate-limit, not a verdict: a new tenant resolves promptly.

    ``_NEGATIVE_TTL_S`` is driven to 0 rather than sleeping, so the test asserts
    that the deadline is *consulted* instead of asserting on wall-clock time.
    """
    monkeypatch.setattr(tenant_resolution, "_NEGATIVE_TTL_S", 0.0)
    calls = _install_licenses_stub(monkeypatch, _known_tenant)

    with pytest.raises(UnresolvableTenantError):
        await resolve_tenant_to_uuid("n8n-dev-pending")
    with pytest.raises(UnresolvableTenantError):
        await resolve_tenant_to_uuid("n8n-dev-pending")

    assert len(calls) == 2, "an expired negative entry must not suppress the retry"


async def test_negative_cache_does_not_shadow_a_positive_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached 404 for one name must not affect a name that does resolve."""
    calls = _install_licenses_stub(monkeypatch, _known_tenant)

    with pytest.raises(UnresolvableTenantError):
        await resolve_tenant_to_uuid("no-such-tenant")

    assert await resolve_tenant_to_uuid("n8n-dev") == LICENSED_TENANT_UUID
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 4b. …and that cache is BOUNDED, or it is the leak it was written to prevent
# ---------------------------------------------------------------------------
# ``_negative`` is keyed on caller-supplied input (a tenant name, and for a
# delegated ``X-Effective-Tenant-Id`` a UUID-shaped one too, because that path
# passes ``verify=True``). ⚠️ This block used to say nothing shrinks it "except
# ``_cache_get``" — the same error that was in the module and is corrected
# there: ``clear_tenant_resolution_cache`` and ``_negative_put`` remove too.
# The true statement is that no remover is driven by the passage of TIME, so
# before the cap a name seen once and never again was retained for the life of
# the process — on exactly the never-repeating stream the comment at the insert
# site names as the reason the cache exists. One 404 was one permanent entry.
#
# The bound is NOT the positive cache's FIFO, and the difference is the point.
# ``_negative``'s keys are caller-chosen, so its working set is whatever a caller
# decides it is; FIFO on a rotating set of ``cap + 1`` such names evicts each
# entry exactly one request before it is needed again — the classic cyclic worst
# case, a 0% hit rate. Measured at cap=4 over 50 inbound requests for a rotating
# set of 5 unknown names: 5 outbound calls with no cap, **50 under FIFO** (and
# 50 under a true LRU, which fails identically), 14 under the shipped
# reclaim-expired-only policy. A memory bound that buys a 1:1 amplification
# against an unrate-limited licences service is not a fix, so the policy here
# is: reclaim the FIFO head only once it has EXPIRED, and when every entry is
# still live, DECLINE to cache rather than drop a live one. That policy is only
# sound while position tracks deadline, which is why ``_negative_put`` pops
# before it inserts — see the re-put test below.
#
# These tests assert ``len(_negative)`` and outbound CALL COUNTS directly,
# because a green resolution test cannot see either the dict growing behind it
# or the cache thrashing behind it.


class _FakeClock:
    """Stands in for the module's ``time`` — it uses only ``monotonic()``.

    The policy under test is a function of entry AGE, so pinning it needs a
    clock the test can move. Sleeping real seconds against a 10s TTL is not an
    option, and shrinking ``_NEGATIVE_TTL_S`` to sleepable values would turn
    every assertion below into a race with the scheduler.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _freeze(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(tenant_resolution, "time", clock)
    return clock


async def test_negative_cache_is_bounded_at_its_configured_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More distinct unknown names than the cap must not grow the dict past it.

    The cap is driven down rather than sending 600 requests so the assertion is
    on the *mechanism* (the ceiling is consulted on insert) and not on a number
    that happens to be the default today.
    """
    monkeypatch.setattr(tenant_resolution, "_NEGATIVE_MAX_ENTRIES", 8)
    _install_licenses_stub(monkeypatch, _known_tenant)

    for i in range(40):
        with pytest.raises(UnresolvableTenantError):
            await resolve_tenant_to_uuid(f"aaa{i:04d}")

    # ⛔ EXACTLY 8, not ``<= 8``. An inequality here is satisfied by a
    # ``_negative_put`` that never writes at all (0 <= 8), so the bound and the
    # cache being USED would both be asserted by a test that cannot tell them
    # apart. The measured steady state is the cap itself: the first 8 names fill
    # it and every later insert is declined while they are live.
    assert len(tenant_resolution._negative) == 8, (
        "40 distinct unknown names did not leave the negative cache at its cap: "
        f"{len(tenant_resolution._negative)} entries"
    )


async def test_negative_cache_is_bounded_at_the_shipped_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same property with NO monkeypatching of the ceiling.

    The test above would still pass if the shipped default were removed and only
    a test-injected attribute existed. This one exercises the value the process
    actually runs with, so the cap cannot be a thing that exists only in tests.

    ⚠️ This assertion used to read ``<= cap`` and was the only test touching 512.
    It passed under the mutation that DELETES the insert entirely, because that
    leaves the dict empty and ``0 <= 512`` holds — an assertion whose failure
    condition the mutation could not reach. It asserts the exact measured value
    now, so under-caching reddens it as loudly as over-caching does.
    """
    _install_licenses_stub(monkeypatch, _known_tenant)
    cap = tenant_resolution._NEGATIVE_MAX_ENTRIES

    for i in range(cap + 64):
        with pytest.raises(UnresolvableTenantError):
            await resolve_tenant_to_uuid(f"bbb{i:05d}")

    assert len(tenant_resolution._negative) == cap, (
        f"{cap + 64} distinct unknown names left "
        f"{len(tenant_resolution._negative)} entries against a cap of {cap}"
    )


async def test_a_full_cache_declines_the_insert_rather_than_dropping_a_live_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half of the policy that diverges from ``_cache_put``'s FIFO.

    With every entry still live there is nothing to reclaim, so the new name is
    simply not cached. FIFO would evict ``ccc-one`` here — and on a rotating
    working set that is the 0%-hit-rate cliff the whole policy exists to avoid.
    """
    clock = _freeze(monkeypatch)
    monkeypatch.setattr(tenant_resolution, "_NEGATIVE_MAX_ENTRIES", 3)
    _install_licenses_stub(monkeypatch, _known_tenant)

    for name in ("ccc-one", "ccc-two", "ccc-three", "ccc-four"):
        with pytest.raises(UnresolvableTenantError):
            await resolve_tenant_to_uuid(name)

    assert list(tenant_resolution._negative) == ["ccc-one", "ccc-two", "ccc-three"], (
        f"a live entry was evicted to make room: {list(tenant_resolution._negative)}"
    )
    assert clock.now == 1000.0  # nothing above depended on real time elapsing


async def test_the_expired_fifo_head_is_reclaimed_and_it_is_the_oldest_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: once the head has EXPIRED it is reclaimed, oldest first.

    Staggered inserts so exactly one entry is past its deadline, which is what
    separates "reclaim the expired head" from "reclaim the newest" (LIFO) and
    from "never reclaim anything" — a cache that only ever declined would wedge
    itself shut for the life of the process.
    """
    clock = _freeze(monkeypatch)
    monkeypatch.setattr(tenant_resolution, "_NEGATIVE_MAX_ENTRIES", 3)
    _install_licenses_stub(monkeypatch, _known_tenant)
    ttl = tenant_resolution._NEGATIVE_TTL_S  # 10s

    for offset, name in ((0.0, "fff-one"), (4.0, "fff-two"), (8.0, "fff-three")):
        clock.now = 1000.0 + offset
        with pytest.raises(UnresolvableTenantError):
            await resolve_tenant_to_uuid(name)
    assert list(tenant_resolution._negative) == ["fff-one", "fff-two", "fff-three"]

    # ``fff-one`` has expired (deadline 1010.0); the other two have not.
    clock.now = 1000.0 + ttl + 1.0
    with pytest.raises(UnresolvableTenantError):
        await resolve_tenant_to_uuid("fff-four")

    assert list(tenant_resolution._negative) == ["fff-two", "fff-three", "fff-four"], (
        f"the expired head was not the entry reclaimed: {list(tenant_resolution._negative)}"
    )


async def test_a_live_negative_entry_still_short_circuits_under_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control: the cap must not have neutered the cache.

    A cap that evicted on every insert, or a ``_negative_put`` that never wrote,
    would pass every ``len(...)`` assertion above. This asserts the thing the
    cache is FOR — a repeat of an unknown name inside its TTL costs no licences
    call — with the cap in force and the dict full to it.
    """
    monkeypatch.setattr(tenant_resolution, "_NEGATIVE_MAX_ENTRIES", 4)
    calls = _install_licenses_stub(monkeypatch, _known_tenant)

    for name in ("ddd-one", "ddd-two", "ddd-three", "ddd-four"):
        with pytest.raises(UnresolvableTenantError):
            await resolve_tenant_to_uuid(name)

    assert len(calls) == 4
    assert len(tenant_resolution._negative) == 4

    # Every one of them is still live (TTL is 10s) and still suppressing.
    for name in ("ddd-one", "ddd-two", "ddd-three", "ddd-four"):
        with pytest.raises(UnresolvableTenantError) as excinfo:
            await resolve_tenant_to_uuid(name)
        assert excinfo.value.reason == "unknown_tenant"

    assert len(calls) == 4, (
        f"a cached negative stopped short-circuiting under the cap: {calls.paths}"
    )


async def test_a_re_put_moves_the_entry_to_the_tail_instead_of_refreshing_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ The head is only usable as "earliest deadline" if re-puts re-anchor.

    Assigning to a key a dict already holds keeps its ORIGINAL position and
    replaces only the value. Without the ``pop`` in ``_negative_put``, a second
    put for a name already cached refreshes its deadline where it sits, the head
    stops being the dict minimum, and the liveness test starts declining inserts
    behind a live-looking head while expired entries squat.

    Measured with the pop removed: head deadline 1013.0 against a true minimum
    of 1011.0, then 3 of 3 further names declined with 2 dead entries resident.
    """
    clock = _freeze(monkeypatch)
    monkeypatch.setattr(tenant_resolution, "_NEGATIVE_MAX_ENTRIES", 4)
    _install_licenses_stub(monkeypatch, _known_tenant)

    for offset, name in ((0.0, "xxx"), (1.0, "yyy"), (2.0, "zzz")):
        clock.now = 1000.0 + offset
        with pytest.raises(UnresolvableTenantError):
            await resolve_tenant_to_uuid(name)

    # The orphaned second fetch for ``xxx`` lands (see the reachability test
    # below): a put for a name the dict already holds.
    clock.now = 1003.0
    tenant_resolution._negative_put("xxx", "unknown_tenant")

    assert list(tenant_resolution._negative) == ["yyy", "zzz", "xxx"], (
        f"a re-put did not move the entry to the tail: {list(tenant_resolution._negative)}"
    )
    deadlines = [d for d, _ in tenant_resolution._negative.values()]
    assert deadlines == sorted(deadlines), f"position no longer tracks deadline: {deadlines}"
    head = next(iter(tenant_resolution._negative))
    assert tenant_resolution._negative[head][0] == min(deadlines)

    # And the consequence the invariant protects: an expired head IS reclaimed,
    # so a newcomer is cached rather than declined behind a stale live-looking one.
    clock.now = 1012.0
    with pytest.raises(UnresolvableTenantError):
        await resolve_tenant_to_uuid("www")
    assert "www" in tenant_resolution._negative


async def test_one_name_can_reach_negative_put_twice_via_a_cancelled_awaiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the re-put above is a real code path and not a thought experiment.

    ``_resolve_name`` shields the fetch task but pops ``_inflight`` in its
    ``finally``. Cancelling the awaiter — a client disconnect, or an outer
    ``wait_for`` — therefore orphans a still-running fetch AND clears the
    single-flight entry, so the next request for the same name starts a second
    fetch. Both land in ``_negative_put``, up to ``_HTTP_TIMEOUT_S`` apart —
    which is 10s, EQUAL to ``_NEGATIVE_TTL_S``, so the skew can span a full TTL.

    This test pins the reachability only. It deliberately does not assert the
    cancellation semantics are wrong; single-flight shielding is the intended
    behaviour, and ``_negative_put`` is simply required to tolerate its output.
    """
    puts: list[str] = []
    real_put = tenant_resolution._negative_put

    def counting_put(name: str, reason: str) -> None:
        puts.append(name)
        real_put(name, reason)

    monkeypatch.setattr(tenant_resolution, "_negative_put", counting_put)

    gate = asyncio.Event()

    async def slow_404(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tenants/slow/byo-mode":
            await gate.wait()
        return httpx.Response(404, json={"detail": "Tenant not found"})

    calls = _install_licenses_stub(monkeypatch, slow_404)

    first = asyncio.ensure_future(resolve_tenant_to_uuid("slow"))
    for _ in range(4):
        await asyncio.sleep(0)
    orphan = tenant_resolution._inflight.get("slow")
    assert orphan is not None, "the single-flight entry was never registered"

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert "slow" not in tenant_resolution._inflight, (
        "the finally-clause did not clear single-flight for the orphan"
    )

    second = asyncio.ensure_future(resolve_tenant_to_uuid("slow"))
    for _ in range(4):
        await asyncio.sleep(0)
    gate.set()
    with pytest.raises(UnresolvableTenantError):
        await second
    await asyncio.gather(orphan, return_exceptions=True)  # retrieve, don't warn

    assert len(calls) == 2, f"expected two outbound fetches for one name, got {len(calls)}"
    assert puts.count("slow") == 2, f"_negative_put saw {puts.count('slow')} puts for 'slow'"


async def test_a_rotating_working_set_above_the_cap_does_not_amplify_one_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ The reason the policy is not the sibling's FIFO. Measured, not argued.

    ``_negative`` exists to stop a stream of unknown names becoming a stream of
    licences calls, and licences has no rate limiter in front of it. A cap that
    is correct about MEMORY can still be catastrophic about CALLS: on a rotating
    working set of ``cap + 1`` names, FIFO evicts every entry exactly one
    request before it is next needed, so the hit rate is zero and the service
    makes one outbound call per inbound request — strictly worse than having no
    cache at all.

    Measured on this exact input (cap=4, a rotating set of 5 unknown names, 50
    inbound requests, clock frozen so the 10s TTL cannot expire mid-run):

        no cap at all (origin/main)   5 outbound   ratio 0.10
        FIFO, as first written       50 outbound   ratio 1.00   ⛔ 1:1
        reclaim-expired-only         14 outbound   ratio 0.28

    14 is the arithmetic of the policy, not a tolerance: 5 warm-up calls fill
    four slots and decline the fifth name, after which each further rotation of
    5 requests costs exactly the one declined name — 5 + 9 = 14.

    ⚠️ The amplification is REDUCED, not removed, and the penalty against the
    uncapped arm is NOT monotonic in the working set. Measured over the same 50
    frozen-clock requests, outbound and as a multiple of uncapped:

        N =            5      8     16     32     50
        uncapped       5      8     16     32     50
        this          14     28     40     46     50
        multiple     2.8x   3.5x   2.5x   1.4x   1.0x

    It peaks near 2xcap and falls back to parity at N = 50, where every name is
    distinct and no policy — uncapped included — beats 1:1. The cap's cost is
    confined to the band ``cap < N < requests`` and is worst in the middle of
    it, not at the top.
    """
    _freeze(monkeypatch)  # a burst: the 10s TTL must not expire mid-measurement
    monkeypatch.setattr(tenant_resolution, "_NEGATIVE_MAX_ENTRIES", 4)
    calls = _install_licenses_stub(monkeypatch, _known_tenant)

    names = [f"rot{i}" for i in range(5)]
    for i in range(50):
        with pytest.raises(UnresolvableTenantError):
            await resolve_tenant_to_uuid(names[i % 5])

    assert len(calls) == 14, (
        f"{len(calls)} outbound calls for 50 requests over 5 distinct names; "
        "50 means the cache is thrashing (FIFO's Bélády case), 5 means the cap "
        "is not being enforced at all"
    )
    assert len(tenant_resolution._negative) == 4


# ---------------------------------------------------------------------------
# 5. A UUID never touches the network — the gap the sibling file leaves
# ---------------------------------------------------------------------------


async def test_a_uuid_is_never_stored_in_the_name_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hot path short-circuits above the cache entirely.

    If a UUID were routed through ``_resolve_name`` it would both cost a
    licences call and occupy one of the bounded cache's entries, letting a
    caller cycling random UUIDs evict the real tenants.
    """
    _install_licenses_stub(monkeypatch, _explode)

    assert await resolve_tenant_to_uuid(LICENSED_TENANT_UUID) == LICENSED_TENANT_UUID
    assert tenant_resolution._cache == {}
    assert tenant_resolution._negative == {}
