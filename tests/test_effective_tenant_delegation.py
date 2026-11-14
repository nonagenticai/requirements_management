"""Effective-tenant delegation (``X-Effective-Tenant-Id``) at the auth boundary.

The three cases that matter, and what each one is guarding:

1. HONOURED — an allowlisted INTROSPECTED subject names another tenant and the
   request is scoped to it. Without this the n8n node's one credential would
   serve every tenant's request under the credential's own tenant.
2. REFUSED — the negative control. The same request from a subject that is NOT
   on the allowlist is 403 (it used to be served as the credential's OWN tenant —
   a silent downgrade that answered with a different tenant's context than the
   one asked for; closed after #222 so every mismatch shape is 403). If this ever
   goes 200 again (e.g. someone keys the allowlist off a client header) the
   control is gone, so it asserts the counter too, not just the status.
3. EMPTY ALLOWLIST — the shipped state. Nobody can delegate: a DIFFERENT
   effective tenant is 403 and counted on the REJECTED counter (the "caller is
   credentialled and ready, the allowlist is not yet armed" signal); an EQUAL one
   (stamped unconditionally by the outbound S2S hook) is a byte-identical no-op.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import REGISTRY

import auth.middleware as auth_middleware
from auth.delegation import delegating_subjects
from auth.middleware import AuthMiddleware
from auth.tenant_resolution import UnresolvableTenantError
from fleet_auth import clear_license_cache

# All UUID-shaped, because the real resolver only accepts a UUID or a registered
# tenant NAME and the assertions here are about WHICH tenant is chosen, not about
# resolution itself.
SA_TENANT = "0195f48b-d000-7000-8000-0000000000aa"
CUSTOMER_TENANT = "0195f48b-d000-7000-8000-0000000000bb"
SA_SUBJECT = "0195f48b-d000-7000-8000-0000000000c1"
OTHER_SUBJECT = "0195f48b-d000-7000-8000-0000000000c2"

HONOURED_METRIC = "requirements_management_effective_tenant_delegation_total"
REJECTED_METRIC = "requirements_management_effective_tenant_delegation_rejected_total"
_LABELS = {"route_group": "requirements"}


def _counters() -> tuple[float, float]:
    """(honoured, rejected) for the /requirements route group."""
    honoured = REGISTRY.get_sample_value(HONOURED_METRIC, _LABELS)
    rejected = REGISTRY.get_sample_value(REJECTED_METRIC, _LABELS)
    # Both series are materialised at import in metrics.py precisely so a zero
    # reading is distinguishable from an absent one — assert that, don't paper
    # over it with `or 0.0`.
    assert honoured is not None, f"{HONOURED_METRIC} series is not exported"
    assert rejected is not None, f"{REJECTED_METRIC} series is not exported"
    return honoured, rejected


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/requirements/echo")
    async def _echo(request: Request) -> JSONResponse:
        user = getattr(request.state, "user", None)
        principal = getattr(request.state, "principal", None)
        body = {
            "tenant_id": getattr(user, "tenant_id", None) if user is not None else None,
            "subject_id": getattr(request.state, "subject_id", None),
            "license_id": getattr(request.state, "license_id", None),
        }
        # Only a delegated request carries a delegator; leave the non-delegated
        # response byte-identical to the pre-existing shape.
        delegator = getattr(principal, "delegator_subject_id", "")
        if delegator:
            body["delegator_subject_id"] = delegator
        return JSONResponse(body)

    return app


@pytest.fixture(autouse=True)
def _clear_license_cache() -> None:
    clear_license_cache()


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from the SHIPPED state, never from ambient env."""
    monkeypatch.delenv("AUTH_DELEGATING_SUBJECTS", raising=False)


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Record every tenant resolution and skip the licences round-trip.

    ``verify=True`` on the delegated branch would otherwise call out to the
    licences registry; recording the call lets the honoured test assert that the
    caller-supplied tenant IS verified, which is the point of the flag.
    """
    calls: list[tuple[str, bool]] = []

    async def _fake(value: str | None, *, verify: bool = False) -> str:
        calls.append((str(value), verify))
        return str(value)

    monkeypatch.setattr(auth_middleware, "resolve_tenant_to_uuid", _fake)
    return calls


@pytest.fixture
def introspects(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, Any] = {
        "active": True,
        "tenant_id": SA_TENANT,
        "subject_id": SA_SUBJECT,
        "license_id": "lic-service-account",
    }

    async def _fake(_token: str) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(auth_middleware, "_introspect_license", _fake)


@pytest.fixture
async def client(
    introspects: None, resolved: list[tuple[str, bool]]
) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --------------------------------------------------------------------------- #
# 1. Honoured
# --------------------------------------------------------------------------- #


async def test_delegation_honoured_for_allowlisted_subject(
    client: httpx.AsyncClient,
    resolved: list[tuple[str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", f"{OTHER_SUBJECT},{SA_SUBJECT}")
    before = _counters()

    resp = await client.get(
        "/requirements/echo",
        headers={
            "X-Tenant-Id": "Bearer sa-token",
            "X-Effective-Tenant-Id": CUSTOMER_TENANT,
            "X-Subject-Id": "customer-subject",
            "X-License-Id": "lic-customer",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == CUSTOMER_TENANT
    # The forwarded subject/licence ride along ONLY on this branch, so downstream
    # cost events attribute to the customer and not to the service account.
    assert body["subject_id"] == "customer-subject"
    assert body["license_id"] == "lic-customer"
    # The AUTHENTICATED delegator is preserved: auth.author_kind classifies the
    # caller by this, never by the caller-supplied X-Subject-Id.
    assert body["delegator_subject_id"] == SA_SUBJECT
    # Caller-supplied tenant ⇒ verified against the registry, not trusted.
    assert (CUSTOMER_TENANT, True) in resolved

    honoured, rejected = _counters()
    assert honoured == before[0] + 1
    assert rejected == before[1]


# --------------------------------------------------------------------------- #
# 2. Refused — the negative control
# --------------------------------------------------------------------------- #


async def test_delegation_refused_for_non_allowlisted_subject(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allowlist is non-empty but does NOT contain this token's subject."""
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", OTHER_SUBJECT)
    before = _counters()

    resp = await client.get(
        "/requirements/echo",
        headers={
            "X-Tenant-Id": "Bearer sa-token",
            "X-Effective-Tenant-Id": CUSTOMER_TENANT,
            "X-Subject-Id": "customer-subject",
            "X-License-Id": "lic-customer",
        },
    )

    # REFUSED, not downgraded: serving this as the credential's own tenant would
    # answer with a different tenant's context than the one the caller asked for.
    assert resp.status_code == 403, resp.text
    assert CUSTOMER_TENANT in resp.json()["error"]["message"]

    honoured, rejected = _counters()
    assert honoured == before[0]
    assert rejected == before[1] + 1


async def test_client_cannot_put_itself_on_the_allowlist_via_a_header(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allowlist is keyed on the INTROSPECTED subject, not X-Subject-Id.

    Here the caller sends ``X-Subject-Id: <an allowlisted subject>``. If the gate
    ever read the client header instead of the introspection payload this would
    delegate — and the control would be forgeable by anyone with any token.
    """
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", OTHER_SUBJECT)
    before = _counters()

    resp = await client.get(
        "/requirements/echo",
        headers={
            "X-Tenant-Id": "Bearer sa-token",
            "X-Effective-Tenant-Id": CUSTOMER_TENANT,
            "X-Subject-Id": OTHER_SUBJECT,
        },
    )

    assert resp.status_code == 403, resp.text
    assert _counters() == (before[0], before[1] + 1)


# --------------------------------------------------------------------------- #
# 3. Empty allowlist — the shipped state, must be a total no-op
# --------------------------------------------------------------------------- #


async def test_empty_allowlist_disables_delegation_entirely() -> None:
    assert delegating_subjects() == frozenset()


@pytest.mark.parametrize("raw", ["", "   ", ",", " , ,"])
async def test_blank_allowlist_spellings_are_all_empty(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", raw)
    assert delegating_subjects() == frozenset()


async def test_empty_allowlist_refuses_a_different_effective_tenant(
    client: httpx.AsyncClient,
) -> None:
    """Shipped state: nobody can delegate, so a DIFFERENT effective tenant is
    refused (403) — never silently served as the credential's own tenant — and is
    still recorded on the REJECTED counter."""
    before = _counters()

    resp = await client.get(
        "/requirements/echo",
        headers={
            "X-Tenant-Id": "Bearer sa-token",
            "X-Effective-Tenant-Id": CUSTOMER_TENANT,
        },
    )

    assert resp.status_code == 403, resp.text
    honoured, rejected = _counters()
    assert honoured == before[0]
    # An empty allowlist means the header-present case IS still visible as a
    # rejection — that is the "caller is ready, config is not" signal that the
    # arming decision is made from.
    assert rejected == before[1] + 1


async def test_empty_allowlist_response_is_identical_with_and_without_header(
    client: httpx.AsyncClient,
) -> None:
    """The strongest form of the no-op claim: same bytes either way.

    The header carries the credential's OWN tenant — the shape the outbound S2S
    hook stamps on every call. (A DIFFERENT tenant is now 403, see above, so it
    can no longer be part of a "same bytes" claim.)"""
    plain = await client.get("/requirements/echo", headers={"X-Tenant-Id": "Bearer sa-token"})
    with_header = await client.get(
        "/requirements/echo",
        headers={
            "X-Tenant-Id": "Bearer sa-token",
            "X-Effective-Tenant-Id": SA_TENANT,
            "X-Subject-Id": "customer-subject",
            "X-License-Id": "lic-customer",
        },
    )

    assert plain.status_code == with_header.status_code == 200
    assert (
        plain.json()
        == with_header.json()
        == {
            "tenant_id": SA_TENANT,
            "subject_id": SA_SUBJECT,
            "license_id": "lic-service-account",
        }
    )


async def test_effective_tenant_equal_to_credential_tenant_is_not_counted(
    client: httpx.AsyncClient,
) -> None:
    """The outbound S2S hook stamps this header unconditionally, so an EQUAL
    value arrives on ordinary traffic. Counting it would bury the real signal."""
    before = _counters()

    resp = await client.get(
        "/requirements/echo",
        headers={
            "X-Tenant-Id": "Bearer sa-token",
            "X-Effective-Tenant-Id": SA_TENANT,
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == SA_TENANT
    assert _counters() == before


async def test_allowlisted_subject_without_the_header_is_unchanged(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arming the allowlist alone changes nothing for callers that never send
    the header — the second half of the inertness argument."""
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", SA_SUBJECT)
    before = _counters()

    resp = await client.get("/requirements/echo", headers={"X-Tenant-Id": "Bearer sa-token"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "tenant_id": SA_TENANT,
        "subject_id": SA_SUBJECT,
        "license_id": "lic-service-account",
    }
    assert _counters() == before


# --------------------------------------------------------------------------- #
# 4. Same-tenant delegation forwards X-Subject-Id exactly like cross-tenant
# --------------------------------------------------------------------------- #

FR_OWN_TENANT_NAME = "fr-own-tenant"


def _whoami_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/requirements/whoami")
    async def _whoami(request: Request) -> JSONResponse:
        principal = request.state.principal
        return JSONResponse(
            {
                "tenant_id": principal.tenant_id,
                "subject_id": principal.subject_id,
                "delegated": principal.delegated,
                "license_id": request.state.license_id,
            }
        )

    return app


@pytest.fixture
async def whoami(
    introspects: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Resolver fake that behaves like the real one where it matters here: a NAME
    maps to its uuid, and a uuid is returned AS SPELLED (so the receiver's own
    comparison is what has to be case-insensitive)."""

    async def _fake(value: str | None, *, verify: bool = False) -> str:
        return SA_TENANT if value == FR_OWN_TENANT_NAME else str(value)

    monkeypatch.setattr(auth_middleware, "resolve_tenant_to_uuid", _fake)
    transport = httpx.ASGITransport(app=_whoami_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.parametrize("spelling", [SA_TENANT, SA_TENANT.upper(), FR_OWN_TENANT_NAME])
async def test_same_tenant_delegation_forwards_x_subject_id(
    whoami: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", SA_SUBJECT)
    resp = await whoami.get(
        "/requirements/whoami",
        headers={
            "X-Tenant-Id": "Bearer sa-token",
            "X-Effective-Tenant-Id": spelling,
            "X-Subject-Id": "end-user",
            "X-License-Id": "lic-end-user",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The WHOLE principal is spelling-independent: "same tenant" is decided on the
    # resolved uuid, so a name / upper-case uuid does not drop into the cross-tenant
    # branch (which would also swap in the forwarded licence).
    assert {**body, "tenant_id": body["tenant_id"].lower()} == {
        "tenant_id": SA_TENANT,
        "subject_id": "end-user",
        "delegated": True,
        "license_id": "lic-service-account",
    }


@pytest.mark.parametrize("subject_header", [None, "", "   "])
async def test_same_tenant_delegation_falls_back_like_cross_tenant(
    whoami: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, subject_header: str | None
) -> None:
    """Absent/blank X-Subject-Id falls back to the introspected subject, as cross-tenant does."""
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", SA_SUBJECT)
    headers = {"X-Tenant-Id": "Bearer sa-token", "X-Effective-Tenant-Id": SA_TENANT}
    if subject_header is not None:
        headers["X-Subject-Id"] = subject_header
    body = (await whoami.get("/requirements/whoami", headers=headers)).json()
    assert (body["subject_id"], body["delegated"]) == (SA_SUBJECT, True)


@pytest.mark.parametrize("spelling", [SA_TENANT, SA_TENANT.upper(), FR_OWN_TENANT_NAME])
async def test_non_allowlisted_same_tenant_x_subject_id_is_not_trusted(
    whoami: httpx.AsyncClient, spelling: str
) -> None:
    """No allowlist: the caller is conveyor and its X-Subject-Id is ignored."""
    resp = await whoami.get(
        "/requirements/whoami",
        headers={
            "X-Tenant-Id": "Bearer sa-token",
            "X-Effective-Tenant-Id": spelling,
            "X-Subject-Id": OTHER_SUBJECT,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "tenant_id": SA_TENANT,
        "subject_id": SA_SUBJECT,
        "delegated": False,
        "license_id": "lic-service-account",
    }


# --------------------------------------------------------------------------- #
# Own-tenant resolution must never break a cross-tenant call that worked before
# --------------------------------------------------------------------------- #

# Introspection gave a tenant NAME the registry cannot resolve, or no tenant_id at all
# (the credential tenant then falls back to the subject, here not a uuid either).
_UNRESOLVABLE_CREDENTIALS = [
    pytest.param({"tenant_id": "fr-unregistered-name"}, id="tenant-name"),
    pytest.param({"tenant_id": None, "subject_id": "svc-fr-not-a-uuid"}, id="no-tenant-id"),
]


@pytest.mark.parametrize("credential", _UNRESOLVABLE_CREDENTIALS)
@pytest.mark.parametrize(
    "reason", ["unknown_tenant", "licenses_auth_failed", "licenses_unreachable", "empty_tenant"]
)
async def test_unresolvable_own_tenant_falls_back_to_the_cross_tenant_path(
    monkeypatch: pytest.MonkeyPatch, credential: dict[str, Any], reason: str
) -> None:
    """The credential tenant is resolved ONLY to spot a same-tenant spelling. If that
    fails (any class: 400/502/503 reasons), the call is cross-tenant, exactly as on
    origin/main, where the credential tenant was never resolved on this path."""
    payload: dict[str, Any] = {
        "active": True,
        "tenant_id": SA_TENANT,
        "subject_id": SA_SUBJECT,
        "license_id": "lic-service-account",
        **credential,
    }

    async def _introspect(_token: str) -> dict[str, Any]:
        return payload

    resolved: list[tuple[str, bool]] = []

    async def _resolve(value: str | None, *, verify: bool = False) -> str:
        resolved.append((str(value), verify))
        if value == CUSTOMER_TENANT:
            return CUSTOMER_TENANT
        raise UnresolvableTenantError(str(value or ""), reason)

    monkeypatch.setattr(auth_middleware, "_introspect_license", _introspect)
    monkeypatch.setattr(auth_middleware, "resolve_tenant_to_uuid", _resolve)
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", str(payload["subject_id"]))

    transport = httpx.ASGITransport(app=_whoami_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/requirements/whoami",
            headers={
                "X-Tenant-Id": "Bearer sa-token",
                "X-Effective-Tenant-Id": CUSTOMER_TENANT,
                "X-Subject-Id": "customer-subject",
                "X-License-Id": "lic-customer",
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "tenant_id": CUSTOMER_TENANT,
        "subject_id": "customer-subject",
        "delegated": True,
        "license_id": "lic-customer",
    }
    # The own-tenant lookup really was attempted and really failed: this is the
    # fallback, not a path that skipped the comparison.
    assert (CUSTOMER_TENANT, True) in resolved
    assert any(v != CUSTOMER_TENANT for v, _ in resolved)


async def test_unresolvable_effective_tenant_still_fails(
    introspects: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback covers ONLY the credential's own tenant; an unresolvable
    EFFECTIVE tenant is still refused exactly as before (STRUCTURAL → 400)."""

    async def _resolve(value: str | None, *, verify: bool = False) -> str:
        if value == SA_TENANT:
            return SA_TENANT
        raise UnresolvableTenantError(str(value or ""), "unknown_tenant")

    monkeypatch.setattr(auth_middleware, "resolve_tenant_to_uuid", _resolve)
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", SA_SUBJECT)
    transport = httpx.ASGITransport(app=_whoami_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/requirements/whoami",
            headers={"X-Tenant-Id": "Bearer sa-token", "X-Effective-Tenant-Id": "nope-tenant"},
        )
    assert resp.status_code == 400, resp.text
