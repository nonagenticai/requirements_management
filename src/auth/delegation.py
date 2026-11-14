"""Effective-tenant delegation (``X-Effective-Tenant-Id``) — receiver side.

⭐ THE PROBLEM THIS EXISTS TO SOLVE.

This service reads a licence token from ``X-Tenant-Id: Bearer <token>`` and takes
the tenant EXCLUSIVELY from the introspection payload
(``auth.middleware._principal_from_license``). That is the right default — it is
why a customer cannot name another tenant — but it leaves a caller holding ONE
credential with no way to say which tenant a given request is FOR. The n8n custom
node is exactly that caller: its live workflows already pass a per-request tenant
as a bare ``X-Tenant-Id: <uuid>`` on the (since-removed) trusted-network path. If it simply moved
its single credential into ``X-Tenant-Id: Bearer …`` without this mechanism,
every one of those calls would silently collapse onto the credential's own
tenant — a cross-tenant WRITE regression, strictly worse than the unauthenticated
path it was meant to close.

⭐ THE CONTROL, AND WHY IT IS NOT FORGEABLE.

The allowlist is keyed on the **introspected subject** — the ``subject_id`` that
came back from license-authz — never on a header the caller supplied. A caller
can put any string it likes in ``X-Subject-Id``; it cannot put a string into the
introspection response. That distinction is the entire control: a customer
token's subject is not on the allowlist, so a customer that presents
``X-Effective-Tenant-Id`` naming another tenant is counted here and then REFUSED
with 403 by ``auth.middleware._principal_from_license`` (never served as its own
tenant — that silent downgrade was closed after #222).

⭐ SHIPS INERT.

``AUTH_DELEGATING_SUBJECTS`` defaults EMPTY. With it empty ``delegating_subjects()``
returns an empty frozenset, the first predicate in ``apply_effective_tenant`` can
never be true, and the function returns its arguments unchanged for every request
— the header is not even read on the honoured branch. Arming is a later, separate
config change, made from the counters in ``metrics.py``.

⛔ WHY THIS IS A LOCAL IMPLEMENTATION.

It mirrors the estate's ``apply_effective_tenant`` semantics, down to the
warn-on-mismatch-but-not-on-equal branch. Since 2026-11-01 this service no longer
depends on ``kag_common`` at all (its remaining surface is vendored in
``fleet_auth``), and this helper is deliberately NOT part of the vendored set:
this module is the implementation, not a mirror awaiting a swap, so changing it
is a behaviour change that needs its own RED/GREEN rather than a library bump.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from logging_config import get_logger
from metrics import record_effective_tenant_delegation

if TYPE_CHECKING:
    from starlette.requests import Request

logger = get_logger(__name__)

#: Wire name, identical to the fleet convention (``EFFECTIVE_TENANT_HEADER``). One
#: spelling across the estate.
EFFECTIVE_TENANT_HEADER = "X-Effective-Tenant-Id"

#: Env var carrying the CSV allowlist of introspected subjects permitted to
#: delegate. Empty (the default) disables delegation entirely.
DELEGATING_SUBJECTS_ENV = "AUTH_DELEGATING_SUBJECTS"


def delegating_subjects() -> frozenset[str]:
    """Subjects allowed to assert an effective tenant via ``X-Effective-Tenant-Id``.

    Read at CALL time — the established staged-rollout idiom in this repo
    (``TENANT_RLS_MODE`` in ``db.py``) — so a ConfigMap change or a test override applies
    without a module reload. Empty is the default and disables delegation.
    """
    raw = os.getenv(DELEGATING_SUBJECTS_ENV, "") or ""
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def apply_effective_tenant(
    request: Request,
    auth_subject: str | None,
    tenant_id: str,
    subject_id: str,
    license_id: str | None,
    requested: str | None = None,
) -> tuple[str, str, str | None]:
    """Re-scope identity to ``X-Effective-Tenant-Id`` for a delegating subject.

    The Bearer licence token authenticates the CALLER. A caller whose
    INTROSPECTED ``auth_subject`` is in ``AUTH_DELEGATING_SUBJECTS`` may act on
    behalf of another tenant by sending ``X-Effective-Tenant-Id``; everybody else
    is returned unchanged.

    Returns the (possibly re-scoped) ``(tenant_id, subject_id, license_id)``.
    The returned tenant is RAW caller input — it has not been through
    ``resolve_tenant_to_uuid`` and must be before it reaches the database. The
    caller does that; see ``_principal_from_license``.

    ``requested`` overrides the header read (``None`` ⇒ read
    ``X-Effective-Tenant-Id``). The middleware passes it when the licence came in
    ``Authorization`` and a plain ``X-Tenant-Id`` stands in for the effective tenant.
    """
    header = (request.headers.get(EFFECTIVE_TENANT_HEADER) or "").strip()
    eff_requested = header if requested is None else requested.strip()
    if not auth_subject or str(auth_subject) not in delegating_subjects():
        # NOT the honoured branch. A non-delegating caller asserting a DIFFERENT
        # tenant is either a spoof attempt or a receiver missing its allowlist
        # config — surface it instead of dropping it silently, because before
        # arming this is precisely the "the caller is ready" signal. The caller
        # (``_principal_from_license``) then refuses it with 403 unless the value
        # resolves to the credential's own tenant. An EQUAL
        # tenant is a benign no-op (the outbound S2S hook stamps this header
        # unconditionally), so it is neither counted nor logged: doing so would
        # be fleet-wide noise on the normal path.
        eff = eff_requested
        if eff and eff != tenant_id:
            record_effective_tenant_delegation(honoured=False, path=request.url.path)
            logger.warning(
                "auth.effective_tenant_rejected",
                subject_id=auth_subject or "",
                requested_tenant=eff,
                credential_tenant=tenant_id,
            )
        return tenant_id, subject_id, license_id

    eff_tenant = eff_requested
    if not eff_tenant or eff_tenant == tenant_id:
        return tenant_id, subject_id, license_id

    record_effective_tenant_delegation(honoured=True, path=request.url.path)
    logger.info(
        "auth.effective_tenant_delegated",
        subject_id=auth_subject,
        effective_tenant=eff_tenant,
        credential_tenant=tenant_id,
    )
    # Carry the run's own subject/licence when the delegator forwarded them, so
    # per-user metering attributes to the real customer rather than to the
    # delegating service account. These headers are only read on this branch —
    # i.e. only for a subject that introspection already proved is allowed to
    # speak for another tenant — so they are not a BOLA vector the way a bare
    # X-Subject-Id on the unauthenticated path would be.
    return (
        eff_tenant,
        forwarded_subject(request, subject_id),
        forwarded_license(request, license_id),
    )


def forwarded_subject(request: Request, fallback: str) -> str:
    """The end-user subject a DELEGATING caller forwarded in ``X-Subject-Id``.

    Stripped; blank or absent falls back to ``fallback`` (the introspected
    subject). Call this ONLY for a caller whose introspected subject is in
    ``delegating_subjects()`` — for anybody else ``X-Subject-Id`` is client input
    and trusting it is within-tenant BOLA. Shared by the cross-tenant branch above
    and the same-tenant branch in ``auth.middleware._principal_from_license`` so
    the two can never disagree about whose subject is recorded.
    """
    return (request.headers.get("X-Subject-Id") or "").strip() or fallback


def forwarded_license(request: Request, fallback: str | None) -> str | None:
    """``X-License-Id`` forwarded by a delegating caller; same rules as ``forwarded_subject``."""
    return (request.headers.get("X-License-Id") or "").strip() or fallback
