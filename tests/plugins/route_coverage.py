"""Record which route templates the test session actually EXERCISES at runtime.

Why this is not a grep
----------------------
Two earlier sessions "measured" route coverage by grepping the test tree for
route strings and got opposite answers (25 uncovered, then 0 uncovered). The
second was refuted in a minute: ``POST /principles/{id}/evaluate`` "matched"
only because ``tests/test_pool_holder_instrument.py:80`` uses that exact string
as a *label for a fake pool holder*. A route name appearing in a test file is
not a route being called. This plugin only ever records a route that a real
request was dispatched to.

Mechanism, and why this one
---------------------------
``fastapi.routing.APIRoute.__init__`` ends with
``self.app = request_response(self.get_route_handler())`` — the per-route
handler is built ONCE, at route construction. So patching
``APIRoute.get_route_handler`` on the CLASS, before ``main`` is imported, wraps
every route this service will ever declare, with no per-app registration step.

The two alternatives were rejected on measured grounds:

* ``app.add_middleware(...)`` requires importing ``main`` at plugin-load time.
  That is actively unsafe here: ``tests/conftest.py`` rewrites ``DATABASE_URL``
  into a per-xdist-worker ``search_path`` **before** its own ``from main import
  app``, because ``src/db.py`` builds its engine from the env at import time.
  Importing ``main`` earlier would pin every worker's app engine to ``public``
  and silently break the suite's schema isolation. This plugin imports only
  ``fastapi``.
* A pure-ASGI middleware appended around ``app`` can see ``scope["route"]``,
  but only *after* awaiting downstream, and it would still need the app object.

The route template itself comes from ``request.scope["route"]``, which
``APIRoute.matches`` populates (``fastapi/routing.py``: ``child_scope["route"]
= self``) — i.e. the route the router actually matched, not a guess. The bound
route is used as a fallback so a scope without the key still records something
truthful rather than nothing.

A hit is recorded when the request reaches the route handler. That includes
requests that then 422 on body validation, which is correct: the route was
dispatched to. It excludes requests rejected upstream by ``AuthMiddleware``,
which is also correct — those never reached the route.

Output
------
Every process (each xdist worker, and the controller) writes its own file::

    <run_dir>/hits-<workerid>.json     {"worker": ..., "hits": [{"method","path"}]}

``run_dir`` is ``$ROUTE_COVERAGE_DIR`` if set, else ``<rootdir>/.pytest_cache/
route-coverage`` — ``.pytest_cache/`` is already in this repo's ``.gitignore``,
so the default drops no untracked files into a clean tree.

Per-worker files are never appended to a shared file: concurrent appends from
``-n auto`` workers interleave and corrupt each other. They are merged, once,
afterwards.

The controller (``not hasattr(config, "workerinput")``) additionally writes
``merged.json`` at session end, after xdist has shut the workers down and their
files are on disk. That file is the proof the controller hook registered at all.

Reporting and enforcement
-------------------------
Recording a number nobody reads is the failure this instrument was built to
avoid, so the controller's ``pytest_sessionfinish`` also *evaluates* the run.
It does that by importing ``scripts/assert_route_coverage.py`` and calling its
``merge_hits`` / ``declared_routes`` / ``load_allowlist`` / ``evaluate``. That
script stays the single source of truth for the merge, the allowlist and the
ratchet; none of that logic is duplicated here, and the script still runs
standalone (``uv run python scripts/assert_route_coverage.py``).

There is no post-pytest step available to run the script from: CI is the shared
archetype ``python-service.yml``, whose 42 inputs include no ``pytest_args`` /
``post_test_command`` / ``extra_checks`` hook, and editing the archetype is out
of lane for this service. So the assertion has to live inside the pytest run,
and the only place that can still change the process's exit code is
``pytest_sessionfinish``: ``_pytest/main.py`` calls the hook and *then* returns
``session.exitstatus``, so assigning to it there genuinely reddens the build.

TWO MODES.

*Report mode* (the default) always prints one greppable line::

    ROUTE_COVERAGE_READING declared=<n> hit=<n> uncovered=<n> floor=<n> files=<n> enforcement=off

followed by one ``ROUTE_COVERAGE_UNCOVERED <METHOD> <template>`` line per
offender that is NOT grandfathered (while the allowlist is still the placeholder
that is every offender, so a seeding run publishes the full list; afterwards a
normal run prints only what is new rather than re-listing the whole floor).
Report mode never fails anything. It exists so the very first CI run
publishes the real number *before* the allowlist has been seeded, because a
floor can only be set from an honest reading.

⚠️ THE ALLOWLIST IS NO LONGER A PLACEHOLDER, AND ZERO IS THE STRICT END.
This paragraph used to say the ``{"floor": 0, "uncovered": []}`` this file
shipped with "can never fail". That was true of the *placeholder*; it is false of
the file on disk today. ``tests/route-coverage-grandfathered.json`` was ratcheted
21 → 12 → 11 → 0 on 2026-09-05 and now carries **zero entries at floor 0**. Floor
0 makes only the ratchet's LOWER leg inert (``len(uncovered) < 0`` is
unsatisfiable, ``assert_route_coverage.py``); the upper leg is at its tightest
setting, because ``unexpected = uncovered - grandfathered`` must come out
**exactly empty** across all 196 declared (method, template) pairs. There is no
slack whatsoever. One route missing from the reading reddens all 2,689 tests.

*Enforce mode* fails the session. It is **ON BY DEFAULT IN CI** — not opt-in —
and structurally guarded, and EVERY one of these must hold or the mode falls back
to report:

1. enforcement is not explicitly disabled, and the run is CI *or* opted in:
   ``ROUTE_COVERAGE_ENFORCE`` is not ``0``/``false``/``no``/``off``, and it is
   either ``1``/``true``/``yes``/``on`` **or** ``CI`` is set (GitHub Actions sets
   ``CI=true`` on every job). ⛔ Do NOT read condition 1 as "off unless somebody
   opts in" — that misreading is what makes a change to the pytest invocation
   look free. ``enforcement_decision`` says why an env-var-only gate was
   rejected: CI here delegates to a shared archetype that passes no ``env:``, so
   an opt-in-only gate is a guard that can never run;
2. this process is the controller (``is_controller(config)``), not an xdist worker;
3. no ``-k`` (``config.option.keyword``);
4. no ``-m`` (``config.option.markexpr``);
5. no ``--deselect``, and zero tests actually deselected (terminalreporter stats);
6. no ``--last-failed`` / ``--stepwise``;
7. the collected args are the whole tree — every arg resolves to ``<rootdir>`` or
   ``<rootdir>/tests``, and none is a ``::`` node id;
8. the run did not stop early (``session.shouldstop`` / ``shouldfail``, i.e. ``-x``
   or ``--maxfail``).

The reason enforcement is off is printed alongside the reading, so a run that
*meant* to enforce and silently did not is visible in the log rather than green.
A developer running ``pytest tests/test_one_thing.py`` therefore never eats a
route-coverage failure — condition 7 alone stops it.

When enforcement is on and the evaluation cannot be COMPUTED (the script will not
import, ``main`` will not import, the run directory is unreadable) that is a
failure too, not a skip. A guard asked to run and unable to is not a pass.

⚠️ WHAT IS TRUE UNDER ``-n 2`` — MEASURED, NOT REASONED.
Two earlier versions of this paragraph were wrong, in opposite directions, and
the second was the dangerous one. The first read "this gate is CI-EFFECTIVE ...
CI runs pytest serially (there is no ``xdist`` / ``-n auto`` / ``PYTEST_ADDOPTS``
anywhere in the archetype)"; that expired when the archetype gained a
``pytest_xdist_workers`` input and ``.github/workflows/ci-cd.yaml`` started
passing ``"2"``. The second replaced it with "enforce mode has never been
switched on anywhere ... the gate has only ever been in report mode", and with
"under xdist ``pytest_sessionfinish`` does not reliably register/enforce on the
controller".

⛔ BOTH HALVES OF THAT SECOND CLAIM ARE FALSE, and it is exactly the claim that
made switching CI to ``-n 2`` look safe. ``ROUTE_COVERAGE_ENFORCE`` being absent
from every workflow is IRRELEVANT: ``enforcement_decision`` enforces by DEFAULT
whenever ``CI`` is set, which GitHub Actions always sets. The repo's own
``tests/test_route_coverage_plugin.py::test_the_gate_is_armed_by_CI_and_can_be_switched_off_explicitly``
had already pinned that.

Measured 2026-09-06 on six synthetic route-exercising tests in this venv
(pytest 9.1.0 / pytest-xdist 3.8.0), ``ROUTE_COVERAGE_ENFORCE`` unset:

* serial   → ``files=1`` ``['hits-controller.json']``, 6 routes merged;
* ``-n 2`` → ``files=3``
  ``['hits-controller.json', 'hits-gw0.json', 'hits-gw1.json']``, the SAME 6
  routes merged — the union is exact, not lossy;
* ``-n 2`` with ``CI=true`` → ``enforcement=on``, ``ROUTE_COVERAGE_FAIL``, and
  **pytest exited 1** on a run in which all six tests passed.

So the controller hook DOES register under xdist and the exit code DOES stick.
The merge is sound for a structural reason, not by luck: xdist's own
``WorkerInteractor.pytest_sessionfinish`` (``xdist/remote.py``) is a
**hookwrapper**. It ``yield``s to every other ``pytest_sessionfinish`` impl —
this module's included — and only *afterwards* sends ``workerfinished``. The
controller therefore cannot observe a worker as finished until that worker has
written its ``hits-<workerid>.json``, and ``DSession`` only leaves its run loop
once every node has reported. ``merge_dir`` on the controller always sees a
complete set on the happy path.

⚠️ AND THE ANALOGY THAT PRODUCED THE FALSE CLAIM IS A REAL DISTINCTION, SO KEEP
IT STRAIGHT. ``tests/integration/conftest.py`` genuinely did have to abandon
``pytest_sessionfinish`` for a per-report ``pytest_runtest_makereport`` backstop,
and CLAUDE.md is right about that. The reason is NOT that the hook cannot fire on
an xdist controller — it is WHERE THAT HOOK IS DEFINED. ``DSession.pytest_collection``
returns ``True`` to "prohibit collection of test items in controller process"
(``xdist/dsession.py``), so the controller never collects ``tests/integration/``
and therefore never imports that subdirectory's conftest; a hook it defines is
registered in the workers only. THIS module is loaded by ``-p
tests.plugins.route_coverage`` in ``addopts``, during ``Config._preparse``, in
every process including the controller — so its hooks are registered where the
exit status lives. Generalising the conftest's constraint to a ``-p`` plugin is
what produced the retracted claim above.

⛔ THE RESIDUAL RISK IS A SHORT MERGE, NOT THE HAPPY PATH — and with the
allowlist at zero entries there is nothing to absorb it. If ONE worker's file
never lands (a worker that dies before ``pytest_sessionfinish``, a replaced node,
an unreadable file) the union is short by everything that worker exercised, and
the gate reddens naming routes that are in fact tested. That failure READS AS A
CODE DEFECT AND IS NOT ONE, and the cheapest way out of it — re-adding those
routes to the allowlist — disarms a working gate over an instrument fault. So
``merge_integrity`` checks the merge FIRST and, when it is short, fails naming
the merge instead of blaming the routes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import threading
from typing import Any

# Method+template pairs seen in THIS process. A set, because a route hit 400
# times is still one covered route.
_HITS: set[tuple[str, str]] = set()
_LOCK = threading.Lock()

_ENV_DIR = "ROUTE_COVERAGE_DIR"
_DEFAULT_SUBDIR = ".pytest_cache/route-coverage"

_PATCH_FLAG = "_route_coverage_patched"


def record(method: str, path: str) -> None:
    """Record one exercised (method, route-template) pair."""
    if not path:
        return
    with _LOCK:
        _HITS.add((method.upper(), path))


def hits() -> set[tuple[str, str]]:
    """The pairs recorded so far in this process (a copy)."""
    with _LOCK:
        return set(_HITS)


def reset() -> None:
    """Drop everything recorded so far. Used by this plugin's own tests."""
    with _LOCK:
        _HITS.clear()


def effective_path(scope: Any, bound_route: Any = None) -> str:
    """The FULL route template for the request in ``scope``.

    ⛔ ``scope["route"].path`` IS NOT IT, and this is the single sharpest edge in
    this whole instrument. FastAPI 0.137 stopped flattening ``include_router``:
    an included router survives in ``app.routes`` as an ``_IncludedRouter``, and
    ``_IncludedRouter._handle_selected`` sets ``scope["route"] =
    effective_context.original_route`` — the route object as DECLARED ON THE
    SUB-ROUTER, whose ``.path`` carries no prefix. The prefixed path lives on the
    ``_EffectiveRouteContext`` parked in the fastapi sub-scope.

    Measured on this app: **183 of 195** routes differ between the two.
    ``scope["route"].path`` reports ``/{rid}`` where the real template is
    ``/requirements/{rid}``, and ``''`` where it is ``/requirements``. An
    instrument reading the wrong one records templates that match no declared
    route at all — every route looks uncovered, and the 12 that happen to agree
    (``/health*`` and friends, included without a prefix) make it look like it
    works.

    Order: effective context, then the matched route, then the bound route.
    """
    try:
        from fastapi.routing import _get_scope_effective_route_context
    except ImportError:  # pragma: no cover - older/newer FastAPI without it
        context = None
    else:
        context = _get_scope_effective_route_context(scope)

    for candidate in (context, scope.get("route"), bound_route):
        path = getattr(candidate, "path", None)
        if path:
            return str(path)
    return ""


def install_recorder() -> bool:
    """Wrap ``APIRoute.get_route_handler`` on the class. Idempotent.

    Returns True if this call installed the patch, False if it was already on.
    """
    from fastapi.routing import APIRoute

    if getattr(APIRoute, _PATCH_FLAG, False):
        return False

    original = APIRoute.get_route_handler

    def get_route_handler(self: APIRoute) -> Any:
        handler = original(self)

        async def recording_handler(request: Any) -> Any:
            record(request.method, effective_path(request.scope, self))
            return await handler(request)

        return recording_handler

    # setattr rather than a plain assignment: the wrapper is deliberately
    # signature-erased (it takes any Request-like object), and a direct
    # assignment makes the type checker compare it against the bound method
    # type it is replacing.
    setattr(APIRoute, "get_route_handler", get_route_handler)
    setattr(APIRoute, _PATCH_FLAG, True)
    return True


# Install at IMPORT time. `-p tests.plugins.route_coverage` is imported during
# Config._preparse, i.e. before initial conftests are loaded and therefore long
# before `from main import app` constructs any APIRoute.
install_recorder()


def run_dir(rootdir: str | os.PathLike[str]) -> pathlib.Path:
    """Directory the per-worker hit files live in."""
    override = os.environ.get(_ENV_DIR, "")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(rootdir) / _DEFAULT_SUBDIR


def is_controller(config: Any) -> bool:
    """True in a serial run and on the xdist controller; False in a worker.

    xdist injects ``workerinput`` onto the worker's config and nowhere else.
    """
    return not hasattr(config, "workerinput")


def worker_id(config: Any) -> str:
    winput = getattr(config, "workerinput", None)
    if isinstance(winput, dict):
        return str(winput.get("workerid", "worker"))
    return "controller"


def merge_dir(directory: pathlib.Path) -> tuple[set[tuple[str, str]], list[str]]:
    """Merge every ``hits-*.json`` in ``directory``. Returns (hits, files read)."""
    merged: set[tuple[str, str]] = set()
    read: list[str] = []
    if not directory.is_dir():
        return merged, read
    for path in sorted(directory.glob("hits-*.json")):
        try:
            payload = json.loads(path.read_text())
        except OSError, ValueError:
            continue
        read.append(path.name)
        for hit in payload.get("hits", []):
            method = str(hit.get("method", "")).upper()
            route = str(hit.get("path", ""))
            if method and route:
                merged.add((method, route))
    return merged, read


def _reads_as_json(path: pathlib.Path) -> bool:
    """True if ``path`` parses. A hit file that does not is a LOST process."""
    try:
        json.loads(path.read_text())
    except OSError, ValueError:
        return False
    return True


def expected_worker_files(config: Any) -> int:
    """How many per-worker ``hits-*.json`` this run should have produced.

    0 in a serial run — nothing but the controller writes one. On the controller
    under xdist, ``config.option.numprocesses`` has already been resolved from
    ``auto``/``logical`` to an integer by xdist's own ``pytest_configure``, which
    runs long before any ``pytest_sessionfinish``. So this reads a number and
    never the literal string ``"auto"``; anything it cannot read as one is
    treated as "not an xdist run" rather than as an error, because a wrong guess
    here would redden a serial run for a missing worker that was never asked for.
    """
    try:
        return max(int(_option(config, "numprocesses")), 0)
    except TypeError, ValueError:
        return 0


def merge_integrity(config: Any, directory: pathlib.Path) -> list[str]:
    """Complaints about the MERGE itself. Empty means the union is complete.

    ⛔ WHY THIS IS SEPARATE FROM THE VERDICT. Under ``-n N`` the reading is a
    union of N+1 files written by N+1 processes, and the allowlist carries zero
    entries, so a short merge and a genuinely untested route produce the SAME
    failure — a ``ROUTE_COVERAGE_FAIL`` naming routes. Only one of the two is a
    code defect. An operator handed the ambiguous version reaches for the
    allowlist, which disarms a working gate to paper over a broken instrument.
    Naming the merge first is what prevents that.

    This does NOT try to detect every way a merge can go wrong. It catches the
    two that are cheap to see from the controller and expensive to misdiagnose:
    a file that will not parse, and a worker that never wrote one at all.
    """
    if not directory.is_dir():
        return [
            f"the route-coverage run directory {directory} does not exist, "
            f"so nothing was merged and the reading below is empty by construction"
        ]

    present = sorted(path.name for path in directory.glob("hits-*.json"))
    complaints: list[str] = []

    corrupt = [name for name in present if not _reads_as_json(directory / name)]
    if corrupt:
        complaints.append(
            f"{len(corrupt)} hit file(s) did not parse and were SKIPPED by the "
            f"merge: {corrupt}. Every route those processes exercised is missing "
            f"from the reading below."
        )

    expected = expected_worker_files(config)
    if expected:
        landed = [name for name in present if name != "hits-controller.json"]
        if len(landed) < expected:
            complaints.append(
                f"this run asked for {expected} xdist worker(s) but only "
                f"{len(landed)} per-worker hit file(s) reached the merge: "
                f"{landed}. A worker that dies before pytest_sessionfinish writes "
                f"nothing, and every route it exercised then looks untested."
            )

    return complaints


def _write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def pytest_configure(config: Any) -> None:
    """Clear stale hit files — controller only, before any worker is spawned.

    xdist spawns its workers during the session, well after ``pytest_configure``
    on the controller, so this cannot race a worker's write. Guarding on the
    controller matters: a worker clearing the directory would delete its peers'
    results.
    """
    install_recorder()
    if not is_controller(config):
        return
    directory = run_dir(config.rootpath)
    if directory.is_dir():
        for stale in list(directory.glob("hits-*.json")) + list(directory.glob("merged.json")):
            try:
                stale.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Reporting + enforcement. See the "Reporting and enforcement" section above.
# --------------------------------------------------------------------------- #
ENFORCE_ENV = "ROUTE_COVERAGE_ENFORCE"
_FALSY = frozenset({"0", "false", "no", "off"})


def _in_ci() -> bool:
    """True on a CI runner. GitHub Actions sets ``CI=true`` on every job."""
    return str(os.environ.get("CI", "")).strip().lower() in _TRUTHY


_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Prefixes are deliberately bracket-free so `grep ROUTE_COVERAGE_READING` works
#: without regex escaping. The existing "[route-coverage] merged …" line is a
#: character class to grep; these are not.
REPORT_PREFIX = "ROUTE_COVERAGE_READING"
UNCOVERED_PREFIX = "ROUTE_COVERAGE_UNCOVERED"
FAIL_PREFIX = "ROUTE_COVERAGE_FAIL"

#: Cap on how many offenders get their own log line, so a broken enumeration
#: cannot flood a CI log with thousands of them.
MAX_LISTED_UNCOVERED = 500

_ASSERTION_MODULE: Any = None


def load_assertion_module() -> Any:
    """Import ``scripts/assert_route_coverage.py`` by path, once.

    The script is not importable as a package (``scripts/`` has no
    ``__init__.py`` and is not on ``sys.path``), so it is loaded from its file
    location. It remains the single source of truth for merge/evaluate/ratchet —
    this plugin calls into it and reimplements none of it.
    """
    global _ASSERTION_MODULE
    if _ASSERTION_MODULE is not None:
        return _ASSERTION_MODULE

    path = pathlib.Path(__file__).resolve().parent.parent.parent / "scripts"
    path = path / "assert_route_coverage.py"
    spec = importlib.util.spec_from_file_location("_route_coverage_assertion", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the assertion script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _ASSERTION_MODULE = module
    return module


def _option(config: Any, name: str, default: Any = None) -> Any:
    return getattr(getattr(config, "option", None), name, default)


def _terminalreporter(config: Any) -> Any:
    manager = getattr(config, "pluginmanager", None)
    if manager is None:
        return None
    try:
        return manager.get_plugin("terminalreporter")
    except Exception:  # pragma: no cover - a plugin manager that will not answer
        return None


def _deselected_count(config: Any) -> int:
    stats = getattr(_terminalreporter(config), "stats", None)
    if not isinstance(stats, dict):
        return 0
    return len(stats.get("deselected") or ())


def args_are_the_whole_tree(config: Any) -> bool:
    """True when the run's args name the whole suite rather than a slice.

    Accepts no args at all (a bare ``pytest`` with no ``testpaths``), ``<rootdir>``
    and ``<rootdir>/tests``. Rejects anything containing ``::`` — a node id is one
    test or one class, and a single-test run must never be able to conclude that
    194 routes are uncovered.
    """
    rootpath = pathlib.Path(str(getattr(config, "rootpath", ".")))
    invocation = getattr(getattr(config, "invocation_params", None), "dir", None)
    base = pathlib.Path(str(invocation)) if invocation else rootpath

    try:
        accepted = {rootpath.resolve(), (rootpath / "tests").resolve()}
    except OSError:  # pragma: no cover - unresolvable rootdir
        return False

    args = [str(arg) for arg in (getattr(config, "args", None) or ())]
    if not args:
        return True

    for arg in args:
        if "::" in arg:
            return False
        candidate = pathlib.Path(arg)
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return False
        if resolved not in accepted:
            return False
    return True


def enforcement_decision(session: Any) -> tuple[bool, str]:
    """``(enforcing, reason)``. The reason is printed either way.

    Every condition is listed in this module's docstring. They are checked in
    cheapest-first order and the FIRST failing one is reported, so the log names
    one concrete thing to change rather than "enforcement was off".
    """
    config = getattr(session, "config", None)
    if config is None:
        return False, "the session carries no config"

    # ⛔ WHY THIS IS NOT A PLAIN OPT-IN.
    #
    # The first version of this gate required ROUTE_COVERAGE_ENFORCE=1 and
    # nothing else. That made it unable to fail the ONLY build that matters:
    # `.github/workflows/ci-cd.yaml` delegates to the shared reusable workflow
    # `gitops-production/.github/workflows/python-service.yml@main`, whose 42
    # inputs carry no `env:` and no `pytest_args` passthrough, and editing the
    # shared archetype is out of this repo's lane. So an env-var-only gate is a
    # guard that CANNOT RUN -- exactly the defect this instrument exists to
    # catch, merely relocated one level.
    #
    # Therefore: enforce by DEFAULT wherever CI is set (GitHub Actions sets
    # CI=true), and treat the env var as an override in BOTH directions so a
    # developer can opt in locally and CI can be switched off without a code
    # change.
    raw = str(os.environ.get(ENFORCE_ENV, "")).strip().lower()
    if raw in _FALSY:
        return False, f"{ENFORCE_ENV}={raw!r} explicitly disables enforcement"
    if raw not in _TRUTHY and not _in_ci():
        return False, f"{ENFORCE_ENV} is not set and this is not CI (report only)"
    if not is_controller(config):
        return False, "this process is an xdist worker, not the controller"
    if _option(config, "keyword"):
        return False, f"-k {_option(config, 'keyword')!r} filtered the run"
    if _option(config, "markexpr"):
        return False, f"-m {_option(config, 'markexpr')!r} filtered the run"
    if _option(config, "deselect"):
        return False, "--deselect filtered the run"
    if _option(config, "lf") or _option(config, "stepwise"):
        return False, "--last-failed/--stepwise runs a subset of the suite"
    if _option(config, "collectonly"):
        # A --collect-only run executes ZERO tests, so every route is trivially
        # uncovered. Enforcing there reddens a run that asserted nothing.
        return False, "--collect-only ran no tests"
    ignored = _option(config, "ignore") or _option(config, "ignore_glob")
    if ignored:
        return False, f"--ignore removed part of the tree: {ignored!r}"
    if not args_are_the_whole_tree(config):
        args = list(getattr(config, "args", None) or ())
        return False, f"the collected args are not the whole tests/ tree: {args}"
    deselected = _deselected_count(config)
    if deselected:
        return False, f"{deselected} test(s) were deselected"
    if getattr(session, "shouldstop", False) or getattr(session, "shouldfail", False):
        return False, "the run stopped early (-x / --maxfail), so it is partial"

    return True, f"{ENFORCE_ENV}=1 on an unfiltered whole-suite controller run"


def report_line(verdict: Any, file_count: int, enforcing: bool) -> str:
    """The one line a CI log is grepped for. Keep this format stable."""
    return (
        f"{REPORT_PREFIX} "
        f"declared={len(verdict.declared)} "
        f"hit={len(verdict.hit & verdict.declared)} "
        f"uncovered={len(verdict.uncovered)} "
        f"floor={verdict.floor} "
        f"files={file_count} "
        f"enforcement={'on' if enforcing else 'off'}"
    )


def _emit(config: Any, line: str) -> None:
    reporter = _terminalreporter(config)
    if reporter is not None and hasattr(reporter, "write_line"):
        reporter.write_line(line)
    else:  # pragma: no cover - no terminal reporter (e.g. -p no:terminal)
        print(line)


def _redden(session: Any, message: str) -> None:
    """Make the run fail from inside ``pytest_sessionfinish``.

    ``_pytest.main.wrap_session`` calls this hook and only afterwards returns
    ``session.exitstatus``, so assigning here is what actually changes the
    process exit code. An already-failing run keeps its own status.
    """
    session.exitstatus = getattr(session, "exitstatus", 0) or 1
    _emit(session.config, f"{FAIL_PREFIX} {message} (exit status = {session.exitstatus})")


def finish_route_coverage(
    session: Any,
    *,
    declared: set[str] | None = None,
    min_declared: int | None = None,
) -> Any | None:
    """Print the reading and, when enforcing, gate on it. Returns the Verdict.

    ``declared`` is an injection point for this plugin's own tests, which must
    not import ``main`` (and must not depend on the real service's route
    surface). Production always passes ``None`` and the real enumeration runs.
    """
    config = session.config
    if not is_controller(config):
        return None

    enforcing, reason = enforcement_decision(session)

    try:
        arc = load_assertion_module()
        hit, files = arc.merge_hits(run_dir(config.rootpath))
        surface = arc.declared_routes() if declared is None else set(declared)
        grandfathered, floor = arc.load_allowlist(arc.DEFAULT_ALLOWLIST)
        # ``min_declared`` is threaded through for this plugin's own tests, whose
        # synthetic surfaces are two routes wide and would otherwise trip the
        # collapsed-enumeration check. Production passes None and gets the real
        # floor — the check exists because a `declared` set that collapses to a
        # handful makes "everything is covered" vacuous.
        verdict = arc.evaluate(
            surface,
            hit,
            grandfathered,
            floor,
            **({} if min_declared is None else {"min_declared": min_declared}),
        )
    except Exception as exc:
        _emit(
            config,
            f"{REPORT_PREFIX} declared=? hit=? uncovered=? floor=? files=? "
            f"enforcement={'on' if enforcing else 'off'} "
            f"error={type(exc).__name__}: {exc}",
        )
        if enforcing:
            # Asked to guard and unable to. That is a failure, not a skip.
            _redden(session, f"route coverage could not be computed: {exc!r}")
        return None

    _emit(config, report_line(verdict, len(files), enforcing))

    # ⛔ THE MERGE IS AUDITED BEFORE ANY ROUTE IS BLAMED FOR IT. See
    # ``merge_integrity``: with the allowlist at zero entries, a worker whose hit
    # file never landed is indistinguishable — in the verdict — from a route
    # nobody tested. Reporting the instrument fault first, and returning without
    # listing offenders, is what stops the next operator from "fixing" it by
    # re-grandfathering routes that are actually covered.
    complaints = merge_integrity(config, run_dir(config.rootpath))
    if complaints:
        for complaint in complaints:
            _emit(config, f"{FAIL_PREFIX} {complaint}")
        if enforcing:
            _redden(
                session,
                "the route-coverage merge is incomplete, so the reading above is "
                "not a verdict about the routes",
            )
        else:
            _emit(config, f"{REPORT_PREFIX} enforcement=off because {reason}")
        return verdict

    # Only the NON-grandfathered offenders get a line each. While the allowlist is
    # the placeholder these are the same set, so the seeding run still publishes
    # every offender; once it is seeded, a steady-state run prints only what is
    # new instead of re-listing the whole floor on every local `pytest`.
    listed = sorted(verdict.unexpected)
    for entry in listed[:MAX_LISTED_UNCOVERED]:
        _emit(config, f"{UNCOVERED_PREFIX} {entry}")
    if len(listed) > MAX_LISTED_UNCOVERED:
        omitted = len(listed) - MAX_LISTED_UNCOVERED
        _emit(config, f"{UNCOVERED_PREFIX} ... {omitted} more not listed")

    if not enforcing:
        _emit(config, f"{REPORT_PREFIX} enforcement=off because {reason}")
        return verdict

    if not verdict.ok:
        for error in verdict.errors:
            _emit(config, f"{FAIL_PREFIX} {error}")
        _redden(session, "the route-coverage gate failed")
    return verdict


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Write this process's hits; on the controller, also merge everyone's."""
    config = session.config
    directory = run_dir(config.rootpath)
    wid = worker_id(config)

    recorded = sorted(hits())
    _write(
        directory / f"hits-{wid}.json",
        {
            "worker": wid,
            "hits": [{"method": m, "path": p} for m, p in recorded],
        },
    )

    if not is_controller(config):
        return

    # Controller: xdist has already shut the workers down, so their files exist.
    merged, files = merge_dir(directory)
    _write(
        directory / "merged.json",
        {
            "files": files,
            "hit_count": len(merged),
            "hits": [{"method": m, "path": p} for m, p in sorted(merged)],
        },
    )
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"[route-coverage] merged {len(merged)} exercised routes from "
            f"{len(files)} file(s) {files} -> {directory / 'merged.json'}"
        )

    # Report the reading, and gate on it when every enforcement condition holds.
    finish_route_coverage(session)
