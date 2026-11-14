"""License token authentication middleware.

Validates license tokens via introspection.
Token is read from ``X-Tenant-Id: Bearer <token>`` or, failing that, from
``Authorization: Bearer <token>`` (the ingress gateway / n8n node shape).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from db import tenant_id_var
from fleet_auth import extract_license_token, introspect_license
from logging_config import bind_request_context, get_logger
from settings import get_settings

from .delegation import (
    EFFECTIVE_TENANT_HEADER,
    apply_effective_tenant,
    delegating_subjects,
    forwarded_subject,
)
from .models import User
from .tenant_resolution import (
    CONFIGURATION,
    STRUCTURAL,
    TRANSIENT,
    UnresolvableTenantError,
    record_tenant_refusal,
    resolve_tenant_to_uuid,
)

logger = get_logger(__name__)

PrincipalSource = Literal["license_token"]


@dataclass(frozen=True, slots=True)
class Principal:
    """Resolved request principal — single source of truth for auth state."""

    tenant_id: str
    subject_id: str  # "" == tenant-only caller
    tenant_name: str
    source: PrincipalSource
    license: dict[str, Any] | None  # the introspection result
    # True only when an introspected subject in ``AUTH_DELEGATING_SUBJECTS`` (fr) sent
    # ``X-Effective-Tenant-Id`` — re-scoped to another tenant, or equal to its own (same-
    # tenant delegation) — and is acting for a user. Never set from a client header alone.
    # Read by ``auth.author_kind``: delegated ⇔ ``user`` unless the delegator is the PM.
    delegated: bool = False
    # The INTROSPECTED subject that delegated, preserved even when a forwarded
    # ``X-Subject-Id`` replaces ``subject_id`` with the end user's id. Read by
    # ``auth.author_kind``: the PM (the conveyor) must delegate the tenant too, and a
    # delegated PM call is still the conveyor — never fr — for the PM-only write rule.
    delegator_subject_id: str = ""


def _license_introspect_url() -> str:
    """Return the license introspection URL from settings (env-configured)."""
    return get_settings().license_introspect_url


# Back-compat module-level constant. Some tests import this directly; runtime
# code should prefer ``_license_introspect_url()`` so the value is re-read after
# env changes (e.g. monkeypatched tests).
LICENSE_INTROSPECT_URL = _license_introspect_url()

# Default-deny: EVERY path requires a valid licence token except these exact paths
# (the kubelet probes and the live schema fr consumes — George 2026-10-29). A new
# router is protected the moment it is mounted; nothing has to remember to list it.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {"/health", "/health/live", "/health/ready", "/openapi.json"}
)


def is_public_path(path: str) -> bool:
    """True iff ``path`` (trailing slash ignored) is one of ``PUBLIC_PATHS``."""
    return (path.rstrip("/") or "/") in PUBLIC_PATHS


def _authorization_bearer(request: Request) -> str | None:
    """The token in ``Authorization: Bearer <token>``, or ``None``."""
    raw = (request.headers.get("Authorization") or "").strip()
    if raw[:7].lower() != "bearer ":
        return None
    return raw[7:].strip() or None


def _licence_from_authorization(request: Request) -> bool:
    """True iff this request's licence was taken from ``Authorization``.

    ``X-Tenant-Id: Bearer`` has precedence, so the licence came from
    ``Authorization`` exactly when ``X-Tenant-Id`` carried none.
    """
    return extract_license_token(request) is None and _authorization_bearer(request) is not None


def _plain_tenant_header(request: Request) -> str:
    """A non-Bearer ``X-Tenant-Id`` value (stripped), or ``""``."""
    raw = (request.headers.get("X-Tenant-Id") or "").strip()
    # Only the auth scheme itself ("Bearer" / "Bearer <tok>") is not a tenant; a tenant
    # NAME that merely starts with "bearer" (e.g. "bearerco") is still a claim.
    low = raw.lower()
    return "" if low == "bearer" or low.startswith("bearer ") else raw


class LicenceTenantMismatch(Exception):
    """A tenant header names a tenant the licence does not belong to. Rendered 403."""


async def _introspect_license(token: str) -> dict[str, Any] | None:
    """Introspect a license token to get tenant context.

    Delegates to the vendored ``fleet_auth.introspect_license``, which adds
    single-flight de-duplication, a positive + short-TTL negative cache, a
    process-pooled httpx client, and a 10s timeout (vs the old bespoke 2s that
    tripped spurious 401s on a CPU-saturated box). ``url`` is threaded from this
    service's settings so monkeypatched-settings tests still resolve correctly.
    """
    return await introspect_license(token, url=_license_introspect_url())


def _is_delegating_subject(subject_id: str) -> bool:
    """Whether the INTROSPECTED subject is on ``AUTH_DELEGATING_SUBJECTS``."""
    return bool(subject_id) and subject_id in delegating_subjects()


def _same_tenant(a: str, b: str) -> bool:
    """Resolved tenant uuids compared case-insensitively."""
    return a.strip().lower() == b.strip().lower()


async def _resolve_own_tenant_or_none(credential_tenant: str, effective_tenant: str) -> str | None:
    """The credential's OWN tenant uuid, or ``None`` if it cannot be resolved.

    Only used to recognise a same-tenant delegation reached via a different spelling.
    origin/main never resolved the credential tenant on the cross-tenant path, so a
    failure here (introspection gave a tenant NAME, or no ``tenant_id`` and the
    subject stood in) must not turn a working cross-tenant call into a 400/502/503:
    the caller treats ``None`` as NOT same-tenant and proceeds cross-tenant.
    """
    try:
        return await resolve_tenant_to_uuid(credential_tenant)
    except UnresolvableTenantError as exc:
        logger.debug(
            "auth.own_tenant_unresolved_fallback_cross_tenant",
            credential_tenant=credential_tenant,
            effective_tenant=effective_tenant,
            reason=exc.reason,
        )
        return None


async def _require_own_tenant(credential_tenant: str, claimed: list[str]) -> None:
    """Raise ``LicenceTenantMismatch`` unless every non-empty claim IS the licence tenant.

    Textual match first (no network); otherwise both sides are resolved and compared
    as uuids. Resolution failures propagate as ``UnresolvableTenantError``.
    """
    pending = [c for c in claimed if c and not _same_tenant(c, credential_tenant)]
    if not pending:
        return
    own = await resolve_tenant_to_uuid(credential_tenant)
    for claim in pending:
        if not _same_tenant(await resolve_tenant_to_uuid(claim), own):
            logger.warning("auth.licence_tenant_mismatch", claimed_tenant=claim, licence_tenant=own)
            raise LicenceTenantMismatch(
                f"Tenant '{claim}' does not match the tenant of the presented licence"
            )


def _same_tenant_delegated_principal(
    request: Request, intro: dict[str, Any], tenant_id: str, subject_id: str
) -> Principal:
    """fr acting for a user of its OWN tenant (George 2026-10-13).

    Delegated (``author_kind='user'``), and the end user's ``X-Subject-Id`` is
    forwarded through the SAME helper as the cross-tenant branch, with the same
    fallback to the introspected subject when absent. Only reached for an
    allowlisted introspected subject, so the header is not a BOLA vector.
    """
    eff_subject = forwarded_subject(request, subject_id)
    return Principal(
        tenant_id=tenant_id,
        subject_id=eff_subject,
        tenant_name=request.headers.get("X-Tenant-Name", ""),
        source="license_token",
        delegated=True,
        delegator_subject_id=subject_id,
        # A COPY: ``intro`` is the shared, TTL-cached introspection payload.
        license={**intro, "subject_id": eff_subject},
    )


async def _principal_from_license(request: Request, intro: dict[str, Any]) -> Principal:
    """Build a Principal from a successful license introspection result.

    The introspection payload's tenant_id is authoritative; subject_id is taken
    ONLY from the introspection payload (the license-authz-validated Subject
    UUID), never the client X-Subject-Id header — trusting the client header
    would let an authenticated caller spoof another user's subject within their
    own tenant (within-tenant BOLA). Empty string == "tenant-only caller"
    (downstream write paths fall back to subject_id := tenant_id; read paths
    return only visibility='tenant' rows).

    The tenant is put through ``resolve_tenant_to_uuid`` before it leaves this
    function: ``tenant_id`` is a real ``uuid`` column and the RLS GUC is cast to
    ``uuid``, so a non-UUID here poisons every statement of the request. Raises
    ``UnresolvableTenantError`` (rendered at the dispatch boundary by failure
    class: STRUCTURAL → 400, CONFIGURATION → 502, TRANSIENT → 503 +
    ``Retry-After``) rather than letting that reach the database.
    """
    credential_tenant = str(intro.get("tenant_id") or intro.get("subject_id") or "")
    subject_id = str(intro.get("subject_id") or "")
    license_id = str(intro.get("license_id") or "") or None
    delegating = _is_delegating_subject(subject_id)
    header_eff = (request.headers.get(EFFECTIVE_TENANT_HEADER) or "").strip()

    # LICENCE IN `Authorization` + PLAIN `X-Tenant-Id` (gateway, n8n node). The plain
    # header is a tenant CLAIM, never ignored and never silently replaced by the
    # licence tenant. Delegating subject: it stands in for X-Effective-Tenant-Id
    # (which wins if both are sent) and takes the verify path below. Anyone else:
    # it — and any X-Effective-Tenant-Id alongside it — must be the licence's own
    # tenant, else 403. A plain header textually equal to the licence tenant is no
    # delegation claim: the gateway injects that on every request.
    plain_tenant = _plain_tenant_header(request) if _licence_from_authorization(request) else ""
    if plain_tenant and not delegating:
        await _require_own_tenant(credential_tenant, [plain_tenant, header_eff])
    if delegating and plain_tenant and _same_tenant(plain_tenant, credential_tenant):
        plain_tenant = ""
    requested_eff = header_eff or (plain_tenant if delegating else "")

    # EFFECTIVE-TENANT DELEGATION. Keyed on the INTROSPECTED subject, which the
    # caller cannot forge — never on a header it supplies. Inert by default:
    # AUTH_DELEGATING_SUBJECTS is empty, so this returns its arguments unchanged
    # and the two lines below collapse to exactly the pre-existing behaviour.
    eff_tenant, eff_subject, eff_license = apply_effective_tenant(
        request, subject_id, credential_tenant, subject_id, license_id, requested=requested_eff
    )
    # NON-DELEGATING caller naming a DIFFERENT tenant in X-Effective-Tenant-Id (either
    # licence carrier). ``apply_effective_tenant`` has already counted + logged it;
    # it must now be REFUSED, not served as the credential's own tenant — a silent
    # downgrade answers with a different tenant's context than the one asked for.
    # A same-tenant header (textual, or resolved-equal) stays a no-op: the outbound
    # S2S hook stamps it unconditionally.
    if not delegating and header_eff:
        await _require_own_tenant(credential_tenant, [header_eff])

    if eff_tenant == credential_tenant:
        # Unchanged path. The uuid came out of a successful introspection and is
        # already registry-authoritative, so no verification round-trip.
        tenant_id = await resolve_tenant_to_uuid(credential_tenant)
        # SAME-TENANT delegation, spelled identically to the credential: an
        # allowlisted delegating subject (fr) acting for a user of its OWN tenant.
        # ``apply_effective_tenant`` only returns the credential tenant unchanged for
        # an allowlisted caller when the header is absent or textually equal, so a
        # non-empty header here IS the same tenant.
        if delegating and requested_eff:
            return _same_tenant_delegated_principal(request, intro, tenant_id, subject_id)
        return Principal(
            tenant_id=tenant_id,
            subject_id=subject_id,
            tenant_name=request.headers.get("X-Tenant-Name", ""),
            source="license_token",
            license=intro,
        )

    # Delegated. The effective tenant is CALLER-SUPPLIED — introspection vouched
    # for the caller, not for this value — so it is verified against the licences
    # registry rather than trusted into a uuid column and an RLS GUC on the
    # delegator's say-so.
    tenant_id = await resolve_tenant_to_uuid(eff_tenant, verify=True)
    # The header may spell fr's OWN tenant differently (its name, an upper-case
    # uuid). Decide "same tenant" on the RESOLVED uuids, case-insensitively, so the
    # recorded principal does not depend on the spelling. Resolution is unchanged.
    own_tenant = await _resolve_own_tenant_or_none(credential_tenant, tenant_id)
    if own_tenant is not None and _same_tenant(tenant_id, own_tenant):
        return _same_tenant_delegated_principal(request, intro, tenant_id, subject_id)
    return Principal(
        tenant_id=tenant_id,
        subject_id=eff_subject,
        tenant_name=request.headers.get("X-Tenant-Name", ""),
        source="license_token",
        delegated=True,
        delegator_subject_id=subject_id,
        # A COPY carrying the delegated identity, so `_apply_principal` publishes
        # the real customer's licence id (billing key on every downstream cost
        # event) instead of the service account's. `intro` itself is the shared,
        # TTL-cached introspection payload and must never be mutated in place.
        license={
            **intro,
            "tenant_id": tenant_id,
            "subject_id": eff_subject,
            **({"license_id": eff_license} if eff_license else {}),
        },
    )


def _apply_principal(request: Request, principal: Principal) -> None:
    """Materialize the principal onto request.state and bind log contextvars."""
    request.state.user = User(
        sub=principal.tenant_id,
        preferred_username=(principal.license or {}).get("subject_id", principal.tenant_id),
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id or None,
    )
    request.state.license = principal.license
    request.state.subject_id = principal.subject_id
    license_id = str((principal.license or {}).get("license_id") or "") or None
    request.state.license_id = license_id
    request.state.tenant_name = principal.tenant_name
    request.state.principal = principal
    # Publish the resolved tenant for the RLS GUC (register_tenant_guc reads this
    # at transaction-begin). Inert until TENANT_RLS_MODE=enforce; a client value
    # is never used — only this middleware-resolved tenant.
    tenant_id_var.set(principal.tenant_id)
    bind_request_context(
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id or "",
        principal_source=principal.source,
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware to validate license tokens.

    Every path except ``PUBLIC_PATHS`` requires a licence: the Bearer token from
    X-Tenant-Id (or, failing that, Authorization), validated via introspection.
    No token ⇒ 401, whatever the peer address or tenant header.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process each request and extract user context."""
        if is_public_path(request.url.path):
            response: Response = await call_next(request)
            return response

        # No token ⇒ 401, always. There is no tokenless / trusted-network identity.
        token = self._extract_license_token(request)
        if not token:
            return self._error_response(401, "Missing authentication token")
        intro = await _introspect_license(token)
        if intro is None:
            # `introspect_license` returns None only when it got NO verdict:
            # transport error, a non-200 from licences, an unparseable body, or
            # no LICENSE_INTROSPECT_URL. An invalid token comes back as a 200
            # `{"active": false}`. Not the caller's fault, so not a 401 — a 401
            # tells a well-behaved client its credential is bad and to stop.
            logger.warning("auth.introspection_unavailable")
            resp = self._error_response(
                503,
                "License introspection is unavailable; the token was not "
                "checked. This is not a problem with the request.",
            )
            resp.headers["Retry-After"] = "5"
            return resp
        if not intro.get("active"):
            reason = intro.get("reason", "invalid_token")
            return self._error_response(401, f"Invalid or expired license token: {reason}")
        try:
            principal = await _principal_from_license(request, intro)
        except UnresolvableTenantError as exc:
            return self._unresolvable_tenant_response(exc)
        except LicenceTenantMismatch as exc:
            return self._error_response(403, str(exc))
        _apply_principal(request, principal)
        logger.debug("auth.principal_resolved", source=principal.source)
        return await call_next(request)

    def _extract_license_token(self, request: Request) -> str | None:
        """The licence from ``X-Tenant-Id: Bearer <token>``, else ``Authorization: Bearer``.

        ``X-Tenant-Id`` delegates to ``fleet_auth.extract_license_token`` and
        keeps precedence. ``Authorization`` is the shape the ingress gateway passes
        through after validating it and the n8n node sends.
        """
        return extract_license_token(request) or _authorization_bearer(request)

    def _unresolvable_tenant_response(self, exc: UnresolvableTenantError) -> Response:
        """400 for a tenant that is neither a UUID nor a resolvable name.

        400, not 500: the caller sent a tenant the system cannot turn into the
        ``uuid`` that the column and the RLS GUC require, and that is a bad
        request — not an internal fault.

        The cases that are NOT the caller's fault are the ones that did not come
        back with an answer at all: the name passed the syntax gate, so it may
        well be valid and we simply could not ask. Blaming the caller for our own
        outage is how an error message ends up naming a cause it never checked,
        so those answer 503 with ``Retry-After``.

        ⚠️ This used to test ``exc.reason == "licenses_unreachable"`` — ONE
        reason string, with every other reason falling through to 400. That
        allowlist is the same shape as the defect that made ``_fetch_name`` call
        a 503 ``unknown_tenant``: the reason nobody remembered to add always
        lands in the "the caller is wrong" branch. It now branches on
        ``exc.classification``, so a new reason gets the right status by default
        (TRANSIENT) rather than the convenient one.

        CONFIGURATION answers 502: licences DID answer, but with something only
        an operator can fix (a 401/403 on a deliberately tokenless endpoint, a
        400/422 for a name licences' own rule accepts, a 200 whose ``tenant_id``
        is not a UUID). That is a bad gateway, not a bad request — and no
        ``Retry-After``, because nothing about it self-heals.
        Uses the same envelope as every other rejection here.
        """
        classification = exc.classification
        # Count BEFORE the status branch, so every refusal this boundary issues
        # is counted exactly once regardless of which status it becomes. Putting
        # the increment in the branches instead would mean a new status branch
        # silently stops counting — the same forgotten-call-site shape that
        # `classification` replaced an allowlist to avoid.
        refusals = record_tenant_refusal(exc.reason)
        logger.warning(
            "auth.tenant_unresolvable",
            tenant=exc.value,
            reason=exc.reason,
            classification=classification,
            refusals_total=refusals["total"],
            refusals_structural=refusals[STRUCTURAL],
        )
        if classification == CONFIGURATION:
            return self._error_response(
                502,
                f"Cannot resolve tenant '{exc.value}': the licences service "
                f"answered but the answer is unusable ({exc.reason}). This is not "
                "a problem with the request.",
            )
        if classification == TRANSIENT:
            resp = self._error_response(
                503,
                f"Cannot resolve tenant '{exc.value}': the licences service is "
                f"unreachable ({exc.reason}). This is not a problem with the "
                "request.",
            )
            # A 503 without Retry-After tells a client nothing about whether to
            # come back. Only a syntactically valid name can reach this branch
            # (malformed input is rejected before the network), so retrying is
            # genuinely the right advice here.
            resp.headers["Retry-After"] = "5"
            return resp
        return self._error_response(
            400,
            f"Unresolvable tenant '{exc.value}': expected a tenant UUID or a known "
            f"tenant name ({exc.reason})",
        )

    def _error_response(self, status_code: int, message: str) -> Response:
        """Create error response in the canonical fleet envelope shape.

        Shape: ``{"error": {"code": "...", "message": "...", "status": N}}``.
        Matches schemas.common.ErrorResponse and the global exception handlers
        in main.py so 401s from auth carry the same shape as every other
        4xx/5xx in the service.
        """
        code = "AUTHENTICATION_REQUIRED" if status_code == 401 else f"HTTP_{status_code}"
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": code, "message": message, "status": status_code}},
            headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else {},
        )
