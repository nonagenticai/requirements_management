"""Every tenant refused at the request boundary must be COUNTED, not just logged.

``tests/test_tenant_resolution_hardening.py`` pins that an unregistered tenant is
REFUSED. This file pins the thing that makes the refusal *actionable*: the count
of refusals, by failure class, readable from the pod on ``/health``.

⛔ WHY THIS IS A SEPARATE PROPERTY AND NOT A DETAIL OF THE REFUSAL. The refusal
boundary shipped 2026-09-09. Whether it was working, and whether the population
being refused was shrinking, could only be answered afterwards by querying
``billing.billing_cost_events`` — i.e. by asking the LEDGER whether unattributable
spend had stopped, weeks later, from a different machine. Measured there on
2026-09-15, the three caller-minted ids
(``ad976f5b-f3a7-4441-ae87-c2090d038f9c``, ``6c6a0da6-d2a3-533a-a28b-2e15319b8832``
— the latter reversible to ``uuid5(NAMESPACE_URL, "1769980520185202330")``, a
snowflake integer — and ``351fdc59-6747-4e48-b130-73d8cdb52bf9``) last spent on
2026-09-09, the day the boundary closed. That is a burn-down curve reconstructed
from the wrong system. The service itself kept no number.

The four properties pinned here, each of which regresses silently:

1. A snowflake-derived ``uuid5`` is refused AND counted as STRUCTURAL.
2. A caller-generated ``uuid4`` that no registry knows is refused AND counted.
3. A REGISTERED tenant still passes and increments NOTHING — without this, a
   counter could "pass" by counting every request.
4. A refusal NEVER yields a fabricated or default tenant: the route is not
   reached at all, so no row can be written under an invented id.

Plus the class split: OUR outage (TRANSIENT) must not be counted as evidence
that a tenant does not exist, or the burn-down number becomes unreadable during
a licences rollout — the same conflation ``classify_reason`` exists to prevent.

Fake-transport idiom is deliberately identical to the sibling files. No respx.

Since R351 (7) there is no tokenless caller, so the caller-supplied tenant that
reaches this boundary is a DELEGATED one: an allowlisted licence (fr) naming the
data tenant in ``X-Effective-Tenant-Id``, verified against the registry exactly
as the removed trusted-network header was. ``_get`` sends it that way.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import auth.middleware as auth_middleware
import auth.tenant_resolution as tenant_resolution
from auth.middleware import AuthMiddleware
from auth.tenant_resolution import (
    CONFIGURATION,
    STRUCTURAL,
    TRANSIENT,
    clear_tenant_resolution_cache,
    reset_tenant_refusal_counts,
    tenant_refusal_counts,
)
from routers.health import health

# The live `n8n-dev` answer's shape (a UUIDv7) — same value as the sibling files.
LICENSED_TENANT_UUID = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"

# ⛔ DERIVED, NOT COPIED. `6c6a0da6-d2a3-533a-a28b-2e15319b8832` was found spending
# in billing, and the reversal below is what identifies it: a SNOWFLAKE INTEGER
# pushed through `uuid5(NAMESPACE_URL, ...)` by some caller that needed "a uuid"
# and made one. Deriving it here rather than pasting the literal means this test
# fails loudly if the derivation is ever wrong, instead of asserting on a magic
# string whose provenance is a comment.
SNOWFLAKE = "1769980520185202330"
SNOWFLAKE_TENANT = str(uuid.uuid5(uuid.NAMESPACE_URL, SNOWFLAKE))

# A caller-generated uuid4, found spending on `inference-router`/`pgx-local` with
# `license_id` NULL. Well-formed, in no registry.
CALLER_MINTED_TENANT = "ad976f5b-f3a7-4441-ae87-c2090d038f9c"


def test_the_snowflake_reversal_still_holds() -> None:
    """Guards the constant above: if this fails, the identification was wrong."""
    assert SNOWFLAKE_TENANT == "6c6a0da6-d2a3-533a-a28b-2e15319b8832"


class _Calls:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def __len__(self) -> int:
        return len(self.paths)


def _install_licenses_stub(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], object],
) -> _Calls:
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


def _only_n8n_dev_exists(request: httpx.Request) -> httpx.Response:
    """The live licences behaviour: one known tenant, 404 for everything else."""
    known = {
        "/tenants/n8n-dev/byo-mode",
        f"/tenants/{LICENSED_TENANT_UUID}/byo-mode",
    }
    if request.url.path in known:
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


def _licences_401(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"detail": "Not authenticated"})


@pytest.fixture(autouse=True)
def _clean_module_state() -> Iterator[None]:
    """Both the caches AND the counters are module-level; leakage makes the
    asserts lie in both directions (a cached negative skips a refusal, a carried
    count turns a delta assertion into a tautology)."""
    clear_tenant_resolution_cache()
    reset_tenant_refusal_counts()
    yield
    clear_tenant_resolution_cache()
    reset_tenant_refusal_counts()


DELEGATOR_TENANT = "0195f48b-d000-7000-8000-0000000000aa"
DELEGATOR_SUBJECT = "0195f48b-d000-7000-8000-0000000000c1"

# Raw headers only: the conftest test-licence shim must not rewrite them.
pytestmark = pytest.mark.tokenless


@pytest.fixture(autouse=True)
def _delegating_licence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Bearer delegator`` introspects as an allowlisted delegating subject."""

    async def _introspect(token: str) -> dict:
        if token == "delegator":
            return {"active": True, "tenant_id": DELEGATOR_TENANT, "subject_id": DELEGATOR_SUBJECT}
        return {"active": False, "reason": "invalid_token"}

    monkeypatch.setattr(auth_middleware, "_introspect_license", _introspect)
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", DELEGATOR_SUBJECT)


# The tenant ids the route actually saw. A refusal must leave this EMPTY — that
# is the "no request is ever assigned a fabricated or default tenant" property,
# asserted on what reached the handler rather than on the status code alone.
_SEEN: list[str | None] = []


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/requirements/echo")
    async def _echo(request: Request) -> JSONResponse:
        user = getattr(request.state, "user", None)
        tenant = getattr(user, "tenant_id", None)
        _SEEN.append(tenant)
        return JSONResponse({"tenant_id": tenant})

    return app


async def _get(headers: dict[str, str]) -> httpx.Response:
    """Send ``headers['X-Tenant-Id']`` as the DELEGATED tenant of a valid licence."""
    sent = {"X-Tenant-Id": "Bearer delegator", "X-Effective-Tenant-Id": headers["X-Tenant-Id"]}
    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get("/requirements/echo", headers=sent)


# ---------------------------------------------------------------------------
# 1. The measured fabricated ids are refused AND counted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tenant", "what"),
    [
        (SNOWFLAKE_TENANT, "a snowflake integer hashed into a uuid5"),
        (CALLER_MINTED_TENANT, "a uuid4 the caller minted for itself"),
        # A platform-series uuid7 on an UNALLOCATED suffix. It is the nastiest of
        # the four because it LOOKS allocated — same `0195f48b-d000-7000-8000-`
        # prefix as the real platform tenants — so only asking the registry can
        # tell it from `...-000000000001`. No shape check ever catches this one.
        ("0195f48b-d000-7000-8000-0000000001f2", "an unallocated platform-series uuid7"),
    ],
)
async def test_a_fabricated_tenant_is_refused_and_counted(
    monkeypatch: pytest.MonkeyPatch, tenant: str, what: str
) -> None:
    _SEEN.clear()
    _install_licenses_stub(monkeypatch, _only_n8n_dev_exists)

    resp = await _get({"X-Tenant-Id": tenant})

    assert resp.status_code == 400, f"{what} was ADMITTED: {resp.status_code} {resp.text}"
    counts = tenant_refusal_counts()
    assert counts[STRUCTURAL] == 1, (
        f"{what} was refused but not counted as structural; a refusal nobody "
        f"counts is a burn-down nobody can read. counts={counts}"
    )
    assert counts["total"] == 1, counts
    # The load-bearing half: the handler never ran, so nothing could be written
    # under a fabricated id, and no default was substituted for it either.
    assert _SEEN == [], f"the route was reached with tenant_id={_SEEN!r}"


# ---------------------------------------------------------------------------
# 2. The control — a real tenant still passes, and counts nothing
# ---------------------------------------------------------------------------


async def test_a_registered_tenant_passes_and_increments_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this, the counter could "pass" every test above by counting every
    request, and the boundary could "pass" by refusing everyone."""
    _SEEN.clear()
    _install_licenses_stub(monkeypatch, _only_n8n_dev_exists)

    resp = await _get({"X-Tenant-Id": LICENSED_TENANT_UUID})

    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == LICENSED_TENANT_UUID
    assert _SEEN == [LICENSED_TENANT_UUID]
    assert tenant_refusal_counts()["total"] == 0, tenant_refusal_counts()


# ---------------------------------------------------------------------------
# 3. The class split — our outage is not a tenant's data fact
# ---------------------------------------------------------------------------


async def test_our_outage_is_not_counted_as_a_missing_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A licences outage refuses the request (503), and MUST count TRANSIENT.

    Folding it into the structural bucket is how a licences rollout reads as a
    sudden fleet of orphaned tenants — precisely the misreading that
    ``classify_reason``'s three classes exist to prevent. The burn-down number
    is only trustworthy if an outage cannot inflate it.
    """
    _SEEN.clear()
    _install_licenses_stub(monkeypatch, _licences_down)

    resp = await _get({"X-Tenant-Id": "n8n-dev"})

    assert resp.status_code == 503, resp.text
    counts = tenant_refusal_counts()
    assert counts[TRANSIENT] == 1, counts
    assert counts[STRUCTURAL] == 0, (
        "a licences outage was counted as 'this tenant does not exist'; that "
        f"reading sends an operator to delete good data. counts={counts}"
    )
    assert _SEEN == []


async def test_a_broken_licences_config_is_counted_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 on a deliberately tokenless endpoint is a CONFIGURATION fault (502).

    It neither self-heals (so it is not TRANSIENT) nor says anything about the
    tenant (so it is not STRUCTURAL). Its own bucket is the difference between
    "go fix licences auth" and "go delete rows".
    """
    _SEEN.clear()
    _install_licenses_stub(monkeypatch, _licences_401)

    resp = await _get({"X-Tenant-Id": "n8n-dev"})

    assert resp.status_code == 502, resp.text
    counts = tenant_refusal_counts()
    assert counts[CONFIGURATION] == 1, counts
    assert counts[STRUCTURAL] == 0, counts
    assert _SEEN == []


# ---------------------------------------------------------------------------
# 4. The count is readable from the pod
# ---------------------------------------------------------------------------


async def test_health_reports_the_refusal_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The burn-down must be answerable by curling the pod, not by a log query.

    ``/health`` is where a human looks first and is the only unauthenticated
    diagnostic surface on the API port (``/metrics`` lives only on the separate
    metrics-only port, not the API).
    """
    _install_licenses_stub(monkeypatch, _only_n8n_dev_exists)
    await _get({"X-Tenant-Id": SNOWFLAKE_TENANT})

    body = await health()

    assert body["status"] == "ok"
    assert body["tenant_refusals"]["total"] == 1, body
    assert body["tenant_refusals"][STRUCTURAL] == 1, body


async def test_health_never_leaks_the_refused_tenant_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ ``/health`` is PUBLIC. Counts are fine there; identifiers are not.

    A future "make it more useful" change that adds the offending values to this
    dict publishes tenant identifiers to anyone who can reach the pod. The
    values stay in the WARNING log, behind cluster access.
    """
    _install_licenses_stub(monkeypatch, _only_n8n_dev_exists)
    await _get({"X-Tenant-Id": SNOWFLAKE_TENANT})

    rendered = repr(await health())

    assert SNOWFLAKE_TENANT not in rendered, rendered
    assert SNOWFLAKE not in rendered, rendered


def test_counts_are_snapshots_not_the_live_dict() -> None:
    """A caller mutating the returned dict must not be able to rewrite history.

    ``/health`` hands this straight into a JSON response; if it were the live
    object, a serializer or a future consumer touching it would corrupt the
    counter itself.
    """
    first = tenant_refusal_counts()
    first[STRUCTURAL] = 9999
    assert tenant_refusal_counts()[STRUCTURAL] == 0
