"""Resolve a caller-supplied tenant identifier to a canonical UUID string.

``tenant_id`` is a real Postgres ``uuid`` column (migration
``2026_08_24_0042_tenant_id_is_a_uuid``), and the live RLS policy is
``tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid``
with ``TENANT_RLS_MODE=enforce``. Two consequences make this module a *request
boundary* concern rather than a repository concern:

* inserting a non-UUID tenant raises ``psycopg.errors.InvalidTextRepresentation``
  — a 500 to the caller;
* binding a non-UUID to the ``app.current_tenant`` GUC raises ``invalid input
  syntax for type uuid`` on **every** statement of that request, not just the
  offending one.

So the tenant must be proven to be a canonical UUID string *before* anything
else happens. ``resolve_tenant_to_uuid`` is the single door:

* a value that parses as a UUID is returned in canonical lowercase form, with
  **no network call** (this is the hot path — every in-cluster request);
* anything else is treated as a tenant NAME and resolved against the licences
  service, ``GET {base}/tenants/{name}/byo-mode`` → 200 ``{"tenant_id": …}``.
  That endpoint is deliberately tokenless (verified live: ``n8n-dev`` → 200,
  an unknown name → 404 ``{"detail":"Tenant not found"}``).

Base-URL resolution order (documented because it is deliberately unusual):

1. derived from ``LICENSE_INTROSPECT_URL`` (scheme://netloc) when set — if a
   deployment already points introspection at a licences service, name
   resolution needs no second setting;
2. else the explicit ``LICENSES_BASE_URL`` env var;
3. else the placeholder ``DEFAULT_LICENSES_BASE_URL`` below.

An operator who needs to point name-resolution somewhere else than
introspection must therefore unset/override ``LICENSE_INTROSPECT_URL`` — both
endpoints live on the same licences service, so splitting them is not a case
we support.

httpx style: one shared
``AsyncClient`` per process, created lazily, closed on shutdown, every
``httpx.HTTPError`` caught at the call site (never allowed to escape as a 500).

⛔ THIS MODULE DERIVES NOTHING — BUT DERIVED TENANT IDS EXIST AND ARE SPENDING
-----------------------------------------------------------------------------
Grep ``uuid5`` / ``TENANT_NAMESPACE`` / ``canonical_tenant_uuid`` across the
repo and you get exactly two hits, neither of them a request path: the one-off
migration ``alembic/versions/2026_08_24_0042_tenant_id_is_a_uuid.py``, and
``tests/tenants.py``, which mirrors its namespace so a label maps to the same
UUID in a test as it does in the repair. Nothing under ``src/`` imports either.
Runtime tenant resolution is this module and only this module, and it does the
*opposite* of deriving: an unresolvable name raises ``UnresolvableTenantError``
(→ 400) rather than minting a synthetic UUID for it. There is no live
derivation, so there is nothing here to instrument — do not go looking.

The migration DID mint them, though, and they are still in the data.
``tenant_id_crosswalk`` in ``requirements_api`` records every legacy value it
remapped: **11 of its 12 rows are ``source=derived``**, i.e.
``uuid5(TENANT_NAMESPACE, label)`` over labels no licences tenant resolves —
opaque 19-digit numbers (``1764108879001552894``, ``1769980520185202330``,
``1797416352504349851``, …) plus ``__system_workflow__``,
``asset-improve-live``, ``auto``, ``smoke-plan-7``. Only ``n8n-dev`` is
``source=registry``. Each mapping is deterministic and 1:1, so the isolation
boundary is exactly what it was. What it is NOT is *billable*: there is no
licences row, no name and no owner behind any of those UUIDs, so LLM spend
attributed to them can never be invoiced. Two of them were still spending on
inference-router (120 events each per 24h) when this was written, and they are
the entire ``billing:license_tenant_unregistered_events:leader`` population.

They no longer arrive as labels — a caller sending ``1764108879001552894``
today is rejected right here. They survive only as **stored values** (orphan
rows, which are inert). The background conflict reconciler that used to
enumerate them and spend LLM calls on them was removed with the LLM lane
(2026-10-26).

⚠️ OPEN QUESTION, deliberately left open: what those 11 labels *should* map to.
Nothing in the estate knows, and inventing a mapping would silently merge two
tenants' data — strictly worse than the visible orphan we have now. Do not
"repair" the crosswalk without an owner-supplied mapping.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from urllib.parse import quote, urlsplit

import httpx

from logging_config import get_logger
from settings import get_settings

logger = get_logger(__name__)

# Placeholder licences service. Only reached when neither LICENSE_INTROSPECT_URL nor
# LICENSES_BASE_URL is configured — i.e. a local run with auth enabled and nothing set.
# A real deployment configures one of those two; this default exists so the code path is
# explicit rather than an empty string.
DEFAULT_LICENSES_BASE_URL = "http://licenses.example.com:8544"

# A tenant's UUID never changes, so a positive entry can be cached for a long
# time; 300s is short enough that a mis-resolution self-heals within a
# deploy-cycle and long enough that a busy tenant costs one lookup per 5 min.
_POSITIVE_TTL_S = float(os.getenv("TENANT_RESOLUTION_CACHE_TTL_S", "300"))
# Bounded so a hostile caller cycling random names cannot grow the process.
_CACHE_MAX_ENTRIES = int(os.getenv("TENANT_RESOLUTION_CACHE_MAX", "512"))
# Unknown names are remembered briefly so an unbounded stream of them cannot be
# turned into an unbounded stream of licences requests.
_NEGATIVE_TTL_S = float(os.getenv("TENANT_RESOLUTION_NEGATIVE_TTL_S", "10"))
# ⛔ AND BOUNDED, for the same reason ``_CACHE_MAX_ENTRIES`` exists. The TTL
# alone does NOT bound this dict. ⚠️ CORRECTED: this comment used to say an entry
# is "only ever removed by ``_cache_get``". There are three removers, and naming
# one of them made the reclaim path look narrower than it is —
# ``clear_tenant_resolution_cache`` drops the whole dict (tests and the escape
# hatch), and ``_negative_put`` below reclaims the expired FIFO head on insert.
# What is TRUE, and is the actual leak, is that no remover is driven by the
# passage of time: ``_cache_get`` only reclaims a name that is LOOKED UP AGAIN
# after expiring, and before the cap ``_negative_put`` reclaimed nothing at all.
# So a name seen once and never again was retained for the life of the process.
# The stream this cache exists to absorb is by definition a stream of names that
# are never repeated, so without a cap the mitigation grew without limit on
# exactly the input it was written for.
_NEGATIVE_MAX_ENTRIES = int(os.getenv("TENANT_RESOLUTION_NEGATIVE_MAX", "512"))

# licences' own tenant-name rule (`^[a-z0-9][a-z0-9-]*$`, main.py TenantCreate).
# Bounded length so the regex cannot be handed something pathological.
_TENANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,126}$")

# name -> (monotonic deadline, reason) for a name licences said it does not know.
#
# ⛔ ONLY STRUCTURAL FAILURES ARE STORED HERE. This used to be a bare deadline
# and ``_fetch_name`` wrote one for EVERY non-200, which is how a single
# licences 503 became ten seconds of confident, callless "that tenant does not
# exist" for that name — one outage multiplied into N false structural
# readings, none of which cost a request that anybody could have counted.
# The reason is carried alongside the deadline so the replay raises the reason
# that was actually measured rather than a hardcoded ``unknown_tenant``.
_negative: dict[str, tuple[float, str]] = {}

# name -> the in-flight lookup, so concurrent resolutions share one request.
_inflight: dict[str, asyncio.Task[str]] = {}

# name -> (uuid, monotonic expiry). Positive results only; unknown names live in
# ``_negative`` above with a much shorter TTL.
#
# This block used to say negative results were deliberately NOT cached, on the
# theory that a failing name terminates the request and so cannot retry-storm
# licences. Measured, that was wrong: 400 requests over 200 distinct unknown
# names produced 400 outbound calls, and every pod IP in 10.0.0.0/8 is on the
# trusted network. Hence ``_NEGATIVE_TTL_S`` — short enough (10s) that a tenant
# registered seconds ago is still usable almost immediately, which was the real
# concern behind the original decision.
_cache: dict[str, tuple[str, float]] = {}

# Longest tenant NAME we will even attempt to resolve. Rejecting oversize input
# before the network call keeps a junk header from becoming an upstream request.
_MAX_NAME_LEN = 128

_HTTP_TIMEOUT_S = float(os.getenv("TENANT_RESOLUTION_TIMEOUT_S", "10"))

_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# Failure CLASSES — why a reason string is not enough
# ---------------------------------------------------------------------------
# ⛔ EVERY non-200 USED TO RAISE ``unknown_tenant``. That single line conflated
# three things a consumer must act on differently, and the damage is not
# theoretical: the (since-removed) conflict reconciler skipped a tenant it could
# not resolve and counted it, deliberately, in TWO counters — a structural one
# ("licences answered, and the answer was no such tenant"; drive it to 0 by
# fixing the data) and a liveness one ("licences could not be asked"; it
# self-clears). Mapping a 503 to ``unknown_tenant`` made a licences rollout
# report a fleet of orphaned tenants, which is precisely the reading the
# two-counter split exists to prevent — and the reconciler SKIPS either way, so
# an outage silently converted into unbilled work reported as somebody else's
# data problem.
#
# The three classes and what a consumer owes each:
#
# * ``STRUCTURAL``  — licences answered, and the answer is a permanent no (404),
#                     or the input never had a chance (malformed/empty/oversize).
#                     Safe to refuse, safe to count as a standing fact, safe to
#                     cache briefly. This is the ONLY class that may be read as
#                     "this tenant does not exist".
# * ``TRANSIENT``   — we did not get an answer: a timeout, a refused connection,
#                     a 5xx, a 429, a body we could not parse. The tenant may be
#                     perfectly real. MUST NOT be cached, MUST NOT be counted as
#                     a structural fact, and a caller that skips work on it MUST
#                     count that skip somewhere an operator can see.
# * ``CONFIGURATION``— licences answered, but with something only a human can
#                     resolve: 401/403 on an endpoint that is meant to be
#                     tokenless, a 400/422 for a name that already passed
#                     licences' own name rule, a 200 whose ``tenant_id`` is not a
#                     UUID. Neither the caller's fault nor self-healing.
#
# ⚠️ ``classify_reason`` defaults UNKNOWN reasons to TRANSIENT on purpose. The
# failure that produced this module's defect was an allowlist of transient
# reasons with everything else falling through to "structural"; a taxonomy whose
# default direction is "this tenant does not exist" will re-acquire that bug the
# next time somebody adds a reason. Defaulting the other way costs a retry.
TRANSIENT = "transient"
STRUCTURAL = "structural"
CONFIGURATION = "configuration"

STRUCTURAL_REASONS = frozenset(
    {
        "unknown_tenant",
        "malformed_tenant",
        "name_too_long",
        "empty_tenant",
    }
)

CONFIGURATION_REASONS = frozenset(
    {
        "licenses_auth_failed",
        "licenses_rejected_request",
        "upstream_returned_non_uuid",
    }
)

TRANSIENT_REASONS = frozenset(
    {
        "licenses_unreachable",
        "unreadable_response",
    }
)

# HTTP status -> reason. Anything absent from both maps is TRANSIENT by the
# default above, which is the safe direction for a status nobody anticipated.
#
# ⚠️ 5xx / 408 / 429 deliberately reuse ``licenses_unreachable`` rather than
# getting a reason of their own. Semantically it is the same fact — we could not
# obtain an answer — and every consumer that already treats
# ``licenses_unreachable`` as "our outage, not the caller's bad request"
# (``auth.middleware``) therefore gets the
# correct behaviour for an upstream 503 with no change of its own. A new reason
# string would have been quietly wrong at every one of those sites until each
# was updated, which is the same defect in a new coat.
_STRUCTURAL_STATUSES: frozenset[int] = frozenset({404, 410})
_CONFIGURATION_STATUSES: dict[int, str] = {
    400: "licenses_rejected_request",
    401: "licenses_auth_failed",
    403: "licenses_auth_failed",
    422: "licenses_rejected_request",
}


def classify_reason(reason: str) -> str:
    """Map an ``UnresolvableTenantError.reason`` to its failure class.

    Unknown reasons are TRANSIENT — see the block above for why the default
    direction is load-bearing rather than a matter of taste.
    """
    if reason in STRUCTURAL_REASONS:
        return STRUCTURAL
    if reason in CONFIGURATION_REASONS:
        return CONFIGURATION
    return TRANSIENT


def _reason_for_status(status_code: int) -> str:
    """The reason a non-200 from licences deserves. 404 is NOT the default."""
    if status_code in _STRUCTURAL_STATUSES:
        return "unknown_tenant"
    configured = _CONFIGURATION_STATUSES.get(status_code)
    if configured is not None:
        return configured
    return "licenses_unreachable"


class UnresolvableTenantError(Exception):
    """Raised when a caller-supplied tenant is neither a UUID nor a known name.

    Carries the offending ``value`` (and a machine-readable ``reason``) so the
    request boundary can answer 400 with something the caller can act on,
    instead of letting the value reach Postgres and become a type error.
    """

    def __init__(self, value: str, reason: str = "unknown_tenant") -> None:
        self.value = value
        self.reason = reason
        super().__init__(f"Unresolvable tenant {value!r} ({reason})")

    @property
    def classification(self) -> str:
        """``TRANSIENT`` / ``STRUCTURAL`` / ``CONFIGURATION`` for this reason.

        Consumers should branch on THIS, not on a hand-maintained tuple of
        reason strings. A tuple at the call site has to be updated every time a
        reason is added, and the update that is forgotten always fails in the
        dangerous direction — an unlisted reason reads as "structural", i.e. as
        proof the tenant does not exist.
        """
        return classify_reason(self.reason)

    @property
    def is_transient(self) -> bool:
        """True when we never got an answer; the tenant may well be real."""
        return self.classification == TRANSIENT

    @property
    def is_structural(self) -> bool:
        """True ONLY when the answer was a permanent no (or the input was junk).

        ⛔ This is the only property that may be read as "this tenant does not
        exist". Anything else must not be counted as an orphan or as a standing
        data fact.
        """
        return self.classification == STRUCTURAL


def get_licenses_client(timeout: float = _HTTP_TIMEOUT_S) -> httpx.AsyncClient:
    """Get or create the shared httpx.AsyncClient for licences reads."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


async def close_licenses_client() -> None:
    """Close the shared httpx client. Call during app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


def clear_tenant_resolution_cache() -> None:
    """Drop every cached mapping, positive and negative (tests; escape hatch)."""
    _cache.clear()
    _negative.clear()


def canonical_uuid(value: str) -> str | None:
    """Return ``value`` as a canonical lowercase UUID string, or None.

    Hot path: one ``uuid.UUID`` parse, no network, no logging, and the caller's
    own string object is handed back when it is already canonical so the common
    case allocates nothing beyond the parse itself.
    """
    try:
        parsed = uuid.UUID(value)
    except ValueError, AttributeError, TypeError:
        return None
    canonical = str(parsed)
    return value if value == canonical else canonical


def _licenses_base_url() -> str:
    """Base URL of the licences service — see the module docstring for order."""
    introspect = get_settings().license_introspect_url
    if introspect:
        parts = urlsplit(introspect)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return os.getenv("LICENSES_BASE_URL", DEFAULT_LICENSES_BASE_URL).rstrip("/")


def _cache_get(name: str) -> str | None:
    negative = _negative.get(name)
    if negative is not None:
        negative_until, negative_reason = negative
        if negative_until > time.monotonic():
            # Replay the reason that was MEASURED. Hardcoding
            # ``unknown_tenant`` here is how a cached entry re-reports itself as
            # a structural fact for the rest of its TTL without a single call to
            # substantiate it.
            raise UnresolvableTenantError(name, negative_reason)
        _negative.pop(name, None)
    entry = _cache.get(name)
    if entry is None:
        return None
    tenant_id, expires_at = entry
    if expires_at <= time.monotonic():
        _cache.pop(name, None)
        return None
    return tenant_id


def _cache_put(name: str, tenant_id: str) -> None:
    # FIFO eviction on insertion order — a plain dict is ordered, and the
    # access pattern here (a handful of long-lived tenants) does not justify an
    # LRU's bookkeeping.
    #
    # ⛔ This policy is safe HERE because these keys are licences-confirmed
    # tenants, so the working set is small and not caller-chosen. Do not copy it
    # into ``_negative_put``, whose keys ARE caller-chosen — read the ⛔ block in
    # that docstring before making the two "consistent".
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)), None)
    _cache[name] = (tenant_id, time.monotonic() + _POSITIVE_TTL_S)


def _negative_put(name: str, reason: str) -> None:
    """Remember a STRUCTURAL failure, under the same bound as ``_cache_put``.

    Locking discipline is the sibling's: NONE, deliberately. Neither dict is
    guarded by a lock; both are touched only from coroutines on the one event
    loop, and neither this function nor ``_cache_put`` awaits between reading
    ``len(...)`` and mutating, so the check-evict-insert sequence cannot
    interleave with another resolution. ⚠️ Do not introduce an ``await`` into
    either body without adding a lock to both.

    ⛔ THE EVICTION POLICY DELIBERATELY DIVERGES FROM ``_cache_put``'s FIFO, AND
    RESTORING "CONSISTENCY" WITH THE SIBLING REINSTATES A MEASURED DEFECT.
    ------------------------------------------------------------------------
    Never evict a LIVE entry. When the cache is full, the FIFO head is reclaimed
    only if it has already EXPIRED; if nothing has expired, this insert is
    DECLINED and the name simply is not cached. The two dicts hold different
    populations, which is why one policy does not fit both:

    * ``_cache`` is keyed on names licences CONFIRMED, so a caller cannot add a
      key to it by inventing one. (How large that population actually is has
      NOT been measured here; what matters for the policy is only that an
      attacker does not choose its keys.)
    * ``_negative`` is the hostile-input absorber — see the comment at the
      ``_negative_put`` call site. Its keys ARE caller-chosen, so its working
      set is whatever a caller decides it is, and its eviction branch is
      reachable at whatever rate a caller chooses.

    Copying FIFO into a cache whose working set the caller controls is the
    classic cyclic worst case: on a set of ``cap + 1`` names, FIFO evicts each
    entry exactly one request before it is needed again, for a 0% hit rate.
    MEASURED, at cap=4 over 50 inbound requests for a rotating set of 5 unknown
    names: **50 outbound calls under FIFO, versus 5 with no cap at all.** The
    cap turned a bounded-memory fix into a 1:1 amplification against licences,
    which has no rate limiter in front of it — 1:1 is the ceiling, and FIFO
    reached it at every rotating-set size measured (N = 5, 8, 16, 32, 50 all
    gave 50/50). Reclaim-expired-only measures 14 on the same input, and it
    cannot drop a live entry at all: the only ``del`` below is guarded on
    ``deadline <= now``.

    ⛔ Do NOT "fix" this with an LRU. The sibling already rejects one as
    unjustified bookkeeping, and on a rotating working set larger than the cap
    LRU fails for the same reason FIFO does — it evicts the least-recently-used
    entry, which on a rotation is precisely the next one needed. MEASURED on
    the identical arms, true LRU is not better than FIFO by a single call:
    **50, 50, 50, 50, 50 outbound at N = 5, 8, 16, 32, 50**, against this
    policy's 14, 28, 40, 46, 50. An LRU would buy the bookkeeping and keep the
    cliff.

    The head peek is O(1) and needs no scan (MEASURED: 291 ns per insert with
    the cache full at cap=512). That is sound ONLY because every
    write below appends at the tail (see the pop) and ``_NEGATIVE_TTL_S`` is
    uniform, so deadlines are assigned in non-decreasing order and the head
    carries the earliest one. Both halves are load-bearing: break either and
    the liveness test is reading a value that is not the minimum.

    ⚠️ WHAT THE CAP COSTS — and it is NOT a uniform gradient. While the cache is
    full of LIVE entries a further unknown name is not cached at all, so every
    request for it reaches licences. For a name that repeats, that is strictly
    WORSE than having no cache. MEASURED, one repeating unknown name against a
    cache pre-filled to capacity with simultaneously-live entries, 20 victim
    requests (cap=512): **20/20 outbound here, against 1/20 both uncapped and
    under plain FIFO.** Let the clock advance 0.5s per request and it is 11/20
    against 2/20 — better, still worse than either. So:

    * for the ROTATING set this policy exists to fix, it is a large win
      (cap=4, 50 requests, 5 names: 14 here, 50 under FIFO, 5 uncapped);
    * for a SINGLE victim behind a full cache, it is a cliff, and the victim
      pays up to ``_NEGATIVE_TTL_S`` of 1:1 pass-through. MEASURED worst-case
      wait before a declined name got a slot: 10.0s at a 10s TTL — the FULL
      TTL, not a fraction of it.

    That trade is deliberate, and it is a trade, not a strict improvement: the
    victim's cost is bounded by the TTL and is paid in outbound calls licences
    can count, whereas the FIFO cliff it replaces hit EVERY name in the rotating
    set at once (measured 50/50 at N = 5, 8, 16, 32 and 50 alike). One name
    paying 20/20 for up to 10s is the price of the whole set no longer paying
    50/50 indefinitely. It is not free, and nobody should read it as free.
    """
    now = time.monotonic()
    # ⛔ POP BEFORE INSERT, AND THE POLICY ABOVE IS WRONG WITHOUT IT. Assigning
    # to a key a dict already holds keeps its ORIGINAL position and replaces only
    # the value, so a re-put would refresh the deadline where it sits. The head
    # would then stop being the earliest deadline, and the liveness test below —
    # which reads the head as the dict minimum — would decline inserts while
    # expired entries squatted behind a live-looking head. MEASURED before this
    # line existed: head deadline 1013.0 against a true minimum of 1011.0, then
    # 3 of 3 further names declined with 2 dead entries occupying the cache.
    #
    # A re-put is reachable from the public API, not hypothetical.
    # ``_resolve_name`` shields the fetch task but pops ``_inflight`` in its
    # ``finally``, so cancelling the awaiter (client disconnect, ``wait_for``)
    # orphans a still-running fetch and the next request for the same name
    # starts a second one; both land here. MEASURED: 2 outbound fetches and 2
    # ``_negative_put`` calls for one name. The two can be up to
    # ``_HTTP_TIMEOUT_S`` apart, which is 10s and EQUAL to ``_NEGATIVE_TTL_S``,
    # so the skew can span a whole TTL.
    _negative.pop(name, None)
    if len(_negative) >= _NEGATIVE_MAX_ENTRIES:
        oldest = next(iter(_negative))
        if _negative[oldest][0] > now:
            return
        del _negative[oldest]
    _negative[name] = (now + _NEGATIVE_TTL_S, reason)


async def _resolve_name(name: str) -> str:
    """Resolve a tenant NAME to its UUID via the tokenless licences endpoint.

    Concurrent resolutions of the SAME name share one outbound request. Without
    that, a cold cache under load turns N simultaneous requests into N calls to
    licences — measured at 50/50 before this was added.
    """
    cached = _cache_get(name)
    if cached is not None:
        return cached

    if len(name) > _MAX_NAME_LEN:
        raise UnresolvableTenantError(name, "name_too_long")

    # Syntax gate BEFORE the network, for two reasons.
    #
    # 1. It is the difference between a bad request and an outage. Without it,
    #    garbage like ``"Bearer "`` is indistinguishable from a real name we
    #    merely could not look up, so a licences blip reports every malformed
    #    header as OUR failure (503) instead of the caller's (400).
    # 2. ``quote(name, safe="")`` does not escape ``.``, and httpx normalises
    #    dot segments: measured, ``X-Tenant-Id: ..`` made this service issue
    #    ``GET {licences}/byo-mode`` — one segment up, off the intended route.
    #    ``/`` IS escaped so it could go no deeper, but a caller-supplied string
    #    should not be steering our URL at all.
    #
    # The pattern is licences' own tenant-name rule (``^[a-z0-9][a-z0-9-]*$``),
    # so anything it would reject is rejected here without asking.
    if not _TENANT_NAME_RE.match(name):
        logger.warning("tenant.resolve_malformed", tenant=name)
        raise UnresolvableTenantError(name, "malformed_tenant")

    inflight = _inflight.get(name)
    if inflight is not None:
        return await asyncio.shield(inflight)

    task = asyncio.ensure_future(_fetch_name(name))
    _inflight[name] = task
    try:
        return await asyncio.shield(task)
    finally:
        _inflight.pop(name, None)


async def _fetch_name(name: str) -> str:
    """The single outbound lookup, behind ``_resolve_name``'s single-flight."""
    url = f"{_licenses_base_url()}/tenants/{quote(name, safe='')}/byo-mode"
    client = get_licenses_client()
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        # Not the caller's fault, and the answer says so. The name already passed
        # the syntax gate above, so it may well be a real tenant we simply could
        # not ask about. This reason classifies TRANSIENT, which the middleware
        # renders as 503 + ``Retry-After``; CONFIGURATION becomes 502 (no
        # Retry-After — retrying a 401 helps nobody) and only STRUCTURAL stays a
        # 400. ⚠️ This comment used to say "everything else stays a 400"; that was
        # true before the three-class split and is exactly the kind of stale
        # comment that outlives the code it describes. Reporting our own outage as
        # the caller's bad request is how an error ends up naming a cause it never
        # checked.
        logger.warning("tenant.resolve_unreachable", tenant=name, error=str(exc))
        raise UnresolvableTenantError(name, "licenses_unreachable") from exc

    if resp.status_code != 200:
        # ⛔ THE STATUS CODE DECIDES THE REASON. This branch used to raise
        # ``unknown_tenant`` for every non-200 — a 503 from a licences rollout
        # was reported, cached and counted as "that tenant does not exist".
        reason = _reason_for_status(resp.status_code)
        classification = classify_reason(reason)
        logger.warning(
            "tenant.resolve_rejected",
            tenant=name,
            status=resp.status_code,
            reason=reason,
            classification=classification,
        )
        if classification == STRUCTURAL:
            # Short negative TTL. Not zero: a syntactically valid unknown name
            # costs one licences round-trip per request otherwise, and any pod
            # on the trusted network can send an unbounded stream of them. Not
            # long either: a tenant registered a second ago must resolve
            # promptly.
            #
            # ⛔ STRUCTURAL ONLY. Caching a transient failure turns ONE outage
            # into `_NEGATIVE_TTL_S` worth of confident, callless "unknown
            # tenant" for that name — the cache re-reports a fact it never
            # measured, and a consumer counting structural skips sees a fleet of
            # orphans that do not exist. A transient failure costs an extra
            # round-trip instead; that is the correct trade, because the thing
            # being rate-limited here is a HOSTILE stream of unknown names, and
            # a licences outage is not that.
            _negative_put(name, reason)
        raise UnresolvableTenantError(name, reason)

    try:
        payload = resp.json()
    except ValueError as exc:
        logger.warning("tenant.resolve_unreadable_body", tenant=name)
        raise UnresolvableTenantError(name, "unreadable_response") from exc

    raw = str(payload.get("tenant_id") or "") if isinstance(payload, dict) else ""
    resolved = canonical_uuid(raw)
    if resolved is None:
        # Upstream answered 200 with something that is not a UUID. Trusting it
        # would push the exact type error we exist to prevent into the GUC.
        logger.warning("tenant.resolve_non_uuid", tenant=name, value=raw)
        raise UnresolvableTenantError(name, "upstream_returned_non_uuid")

    _cache_put(name, resolved)
    logger.info("tenant.resolved", tenant=name, tenant_id=resolved)
    return resolved


async def resolve_tenant_to_uuid(value: str | None, *, verify: bool = False) -> str:
    """Return the canonical UUID string for a caller-supplied tenant.

    ``value`` may be a UUID in any accepted spelling (upper case, braced,
    urn-prefixed, unhyphenated) or a tenant NAME. The return value is ALWAYS a
    canonical lowercase UUID string — every other outcome raises
    ``UnresolvableTenantError``.

    ``verify`` defaults to False, preserving this function's documented hot-path
    contract: an already-UUID-shaped value is accepted with NO network call. That
    is correct for ``_principal_from_license`` (``auth/middleware.py``) — the uuid
    there came out of a successful license introspection and is already
    registry-authoritative, so re-checking it would be a redundant round-trip on
    the highest-volume path.

    It is NOT correct for a caller-supplied tenant (today: the delegated
    ``X-Effective-Tenant-Id``; historically the since-removed tokenless
    trusted-network path, which trusted a bare ``X-Tenant-Id`` header alone), so
    a syntactically valid but never-registered uuid sails through unchallenged, gets written into
    owned tables, and then drives real (and permanently unbillable) LLM spend —
    measured live 2026-09-07: ``ad976f5b-f3a7-4441-ae87-c2090d038f9c``,
    ``6c6a0da6-d2a3-533a-a28b-2e15319b8832`` and
    ``351fdc59-6747-4e48-b130-73d8cdb52bf9``, none of them in ``licenses.tenants``,
    all reachable only via that path. Callers on that boundary must pass
    ``verify=True``, which routes an already-UUID-shaped value through the SAME
    licences round-trip (cache, single-flight, negative-TTL included) a tenant
    NAME already gets — ``GET {base}/tenants/{name_or_id}/byo-mode`` accepts
    either spelling, and a canonical uuid's characters (hex digits + dashes)
    satisfy ``_TENANT_NAME_RE`` too, so no new code path is needed, only a new
    caller of the existing one.
    """
    if not value:
        raise UnresolvableTenantError("", "empty_tenant")
    value = value.strip()
    if not value:
        raise UnresolvableTenantError("", "empty_tenant")

    resolved = canonical_uuid(value)
    if resolved is not None:
        if not verify:
            return resolved
        return await _resolve_name(resolved)

    return await _resolve_name(value)


# ---------------------------------------------------------------------------
# Refusal accounting — the burn-down signal for the boundary this module closes
# ---------------------------------------------------------------------------
# ⛔ WHY A COUNTER AND NOT JUST THE LOG LINE. The refusal already produces
# ``auth.tenant_unresolvable`` at WARNING, and that is the right thing for
# diagnosing ONE request. It is the wrong thing for the question this boundary
# actually has to answer: *is anybody still being refused, and is it getting
# better or worse?* Answering that from logs means a log query per window, over a
# stream nobody retains for long, and it cannot be read from the pod itself.
#
# The population is not hypothetical. Measured in ``billing.billing_cost_events``
# on 2026-09-15, four never-registered tenant ids had spent on this service's
# behalf: ``6c6a0da6-…`` (a snowflake integer run through
# ``uuid5(NAMESPACE_URL, …)``), ``ad976f5b-…`` and ``351fdc59-…`` (caller-minted
# uuid4s), and ``0195f48b-d000-7000-8000-0000000001f2`` (a platform-series uuid7
# on an UNALLOCATED suffix). The first three stopped on 2026-09-09, the day
# ``verify=True`` reached the (since-removed) trusted-network path. That is the
# shape of a burn-down, and it was reconstructed after the fact from the BILLING
# ledger, because the service itself kept no count. This is that count.
#
# Keyed by failure CLASS, not by reason, for the same reason
# the (since-removed) conflict reconciler split its skip counters: the classes map 1:1 to
# what a human must go and do. STRUCTURAL is the burn-down proper (a caller is
# naming a tenant that does not exist — drive it to 0). CONFIGURATION and
# TRANSIENT are OUR faults and must never be read as evidence about a tenant.
#
# ⚠️ Deliberately NOT keyed by tenant value. The keys would then be
# caller-chosen, and an unbounded dict fed by a hostile header is the exact leak
# ``_NEGATIVE_MAX_ENTRIES`` exists to bound a few hundred lines above. Three
# fixed keys cannot grow. The offending VALUE is already in the log line, which
# is where a per-tenant question belongs.
#
# No lock, matching ``_cache_put``/``_negative_put``: touched only from
# coroutines on the one event loop, with no ``await`` between read and write.
_refusals: dict[str, int] = {STRUCTURAL: 0, CONFIGURATION: 0, TRANSIENT: 0}


def record_tenant_refusal(reason: str) -> dict[str, int]:
    """Count one refused tenant, and return the running totals.

    Returns the snapshot so the caller can put the totals ON the same log line
    that reports the refusal — a WARNING that says "this is the 41st structural
    refusal" is actionable where a bare warning is noise.

    ``reason`` is classified here rather than at the call site, so a reason added
    to the taxonomy upstream is counted correctly with no change to this
    function's callers.
    """
    classification = classify_reason(reason)
    _refusals[classification] = _refusals.get(classification, 0) + 1
    return tenant_refusal_counts()


def tenant_refusal_counts() -> dict[str, int]:
    """Snapshot of refusals since process start, by failure class, plus total.

    A process-lifetime monotonic count, read by ``/health``. It is deliberately
    NOT reset on read: a counter that a reader can zero is a counter two readers
    disagree about.
    """
    snapshot = dict(_refusals)
    snapshot["total"] = sum(_refusals.values())
    return snapshot


def reset_tenant_refusal_counts() -> None:
    """Zero the refusal counters. Tests only — never called from ``src/``."""
    for key in _refusals:
        _refusals[key] = 0
