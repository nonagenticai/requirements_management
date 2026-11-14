"""The request boundary must hand the database a canonical UUID tenant.

``tenant_id`` is a real Postgres ``uuid`` column and the live RLS policy casts
the ``app.current_tenant`` GUC to ``uuid`` (``TENANT_RLS_MODE=enforce``). A
non-UUID tenant therefore does not fail *somewhere* — it fails on EVERY
statement of the request, as a 500. ``auth.tenant_resolution`` is the single
door that makes that impossible, and this file pins its contract:

- a UUID passes through with ZERO network calls (the hot path)
- a non-canonical UUID spelling is canonicalised
- a tenant NAME is resolved via the tokenless licences endpoint, and cached
- an unresolvable tenant is a clean 400 from the app, never a 500
- the resolver never returns a string that is not a canonical UUID

The licences call is faked with ``httpx.MockTransport`` — the idiom this repo
already uses everywhere it stubs httpx (see ``test_clients_tenant_headers.py``,
``test_llm_agents_new.py``). No respx.

The middleware is exercised against a small ASGI app built here, mirroring
``test_auth_middleware.py``, so nothing in this file needs postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

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
from tests.tenants import T

# A licences-supplied UUID (shape of the live n8n-dev answer: a UUIDv7).
LICENSED_TENANT_UUID = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"


class _Calls:
    """Counts the licences requests a test caused, and remembers their paths."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __len__(self) -> int:
        return len(self.paths)


def _install_licenses_stub(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> _Calls:
    """Point the module's shared client at a MockTransport and count calls."""
    calls = _Calls()

    def _counting(request: httpx.Request) -> httpx.Response:
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


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """Every test starts and ends with an empty name→uuid cache."""
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
# The hot path: a UUID never touches the network
# ---------------------------------------------------------------------------


async def test_uuid_passes_through_with_zero_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-cluster common case must not cost a licences round-trip."""

    def _explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"a UUID tenant must not call licences: {request.url}")

    calls = _install_licenses_stub(monkeypatch, _explode)

    tenant = T("user-123")
    assert await resolve_tenant_to_uuid(tenant) == tenant
    assert len(calls) == 0


@pytest.mark.parametrize(
    "spelling",
    [
        "019DE842-C92A-7E37-8FDD-EB0D8C5396F9",  # upper case
        "{019de842-c92a-7e37-8fdd-eb0d8c5396f9}",  # braced
        "urn:uuid:019de842-c92a-7e37-8fdd-eb0d8c5396f9",  # urn form
        # 32 hex chars, so detect-secrets scores it as a "Hex High Entropy
        # String". It is the same public tenant id as every other spelling in
        # this list, just without the dashes — not a secret.
        "019de842c92a7e378fddeb0d8c5396f9",  # unhyphenated # pragma: allowlist secret
        "  019de842-c92a-7e37-8fdd-eb0d8c5396f9  ",  # surrounding whitespace
    ],
)
async def test_non_canonical_uuid_is_canonicalised(
    monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """Every accepted UUID spelling collapses to the one canonical string.

    This matters beyond tidiness: two spellings of the same tenant must not
    become two tenants in the GUC, in logs, or in a cache key.
    """

    def _explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"a UUID tenant must not call licences: {request.url}")

    calls = _install_licenses_stub(monkeypatch, _explode)

    assert await resolve_tenant_to_uuid(spelling) == LICENSED_TENANT_UUID
    assert len(calls) == 0


# ---------------------------------------------------------------------------
# The name path: one licences call, then cached
# ---------------------------------------------------------------------------


async def test_name_is_resolved_against_licences(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_licenses_stub(monkeypatch, _known_tenant)

    assert await resolve_tenant_to_uuid("n8n-dev") == LICENSED_TENANT_UUID
    assert calls.paths == ["/tenants/n8n-dev/byo-mode"]


async def test_resolved_name_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tenant's UUID does not change — the second lookup is free."""
    calls = _install_licenses_stub(monkeypatch, _known_tenant)

    first = await resolve_tenant_to_uuid("n8n-dev")
    second = await resolve_tenant_to_uuid("n8n-dev")

    assert first == second == LICENSED_TENANT_UUID
    assert len(calls) == 1, f"expected the second call to be cached, got {calls.paths}"


async def test_unknown_name_raises_carrying_the_offending_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_licenses_stub(monkeypatch, _known_tenant)

    with pytest.raises(UnresolvableTenantError) as excinfo:
        await resolve_tenant_to_uuid("no-such-tenant")

    assert excinfo.value.value == "no-such-tenant"


async def test_upstream_200_with_a_non_uuid_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 is not a licence to return junk — that junk would reach the GUC."""

    def _liar(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tenant_id": "not-a-uuid", "name": "x"})

    _install_licenses_stub(monkeypatch, _liar)

    with pytest.raises(UnresolvableTenantError):
        await resolve_tenant_to_uuid("x")


async def test_licences_unreachable_raises_rather_than_leaking_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_licenses_stub(monkeypatch, _down)

    with pytest.raises(UnresolvableTenantError) as excinfo:
        await resolve_tenant_to_uuid("n8n-dev")

    assert excinfo.value.reason == "licenses_unreachable"


# ---------------------------------------------------------------------------
# Property: the resolver has exactly one shape of return value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        T("user-123"),
        "019DE842-C92A-7E37-8FDD-EB0D8C5396F9",
        "{019de842-c92a-7e37-8fdd-eb0d8c5396f9}",
        "019de842c92a7e378fddeb0d8c5396f9",  # pragma: allowlist secret
        "n8n-dev",
    ],
)
async def test_resolver_only_ever_returns_a_canonical_uuid(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Property-style: whatever comes back parses as a UUID AND is canonical.

    ``str(uuid.UUID(result)) == result`` is the load-bearing half — a value that
    merely *parses* (e.g. braced) would still be a different string from the one
    Postgres normalises to, which is how two spellings become two tenants.
    """
    _install_licenses_stub(monkeypatch, _known_tenant)

    result = await resolve_tenant_to_uuid(value)

    assert isinstance(result, str)
    assert str(uuid.UUID(result)) == result


@pytest.mark.parametrize("value", ["", "   ", "no-such-tenant", "a" * 200])
async def test_resolver_raises_rather_than_returning_a_non_uuid(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The other half of the property: there is no non-UUID escape hatch."""
    _install_licenses_stub(monkeypatch, _known_tenant)

    with pytest.raises(UnresolvableTenantError):
        await resolve_tenant_to_uuid(value)


# ---------------------------------------------------------------------------
# The boundary: what the caller actually sees
# ---------------------------------------------------------------------------


async def test_unresolvable_tenant_is_400_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown X-Tenant-Id must be rejected before it reaches the database.

    Before this wiring the value flowed straight into the RLS GUC and Postgres
    answered ``invalid input syntax for type uuid`` on every statement — a 500.
    """
    _install_licenses_stub(monkeypatch, _known_tenant)

    resp = await _get({"X-Tenant-Id": "no-such-tenant"})

    assert resp.status_code == 400, resp.text
    body: dict[str, Any] = resp.json()
    # Canonical fleet envelope — same shape as the middleware's 401s.
    assert body["error"]["status"] == 400
    assert body["error"]["code"] == "HTTP_400"
    assert "no-such-tenant" in body["error"]["message"]


async def test_resolvable_name_header_reaches_the_route_as_a_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant NAME header is admitted, and the route sees the UUID."""
    _install_licenses_stub(monkeypatch, _known_tenant)

    resp = await _get({"X-Tenant-Id": "n8n-dev"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == LICENSED_TENANT_UUID
