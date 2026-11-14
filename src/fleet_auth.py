"""Fleet auth + Postgres RLS helpers, vendored out of ``kag_common`` 0.37.0.

WHY THIS FILE EXISTS
--------------------
This service used ``kag_common`` for exactly the surface below and nothing else.
Depending on a shared fleet package for one service's auth boundary meant a pin
bump could change this service's admission behaviour without a line of its code
changing, and it made a historical Alembic migration depend on whatever DDL the
installed library happened to emit that week (see the frozen-predicate note in
``2026_07_02_0030_rls_tenant_isolation_backstop``). Copied BEHAVIOURALLY from
the 0.37.0 this service pinned, from:

  * ``kag_common/auth/__init__.py`` — licence-token extraction, pooled + cached
    licence introspection (single-flight, negative cache, bounded lock map).
  * ``kag_common/auth/outbound.py`` — :func:`outbound_headers`;
    ``kag_common/database/rls.py`` — RLS statements and :func:`register_tenant_guc`.

Not vendored: ``apply_effective_tenant`` / ``extract_license_token_with_source``
/ ``bind_plain_tenant`` (this service already carries local, behaviour-identical
implementations in ``auth/delegation.py`` and ``auth/middleware.py``); the
trusted-network predicate and its ``kag_trusted_network_bypass_total`` counter
(this service admits no tokenless caller, R351 (7)); and the modules this service
never imported (``config``, ``logging``, ``middleware``, ``telemetry``,
``idempotency``, ``internal_token``, ``database.audit``).

The reasoning in the docstrings is the specification: the single-flight collapse
exists for the 2026-06-27 TTS 401 class; the fail-closed GUC makes an unbound
tenant read zero rows rather than raise a cast error.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx
from cachetools import LRUCache, TTLCache
from sqlalchemy import Connection, MetaData, String, event
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from starlette.requests import Request

logger = logging.getLogger(__name__)

__all__ = [
    "clear_license_cache",
    "close_license_introspect_client",
    "downgrade_statements",
    "extract_license_token",
    "introspect_license",
    "outbound_headers",
    "register_tenant_guc",
    "tenant_table_names",
    "upgrade_statements",
]


def extract_license_token(request: Request) -> str | None:
    """Pull the license token out of ``X-Tenant-Id: Bearer <token>`` only.

    Unchanged from the library and deliberately so: it never reads
    ``Authorization``. The caller's middleware adds that carrier itself.
    """
    x_tenant_id = request.headers.get("X-Tenant-Id", "")
    if x_tenant_id and x_tenant_id.lower().startswith("bearer "):
        return x_tenant_id[7:].strip() or None
    return None


# --------------------------------------------------------------------------- #
# Pooled introspect client: one process-scoped AsyncClient, built lazily so it
# binds to the running event loop; closed from lifespan shutdown.
# --------------------------------------------------------------------------- #
_LICENSE_LIMITS = httpx.Limits(
    max_keepalive_connections=10,
    max_connections=20,
    keepalive_expiry=30.0,
)
_license_client: httpx.AsyncClient | None = None


def _license_timeout() -> httpx.Timeout:
    """Build the introspect timeout: 10s read/write (``LICENSE_INTROSPECT_TIMEOUT``),
    connect/pool tight at 1s.

    The old hardcoded 2s wall-clock was the cold-box trap: on a CPU-saturated
    host the event loop can starve long enough that a tighter budget fires before
    the licenses endpoint replies, surfacing as a spurious 401. Resolved at
    client-build time so an env override applies after a reconnect.
    """
    budget = float(os.getenv("LICENSE_INTROSPECT_TIMEOUT", "10.0"))
    return httpx.Timeout(budget, connect=1.0, read=budget, write=budget, pool=1.0)


def get_license_http_client() -> httpx.AsyncClient:
    """Return the process-wide pooled ``httpx.AsyncClient`` for introspection."""
    global _license_client
    if _license_client is None or _license_client.is_closed:
        _license_client = httpx.AsyncClient(timeout=_license_timeout(), limits=_LICENSE_LIMITS)
    return _license_client


async def close_license_introspect_client() -> None:
    """Close the shared introspection client (call from lifespan shutdown)."""
    global _license_client
    if _license_client is not None and not _license_client.is_closed:
        await _license_client.aclose()
    _license_client = None


# --------------------------------------------------------------------------- #
# Introspection cache + call: a positive TTLCache (5 min) shared by every
# introspection path, plus a short-TTL negative cache for ``active: false`` so a
# revoked token can't pin every request against the licenses endpoint.
# --------------------------------------------------------------------------- #
_license_cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=1000, ttl=300)
_license_neg_cache: TTLCache[str, dict[str, Any]] = TTLCache(
    maxsize=1000,
    ttl=int(os.getenv("LICENSE_INTROSPECT_NEG_TTL", "60")),
)

# Per-token single-flight locks. A cold-cache burst of concurrent requests for
# the SAME token would otherwise each miss the cache and independently POST the
# licenses endpoint. On a CPU-saturated box that stampede starves the event loop
# and trips the introspection timeout → spurious 401 (the 2026-06-27 TTS bug
# class). The lock collapses the burst into one upstream call.
#
# BOUNDED — this MUST NOT be a plain dict. The key is ``sha256(token)[:16]``,
# i.e. per distinct BEARER TOKEN and *not* per tenant, and introspection is
# reached PRE-AUTH: the middleware hands us the caller's raw header before
# anything about it has been validated. A caller sending a fresh random token per
# request would grow the map without bound. LRU keeps the HOT token's lock.
#
# Evicting a HELD lock is safe: the holder owns a strong reference on its own
# stack, so ``async with`` still releases it. A later caller then creates a fresh
# lock and proceeds concurrently — one duplicate upstream POST, exactly the
# pre-lock behaviour. The lock guards no invariant: ``_introspect_uncached`` only
# assigns whole values into two TTLCaches (no read-modify-write).
_INTROSPECT_LOCKS_MAXSIZE = 1000
_introspect_locks: LRUCache[str, asyncio.Lock] = LRUCache(maxsize=_INTROSPECT_LOCKS_MAXSIZE)


def clear_license_cache() -> None:
    """Drop all cached introspection results, positive and negative (test helper).

    Also drops the single-flight lock map, so a test that drives many distinct
    tokens cannot leak locks into the next test's assertions.
    """
    _license_cache.clear()
    _license_neg_cache.clear()
    _introspect_locks.clear()


def _resolve_introspect_url(url: str | None) -> str:
    """Resolve the introspect endpoint each call so env/test overrides win."""
    if url is not None:
        return url
    return os.getenv("LICENSE_INTROSPECT_URL", "")


def _cached(cache_key: str) -> dict[str, Any] | None:
    """Return a positive- or negative-cache hit for ``cache_key`` (or ``None``)."""
    hit = _license_cache.get(cache_key)
    if hit is not None:
        return hit
    return _license_neg_cache.get(cache_key)


async def introspect_license(token: str, *, url: str | None = None) -> dict[str, Any] | None:
    """Validate ``token`` against the licenses service.

    ``url`` defaults to the ``LICENSE_INTROSPECT_URL`` env var, resolved per call.
    Returns the introspection payload (or ``None`` on any failure / empty URL).
    ``active`` payloads are cached 5 min; ``active: false`` payloads are cached
    briefly in the negative cache (``LICENSE_INTROSPECT_NEG_TTL``, default 60s)
    so re-issuance is still picked up quickly. A cold-cache burst for the same
    token is collapsed into a single upstream POST via a per-token lock.
    """
    cache_key = hashlib.sha256(token.encode()).hexdigest()[:16]
    hit = _cached(cache_key)
    if hit is not None:
        return hit

    resolved = _resolve_introspect_url(url)
    if not resolved:
        logger.warning("LICENSE_INTROSPECT_URL not configured; rejecting bearer token")
        return None

    # Cache miss: single-flight so a concurrent burst for the same cold token
    # collapses into ONE upstream introspection. Re-check the caches inside the
    # lock — a prior waiter may have populated them while we were blocked.
    # ``setdefault`` on the LRU is a hit-then-insert: a hit refreshes recency, an
    # insert may evict the least-recently-used lock (safe — see the map comment).
    lock = _introspect_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        hit = _cached(cache_key)
        if hit is not None:
            return hit
        return await _introspect_uncached(token, resolved, cache_key)


async def _introspect_uncached(token: str, resolved: str, cache_key: str) -> dict[str, Any] | None:
    """Perform the introspection POST and populate the caches.

    Always called under the per-token single-flight lock held by
    :func:`introspect_license`.
    """
    try:
        client = get_license_http_client()
        resp = await client.post(resolved, json={"token": token})
    except (httpx.HTTPError, OSError) as e:
        logger.warning("license_introspect_failed", extra={"error": str(e)})
        return None

    if resp.status_code != 200:
        return None
    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        return None
    if data.get("active"):
        _license_cache[cache_key] = data
    else:
        _license_neg_cache[cache_key] = data
    return data


# --------------------------------------------------------------------------- #
# Outbound HTTP header builder for inter-service hops.
#
# The three-branch ``X-Tenant-Id`` ladder: ``tenant_token`` (JWT-shaped, >=2
# dots, Bearer-prefixed; opaque sent bare) → ``service_token`` (always
# Bearer-prefixed) → bare ``tenant_id``.
#
# Attribution arms, never credentials and never Bearer-prefixed: ``license_id``
# -> ``X-License-Id`` names WHICH customer licence; ``app_id`` -> ``X-App-Id``
# names WHICH application asked. Billing stores them separately. There is
# deliberately no default, no service self-naming and no "unknown" sentinel: a
# service stamping its own app_id collapses every distinct upstream origin into
# one bucket while reading as 100% coverage — strictly worse than an absence,
# because an absence can be counted and chased and a collapsed bucket cannot.
# --------------------------------------------------------------------------- #
def outbound_headers(
    tenant_id: str | None = None,
    *,
    tenant_token: str | None = None,
    service_token: str | None = None,
    authorization_service_token: str | None = None,
    subject_id: str | None = None,
    license_id: str | None = None,
    app_id: str | None = None,
    tenant_name: str | None = None,
    idempotency_key: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build standard outbound headers for inter-service hops.

    Always sets ``Content-Type: application/json`` and sets ``X-Tenant-Id`` via
    the three-branch ladder documented above. Optional fields are included only
    when truthy — an empty ``license_id``/``app_id`` sends NO header, because
    billing distinguishes "not provided" from "provided as the empty string".
    ``extra`` is merged last and wins on key conflict.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if tenant_token:
        headers["X-Tenant-Id"] = (
            f"Bearer {tenant_token}" if tenant_token.count(".") >= 2 else tenant_token
        )
    elif service_token:
        headers["X-Tenant-Id"] = f"Bearer {service_token}"
    elif tenant_id:
        headers["X-Tenant-Id"] = tenant_id

    if authorization_service_token:
        headers["Authorization"] = f"Bearer {authorization_service_token}"

    if tenant_name:
        headers["X-Tenant-Name"] = tenant_name
    if subject_id:
        headers["X-Subject-Id"] = subject_id
    if license_id:
        headers["X-License-Id"] = license_id
    if app_id:
        headers["X-App-Id"] = app_id
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if extra:
        headers.update(extra)

    return headers


# --------------------------------------------------------------------------- #
# Postgres Row-Level-Security helpers.
#
# The provable tenant-isolation backstop beneath this service's application-layer
# ``WHERE tenant_id = …`` predicates: even if a predicate were dropped, Postgres
# returns zero foreign-tenant rows and rejects foreign-tenant writes.
#
# Each tenant-scoped table (every table with a ``tenant_id`` column, minus
# ``exclude``) gets RLS ``ENABLE`` + a policy keyed on the per-request GUC
# ``app.current_tenant``: a NOT NULL ``tenant_id`` gets strict equality; a
# NULLABLE one gets "global-or-own" (NULL rows readable by every tenant, writes
# still GUC-scoped).
#
# Design invariants:
#   * ENABLE, never FORCE. The runtime connects as a separate non-owner restricted
#     role, which is bound under ENABLE; migrations and cross-tenant platform
#     processors keep connecting as the owner (owner-bypass under ENABLE). FORCE
#     would 0-row the owner and break them.
#   * Fail-closed GUC. ``NULLIF(current_setting('app.current_tenant', true), '')``
#     — ``current_setting(…, true)`` is missing-ok (unset → NULL); ``NULLIF(…)``
#     folds the empty string ``set_config(…, is_local=true)`` leaves on a pooled
#     PgBouncer connection back to NULL → predicate false → 0 rows (never a cast
#     500).
# --------------------------------------------------------------------------- #
TENANT_COLUMN = "tenant_id"
POLICY_NAME = "tenant_isolation"
_GLOBAL_READ = f"{POLICY_NAME}_read"
_GLOBAL_INS = f"{POLICY_NAME}_ins"
_GLOBAL_UPD = f"{POLICY_NAME}_upd"
_GLOBAL_DEL = f"{POLICY_NAME}_del"
_ALL_POLICY_NAMES = (POLICY_NAME, _GLOBAL_READ, _GLOBAL_INS, _GLOBAL_UPD, _GLOBAL_DEL)

# The transaction-scoped GUC the request path binds, as TEXT. The predicate casts
# to match the column type: ``uuid`` columns cast ``::uuid``; ``varchar``/text
# columns compare text-to-text (casting would raise "operator does not exist:
# character varying = uuid"). Factored once so migration + test event never drift.
_GUC_TEXT = "NULLIF(current_setting('app.current_tenant', true), '')"


def tenant_table_names(metadata: MetaData, *, exclude: Collection[str] = ()) -> list[str]:
    """Names of every table in ``metadata`` carrying a ``tenant_id`` column.

    ``exclude`` drops operational tables (outbox/idempotency/global stats) that
    carry ``tenant_id`` but are intentionally NOT under RLS — they are swept
    cross-tenant by background processors that connect as the owner.
    """
    ex = set(exclude)
    return [
        t.name for t in metadata.sorted_tables if TENANT_COLUMN in t.columns and t.name not in ex
    ]


def _tenant_column(metadata: MetaData, table_name: str):  # noqa: ANN202
    return metadata.tables[table_name].columns[TENANT_COLUMN]


def _is_nullable(metadata: MetaData, table_name: str) -> bool:
    return bool(_tenant_column(metadata, table_name).nullable)


def _predicate(metadata: MetaData, table_name: str) -> str:
    col = _tenant_column(metadata, table_name)
    guc = _GUC_TEXT if isinstance(col.type, String) else f"{_GUC_TEXT}::uuid"
    return f"{TENANT_COLUMN} = {guc}"


def _strict_statements(table: str, predicate: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}",
        f"CREATE POLICY {POLICY_NAME} ON {table} USING ({predicate}) WITH CHECK ({predicate})",
    ]


def _global_statements(table: str, predicate: str) -> list[str]:
    # Nullable tenant_id ⇒ NULL rows are shared/global (read by all), writes scoped.
    read = f"{TENANT_COLUMN} IS NULL OR {predicate}"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {_GLOBAL_READ} ON {table}",
        f"DROP POLICY IF EXISTS {_GLOBAL_INS} ON {table}",
        f"DROP POLICY IF EXISTS {_GLOBAL_UPD} ON {table}",
        f"DROP POLICY IF EXISTS {_GLOBAL_DEL} ON {table}",
        f"CREATE POLICY {_GLOBAL_READ} ON {table} FOR SELECT USING ({read})",
        f"CREATE POLICY {_GLOBAL_INS} ON {table} FOR INSERT WITH CHECK ({predicate})",
        f"CREATE POLICY {_GLOBAL_UPD} ON {table} FOR UPDATE "
        f"USING ({predicate}) WITH CHECK ({predicate})",
        f"CREATE POLICY {_GLOBAL_DEL} ON {table} FOR DELETE USING ({predicate})",
    ]


def upgrade_statements(metadata: MetaData, *, exclude: Collection[str] = ()) -> list[str]:
    """DDL applying RLS to every ``tenant_id`` table (use in a migration ``upgrade()``)."""
    out: list[str] = []
    for name in tenant_table_names(metadata, exclude=exclude):
        predicate = _predicate(metadata, name)
        out += (
            _global_statements(name, predicate)
            if _is_nullable(metadata, name)
            else _strict_statements(name, predicate)
        )
    return out


def downgrade_statements(metadata: MetaData, *, exclude: Collection[str] = ()) -> list[str]:
    """Reverse of :func:`upgrade_statements` (fully reversible)."""
    out: list[str] = []
    for name in tenant_table_names(metadata, exclude=exclude):
        for policy in _ALL_POLICY_NAMES:
            out.append(f"DROP POLICY IF EXISTS {policy} ON {name}")
        out.append(f"ALTER TABLE {name} DISABLE ROW LEVEL SECURITY")
    return out


def tenant_rls_mode() -> str:
    """Current enforcement mode from ``TENANT_RLS_MODE`` (``off`` | ``enforce``; default ``off``)."""
    return os.getenv("TENANT_RLS_MODE", "off").strip().lower()


def register_tenant_guc(
    engine: AsyncEngine,
    get_tenant: Callable[[], str | None],
    *,
    mode: str | None = None,
) -> None:
    """Bind ``app.current_tenant`` on **every** transaction of ``engine``.

    Registers one ``begin`` listener on the engine, so a single call covers
    *all* session paths — ``get_session`` plus any direct ``sessionmaker()``
    usage — making the binding **correct-by-construction**: a new session site
    can never forget it.

    ``get_tenant`` is invoked at transaction-begin and should return the
    request-scoped tenant (e.g. ``lambda: tenant_id_var.get(None)``); a
    client-asserted value must never be used. No-op unless enforcing; with no
    resolvable tenant the GUC is left unset → RLS fail-closes to 0 rows.
    ``set_config(…, is_local=true)`` is transaction-scoped → PgBouncer-safe.
    """

    @event.listens_for(engine.sync_engine, "begin")
    def _bind_tenant_guc(conn: Connection) -> None:
        if (mode or tenant_rls_mode()) != "enforce":
            return
        tenant = get_tenant()
        if tenant:
            conn.exec_driver_sql(
                "SELECT set_config('app.current_tenant', %s, true)", (str(tenant),)
            )
