"""Unit tests for the license-token AuthMiddleware.

The middleware is exercised against a small ASGI app constructed inside each
test, so we are testing the middleware behaviour in isolation rather than the
full requirements_management app — that keeps the assertions tight and
avoids depending on the postgres-backed conftest fixtures.

Coverage:
- exactly ``PUBLIC_PATHS`` bypass auth; every other path (including one no
  prefix list ever named) is 401 without a token — default deny
- no token ⇒ 401, whatever the peer address (ASGITransport is 127.0.0.1) or
  tenant header (R351 (7): there is no tokenless / trusted-network identity)
- /requirements/* with a valid Bearer token in ``X-Tenant-Id`` is admitted
  and the introspection response populates ``request.state``
- /requirements/* with an invalid Bearer token → 401 (canonical envelope)
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import auth.middleware as auth_middleware
from auth.middleware import AuthMiddleware
from fleet_auth import clear_license_cache
from tests.tenants import T

# These tests drive the middleware with raw headers; the suite-wide conftest shim
# that turns a plain X-Tenant-Id into a test licence must not run here.
pytestmark = pytest.mark.tokenless


def _make_app() -> FastAPI:
    """Build a minimal app with one public + one protected route."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/made-up/route")
    async def _made_up() -> dict[str, str]:
        return {"status": "served"}

    @app.get("/requirements/echo")
    async def _echo(request: Request) -> JSONResponse:
        # AuthMiddleware stores the resolved identity on request.state.user
        # (a pydantic User model with sub/tenant_id/subject_id fields) and
        # also pins subject_id at the top level.
        user = getattr(request.state, "user", None)
        return JSONResponse(
            {
                "tenant_id": getattr(user, "tenant_id", None) if user is not None else None,
                "subject_id": getattr(request.state, "subject_id", None),
            }
        )

    return app


@pytest.fixture(autouse=True)
def _clear_license_cache() -> None:
    """Each test starts with a fresh introspection cache (shared fleet_auth)."""
    clear_license_cache()


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Public-endpoint bypass
# ---------------------------------------------------------------------------


async def test_health_is_public(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-headers"),
        pytest.param({"X-Tenant-Id": T("tenant-uuid-1")}, id="bare-tenant-uuid"),
        pytest.param({"X-Tenant-Id": "n8n-dev", "X-Subject-Id": "s-1"}, id="bare-tenant-name"),
        pytest.param({"X-Tenant-Id": "Bearer "}, id="empty-bearer"),
    ],
)
@pytest.mark.parametrize("path", ["/requirements/echo", "/made-up/route"])
async def test_no_token_is_401_from_loopback(
    client: httpx.AsyncClient, path: str, headers: dict[str, str]
) -> None:
    """ASGITransport's peer is 127.0.0.1 — once a trusted network. No token ⇒ 401,
    and a route under no old protected prefix is denied all the same."""
    resp = await client.get(path, headers=headers)
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_public_paths_are_exactly_the_probes_and_the_schema() -> None:
    assert auth_middleware.PUBLIC_PATHS == {
        "/health",
        "/health/live",
        "/health/ready",
        "/openapi.json",
    }


async def test_real_app_denies_every_documented_path_but_the_public_ones() -> None:
    """Every path in RM's own schema, plus /docs, /redoc, / and a made-up one, is 401
    without a token; only ``PUBLIC_PATHS`` answer. A new router is protected by
    default — nothing has to remember to list it."""
    from main import app

    schema_paths = list(app.openapi()["paths"])
    assert len(schema_paths) > 100  # vacuity floor: the real route table was walked
    probe = {p.replace("{", "x").replace("}", "") for p in schema_paths}
    probe |= {"/", "/docs", "/redoc", "/favicon.ico", "/metrics", "/no/such/router"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        opened = []
        for path in sorted(probe):
            if auth_middleware.is_public_path(path):
                continue
            resp = await ac.get(path)
            if resp.status_code != 401:
                opened.append((path, resp.status_code))
        assert opened == []
        schema = await ac.get("/openapi.json")
        assert schema.status_code == 200
        assert "paths" in schema.json()


async def test_protected_route_with_valid_license_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid Bearer token in X-Tenant-Id is introspected and admitted."""
    introspect_payload: dict[str, Any] = {
        "active": True,
        "tenant_id": T("tenant-uuid-1"),
        "subject_id": "subject-uuid-9",
        "license_id": "lic-77",
        "roles": ["user"],
        "tenant_name": "acme",
    }

    async def _fake(token: str) -> dict[str, Any]:
        assert token == "abc.def.ghi"
        return introspect_payload

    monkeypatch.setattr(auth_middleware, "_introspect_license", _fake)

    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/requirements/echo",
            headers={"X-Tenant-Id": "Bearer abc.def.ghi"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == T("tenant-uuid-1")
    assert body["subject_id"] == "subject-uuid-9"


async def test_license_token_subject_id_not_spoofable_via_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the license-token path the subject_id MUST come from introspection
    only — a client-supplied X-Subject-Id header must NOT override it, else a
    caller could impersonate another user within their tenant (within-tenant
    BOLA)."""
    introspect_payload: dict[str, Any] = {
        "active": True,
        "tenant_id": T("tenant-uuid-1"),
        "subject_id": "subject-uuid-9",
        "license_id": "lic-77",
        "roles": ["user"],
        "tenant_name": "acme",
    }

    async def _fake(_token: str) -> dict[str, Any]:
        return introspect_payload

    monkeypatch.setattr(auth_middleware, "_introspect_license", _fake)

    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/requirements/echo",
            headers={
                "X-Tenant-Id": "Bearer abc.def.ghi",
                "X-Subject-Id": "attacker-spoofed-victim",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The spoofed header is ignored; subject_id is the introspected value.
    assert body["subject_id"] == "subject-uuid-9"


async def test_protected_route_with_invalid_license_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inactive introspection result returns 401 with the envelope shape."""

    async def _fake(_token: str) -> dict[str, Any]:
        return {"active": False}

    monkeypatch.setattr(auth_middleware, "_introspect_license", _fake)

    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/requirements/echo",
            headers={"X-Tenant-Id": "Bearer dud-token"},
        )

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["status"] == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


async def test_protected_route_introspection_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Introspection returning None (no verdict: network error / non-200 /
    no URL) → 503 + Retry-After, not 401. An invalid token is a 200
    ``{"active": false}`` and is covered by the test above; None means the token
    was never checked, which is not the caller's fault."""

    async def _fake(_token: str) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr(auth_middleware, "_introspect_license", _fake)

    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/requirements/echo",
            headers={"X-Tenant-Id": "Bearer net-down"},
        )

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
    body = resp.json()
    assert body["error"]["status"] == 503
    assert "WWW-Authenticate" not in resp.headers


# ---------------------------------------------------------------------------
# A licence in `Authorization: Bearer …` (ingress gateway / n8n node shape)
# ---------------------------------------------------------------------------
#
# The gateway validates `Authorization`, strips client headers and injects a plain
# uuid `X-Tenant-Id`; the n8n node sends `Authorization` + a pinned plain
# `X-Tenant-Id`. Both must be licence principals.

LIC_TENANT = T("licence-tenant")
OTHER_TENANT = T("other-tenant")
LIC_SUBJECT = "0195f48b-d000-7000-8000-0000000000d1"


def _make_principal_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/requirements/whoami")
    async def _whoami(request: Request) -> JSONResponse:
        p = request.state.principal
        return JSONResponse(
            {
                "tenant_id": p.tenant_id,
                "subject_id": p.subject_id,
                "source": p.source,
                "delegated": p.delegated,
            }
        )

    return app


@pytest.fixture
def introspected(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Introspection by token: ``good`` → LIC_TENANT, ``xtid`` → OTHER_TENANT, else inactive."""
    seen: list[str] = []

    async def _fake(token: str) -> dict[str, Any]:
        seen.append(token)
        if token == "good":
            return {"active": True, "tenant_id": LIC_TENANT, "subject_id": LIC_SUBJECT}
        if token == "xtid":
            return {"active": True, "tenant_id": OTHER_TENANT, "subject_id": "s-x"}
        return {"active": False, "reason": "invalid_token"}

    monkeypatch.setattr(auth_middleware, "_introspect_license", _fake)
    monkeypatch.delenv("AUTH_DELEGATING_SUBJECTS", raising=False)
    return seen


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Identity resolver that records calls (``verify=True`` would hit licences)."""
    calls: list[tuple[str, bool]] = []

    async def _fake(value: str | None, *, verify: bool = False) -> str:
        calls.append((str(value), verify))
        return str(value).strip().lower()

    monkeypatch.setattr(auth_middleware, "resolve_tenant_to_uuid", _fake)
    return calls


async def _whoami(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_make_principal_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get("/requirements/whoami", headers=headers)


async def test_gateway_shape_is_a_licence_principal(introspected: list[str]) -> None:
    """Authorization licence + injected uuid X-Tenant-Id equal to it → licence principal."""
    resp = await _whoami({"Authorization": "Bearer good", "X-Tenant-Id": LIC_TENANT})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "tenant_id": LIC_TENANT,
        "subject_id": LIC_SUBJECT,
        "source": "license_token",
        "delegated": False,
    }
    assert introspected == ["good"]


async def test_gateway_shape_tenant_compare_ignores_case(introspected: list[str]) -> None:
    resp = await _whoami({"Authorization": "Bearer good", "X-Tenant-Id": LIC_TENANT.upper()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == LIC_TENANT


async def test_lowercase_authorization_scheme_is_a_licence(introspected: list[str]) -> None:
    """The auth scheme is case-insensitive (RFC 9110 §11.1)."""
    resp = await _whoami({"Authorization": "bearer good", "X-Tenant-Id": LIC_TENANT})
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "license_token"
    assert introspected == ["good"]


async def test_tenant_name_starting_with_bearer_is_still_a_claim(
    introspected: list[str], resolved: list[tuple[str, bool]]
) -> None:
    """A plain tenant NAME beginning with "bearer" is a tenant claim, not a dropped auth header."""
    resp = await _whoami({"Authorization": "Bearer good", "X-Tenant-Id": "bearerco"})
    assert resp.status_code == 403, resp.text


async def test_authorization_licence_with_other_plain_tenant_is_403(
    introspected: list[str], resolved: list[tuple[str, bool]]
) -> None:
    """A plain X-Tenant-Id naming another tenant is refused, never re-scoped to the licence."""
    resp = await _whoami({"Authorization": "Bearer good", "X-Tenant-Id": OTHER_TENANT})
    assert resp.status_code == 403, resp.text
    assert OTHER_TENANT in resp.json()["error"]["message"]


async def test_authorization_licence_with_other_effective_tenant_is_403(
    introspected: list[str], resolved: list[tuple[str, bool]]
) -> None:
    """Non-delegating: X-Effective-Tenant-Id alongside the plain header must match too."""
    resp = await _whoami(
        {
            "Authorization": "Bearer good",
            "X-Tenant-Id": LIC_TENANT,
            "X-Effective-Tenant-Id": OTHER_TENANT,
        }
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize(
    "carrier",
    [
        pytest.param({"Authorization": "Bearer good"}, id="authorization"),
        pytest.param({"X-Tenant-Id": "Bearer good"}, id="x-tenant-id-bearer"),
    ],
)
async def test_non_delegating_other_effective_tenant_alone_is_403(
    introspected: list[str], resolved: list[tuple[str, bool]], carrier: dict[str, str]
) -> None:
    """No plain X-Tenant-Id, only X-Effective-Tenant-Id naming ANOTHER tenant: refused.

    Before this it was served 200 as the licence's OWN tenant — a silent downgrade
    that answered with a different tenant's context than the one asked for, the
    only mismatch shape #222 left open.
    """
    resp = await _whoami({**carrier, "X-Effective-Tenant-Id": OTHER_TENANT})
    assert resp.status_code == 403, resp.text
    assert OTHER_TENANT in resp.json()["error"]["message"]


@pytest.mark.parametrize(
    "carrier",
    [
        pytest.param({"Authorization": "Bearer good"}, id="authorization"),
        pytest.param({"X-Tenant-Id": "Bearer good"}, id="x-tenant-id-bearer"),
    ],
)
@pytest.mark.parametrize("spelling", [LIC_TENANT, LIC_TENANT.upper(), f" {LIC_TENANT} "])
async def test_non_delegating_own_effective_tenant_is_a_no_op(
    introspected: list[str],
    resolved: list[tuple[str, bool]],
    carrier: dict[str, str],
    spelling: str,
) -> None:
    """The outbound S2S hook stamps X-Effective-Tenant-Id unconditionally: the licence's
    own tenant (textually or resolved-equal) must stay a 200 licence principal."""
    resp = await _whoami({**carrier, "X-Effective-Tenant-Id": spelling})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "tenant_id": LIC_TENANT,
        "subject_id": LIC_SUBJECT,
        "source": "license_token",
        "delegated": False,
    }


async def test_invalid_authorization_licence_is_401(
    introspected: list[str],
) -> None:
    """A bad licence is a 401, never a fall-through to some other identity."""
    resp = await _whoami({"Authorization": "Bearer dud", "X-Tenant-Id": LIC_TENANT})
    assert resp.status_code == 401, resp.text
    assert introspected == ["dud"]


async def test_delegating_subject_plain_tenant_takes_the_verify_path(
    introspected: list[str],
    resolved: list[tuple[str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allowlisted subject: plain X-Tenant-Id acts as X-Effective-Tenant-Id (verified)."""
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", LIC_SUBJECT)
    resp = await _whoami({"Authorization": "Bearer good", "X-Tenant-Id": OTHER_TENANT})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == OTHER_TENANT
    assert body["delegated"] is True
    assert (OTHER_TENANT, True) in resolved


async def test_delegating_subject_effective_header_wins_over_plain_tenant(
    introspected: list[str],
    resolved: list[tuple[str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    third = T("third-tenant")
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", LIC_SUBJECT)
    resp = await _whoami(
        {
            "Authorization": "Bearer good",
            "X-Tenant-Id": OTHER_TENANT,
            "X-Effective-Tenant-Id": third,
        }
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == third


async def test_delegating_subject_gateway_injected_own_tenant_is_not_delegation(
    introspected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway injects the licence tenant on every call; that is no delegation claim."""
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", LIC_SUBJECT)
    resp = await _whoami({"Authorization": "Bearer good", "X-Tenant-Id": LIC_TENANT})
    assert resp.status_code == 200, resp.text
    assert resp.json()["delegated"] is False


async def test_x_tenant_id_bearer_wins_over_authorization(introspected: list[str]) -> None:
    resp = await _whoami({"X-Tenant-Id": "Bearer xtid", "Authorization": "Bearer good"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == OTHER_TENANT
    assert introspected == ["xtid"]


async def test_authorization_only_uses_the_licence_tenant(introspected: list[str]) -> None:
    resp = await _whoami({"Authorization": "Bearer good"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == LIC_TENANT
    assert resp.json()["source"] == "license_token"


async def test_empty_authorization_bearer_is_not_a_licence(introspected: list[str]) -> None:
    """`Authorization: Bearer ` carries no token ⇒ 401, and nothing is introspected."""
    resp = await _whoami({"Authorization": "Bearer "})
    assert resp.status_code == 401, resp.text
    assert introspected == []


async def test_tokenless_tenant_header_is_401(introspected: list[str]) -> None:
    resp = await _whoami({"X-Tenant-Id": LIC_TENANT})
    assert resp.status_code == 401, resp.text
    assert introspected == []


# ---------------------------------------------------------------------------
# The resolved licence id is published on request.state
# ---------------------------------------------------------------------------


def _make_license_id_app() -> FastAPI:
    """Same shape as `_make_app`, but the route reports `request.state.license_id`."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/requirements/licence")
    async def _licence(request: Request) -> JSONResponse:
        return JSONResponse({"from_state": getattr(request.state, "license_id", None)})

    return app


async def test_license_id_is_published_on_request_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A licensed request publishes `license_id` from the introspection payload."""

    async def _fake(_token: str) -> dict[str, Any]:
        return {
            "active": True,
            "tenant_id": T("tenant-uuid-1"),
            "subject_id": "subject-uuid-9",
            "license_id": "lic-77",
        }

    monkeypatch.setattr(auth_middleware, "_introspect_license", _fake)

    transport = httpx.ASGITransport(app=_make_license_id_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/requirements/licence",
            headers={"X-Tenant-Id": "Bearer abc.def.ghi"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"from_state": "lic-77"}
