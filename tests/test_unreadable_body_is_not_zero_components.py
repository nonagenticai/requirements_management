"""An unreadable 200 body must not read as "this container has no code units".

THE DEFECT
==========
``clients/coding_analysis.fetch_code_units`` ended with::

    data = resp.json()
    components = data if isinstance(data, list) else (data.get("components", []) or [])

So any 200 whose body was neither a list nor ``{"components": [...]}`` produced
``[]`` — a positive claim that the container contains no code units. The ``or []``
additionally swallowed a falsy value, so ``{"components": null}`` degraded the
same way.

WHY `[]` IS NOT A HARMLESS EMPTY HERE
-------------------------------------
``tools/architecture/conformance.analyze_conformance`` measures the as-designed
ArchiMate model against the as-built code units. With zero code units, EVERY
designed element is unrealized — so an unreadable upstream reply did not produce
an empty report, it produced a confident, complete-looking report asserting that
the entire architecture is a gap.

**And the caller loops.** ``routers/architecture_tools.py`` does::

    for container in _bound_containers(elements):
        code_units.extend(await fetch_code_units(container, tenant_id=tenant_id))

so one unreadable container silently under-reported while its siblings returned
real data. That is worse than an all-or-nothing failure: the resulting report
still looked complete, and nothing in the response says which containers were
actually read. This is the sibling of the (since-removed) ``procedure_management`` client defect fixed
in #118 — same shape, different consequence (a wrong report rather than
destroyed conflict state), which is why it was left for a separate contract
decision rather than pasted over.

THE CONTRACT CHOSEN, AND WHY NOT `None`
---------------------------------------
#118's twin returns ``None`` for "couldn't fetch" because its caller
(``conflict_reconciler``) branches on exactly that. This caller does not — it
``.extend()``s the result — so ``None`` would break it and the function's
declared ``-> list[...]`` would have to become ``| None`` and be threaded
through. Instead this raises, which:

* keeps the declared return type honest;
* matches every OTHER upstream failure on this path, which already propagates
  (``raise_for_status()`` for a 5xx, ``resp.json()`` for a non-JSON body) — an
  unreadable 200 was the *only* one that silently degraded;
* treats a 404 separately (``CodingAnalysisContainerNotFoundError``): "container
  not found" is missing evidence for that one container, which the router
  reports as ``insufficient_evidence`` rather than failing the whole request.

WHAT THESE TESTS PIN
--------------------
The malformed cases raise AND the legitimate ones still return data. Both halves
matter: a regression that raised on everything would satisfy the first half
alone, and a regression that returned ``[]`` for everything would satisfy the
second. Note in particular that a genuinely empty container (``[]`` and
``{"components": []}``) MUST still be an empty answer — the fix must not turn a
real "nothing here" into an error.
"""

from __future__ import annotations

import httpx
import pytest

from clients import coding_analysis
from clients.coding_analysis import (
    CodingAnalysisContainerNotFoundError,
    CodingAnalysisUnreadableBodyError,
    _adapt_components,
    fetch_code_units,
)
from tests.tenants import T

_TENANT = T("tenant-a")
_CONTAINER = "some-container"


@pytest.fixture
def _configured(monkeypatch):
    """Point the client at a dummy base URL so no real network is involved."""
    import settings as settings_module

    real = settings_module.get_settings()

    class _Stub:
        coding_analysis_url = "http://coding-analysis.test"

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(settings_module, "get_settings", lambda: _Stub())


def _client_returning(status: int = 200, **resp_kwargs) -> httpx.AsyncClient:
    """A real AsyncClient whose transport serves one canned response.

    Takes raw ``httpx.Response`` kwargs rather than a decoded object because the
    two are not interchangeable: ``httpx.Response(200, json=None)`` sends an
    EMPTY body, not the JSON literal ``null``. A body of ``null`` has to be sent
    as ``content="null"``, and only that exercises this client's shape check —
    an empty body fails earlier, inside ``resp.json()``.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, **resp_kwargs)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


def _install(monkeypatch, status: int = 200, **resp_kwargs) -> None:
    monkeypatch.setattr(
        coding_analysis,
        "get_coding_analysis_client",
        lambda *a, **k: _client_returning(status, **resp_kwargs),
    )


_JSON = {"content-type": "application/json"}


# ── the malformed bodies that used to become "zero code units" ───────────────

_UNREADABLE = [
    pytest.param({"json": {"detail": "container not indexed"}}, id="error-envelope"),
    pytest.param({"json": {"items": [{"id": "1", "name": "a"}]}}, id="wrapper-keyed-items"),
    pytest.param({"json": {"results": [{"id": "1", "name": "a"}]}}, id="wrapper-keyed-results"),
    # `or []` swallowed this one even though the key was present.
    pytest.param({"json": {"components": None}}, id="components-null"),
    pytest.param({"json": {"components": "oops"}}, id="components-not-a-list"),
    # A literal JSON `null` — must be sent as raw content; see _client_returning.
    pytest.param({"content": "null", "headers": _JSON}, id="body-null"),
    pytest.param({"json": 42}, id="body-scalar"),
    pytest.param({"json": "a string"}, id="body-string"),
]


@pytest.mark.parametrize("resp", _UNREADABLE)
async def test_unreadable_200_raises_instead_of_claiming_zero(resp, monkeypatch, _configured):
    """Every shape that is not a recognisable component list must raise."""
    _install(monkeypatch, **resp)

    with pytest.raises(CodingAnalysisUnreadableBodyError) as exc:
        await fetch_code_units(_CONTAINER, tenant_id=_TENANT)

    # The message must name the container, or an operator cannot tell WHICH of
    # the looped containers failed.
    assert _CONTAINER in str(exc.value)


# ── the bodies that must keep working, including a genuine empty ─────────────


async def test_bare_list_still_works(monkeypatch, _configured):
    _install(
        monkeypatch,
        json=[{"id": "1", "name": "svc", "layer": "app", "files": [], "dependencies": []}],
    )

    units = await fetch_code_units(_CONTAINER, tenant_id=_TENANT)

    assert [u["name"] for u in units] == ["svc"]
    assert units[0]["container"] == _CONTAINER


async def test_components_wrapper_still_works(monkeypatch, _configured):
    _install(monkeypatch, json={"components": [{"id": "1", "name": "svc", "dependencies": []}]})

    units = await fetch_code_units(_CONTAINER, tenant_id=_TENANT)

    assert [u["name"] for u in units] == ["svc"]


@pytest.mark.parametrize(
    "body",
    [pytest.param([], id="bare-empty-list"), pytest.param({"components": []}, id="empty-wrapper")],
)
async def test_a_genuinely_empty_container_is_still_an_empty_answer(body, monkeypatch, _configured):
    """THE DISCRIMINATOR.

    A container that really has no components must stay an empty list. If this
    raised, the fix would have replaced a silent-wrong-answer bug with an
    outage on every empty container — the failure mode recorded in
    ``a-validation-gate-can-cost-more-than-it-saves``.
    """
    _install(monkeypatch, json=body)

    assert await fetch_code_units(_CONTAINER, tenant_id=_TENANT) == []


async def test_404_is_missing_evidence_not_zero_components(monkeypatch, _configured):
    """'Container not found' says nothing about the bound components. Returning
    ``[]`` made conformance report every element bound into it as unrealised;
    it raises a distinct error the router degrades to ``insufficient_evidence``."""
    _install(monkeypatch, status=404, json={"detail": "not found"})

    with pytest.raises(CodingAnalysisContainerNotFoundError) as exc:
        await fetch_code_units(_CONTAINER, tenant_id=_TENANT)
    assert exc.value.container == _CONTAINER


# ── the blast radius: the caller loops over containers ───────────────────────


async def test_one_unreadable_container_does_not_silently_shrink_the_report(
    monkeypatch, _configured
):
    """A partial read must not pass as a complete one.

    Two bound containers, the second unreadable. Before the fix the loop
    produced 1 container's units and reported them as the whole as-built
    picture, so every element realized only by the second container was
    reported as an unrealized gap.
    """
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        container = request.url.path.rsplit("/", 1)[-1]
        seen.append(container)
        if container == "good":
            return httpx.Response(200, json=[{"id": "1", "name": "svc", "dependencies": []}])
        return httpx.Response(200, json={"detail": "upstream hiccup"})

    monkeypatch.setattr(
        coding_analysis,
        "get_coding_analysis_client",
        lambda *a, **k: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )

    code_units: list[dict] = []
    with pytest.raises(CodingAnalysisUnreadableBodyError):
        for container in ("good", "bad"):
            code_units.extend(await fetch_code_units(container, tenant_id=_TENANT))

    assert seen == ["good", "bad"], "both containers should have been attempted"
    # The point: the caller never gets to hand a half-populated list to
    # analyze_conformance and call it the as-built architecture.
    assert len(code_units) == 1, "the good container's units were collected, then the loop aborted"


# ── the same defect one level deeper: a component with no `dependencies` ─────
#
# THE GAP THIS CLOSES
# ===================
# `_adapt_components` used to do `[str(d) for d in (c.get("dependencies", []) or [])]`,
# collapsing a missing key, an explicit `null` and a non-list into the same thing
# an empty list means. Downstream that empty is read as a POSITIVE claim about
# the code, twice, in the same user-visible verdict:
#
#   * `dependency_conformance` does `missing = designed - realized`, so an absent
#     list makes `realized` empty and reports EVERY designed ArchiMate edge as
#     unrealized; and
#   * `illegal_dependencies` reports `illegal_count: 0`, i.e. "no layering
#     violations", off the same absence.
#
# A gap report that is 100% gaps and 0 violations is the loudest possible wrong
# answer delivered in the quietest possible way.
#
# NON-VACUITY NOTE: after three fixtures in this file gained `"dependencies": []`
# so they would keep working, NO test in the repo reached the raise at all —
# deleting it left the whole suite green. Every case below omits or corrupts the
# key on purpose, and the control at the bottom keeps `[]` meaning what it says.

_UNREADABLE_DEPENDENCIES = [
    pytest.param({"id": "1", "name": "svc"}, id="dependencies-key-absent"),
    pytest.param({"id": "1", "name": "svc", "dependencies": None}, id="dependencies-null"),
    pytest.param({"id": "1", "name": "svc", "dependencies": "a,b"}, id="dependencies-string"),
    pytest.param({"id": "1", "name": "svc", "dependencies": {"a": 1}}, id="dependencies-object"),
    pytest.param({"id": "1", "name": "svc", "dependencies": 0}, id="dependencies-zero"),
]


@pytest.mark.parametrize("component", _UNREADABLE_DEPENDENCIES)
def test_a_component_without_a_readable_dependencies_list_raises(component):
    """Absence is off-contract, not a shape a healthy upstream emits.

    coding-analysis serves this route with ``response_model=list[Component]`` and
    its ``Component.dependencies`` is ``list[str]`` with ``default_factory=list``,
    and FastAPI sets no ``response_model_exclude_*`` there — so every 200 from
    that model carries the key as a list. A missing/null/non-list value means the
    body did not come from ``list[Component]``: the same unreadable-200 family
    the raise at the bottom of ``fetch_code_units`` already covers.
    """
    with pytest.raises(CodingAnalysisUnreadableBodyError) as exc:
        _adapt_components([component], _CONTAINER)

    assert _CONTAINER in str(exc.value), (
        "the message must name the container, or an operator looping over a dozen "
        "of them cannot tell which one served the bad body"
    )


def test_an_explicit_empty_dependencies_list_stays_an_empty_list():
    """THE DISCRIMINATOR for the raise above.

    An explicit ``[]`` is upstream stating a judgeable fact — "this component
    depends on nothing". Raising on it too would swap a silent-wrong-answer bug
    for an outage on every leaf component, which is strictly the worse trade.
    """
    units = _adapt_components([{"id": "1", "name": "svc", "dependencies": []}], _CONTAINER)

    assert len(units) == 1, "a well-formed component must still be adapted"
    assert units[0]["dependencies"] == [], (
        f"an explicit empty list must survive as an empty list (got "
        f"{units[0]['dependencies']!r}) — it is a legitimate answer, not an error"
    )


def test_a_missing_files_key_is_still_tolerated():
    """The deliberate ASYMMETRY: `files` gets the shape guard but NO raise.

    Nothing in ``src/`` reads ``code_units[].files`` — not conformance, not the
    routers — so there is no verdict to corrupt and a raise here would be an
    outage lever with no bug behind it. Pinned so a later "consistency" cleanup
    that raises on `files` too has to argue with a test rather than with a
    comment.
    """
    units = _adapt_components([{"id": "1", "name": "svc", "dependencies": []}], _CONTAINER)

    assert units[0]["files"] == [], "a missing `files` key must degrade to [], not raise"


async def test_the_dependencies_raise_is_reachable_through_fetch_code_units(
    monkeypatch, _configured
):
    """The raise must fire on the REAL path, not only when called directly.

    A guard that only a unit test can reach is a guard the product never runs.
    This sends a 200 whose body is a perfectly well-formed component list except
    that the component omits `dependencies` — exactly the shape that used to be
    adapted into "this component realizes zero dependencies".
    """
    _install(monkeypatch, json=[{"id": "1", "name": "svc", "layer": "app", "files": []}])

    with pytest.raises(CodingAnalysisUnreadableBodyError) as exc:
        await fetch_code_units(_CONTAINER, tenant_id=_TENANT)

    assert _CONTAINER in str(exc.value)
