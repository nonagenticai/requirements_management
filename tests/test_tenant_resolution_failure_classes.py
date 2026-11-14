"""A licences OUTAGE is not a tenant that does not exist.

WHAT THIS GUARDS, IN ONE LINE: ``_fetch_name`` mapped **every** non-200 to
reason ``unknown_tenant``, so a licences 503 was indistinguishable from a 404.

WHY THAT IS EXPENSIVE RATHER THAN UNTIDY. ``services.conflict_reconciler``
(since removed, 2026-10-26) refused to sweep a tenant it cannot resolve, and counts the refusal in two
deliberately separate counters: a STRUCTURAL one (licences answered, and the
answer was "no such tenant" — a standing data fact an operator should drive to
zero) and a LIVENESS one (licences could not be asked — it self-clears). With
every non-200 reported as ``unknown_tenant``, a licences rollout returning 503
made the reconciler skip real, billable tenants and file them under the
structural counter, i.e. report an outage as a fleet of orphans. That is exactly
the conflation the two-counter design exists to prevent, and the skip happened
either way — so the only signal an operator had was pointing at the wrong cause.

THE SECOND HALF, WHICH IS WORSE. The 10s negative cache stored an entry for
every non-200 too, and ``_cache_get`` replayed it with a **hardcoded**
``unknown_tenant``. One 503 therefore became ten seconds of confident, CALLLESS
"that tenant does not exist" for that name — a structural claim re-asserted
without a single request behind it, and multiplied across every name in flight.

WHY THIS FILE EXISTS AT ALL. The four tests that cover the reconciler's refusal
all stub ``resolve_tenant_to_uuid`` outright, and the sibling resolution suites
only ever drive a 404 or a ``ConnectError``. Nothing in the repo had ever driven
``_fetch_name`` with a 5xx, a 429, a 401 or a read timeout, so the defect was
invisible to a green suite. Every test below reaches the real ``_fetch_name``
through the real ``httpx`` stack (``MockTransport``, the same idiom as
``test_tenant_resolution_hardening.py``) and asserts on the classification a
consumer actually branches on — never on a stub.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import auth.tenant_resolution as tenant_resolution
from auth.middleware import AuthMiddleware
from auth.tenant_resolution import (
    CONFIGURATION,
    CONFIGURATION_REASONS,
    STRUCTURAL,
    STRUCTURAL_REASONS,
    TRANSIENT,
    TRANSIENT_REASONS,
    UnresolvableTenantError,
    classify_reason,
    clear_tenant_resolution_cache,
    resolve_tenant_to_uuid,
)

LICENSED_TENANT_UUID = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"

# A syntactically valid name — it must clear the pre-network syntax gate, or the
# test would be measuring the regex instead of the status-code branch.
NAME = "n8n-dev"


class _Calls:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def __len__(self) -> int:
        return len(self.paths)


def _install(
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


def _status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    """Every request answers ``code``. The body mimics the live error shape."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json={"detail": f"synthetic {code}"})

    return _handler


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"tenant_id": LICENSED_TENANT_UUID, "name": NAME, "byo_mode": "platform"}
    )


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """Module-level caches leak between tests; without this the asserts lie."""
    clear_tenant_resolution_cache()
    yield
    clear_tenant_resolution_cache()


async def _raises(name: str = NAME) -> UnresolvableTenantError:
    with pytest.raises(UnresolvableTenantError) as excinfo:
        await resolve_tenant_to_uuid(name)
    return excinfo.value


# ---------------------------------------------------------------------------
# 1. The status code decides the class — the defect itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [500, 502, 503, 504, 408, 429])
async def test_a_server_side_status_is_transient_not_unknown_tenant(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """⛔ THE DEFECT. Each of these used to raise ``unknown_tenant``.

    Both halves are asserted deliberately. The reason is what
    ``auth.middleware`` actually matches on (as did the since-removed
    ``services.conflict_reconciler``), so a fix that only set ``classification`` correctly while leaving the
    reason as ``unknown_tenant`` would still report an outage as an orphan at
    every existing call site. ``is_structural`` is the property that must be
    false for the reconciler never to count this as a standing data fact.
    """
    _install(monkeypatch, _status(code))

    exc = await _raises()

    assert exc.reason == "licenses_unreachable", (
        f"a {code} from licences was reported as {exc.reason!r}; a consumer "
        "cannot tell an outage from an orphan with that"
    )
    assert exc.classification == TRANSIENT
    assert exc.is_transient is True
    assert exc.is_structural is False


async def test_a_404_is_the_only_kind_of_non_200_that_means_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control. Without it, "make everything transient" would pass above.

    404 is licences' measured answer for a name it does not have
    (``{"detail":"Tenant not found"}``), and it is the one non-200 that is
    genuinely a permanent, structural no.
    """
    _install(monkeypatch, _status(404))

    exc = await _raises()

    assert exc.reason == "unknown_tenant"
    assert exc.classification == STRUCTURAL
    assert exc.is_structural is True
    assert exc.is_transient is False


@pytest.mark.parametrize("code", [401, 403])
async def test_an_auth_status_is_a_configuration_fault(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """The byo-mode endpoint is deliberately tokenless (verified live).

    A 401/403 therefore means licences changed its auth posture under us. It is
    neither the caller's fault nor a self-healing outage, and above all it is NOT
    evidence the tenant is missing — counting it as one would send an operator
    to delete perfectly good rows.
    """
    _install(monkeypatch, _status(code))

    exc = await _raises()

    assert exc.reason == "licenses_auth_failed"
    assert exc.classification == CONFIGURATION
    assert exc.is_structural is False


@pytest.mark.parametrize("code", [400, 422])
async def test_a_rejected_request_is_a_configuration_fault(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """The name already matched licences' own tenant-name rule before we sent
    it, so a 400/422 is contract drift between the two services — not a missing
    tenant."""
    _install(monkeypatch, _status(code))

    exc = await _raises()

    assert exc.reason == "licenses_rejected_request"
    assert exc.classification == CONFIGURATION
    assert exc.is_structural is False


async def test_a_read_timeout_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transport-error path. Already correct before this change — pinned so
    a refactor of the status-code branch cannot regress it on the way past."""
    calls = _install(monkeypatch, _timeout)

    exc = await _raises()

    assert exc.reason == "licenses_unreachable"
    assert exc.is_transient is True
    assert len(calls) == 1, "the timeout must come from a real outbound attempt"


async def test_an_unanticipated_status_defaults_to_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status nobody mapped must NOT read as "this tenant does not exist".

    ``302`` stands in for the whole unmapped space. The default direction is the
    entire point: the original bug was a taxonomy whose fall-through was
    "structural", and any taxonomy shaped that way re-acquires it the next time
    somebody adds a status.
    """
    _install(monkeypatch, _status(302))

    exc = await _raises()

    assert exc.is_structural is False
    assert exc.classification == TRANSIENT


# ---------------------------------------------------------------------------
# 2. The negative cache must not re-report a transient failure callessly
# ---------------------------------------------------------------------------


async def test_a_transient_failure_is_never_negatively_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One 503 must not become ten seconds of callless "unknown tenant".

    The call count is the load-bearing assertion: a cached transient entry would
    suppress the second request and answer from the cache, which is a structural
    claim with nothing behind it. ``_negative`` is asserted empty as well so the
    test fails for the right reason if some other short-circuit ever suppresses
    the call.
    """
    calls = _install(monkeypatch, _status(503))

    first = await _raises()
    second = await _raises()

    assert first.reason == second.reason == "licenses_unreachable"
    assert len(calls) == 2, (
        f"a transient failure was cached and replayed without a call: {calls.paths}"
    )
    assert tenant_resolution._negative == {}, (
        f"a transient failure left a negative-cache entry: {tenant_resolution._negative}"
    )


async def test_a_structural_404_is_still_negatively_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control on the test above: the rate-limit that the cache exists for must
    survive the fix.

    Any pod on the trusted network can repeat one bad name indefinitely; without
    the negative entry that stream of requests is a stream of licences calls.
    Deleting the cache outright would also have passed the transient test.
    """
    calls = _install(monkeypatch, _status(404))

    for _ in range(3):
        exc = await _raises()
        assert exc.reason == "unknown_tenant"

    assert len(calls) == 1, f"the 404 rate-limit was lost: {calls.paths}"


async def test_a_cached_negative_replays_the_reason_it_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_cache_get`` used to raise a HARDCODED ``unknown_tenant``.

    That is only ever correct by accident. Storing the reason alongside the
    deadline makes the replay a report of something that was measured rather
    than a constant that happens to match today's single cached case.
    """
    _install(monkeypatch, _status(404))

    await _raises()
    replayed = await _raises()

    assert replayed.reason == "unknown_tenant"
    assert replayed.is_structural is True
    stored = tenant_resolution._negative[NAME]
    assert stored[1] == "unknown_tenant", f"the measured reason was not stored: {stored}"


async def test_the_replay_reports_the_STORED_reason_not_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replay path, tested so that it can actually FAIL.

    ⚠️ Written after a mutation run: reverting ``_cache_get`` to a hardcoded
    ``unknown_tenant`` survived every other test in this file. It had to — the
    only reason that reaches ``_negative`` today IS ``unknown_tenant`` (the other
    structural reasons are raised before the fetch), so the constant and the
    stored value are the same string and the test was passing on a coincidence
    rather than on the property.

    Seeding ``_negative`` directly is deliberate white-box: it is the only way to
    make the two hypotheses produce different answers, and the property being
    pinned — "a cached refusal reports what was measured" — is precisely what
    stops the next structural reason (a 410, say, or a distinct
    ``tenant_deprovisioned``) from being silently relabelled by the cache.
    """
    _install(monkeypatch, _status(500))  # any call at all would fail the assert below
    tenant_resolution._negative[NAME] = (
        tenant_resolution.time.monotonic() + 60.0,
        "a_different_structural_reason",
    )

    exc = await _raises()

    assert exc.reason == "a_different_structural_reason", (
        "the negative cache relabelled a stored refusal with a constant"
    )


async def test_an_outage_does_not_poison_the_next_successful_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end on the multiplication: 503, then licences comes back.

    With the old code the name was negatively cached for ``_NEGATIVE_TTL_S``, so
    the tenant kept resolving to "unknown" for ten seconds AFTER the outage
    ended — every one of those answers callless, and every consumer counting
    structural skips seeing an orphan. The recovery must be immediate.
    """
    state = {"down": True}

    def _flapping(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            return httpx.Response(503, json={"detail": "rolling out"})
        return _ok(request)

    calls = _install(monkeypatch, _flapping)

    outage = await _raises()
    assert outage.is_transient is True

    state["down"] = False
    assert await resolve_tenant_to_uuid(NAME) == LICENSED_TENANT_UUID
    assert len(calls) == 2, "recovery required a second real call, not a cached verdict"


# ---------------------------------------------------------------------------
# 3. The taxonomy itself
# ---------------------------------------------------------------------------


def test_the_three_classes_are_disjoint() -> None:
    """A reason in two classes makes ``classify_reason`` order-dependent."""
    assert not STRUCTURAL_REASONS & TRANSIENT_REASONS
    assert not STRUCTURAL_REASONS & CONFIGURATION_REASONS
    assert not TRANSIENT_REASONS & CONFIGURATION_REASONS


def test_an_unclassified_reason_defaults_to_transient() -> None:
    """The fail-safe direction, asserted directly rather than inferred.

    A future reason that nobody remembers to classify must cost a retry, never a
    false "this tenant does not exist".
    """
    assert classify_reason("a_reason_nobody_has_written_yet") == TRANSIENT


def test_every_reason_the_module_raises_is_explicitly_classified() -> None:
    """The durable half: adding a reason without classifying it reds the build.

    Parsed out of the module's own AST rather than listed here, because a list
    maintained by hand drifts exactly like the allowlist this change removed.
    Only STRING-LITERAL reasons are visible to this scan; that is the entire
    population today, and the assertion below fails loudly if the module ever
    starts raising a computed one, since the literal set would shrink.
    """
    source = pathlib.Path(tenant_resolution.__file__).read_text()
    raised: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "UnresolvableTenantError":
            continue
        for arg in node.args[1:]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                raised.add(arg.value)

    assert raised, "the AST scan found no reasons — the scan itself is broken"

    known = STRUCTURAL_REASONS | TRANSIENT_REASONS | CONFIGURATION_REASONS
    assert raised <= known, (
        f"reason(s) raised but never classified: {sorted(raised - known)}. "
        "An unclassified reason falls through to TRANSIENT, which is safe but "
        "silent — say which class it belongs to."
    )


def test_the_reason_the_reconciler_treats_as_transient_is_the_one_a_5xx_raises() -> None:
    """The coupling this whole change turns on, pinned where it can be read.

    ⚠️ CORRECTED: this used to say the reconciler "decides
    ``tenants_unverifiable_skipped`` vs ``tenants_unregistered_skipped`` from the
    reason string". It did, and that WAS the defect — a hardcoded two-reason
    tuple written one PR before this three-class model existed, which filed every
    CONFIGURATION reason under "this tenant does not exist". It now maps
    ``exc.classification`` to one of THREE counters
    (``_SKIP_COUNTER_BY_CLASSIFICATION``), so the coupling pinned here is the
    status→reason→CLASS chain, not a reason string read at the call site.

    A 5xx still has to raise a reason that is in ``TRANSIENT_REASONS`` — reusing
    ``licenses_unreachable`` rather than minting a new one is what makes an
    upstream 503 land in the liveness counter with no change at that call site.
    """
    assert tenant_resolution._reason_for_status(503) in TRANSIENT_REASONS
    assert tenant_resolution._reason_for_status(500) in TRANSIENT_REASONS
    assert tenant_resolution._reason_for_status(404) in STRUCTURAL_REASONS


# ---------------------------------------------------------------------------
# 4. What the caller actually sees at the HTTP boundary
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
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


@pytest.mark.parametrize("code", [500, 503, 429])
async def test_an_upstream_outage_is_a_503_to_the_caller(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Blaming the caller for our outage is how an error names a cause it never
    checked — and a 400 tells a client never to retry."""
    _install(monkeypatch, _status(code))

    resp = await _get({"X-Tenant-Id": NAME})

    assert resp.status_code == 503, resp.text
    assert resp.headers["Retry-After"] == "5"


async def test_an_upstream_401_is_a_502_not_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Our misconfiguration is not the caller's bad request, and it does not
    self-heal, so there is no ``Retry-After``."""
    _install(monkeypatch, _status(401))

    resp = await _get({"X-Tenant-Id": NAME})

    assert resp.status_code == 502, resp.text
    assert "Retry-After" not in resp.headers


async def test_a_genuinely_unknown_tenant_is_still_a_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control on both boundary tests above: the caller's own bad tenant must
    still be a 400, or "make everything a 503" would pass them."""
    _install(monkeypatch, _status(404))

    resp = await _get({"X-Tenant-Id": NAME})

    assert resp.status_code == 400, resp.text
    assert "Retry-After" not in resp.headers
