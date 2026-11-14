"""Every built route's session dependency must be FUNCTION-scoped, at runtime.

`tests/test_session_scope_is_function.py` proves the SOURCE TEXT carries
``scope="function"``. This file proves something strictly stronger and quite
different: that FastAPI actually *parsed* it into ``Dependant.scope`` on every
route it built. Text can be right while the framework ignores it — a keyword
silently dropped by a signature change, a route registered through a path that
rebuilds its dependant, an alias. Only the built object settles that.

⚠️ THE VACUITY TRAP THIS FILE EXISTS TO AVOID.
In FastAPI 0.137 ``include_router`` does NOT eagerly splice routes into
``app.routes``. It appends a lazy ``_IncludedRouter`` wrapper
(``fastapi/routing.py:1458``). Measured on this app: ``app.routes`` holds 33
entries — 28 ``_IncludedRouter``, 4 ``Route``, and exactly **1** ``APIRoute``.
So the obvious implementation::

    for r in app.routes:
        if isinstance(r, APIRoute): ...   # sees 1 route, asserts nothing

passes trivially while inspecting 0.6% of the surface. That is not a
hypothetical: this repo has already shipped one guard whose objects under test
were invisible to its own assertion. ``_reachable_api_routes`` below recurses
through the wrappers, and ``test_the_naive_walk_would_have_been_vacuous`` pins
the trap so nobody "simplifies" the recursion away.

Why FUNCTION scope matters at all: see ``091f684:docs/incidents/2026-09-03-pool-exhaustion.md``
§10.6 and ``tests/test_session_released_before_response.py``.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.dependencies.models import (
    _get_computed_scope,
    _is_async_gen_callable,
    _is_gen_callable,
)
from fastapi.routing import APIRoute, _IncludedRouter


# FastAPI >= 0.15x made ``Dependant`` a plain dataclass: the old
# ``computed_scope`` / ``is_gen_callable`` / ``is_async_gen_callable`` cached
# properties became the module functions imported above, which are exactly what
# ``solve_dependencies`` branches on. Imported (not ``getattr(..., default)``) on
# purpose: a ``getattr`` fallback is how this file went silently vacuous on that
# upgrade — every dependant read as "not a generator". If FastAPI renames them
# again, the import fails loudly instead.
def _computed_scope(dep) -> str | None:
    return _get_computed_scope(dependant=dep)


def _is_generator_dependant(dep) -> bool:
    return _is_gen_callable(dep.call) or _is_async_gen_callable(dep.call)


# Floors are on the population EXAMINED, never on violations found: a
# violations floor is satisfied by zero violations by construction and can
# never go red. If route discovery breaks, these are what catch it.
MINIMUM_API_ROUTES = 180
MINIMUM_SESSION_DEPENDANTS = 174


def _reachable_api_routes(app):
    """Every real APIRoute, recursing through lazy _IncludedRouter wrappers."""
    seen_routers: set[int] = set()
    found: list[APIRoute] = []

    def walk(routes):
        for route in routes:
            if isinstance(route, _IncludedRouter):
                if id(route.original_router) in seen_routers:
                    continue
                seen_routers.add(id(route.original_router))
                walk(route.original_router.routes)
            elif isinstance(route, APIRoute):
                found.append(route)

    walk(app.routes)
    return found


def _session_dependants(dependant, get_session):
    """Every sub-dependant in the tree whose callable IS db.get_session."""
    out = []
    for sub in dependant.dependencies:
        if sub.call is get_session:
            out.append(sub)
        out.extend(_session_dependants(sub, get_session))
    return out


def _collect(app, get_session):
    routes = _reachable_api_routes(app)
    sites = []
    for route in routes:
        sites.extend(_session_dependants(route.dependant, get_session))
    return routes, sites


def test_every_built_route_binds_the_session_at_function_scope() -> None:
    from db import get_session
    from main import app

    routes, sites = _collect(app, get_session)

    assert len(routes) >= MINIMUM_API_ROUTES, (
        f"only {len(routes)} APIRoutes were reachable, expected at least "
        f"{MINIMUM_API_ROUTES}. Route discovery is broken, so this file is "
        "not checking what it claims to check."
    )
    assert len(sites) >= MINIMUM_SESSION_DEPENDANTS, (
        f"only {len(sites)} session dependants found, expected at least "
        f"{MINIMUM_SESSION_DEPENDANTS}."
    )

    # Name the ENDPOINT FUNCTION, not r.path. In FastAPI 0.137 the prefix lives
    # on the lazy _IncludedRouter context, so an APIRoute built from
    # ``@router.post("")`` has ``r.path == ""`` — a mutation run proved the
    # resulting message read " [POST]" and named nothing.
    offenders = sorted(
        {
            f"{r.endpoint.__module__}.{r.endpoint.__qualname__} "
            f"[{','.join(sorted(r.methods or []))}] path={r.path!r}"
            for r in routes
            for d in _session_dependants(r.dependant, get_session)
            if d.scope != "function"
        }
    )
    assert not offenders, (
        "these routes hold their DB connection across the response send and any "
        "BackgroundTask, because their session dependency is not function-scoped:\n  "
        + "\n  ".join(offenders)
    )


def test_computed_scope_agrees_with_the_declared_scope() -> None:
    """``computed_scope`` is what ``solve_dependencies`` actually branches on.

    A generator dependency with ``scope=None`` computes to ``"request"``
    (``fastapi/dependencies/models.py``), so asserting only on the declared
    ``.scope`` would miss a default that never got set.
    """
    from db import get_session
    from main import app

    routes, sites = _collect(app, get_session)
    assert sites, "no session dependants found — the walk is broken"
    bad = sorted(
        {
            f"{r.endpoint.__module__}.{r.endpoint.__qualname__}"
            for r in routes
            for d in _session_dependants(r.dependant, get_session)
            if _computed_scope(d) != "function"
        }
    )
    assert not bad, (
        "these endpoints' session dependency COMPUTES to a non-function scope:\n  "
        + "\n  ".join(bad)
    )


def test_the_offender_predicate_can_actually_fire() -> None:
    """The offender predicate must be provably able to discriminate.

    This is this file's analogue of the extractor self-test in
    ``tests/test_session_scope_is_function.py``
    (``test_the_extractor_reports_exactly_the_non_compliant_synthetic_sites``):
    without it, every assertion above rests on an unverified belief that
    ``d.scope != "function"`` can ever be True. A mutant that widened the
    predicate to accept ``"request"`` too would survive, because the real app is
    (now) fully compliant and would report zero offenders either way.

    Built in-memory on a throwaway ``FastAPI()`` and driven through this file's
    OWN ``_collect`` helper — not a mock — so what is pinned is the real walk
    and the real predicate.
    """
    from db import get_session

    probe = FastAPI()

    @probe.get("/compliant")
    async def _compliant(session=Depends(get_session, scope="function")):
        return {}

    @probe.get("/offender")
    async def _offender(session=Depends(get_session)):
        return {}

    routes, sites = _collect(probe, get_session)

    assert len(routes) == 2, f"expected 2 APIRoutes on the probe app, got {len(routes)}"
    assert len(sites) == 2, (
        f"expected 2 session dependants on the probe app, got {len(sites)} — "
        "_session_dependants cannot see the dependency it exists to find, so the "
        "assertions above are walking an empty list"
    )

    non_function = [d for d in sites if d.scope != "function"]
    assert len(non_function) == 1, (
        f"the offender predicate flagged {len(non_function)} of 2 probe routes, expected "
        "exactly 1. It must flag the bare `Depends(get_session)` and NOT the "
        '`scope="function"` one — anything else means it cannot discriminate, and the '
        "empty offender list on the real app proves nothing."
    )
    assert non_function[0].scope != "function"


def test_the_naive_walk_would_have_been_vacuous() -> None:
    """Pin the trap. If this ever fails, FastAPI changed and the recursion in
    ``_reachable_api_routes`` should be re-derived rather than trusted."""
    from main import app

    naive = [r for r in app.routes if isinstance(r, APIRoute)]
    recursive = _reachable_api_routes(app)

    assert any(isinstance(r, _IncludedRouter) for r in app.routes), (
        "app.routes no longer contains lazy _IncludedRouter wrappers; re-derive "
        "the recursion in _reachable_api_routes against the installed FastAPI."
    )
    assert len(naive) < len(recursive) / 10, (
        f"the naive walk saw {len(naive)} of {len(recursive)} routes — this "
        "assertion documents WHY the recursion exists."
    )


# ─────────────────────────────────────────────────────────────────────────────
# The identity check above matches `call is get_session`. A ONE-HOP WRAPPER
# escapes it — and escapes the AST guard too, which matches spelling:
#
#     async def _sess():
#         async for s in get_session():
#             yield s
#     ... Depends(_sess)            # computed_scope == "request": the defect
#
# An adversarial review demonstrated exactly that, with the whole guard family
# still reporting 45 passed. So the real invariant is not about `get_session`
# at all: it is that NO generator dependency on any route may be request-scoped,
# whatever it wraps. That is what this leg asserts, and it is the backstop for
# every indirection (wrappers, functools.partial, factories, re-exports).
#
# If a request-scoped generator dependency is ever DELIBERATELY needed — one
# that must stay open until the response is sent — add it here by name with the
# reason. Do not delete the leg.
DELIBERATE_REQUEST_SCOPED_GENERATORS: set[str] = set()


def _generator_dependants(dependant, out=None):
    out = [] if out is None else out
    for sub in dependant.dependencies:
        if _is_generator_dependant(sub):
            out.append(sub)
        _generator_dependants(sub, out)
    return out


def test_no_route_has_a_request_scoped_generator_dependency() -> None:
    from main import app

    routes = _reachable_api_routes(app)
    assert len(routes) >= MINIMUM_API_ROUTES, "route discovery is broken"

    seen = 0
    offenders = []
    for route in routes:
        for dep in _generator_dependants(route.dependant):
            seen += 1
            name = getattr(dep.call, "__name__", repr(dep.call))
            if name in DELIBERATE_REQUEST_SCOPED_GENERATORS:
                continue
            if _computed_scope(dep) != "function":
                offenders.append(
                    f"{route.endpoint.__module__}.{route.endpoint.__qualname__} -> {name}"
                )

    assert seen >= MINIMUM_SESSION_DEPENDANTS, (
        f"only {seen} generator dependencies found across {len(routes)} routes, "
        f"expected at least {MINIMUM_SESSION_DEPENDANTS} — the walk is broken, so "
        "this leg is inspecting almost nothing."
    )
    assert not offenders, (
        "these routes hold a generator dependency across the response send and "
        "any BackgroundTask:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_the_offender_predicate_fires_on_an_explicit_request_scope() -> None:
    """M5: the earlier self-test's probe had `.scope is None`, never "request".

    So widening the predicate to `not in ("function", "request")` left it green
    while a live `scope="request"` route would ship. This pins BOTH offender
    shapes: an explicit "request" and an unset default.
    """
    from fastapi import Depends, FastAPI

    from db import get_session

    app = FastAPI()

    @app.get("/ok")
    async def ok(session=Depends(get_session, scope="function")):  # noqa: B008
        return {}

    @app.get("/default")
    async def default(session=Depends(get_session)):  # noqa: B008
        return {}

    @app.get("/explicit")
    async def explicit(session=Depends(get_session, scope="request")):  # noqa: B008
        return {}

    _, sites = _collect(app, get_session)
    assert len(sites) == 3, f"expected 3 session dependants, got {len(sites)}"
    non_function = [s for s in sites if _computed_scope(s) != "function"]
    assert len(non_function) == 2, (
        "the predicate must flag BOTH the unset default and an explicit "
        f'scope="request"; it flagged {len(non_function)}'
    )
