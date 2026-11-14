"""Outbound read client for coding-analysis (code-graph as-built facts).

Uses one shared ``httpx.AsyncClient`` per process, closed on app shutdown.
Pulls a container's architectural components and adapts them to the
conformance ``code_units`` shape consumed by
``tools/architecture/conformance.py``.

This is the only outbound dependency the conformance feature adds. The EA model
of record (this service) *pulls* the realized facts; coding-analysis stays
unaware of the conformance use.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from clients._coding_auth import coding_headers

logger = logging.getLogger(__name__)


class CodingAnalysisUnreadableBodyError(RuntimeError):
    """coding-analysis answered 200 with a body this client cannot interpret.

    Deliberately NOT an empty result. The conformance report treats "zero code
    units" as "every designed element is unrealized", so degrading an unreadable
    reply to ``[]`` produces a confident, complete-looking report asserting the
    whole architecture is a gap. See the raise site for the full reasoning.

    A ``RuntimeError`` to match this module's existing convention (``_base_url``
    raises ``RuntimeError`` for missing configuration), and so it cannot be
    mistaken for the ``ValueError`` that ``fetch_code_units`` raises for a
    missing ``tenant_id`` — that one is a caller bug, this one is an upstream
    fault.
    """


class CodingAnalysisContainerNotFoundError(RuntimeError):
    """coding-analysis answered 404 for a bound container.

    Deliberately NOT ``[]``. A 404 means coding-analysis holds no graph for the
    container (not yet analysed, renamed, or deleted) — it says nothing about
    whether the bound components exist. Returning ``[]`` made conformance report
    every element bound into it as unrealised. The caller marks that container's
    elements ``insufficient_evidence`` instead and keeps the other containers.
    """

    def __init__(self, container: str) -> None:
        super().__init__(f"coding-analysis has no code graph for container {container!r} (404)")
        self.container = container


# Shared httpx client — one per process, avoids per-request pool overhead.
_client: httpx.AsyncClient | None = None


def get_coding_analysis_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Get or create the shared httpx.AsyncClient for coding-analysis reads."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


async def close_coding_analysis_client() -> None:
    """Close the shared httpx client. Call during app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


def _base_url() -> str:
    from settings import get_settings

    url = get_settings().coding_analysis_url
    if not url:
        raise RuntimeError(
            "CODING_ANALYSIS_URL is not configured. Set it via the Deployment env "
            "block (see gitops-production) to enable architecture conformance."
        )
    return url.rstrip("/")


def _adapt_components(components: list[dict], container: str) -> list[dict[str, Any]]:
    adapted: list[dict[str, Any]] = []
    for c in components:
        # ⚠️ `dependencies` is NOT coalesced. This used to be
        # `[str(d) for d in (c.get("dependencies", []) or [])]`, which turned a
        # missing key, an explicit `null`, and a non-list into the same thing an
        # empty list means — and downstream that empty is read as a positive
        # claim about the code, twice, in the same user-visible verdict:
        #
        #   tools/architecture/conformance.dependency_conformance:220 does
        #   `missing = designed - realized`, so an absent list makes `realized`
        #   empty and reports EVERY designed ArchiMate edge as unrealized; and
        #   illegal_dependencies:276 reports `illegal_count: 0`, i.e. "no
        #   layering violations", off the same absence. Both surface through
        #   POST /architecture/tools/conformance as a complete-looking report.
        #   A gap report that is 100% gaps and 0 violations is the loudest
        #   possible wrong answer delivered in the quietest possible way.
        #
        # Absence is off-contract, not a shape a healthy upstream emits:
        # coding-analysis serves this route with `response_model=list[Component]`
        # and its `Component.dependencies` is `list[str]` with
        # `default_factory=list` (models/architecture_models.py). FastAPI sets no
        # `response_model_exclude_*` there, so every 200 from that model carries
        # the key as a list. A missing/null/non-list value therefore means the
        # body did not come from `list[Component]` — the same unreadable-200
        # family the raise at the bottom of `fetch_code_units` already covers,
        # just one level deeper. Raising here keeps it one failure shape, and the
        # caller loop in routers/architecture_tools.py:422 aborts rather than
        # handing a half-read container to `analyze_conformance`.
        #
        # An explicit `[]` stays a legitimate "this component depends on nothing".
        deps = c.get("dependencies")
        if not isinstance(deps, list):
            raise CodingAnalysisUnreadableBodyError(
                f"coding-analysis returned a component without a readable "
                f"`dependencies` list for container {container!r} "
                f"(component id={c.get('id')!r}, type={type(deps).__name__}); "
                f"refusing to report it as zero realized dependencies"
            )

        # `files` gets the shape guard but NO raise, deliberately. Blast radius is
        # zero: nothing in `src/` reads `code_units[].files` — not conformance,
        # not the routers — it is carried only so the adapted shape stays a
        # faithful view of the upstream component. There is no verdict to
        # corrupt, so a raise here would be pure cost (an outage lever with no
        # bug behind it). Do not "improve" this into a raise without first
        # finding a reader.
        files = c.get("files")
        adapted.append(
            {
                "id": str(c.get("id", "")),
                "name": c.get("name", ""),
                "layer": c.get("layer") or "",
                "files": list(files) if isinstance(files, list) else [],
                "dependencies": [str(d) for d in deps],
                "container": container,
            }
        )
    return adapted


async def fetch_code_units(container: str, *, tenant_id: str) -> list[dict[str, Any]]:
    """Fetch a container's code-graph components as conformance ``code_units``.

    Raises :class:`CodingAnalysisContainerNotFoundError` when the container is
    unknown (404): that is missing evidence, not "no code units". The caller
    reports the container's elements as ``insufficient_evidence`` and carries on
    with the other containers.

    ``tenant_id`` is REQUIRED and is the **customer's** tenant. No
    ``DEFAULT_TENANT_ID`` fallback, deliberately — the response is *tenant-scoped
    data returned to the caller*. Guessing a tenant would answer one tenant's
    conformance report out of another tenant's code graph, which is a
    data-isolation bug, not just an auth one. Same reasoning as
    coding-analysis#75's snapshot writer.

    The call also presents this service's own licence via ``Authorization`` —
    see :mod:`clients._coding_auth`. Its predecessor was coding-analysis's
    trusted-network fallback (any RFC1918 source IP, i.e. the whole pod CIDR),
    which has now been emptied: a header-only call 401s, measured live
    2026-08-15. That failure is near-invisible from this side — a 401 here
    surfaces as an empty/failed conformance report, not as an auth error.
    """
    if not tenant_id:
        raise ValueError(
            "fetch_code_units requires a tenant_id: the coding-analysis container "
            "scope is tenant-scoped, so an anonymous call would either 401 once the "
            "trusted-network fallback is emptied or read another tenant's code graph. "
            "Pass the caller's tenant (routers use Depends(get_tenant_id))."
        )

    client = get_coding_analysis_client()
    url = f"{_base_url()}/api/analysis/architecture/components/{container}"
    headers = coding_headers(tenant_id)

    resp = await client.get(url, headers=headers)
    if resp.status_code == 404:
        logger.warning("coding-analysis: container %r not found", container)
        raise CodingAnalysisContainerNotFoundError(container)
    resp.raise_for_status()

    data = resp.json()
    if isinstance(data, list):
        components = data
    elif isinstance(data, dict) and isinstance(data.get("components"), list):
        components = data["components"]
    else:
        # ⚠️ NOT `[]`. This used to end:
        #
        #     components = data if isinstance(data, list) else (data.get("components", []) or [])
        #
        # so any 200 whose body was neither a list nor `{"components": [...]}`
        # became a positive claim that the container has NO code units — and
        # `or []` additionally swallowed a falsy value such as
        # `{"components": null}`. Realistic triggers: an error envelope
        # (`{"detail": ...}`), a wrapper keyed differently (`{"items": [...]}`),
        # or a proxy's JSON error page served as 200.
        #
        # Why that is not a harmless empty: `analyze_conformance` reports "zero
        # code units" as EVERY designed element being unrealized, so an
        # unreadable reply produced a full-looking conformance report claiming
        # the entire architecture is a gap.
        #
        # And the caller loops — `routers/architecture_tools.py` does
        # `code_units.extend(await fetch_code_units(c, ...))` over every bound
        # container — so one unreadable container silently under-reported while
        # its siblings succeeded. That is worse than an all-or-nothing failure:
        # the report still looked complete.
        #
        # Raising rather than returning `None` is deliberate. It keeps the
        # declared `-> list[...]` return type honest (a `None` would break the
        # caller's `.extend`), and it matches how EVERY other upstream failure
        # on this path already behaves — `raise_for_status()` above propagates a
        # 5xx, and a non-JSON body propagates from `resp.json()`. An unreadable
        # 200 was the ONLY upstream failure mode that silently degraded.
        #
        # The 404 branch above raises its own `CodingAnalysisContainerNotFoundError`:
        # "no graph for this container" is missing evidence for that container
        # only, so the caller degrades it to `insufficient_evidence` per
        # container rather than failing the whole report as it does here.
        raise CodingAnalysisUnreadableBodyError(
            f"coding-analysis returned an unreadable 200 body for container "
            f"{container!r} (type={type(data).__name__}); refusing to report it "
            f"as zero code units"
        )
    return _adapt_components(components, container)
