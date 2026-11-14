"""Prometheus counters for requirements_management.

Naming follows the estate convention ``<service>_<thing>_total``, every label
bounded. Nothing here enforces anything: these counters only count.
"""

from __future__ import annotations

from prometheus_client import Counter

# G3 / R313 §1(b): a non-fr (conveyor) write to a governed asset (requirements /
# principles / decisions) with no project scope. Counted in ``observe`` (allowed) and
# ``enforce`` (refused) so the population is sized before GOVERNED_WRITE_SCOPE_MODE flips.
governed_write_unscoped_total = Counter(
    "requirements_management_governed_write_unscoped_total",
    "Non-fr writes to requirements/principles/decisions without a project scope; "
    "result=allowed (observe) or refused (enforce).",
    ["kind", "op", "mode", "result"],
)

_SEED_ROUTE_GROUP = "requirements"


def route_group(path: str) -> str:
    """First path segment only — bounded by router count, carries no ids."""
    segment = path.lstrip("/").split("/", 1)[0]
    return segment or "root"


# --------------------------------------------------------------------------- #
# ⭐ EFFECTIVE-TENANT DELEGATION (``X-Effective-Tenant-Id``). Ships INERT.
#
# A credentialled caller whose INTROSPECTED subject is listed in
# ``AUTH_DELEGATING_SUBJECTS`` may name the data tenant in a separate header, so
# one credential can still serve many tenants without collapsing them onto the
# credential's own tenant. ``AUTH_DELEGATING_SUBJECTS`` defaults EMPTY, so on the
# day this ships no subject can delegate and both counters stay at zero.
#
# TWO counters, not one with an outcome label, because the two readings answer
# different questions and are alerted on differently:
#   * ``..._delegation_total``  — delegation HONOURED. The volume that would move
#     if the allowlist were emptied again. This is the number that makes arming
#     reviewable after the fact.
#   * ``..._delegation_rejected_total`` — the header was PRESENT and named a
#     different tenant, but the subject is NOT allowlisted. Before arming this is
#     the "caller is ready, config is not" signal; after arming it is a spoof
#     attempt or a missed allowlist entry. Either way it must never be summed
#     into the honoured series.
#
# CARDINALITY — every label is bounded. The subject id and the named
# tenant are both unbounded and are deliberately NOT labels; they are carried on
# the structured log line in ``auth/delegation.py`` instead, where a one-off
# investigation can join them. ``route_group`` is the first path segment only.
# --------------------------------------------------------------------------- #
effective_tenant_delegation_total = Counter(
    "requirements_management_effective_tenant_delegation_total",
    "Requests where an allowlisted introspected subject named a different data "
    "tenant via X-Effective-Tenant-Id and the delegation was HONOURED.",
    ["route_group"],
)

effective_tenant_delegation_rejected_total = Counter(
    "requirements_management_effective_tenant_delegation_rejected_total",
    "Requests carrying an X-Effective-Tenant-Id naming a different tenant whose "
    "introspected subject is NOT in AUTH_DELEGATING_SUBJECTS — refused with 403 "
    "unless it resolves to the credential's own tenant.",
    ["route_group"],
)

# Materialise the zero series: a labelled Counter exports nothing until a child
# exists, and an unexported series reads as "never deployed", which is indistinguishable from
# "armed and never fired". With the allowlist empty these two are the proof that
# the code is live and doing nothing.
effective_tenant_delegation_total.labels(route_group=_SEED_ROUTE_GROUP)
effective_tenant_delegation_rejected_total.labels(route_group=_SEED_ROUTE_GROUP)


def record_effective_tenant_delegation(*, honoured: bool, path: str) -> None:
    """Count one effective-tenant outcome.

    Contracted never to raise: this runs inside the auth path
    and an observability failure must not be able to change an authentication
    decision.
    """
    try:
        counter = (
            effective_tenant_delegation_total
            if honoured
            else effective_tenant_delegation_rejected_total
        )
        counter.labels(route_group=route_group(path)).inc()
    except Exception:  # pragma: no cover - never break auth for a metric
        pass
