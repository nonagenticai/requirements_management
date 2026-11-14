#!/usr/bin/env python3
"""Fail if a declared route was never exercised by the test suite.

This is the assertion half of the instrument whose recording half is
``tests/plugins/route_coverage.py``. It:

1. merges every per-worker ``hits-*.json`` written during the pytest run,
2. enumerates the DECLARED routes from ``main.app.routes`` (method + template),
3. subtracts, and
4. fails unless ``declared - hit`` is a subset of the grandfathered allowlist at
   ``tests/route-coverage-grandfathered.json``.

⛔ THE ALLOWLIST IS A RATCHET, NOT A RUG. It records a ``floor``: the number of
uncovered routes measured on the honest run that created it. A run that finds
FEWER uncovered routes than the floor also fails. That is deliberate. The most
likely cause of "zero offenders" is not that 40 routes acquired tests in one
afternoon — it is that the recorder stopped recording, the merge read the wrong
directory, or the declared enumeration returned nothing. An instrument that can
only report good news is the thing this repo keeps getting burned by.

To legitimately cover a route: delete its entry AND lower ``floor`` by the same
amount, in the same commit. Both edits are visible in review; a silent
improvement is not.

Usage::

    uv run python scripts/assert_route_coverage.py
    uv run python scripts/assert_route_coverage.py --run-dir .pytest_cache/route-coverage
    uv run python scripts/assert_route_coverage.py --json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass, field

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_RUN_DIR = REPO_ROOT / ".pytest_cache" / "route-coverage"
DEFAULT_ALLOWLIST = REPO_ROOT / "tests" / "route-coverage-grandfathered.json"

# Neither is a route anybody writes a test for; both are synthesised by
# Starlette/FastAPI itself rather than declared by this service.
_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})

# Sanity floor on the DECLARED count. Measured 2026-09-05: this service declares
# 195 operations / 196 (method, template) pairs. 150 leaves room for genuine
# route removal while still catching an enumeration that has collapsed — which
# is the failure that makes a green run meaningless.
MIN_DECLARED_ROUTES = 150


def key(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def merge_hits(run_dir: pathlib.Path) -> tuple[set[str], list[str]]:
    """Merge every ``hits-*.json`` under ``run_dir``.

    Returns (hit keys, names of the files that were read). Per-worker files are
    merged here rather than appended to during the run: concurrent appends from
    ``-n auto`` workers interleave and corrupt each other.
    """
    hits: set[str] = set()
    files: list[str] = []
    if not run_dir.is_dir():
        return hits, files
    for path in sorted(run_dir.glob("hits-*.json")):
        try:
            payload = json.loads(path.read_text())
        except OSError, ValueError:
            continue
        files.append(path.name)
        for hit in payload.get("hits", []):
            method = str(hit.get("method", "")).upper()
            route = str(hit.get("path", ""))
            if method and route and method not in _IGNORED_METHODS:
                hits.add(key(method, route))
    return hits, files


def declared_routes() -> set[str]:
    """Every ``METHOD /template`` this service declares, from the live app.

    ⛔ ``app.routes`` IS NOT A FLAT LIST OF ``APIRoute`` on FastAPI 0.137.
    Measured on this app: 33 entries — 1 ``APIRoute`` (the bare ``GET /``), 4
    plain Starlette routes (``/openapi.json``, ``/docs``,
    ``/docs/oauth2-redirect``, ``/redoc``) and **28 ``_IncludedRouter``**
    objects, each a lazy branch holding a whole router. A naive
    ``[r for r in app.routes if isinstance(r, APIRoute)]`` returns **1 route out
    of 195** — and, being non-empty, does not look broken.

    ``_IncludedRouter.effective_route_contexts()`` flattens a branch into one
    ``_EffectiveRouteContext`` per reachable route, carrying the PREFIXED path
    (the ``original_route.path`` on the context does not). The four Starlette
    routes are excluded on purpose: they are synthesised by FastAPI, not
    declared by this service.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from main import app

    return routes_of(app)


def routes_of(app: object) -> set[str]:
    """Flatten an app's declared route surface. Pure — no ``main`` import."""
    declared: set[str] = set()

    def add(path: str, methods: object) -> None:
        # ``or ()`` narrows to ``AlwaysFalsy | tuple`` for ty, which cannot see that
        # the falsy branch is the tuple. An explicit isinstance keeps the runtime
        # behaviour identical and the type checker honest.
        for method in methods if isinstance(methods, (list, set, tuple, frozenset)) else ():
            if str(method).upper() in _IGNORED_METHODS:
                continue
            if path:
                declared.add(key(str(method), path))

    def walk(routes: object) -> None:
        for route in routes if isinstance(routes, (list, set, tuple, frozenset)) else ():
            contexts = getattr(route, "effective_route_contexts", None)
            if callable(contexts):
                # An _IncludedRouter branch: flatten it to effective contexts.
                for ctx in contexts():
                    add(getattr(ctx, "path", ""), getattr(ctx, "methods", None))
                continue
            # A route declared directly on the app. Only FastAPI's own routes
            # count; a plain starlette.routing.Route has no `dependant`.
            if hasattr(route, "dependant"):
                add(getattr(route, "path", ""), getattr(route, "methods", None))

    walk(getattr(app, "routes", []))
    return declared


def load_allowlist(path: pathlib.Path) -> tuple[set[str], int]:
    """Return (grandfathered keys, floor). A missing file means (empty, 0)."""
    if not path.is_file():
        return set(), 0
    payload = json.loads(path.read_text())
    return set(payload.get("uncovered", [])), int(payload.get("floor", 0))


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
@dataclass
class Verdict:
    declared: set[str]
    hit: set[str]
    uncovered: set[str]
    unexpected: set[str]
    stale: set[str]
    floor: int
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def evaluate(
    declared: set[str],
    hit: set[str],
    grandfathered: set[str],
    floor: int,
    min_declared: int = MIN_DECLARED_ROUTES,
) -> Verdict:
    """Decide whether this run passes. Pure — no I/O, no app import.

    ``min_declared`` is injectable ONLY so the unit tests can exercise the other
    legs with two-route fixtures. Production callers must leave it at the
    default; a caller that lowers it is disabling the collapse check.
    """
    uncovered = declared - hit
    unexpected = uncovered - grandfathered
    stale = grandfathered - uncovered

    verdict = Verdict(
        declared=declared,
        hit=hit,
        uncovered=uncovered,
        unexpected=unexpected,
        stale=stale,
        floor=floor,
    )

    # Vacuity, first. An empty declared set or an empty hit set makes every
    # other conclusion below meaningless, and both look like a clean pass.
    #
    # ⛔ AND "empty" IS THE WRONG THRESHOLD. Review fed this function
    # `declared={one route}, hit={the same route}` and it returned GREEN — the
    # exact "1 route of 195" shape that happens when the app fails to register
    # its routers, or when FastAPI internals change and the enumeration silently
    # collapses to a handful. A set that is merely SMALL is as broken as one that
    # is empty, and it is the more likely failure. So the floor is on the
    # DECLARED COUNT, not on emptiness.
    if declared and len(declared) < min_declared:
        verdict.errors.append(
            f"only {len(declared)} route(s) DECLARED, below the sanity floor of "
            f"{min_declared}. This service mounts ~195; a collapsed "
            f"enumeration makes 'everything is covered' vacuous. Raise the floor "
            f"deliberately if the service genuinely shrank."
        )
    if not declared:
        verdict.errors.append(
            "no routes DECLARED — main.app.routes yielded nothing, so 'everything "
            "is covered' proves nothing. The enumeration is broken."
        )
    if not hit:
        verdict.errors.append(
            "no routes HIT — the recorder wrote nothing, so every declared route "
            "looks uncovered for the wrong reason. Check that "
            "`-p tests.plugins.route_coverage` is in addopts and that the run "
            "directory matches."
        )

    if unexpected:
        verdict.errors.append(
            f"{len(unexpected)} declared route(s) are never exercised and are not "
            f"grandfathered:\n  " + "\n  ".join(sorted(unexpected))
        )

    # The ratchet's vacuity leg. See this module's docstring, and the header of
    # tests/route-coverage-grandfathered.json.
    if len(uncovered) < floor:
        verdict.errors.append(
            f"found {len(uncovered)} uncovered route(s) but the recorded floor is "
            f"{floor}. Fewer offenders than the floor is a BROKEN INSTRUMENT until "
            f"proven otherwise, not a win. If routes were genuinely covered, remove "
            f"them from `uncovered` and lower `floor` to {len(uncovered)} in the "
            f"same commit."
        )

    return verdict


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=pathlib.Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--allowlist", type=pathlib.Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--write-allowlist",
        action="store_true",
        help="rewrite the allowlist from THIS run's findings (seeding only)",
    )
    args = parser.parse_args(argv)

    hit, files = merge_hits(args.run_dir)
    declared = declared_routes()
    grandfathered, floor = load_allowlist(args.allowlist)

    if args.write_allowlist:
        uncovered = sorted(declared - hit)
        args.allowlist.write_text(
            json.dumps(
                {
                    "_header": _HEADER,
                    "floor": len(uncovered),
                    "uncovered": uncovered,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"wrote {args.allowlist} with {len(uncovered)} entries, floor={len(uncovered)}")
        return 0

    verdict = evaluate(declared, hit, grandfathered, floor)

    if args.json:
        print(
            json.dumps(
                {
                    "files": files,
                    "declared": len(verdict.declared),
                    "hit": len(verdict.hit & verdict.declared),
                    "uncovered": len(verdict.uncovered),
                    "unexpected": sorted(verdict.unexpected),
                    "stale_allowlist_entries": sorted(verdict.stale),
                    "floor": verdict.floor,
                    "ok": verdict.ok,
                    "errors": verdict.errors,
                },
                indent=2,
            )
        )
        return 0 if verdict.ok else 1

    print(f"route coverage  (merged {len(files)} worker file(s): {files})")
    print(f"  declared : {len(verdict.declared)}")
    print(f"  hit      : {len(verdict.hit & verdict.declared)}")
    print(f"  uncovered: {len(verdict.uncovered)}   (grandfathered floor {verdict.floor})")
    if verdict.stale:
        print(f"  note: {len(verdict.stale)} allowlist entr(ies) no longer uncovered")
    for error in verdict.errors:
        print(f"\nFAIL: {error}")
    if verdict.ok:
        print("\nOK")
    return 0 if verdict.ok else 1


_HEADER = [
    "Grandfathered route-coverage offenders. Generated by",
    "scripts/assert_route_coverage.py --write-allowlist from a real pytest run",
    "with tests/plugins/route_coverage.py enabled.",
    "",
    "`uncovered` lists METHOD + route template declared by main.app that no test",
    "in the suite ever dispatched a request to. A route is 'hit' only when a real",
    "request reached its handler — never because its path appears as a string in a",
    "test file.",
    "",
    "FLOOR LOGIC — read before editing:",
    "  `floor` is the number of uncovered routes measured on the run that produced",
    "  this file. scripts/assert_route_coverage.py fails in BOTH directions:",
    "    * MORE than the allowlist  -> a new route shipped with no test.",
    "    * FEWER than `floor`       -> the instrument is broken until proven",
    "      otherwise. A run that reports 0 offenders against a floor of 40 has",
    "      almost certainly stopped recording, merged an empty directory, or",
    "      enumerated no routes. That failure mode is exactly why the two prior",
    "      grep-based measurements of this number disagreed.",
    "  To genuinely cover a route: delete its entry AND lower `floor` by the same",
    "  amount in the SAME commit. Both edits are visible in review.",
]


if __name__ == "__main__":
    raise SystemExit(main())
