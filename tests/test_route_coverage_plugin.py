"""Unit tests for the route-coverage instrument (merge + assertion + recorder).

These run on synthetic inputs. They do NOT need the live suite, a database, or a
ten-minute full run — which is the point: the instrument's own logic has to be
checkable independently of the thing it measures, or "it reported zero" is
unfalsifiable.

The load-bearing case is ``test_a_run_below_the_floor_fails``. An allowlist that
only fails when offenders INCREASE is a rug: the first broken recorder produces
zero offenders and a green gate.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "_assert_route_coverage", REPO_ROOT / "scripts" / "assert_route_coverage.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


arc = _load_script()


# --------------------------------------------------------------------------- #
# Merge (the xdist half)
# --------------------------------------------------------------------------- #
def _write_worker(directory: pathlib.Path, worker: str, pairs: list[tuple[str, str]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"hits-{worker}.json").write_text(
        json.dumps({"worker": worker, "hits": [{"method": m, "path": p} for m, p in pairs]})
    )


def test_merge_unions_every_worker_file(tmp_path):
    _write_worker(tmp_path, "gw0", [("GET", "/requirements"), ("POST", "/requirements")])
    _write_worker(tmp_path, "gw1", [("GET", "/principles"), ("GET", "/requirements")])

    hits, files = arc.merge_hits(tmp_path)

    assert files == ["hits-gw0.json", "hits-gw1.json"]
    assert hits == {"GET /requirements", "POST /requirements", "GET /principles"}


def test_merge_of_an_empty_directory_is_empty_not_an_error(tmp_path):
    assert arc.merge_hits(tmp_path) == (set(), [])
    assert arc.merge_hits(tmp_path / "does-not-exist") == (set(), [])


def test_merge_survives_one_corrupt_worker_file(tmp_path):
    """A truncated file from a killed worker must not lose the healthy ones."""
    _write_worker(tmp_path, "gw0", [("GET", "/requirements")])
    (tmp_path / "hits-gw1.json").write_text('{"hits": [{"method": "GET",')

    hits, files = arc.merge_hits(tmp_path)
    assert hits == {"GET /requirements"}
    assert files == ["hits-gw0.json"]


def test_head_and_options_are_not_counted_as_hits(tmp_path):
    """Starlette synthesises HEAD for every GET; nobody writes a test for it."""
    _write_worker(tmp_path, "gw0", [("HEAD", "/requirements"), ("GET", "/requirements")])
    hits, _ = arc.merge_hits(tmp_path)
    assert hits == {"GET /requirements"}


# --------------------------------------------------------------------------- #
# Declared-route enumeration
# --------------------------------------------------------------------------- #
class _FakeAPIRoute:
    """Stands in for a route declared directly on the app (has ``dependant``)."""

    dependant = object()

    def __init__(self, path, methods):
        self.path = path
        self.methods = set(methods)


class _NotAnAPIRoute:
    """A plain starlette Route: /openapi.json, /docs, /redoc. No ``dependant``."""

    path = "/openapi.json"
    methods = {"GET"}


class _FakeContext:
    """An _EffectiveRouteContext: PREFIXED path + the unprefixed original."""

    def __init__(self, path, methods, original_path):
        self.path = path
        self.methods = set(methods)
        self.original_route = _FakeAPIRoute(original_path, methods)


class _FakeIncludedRouter:
    def __init__(self, contexts):
        self._contexts = contexts

    def effective_route_contexts(self):
        return iter(self._contexts)


def _app(routes):
    return type("App", (), {"routes": routes})()


def test_only_fastapi_routes_count_as_declared():
    app = _app(
        [
            _FakeAPIRoute("/", {"GET", "HEAD"}),
            _NotAnAPIRoute(),
        ]
    )
    assert arc.routes_of(app) == {"GET /"}


def test_included_routers_are_flattened_at_their_prefixed_path():
    """The 183-of-195 trap, pinned.

    FastAPI 0.137 keeps an included router in ``app.routes`` as an
    ``_IncludedRouter``. A walk that only looks at ``isinstance(r, APIRoute)``
    finds 1 route out of 195 here, and a walk that reads
    ``context.original_route.path`` records ``/{rid}`` for what is really
    ``/requirements/{rid}``.
    """
    app = _app(
        [
            _FakeAPIRoute("/", {"GET"}),
            _FakeIncludedRouter(
                [
                    _FakeContext("/requirements", {"GET", "POST"}, original_path=""),
                    _FakeContext("/requirements/{rid}", {"PUT"}, original_path="/{rid}"),
                ]
            ),
        ]
    )

    declared = arc.routes_of(app)
    assert declared == {
        "GET /",
        "GET /requirements",
        "POST /requirements",
        "PUT /requirements/{rid}",
    }
    # The unprefixed spellings must not appear at all.
    assert not any(d.endswith(" /{rid}") or d.endswith(" ") for d in declared)


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
def test_a_route_that_was_hit_is_not_reported():
    verdict = arc.evaluate(
        declared={"GET /a", "GET /b"},
        hit={"GET /a", "GET /b"},
        grandfathered=set(),
        floor=0,
        min_declared=0,  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    )
    assert verdict.uncovered == set()
    assert verdict.ok, verdict.errors


def test_a_route_never_hit_is_reported():
    verdict = arc.evaluate(
        declared={"GET /a", "POST /principles/{id}/evaluate"},
        hit={"GET /a"},
        grandfathered=set(),
        floor=0,
        min_declared=0,  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    )
    assert verdict.uncovered == {"POST /principles/{id}/evaluate"}
    assert not verdict.ok
    assert any("POST /principles/{id}/evaluate" in e for e in verdict.errors)


def test_a_route_named_in_a_test_file_but_never_called_is_still_uncovered():
    """The refutation that killed the previous grep-based measurement.

    ``POST /principles/{id}/evaluate`` "matched" a grep only because
    ``tests/test_pool_holder_instrument.py:80`` uses that string as a label for a
    fake pool holder. The instrument's input is a set of RUNTIME hits, so a
    string in a source file cannot enter it at all — this test pins that the hit
    set is the only thing consulted.
    """
    declared = {"POST /principles/{id}/evaluate"}
    verdict = arc.evaluate(
        declared=declared, hit={"GET /other"}, grandfathered=set(), floor=0, min_declared=0
    )  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    assert verdict.uncovered == declared


def test_a_grandfathered_route_is_tolerated():
    verdict = arc.evaluate(
        declared={"GET /a", "GET /legacy"},
        hit={"GET /a"},
        grandfathered={"GET /legacy"},
        floor=1,
        min_declared=0,  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    )
    assert verdict.uncovered == {"GET /legacy"}
    assert verdict.unexpected == set()
    assert verdict.ok, verdict.errors


def test_a_new_uncovered_route_fails_even_when_others_are_grandfathered():
    verdict = arc.evaluate(
        declared={"GET /legacy", "GET /brand-new"},
        hit=set(),
        grandfathered={"GET /legacy"},
        floor=1,
        min_declared=0,  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    )
    assert verdict.unexpected == {"GET /brand-new"}
    assert not verdict.ok


def test_a_run_below_the_floor_fails():
    """The ratchet's vacuity leg. Fewer offenders than the floor is a defect."""
    verdict = arc.evaluate(
        declared={"GET /a", "GET /legacy"},
        hit={"GET /a", "GET /legacy"},
        grandfathered={"GET /legacy"},
        floor=1,
        min_declared=0,  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    )
    assert verdict.uncovered == set()
    assert verdict.unexpected == set()
    assert not verdict.ok, "0 offenders against a floor of 1 must FAIL, not pass"
    assert any("floor" in e for e in verdict.errors)


def test_the_floor_leg_is_not_vacuous_itself():
    """Control: at exactly the floor, and above it, the floor leg stays quiet."""
    at_floor = arc.evaluate(
        {"GET /a", "GET /l"}, {"GET /a"}, {"GET /l"}, floor=1, min_declared=0
    )  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    assert at_floor.ok, at_floor.errors

    above = arc.evaluate(
        {"GET /a", "GET /l", "GET /m"}, {"GET /a"}, {"GET /l", "GET /m"}, floor=1, min_declared=0
    )  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    assert above.ok, above.errors


def test_an_empty_declared_set_fails_rather_than_passing():
    verdict = arc.evaluate(
        declared=set(), hit={"GET /a"}, grandfathered=set(), floor=0, min_declared=0
    )  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    assert not verdict.ok
    assert any("DECLARED" in e for e in verdict.errors)


def test_an_empty_hit_set_fails_rather_than_reporting_everything():
    verdict = arc.evaluate(
        declared={"GET /a"}, hit=set(), grandfathered=set(), floor=0, min_declared=0
    )  # min_declared=0: this fixture is a 1-2 route synthetic, not a real app
    assert not verdict.ok
    assert any("HIT" in e for e in verdict.errors)


def test_the_shipped_allowlist_matches_its_own_floor():
    """The file on disk must be internally consistent, or the gate is nonsense."""
    grandfathered, floor = arc.load_allowlist(arc.DEFAULT_ALLOWLIST)
    assert arc.DEFAULT_ALLOWLIST.is_file(), "the grandfathered allowlist is missing"
    assert floor == len(grandfathered), (
        f"floor={floor} but the allowlist lists {len(grandfathered)} routes. The "
        f"floor is the count of grandfathered offenders; they move together."
    )


# --------------------------------------------------------------------------- #
# The recorder itself
# --------------------------------------------------------------------------- #
async def test_the_recorder_records_a_real_request():
    """Prove the mechanism, not just the arithmetic.

    Builds a throwaway FastAPI app and drives it over the same
    ``httpx.ASGITransport`` the integration suite uses. The route is declared
    AFTER the plugin patched ``APIRoute.get_route_handler``, which is the whole
    premise: the patch is on the class, so it wraps every route constructed from
    then on.
    """
    import httpx
    from fastapi import FastAPI

    from tests.plugins import route_coverage

    probe = "/__route_coverage_probe__/{thing}"
    app = FastAPI()

    @app.get(probe)
    async def _probe(thing: str):  # pragma: no cover - exercised via HTTP below
        return {"thing": thing}

    assert ("GET", probe) not in route_coverage.hits()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
            response = await client.get("/__route_coverage_probe__/abc")
        assert response.status_code == 200

        recorded = route_coverage.hits()
        assert ("GET", probe) in recorded, (
            "the recorder did not see a request it definitely served — the "
            f"APIRoute patch is not installed. Recorded: {sorted(recorded)[:5]}"
        )
        # The TEMPLATE, not the concrete URL. Recording "/…/abc" would make every
        # parameterised route look uncovered forever.
        assert ("GET", "/__route_coverage_probe__/abc") not in recorded
    finally:
        # Do not leak the probe into the run's merged hit file.
        route_coverage._HITS.discard(("GET", probe))


async def test_the_recorder_records_the_PREFIXED_path_of_an_included_router():
    """The defect this instrument nearly shipped with.

    183 of this service's 195 routes live on a router included under a prefix.
    FastAPI 0.137 sets ``scope["route"]`` to the route as declared on the
    SUB-ROUTER, so a recorder reading ``scope["route"].path`` logs ``/{rid}``
    for what is really ``/requirements/{rid}``. Every such route then looks
    uncovered forever, while the dozen prefix-free ones (``/health*``) make the
    instrument look like it works.

    So: a router with a real prefix, driven over real HTTP.
    """
    import httpx
    from fastapi import APIRouter, FastAPI

    from tests.plugins import route_coverage

    prefix = "/__rc_probe__"
    router = APIRouter()

    @router.get("/{rid}")
    async def _one(rid: str):  # pragma: no cover - exercised via HTTP below
        return {"rid": rid}

    app = FastAPI()
    app.include_router(router, prefix=prefix)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
            assert (await client.get(f"{prefix}/xyz")).status_code == 200

        recorded = route_coverage.hits()
        assert ("GET", f"{prefix}/{{rid}}") in recorded, (
            "the recorder logged the UNPREFIXED sub-router path. Read the "
            "_EffectiveRouteContext, not scope['route']. "
            f"Recorded: {sorted(p for _, p in recorded if 'rc_probe' in p or p == '/{rid}')}"
        )
        assert ("GET", "/{rid}") not in recorded, (
            "the unprefixed spelling was recorded — this is the 183-of-195 bug"
        )
    finally:
        route_coverage._HITS.discard(("GET", f"{prefix}/{{rid}}"))
        route_coverage._HITS.discard(("GET", "/{rid}"))


def test_the_controller_is_distinguished_from_a_worker():
    from tests.plugins import route_coverage

    class _Controller:
        """A serial run / the xdist controller: no ``workerinput`` at all."""

    class _Worker:
        workerinput = {"workerid": "gw3"}

    controller = _Controller()
    assert route_coverage.is_controller(controller)
    assert route_coverage.worker_id(controller) == "controller"

    worker = _Worker()
    assert not route_coverage.is_controller(worker)
    assert route_coverage.worker_id(worker) == "gw3"


@pytest.mark.parametrize("name", ["hits-gw0.json", "hits-controller.json"])
def test_the_plugin_and_the_script_agree_on_the_filename_shape(tmp_path, name):
    """Both halves glob ``hits-*.json``. A rename in one silently empties the other."""
    from tests.plugins import route_coverage

    (tmp_path / name).write_text(json.dumps({"hits": [{"method": "GET", "path": "/x"}]}))
    plugin_hits, plugin_files = route_coverage.merge_dir(tmp_path)
    script_hits, script_files = arc.merge_hits(tmp_path)

    assert plugin_files == script_files == [name]
    assert {arc.key(m, p) for m, p in plugin_hits} == script_hits


# --------------------------------------------------------------------------- #
# Reporting + enforcement (the half that makes the instrument able to FAIL)
#
# The recorder and the assertion both existed and both worked. The instrument was
# still inert, because nothing ran the assertion and nothing could turn it into a
# non-zero exit code. These tests are about that seam, so they exercise the
# plugin's `pytest_sessionfinish` path directly against a fake session — there is
# no `pytester` fixture anywhere in this repo, and spawning a nested pytest to
# check an exit code would cost more than it proves.
# --------------------------------------------------------------------------- #
class _FakeOption:
    keyword = ""
    markexpr = ""
    deselect = None
    lf = False
    stepwise = False


class _FakeReporter:
    def __init__(self):
        self.lines: list[str] = []
        self.stats: dict[str, list] = {}

    def write_line(self, line, **_kwargs):
        self.lines.append(line)


class _FakePluginManager:
    def __init__(self, reporter):
        self._reporter = reporter

    def get_plugin(self, name):
        return self._reporter if name == "terminalreporter" else None


class _FakeConfig:
    def __init__(self, rootpath, args=None, **options):
        self.rootpath = rootpath
        self.args = [str(rootpath)] if args is None else list(args)
        self.option = _FakeOption()
        for name, value in options.items():
            setattr(self.option, name, value)
        self.reporter = _FakeReporter()
        self.pluginmanager = _FakePluginManager(self.reporter)


class _FakeWorkerConfig(_FakeConfig):
    """xdist marks a worker by injecting ``workerinput``, and nothing else does."""

    workerinput = {"workerid": "gw2"}


class _FakeSession:
    def __init__(self, config):
        self.config = config
        self.exitstatus = 0
        self.shouldstop = False
        self.shouldfail = False


def _plugin():
    from tests.plugins import route_coverage

    return route_coverage


def _prepare(tmp_path, monkeypatch, *, hits, allowlist, enforce, **config_options):
    """A fake whole-suite controller run with a seeded run-dir and allowlist."""
    plugin = _plugin()

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_worker(run_dir, "controller", hits)
    monkeypatch.setenv("ROUTE_COVERAGE_DIR", str(run_dir))

    allowlist_path = tmp_path / "allowlist.json"
    allowlist_path.write_text(json.dumps(allowlist))
    monkeypatch.setattr(plugin.load_assertion_module(), "DEFAULT_ALLOWLIST", allowlist_path)

    if enforce:
        monkeypatch.setenv(plugin.ENFORCE_ENV, "1")
    else:
        # ⛔ setenv("0"), NOT delenv. Enforcement now DEFAULTS ON wherever `CI` is
        # set, because no CI job here can inject an env var (the shared archetype
        # passes none), so an opt-in-only gate could never fire. Unsetting the
        # variable therefore no longer means "off" — on a runner it means ON, and
        # these synthetic two-route sessions would flip to enforcing and trip the
        # collapsed-enumeration floor. Measured: this exact difference passed
        # locally and failed in CI.
        monkeypatch.setenv(plugin.ENFORCE_ENV, "0")

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    return _FakeSession(_FakeConfig(root, **config_options))


def test_the_plugin_reuses_the_script_rather_than_reimplementing_it():
    """If the plugin ever grows its own merge/evaluate, they will drift apart."""
    plugin = _plugin()
    module = plugin.load_assertion_module()

    assert module.__file__ == str(REPO_ROOT / "scripts" / "assert_route_coverage.py")
    for name in ("merge_hits", "declared_routes", "load_allowlist", "evaluate"):
        assert callable(getattr(module, name)), f"the plugin depends on {name}"
    assert plugin.load_assertion_module() is module, "the module load must be cached"


@pytest.mark.parametrize(
    ("options", "args", "session_attrs", "expected_in_reason"),
    [
        ({"keyword": "test_thing"}, None, {}, "-k"),
        ({"markexpr": "not integration"}, None, {}, "-m"),
        ({"deselect": ["tests/test_a.py::test_b"]}, None, {}, "--deselect"),
        ({"lf": True}, None, {}, "--last-failed"),
        ({"stepwise": True}, None, {}, "--last-failed"),
        ({}, ["tests/test_one_file.py"], {}, "whole tests/ tree"),
        ({}, ["tests/test_a.py::test_b"], {}, "whole tests/ tree"),
        ({}, None, {"shouldstop": True}, "stopped early"),
        ({}, None, {"shouldfail": "maxfail"}, "stopped early"),
    ],
)
def test_enforcement_stays_off_for_a_filtered_or_partial_run(
    tmp_path, monkeypatch, options, args, session_attrs, expected_in_reason
):
    """A developer running one file, or a `-k` slice, must never eat this gate.

    Every one of these runs collects a fraction of the suite, so nearly every
    route would look uncovered. Firing here would not be a strict gate, it would
    be a broken one — and it would train everyone to set the opt-out.
    """
    plugin = _plugin()
    monkeypatch.setenv(plugin.ENFORCE_ENV, "1")

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    session = _FakeSession(_FakeConfig(root, args=args, **options))
    for name, value in session_attrs.items():
        setattr(session, name, value)

    enforcing, reason = plugin.enforcement_decision(session)

    assert not enforcing, f"enforcement fired on a partial run (reason given: {reason})"
    assert expected_in_reason in reason, reason


def test_the_gate_is_armed_by_CI_and_can_be_switched_off_explicitly(tmp_path, monkeypatch):
    """Three states, because the env var alone could never arm this gate.

    ⛔ THIS TEST USED TO ASSERT THE OPPOSITE — "the env var is required,
    structural checks alone do not arm the gate". That contract made the gate
    unable to fail the only build that matters: `.github/workflows/ci-cd.yaml`
    delegates to a shared reusable workflow whose 42 inputs carry no `env:` and
    no pytest-args passthrough, and editing that archetype is out of this repo's
    lane. An opt-in-only gate was therefore a guard that CANNOT RUN.

    So the contract is now: ON by default under `CI`, OFF outside it unless the
    developer opts in, and OFF whenever the variable explicitly says so — the
    escape hatch, which is what the synthetic sessions elsewhere in this file
    now use, because "unset" no longer means "off" on a runner.
    """
    plugin = _plugin()
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)

    def decide():
        return plugin.enforcement_decision(_FakeSession(_FakeConfig(root)))

    # 1. A developer's machine, no opt-in: OFF, and the reason names both halves.
    monkeypatch.delenv(plugin.ENFORCE_ENV, raising=False)
    monkeypatch.delenv("CI", raising=False)
    enforcing, reason = decide()
    assert not enforcing
    assert plugin.ENFORCE_ENV in reason and "not CI" in reason, reason

    # 2. A CI runner, no opt-in: ON. This is the case the old contract forbade.
    monkeypatch.setenv("CI", "true")
    enforcing, reason = decide()
    assert enforcing, f"the gate must arm itself on CI; got: {reason}"

    # 3. A CI runner with an explicit disable: OFF. Without this escape hatch the
    #    gate could not be switched off without a code change either.
    monkeypatch.setenv(plugin.ENFORCE_ENV, "0")
    enforcing, reason = decide()
    assert not enforcing
    assert "disables" in reason, reason


def test_enforcement_stays_off_on_an_xdist_worker(tmp_path, monkeypatch):
    """A worker sees a slice of the suite AND a slice of the hits. Both lie."""
    plugin = _plugin()
    monkeypatch.setenv(plugin.ENFORCE_ENV, "1")

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    enforcing, reason = plugin.enforcement_decision(_FakeSession(_FakeWorkerConfig(root)))

    assert not enforcing
    assert "worker" in reason


def test_enforcement_stays_off_when_tests_were_deselected(tmp_path, monkeypatch):
    """Deselection can come from a marker in a conftest, not just from the CLI."""
    plugin = _plugin()
    monkeypatch.setenv(plugin.ENFORCE_ENV, "1")

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    config = _FakeConfig(root)
    config.reporter.stats["deselected"] = [object(), object()]

    enforcing, reason = plugin.enforcement_decision(_FakeSession(config))

    assert not enforcing
    assert "deselected" in reason


def test_a_whole_suite_controller_run_with_the_opt_in_does_enforce(tmp_path, monkeypatch):
    """Control. If this ever goes False, every test above passes vacuously."""
    plugin = _plugin()
    monkeypatch.setenv(plugin.ENFORCE_ENV, "1")

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    for args in (None, [str(root / "tests")], ["tests"], []):
        session = _FakeSession(_FakeConfig(root, args=args))
        enforcing, reason = plugin.enforcement_decision(session)
        assert enforcing, f"args={args!r} should enforce but did not: {reason}"


def test_report_mode_prints_the_reading_and_fails_nothing(tmp_path, monkeypatch):
    """The line the first CI run is grepped for, before any allowlist exists.

    Format is pinned here on purpose: it is how the floor gets seeded, and a
    silent rename would leave the next reader grepping for nothing.
    """
    plugin = _plugin()
    session = _prepare(
        tmp_path,
        monkeypatch,
        hits=[("GET", "/a")],
        allowlist={"floor": 0, "uncovered": []},
        enforce=False,
    )

    verdict = plugin.finish_route_coverage(session, declared={"GET /a", "GET /b"})

    lines = session.config.reporter.lines
    expected = "ROUTE_COVERAGE_READING declared=2 hit=1 uncovered=1 floor=0 files=1 enforcement=off"
    assert expected in lines, lines
    assert "ROUTE_COVERAGE_UNCOVERED GET /b" in lines, lines
    # The verdict is NOT ok — an unexpected offender — and report mode still
    # leaves the run green. That is the whole point of report mode.
    assert not verdict.ok
    assert session.exitstatus == 0
    assert any("enforcement=off because" in line for line in lines), lines


def test_report_mode_does_not_re_list_the_grandfathered_floor(tmp_path, monkeypatch):
    """Steady state stays quiet, so a NEW offender is visible instead of buried.

    Once the allowlist is seeded, re-printing the whole floor on every local
    ``pytest`` is ~200 lines of noise that trains people to scroll past the one
    line that matters.
    """
    plugin = _plugin()
    session = _prepare(
        tmp_path,
        monkeypatch,
        hits=[("GET", "/a")],
        allowlist={"floor": 1, "uncovered": ["GET /legacy"]},
        enforce=False,
    )

    plugin.finish_route_coverage(session, declared={"GET /a", "GET /legacy", "GET /new"})

    lines = session.config.reporter.lines
    listed = [line for line in lines if line.startswith("ROUTE_COVERAGE_UNCOVERED")]
    assert listed == ["ROUTE_COVERAGE_UNCOVERED GET /new"], listed
    # ...but the COUNT in the reading still includes the grandfathered one, or the
    # floor could never be checked against it.
    reading = next(line for line in lines if line.startswith("ROUTE_COVERAGE_READING "))
    assert "uncovered=2 floor=1" in reading, reading


def test_enforcement_reddens_the_run_for_an_ungrandfathered_route(tmp_path, monkeypatch):
    """The gate's forward leg: a route shipped with no test fails the build.

    ``session.exitstatus`` is the assertion, not a printed message.
    ``_pytest.main.wrap_session`` returns that attribute AFTER calling
    ``pytest_sessionfinish``, so this is the mutation that actually reddens CI.
    """
    plugin = _plugin()
    session = _prepare(
        tmp_path,
        monkeypatch,
        hits=[("GET", "/a")],
        allowlist={"floor": 0, "uncovered": []},
        enforce=True,
    )

    assert session.exitstatus == 0
    plugin.finish_route_coverage(session, declared={"GET /a", "GET /brand-new"})

    assert session.exitstatus != 0, (
        "an uncovered, ungrandfathered route left the session green — the gate is "
        "still inert, which is exactly the defect it was written to end"
    )
    assert any("GET /brand-new" in line for line in session.config.reporter.lines)


def test_enforcement_reddens_the_run_when_offenders_are_BELOW_the_floor(tmp_path, monkeypatch):
    """The ratchet's vacuity leg, end to end.

    This is the assertion that catches a recorder which stopped recording. Here
    every declared route is "hit", so `unexpected` is empty and the forward leg
    is silent — the ONLY thing that can fail is the floor comparison. A run that
    finds 0 offenders against a floor of 1 must be red.
    """
    plugin = _plugin()
    session = _prepare(
        tmp_path,
        monkeypatch,
        hits=[("GET", "/a"), ("GET", "/legacy")],
        allowlist={"floor": 1, "uncovered": ["GET /legacy"]},
        enforce=True,
    )

    verdict = plugin.finish_route_coverage(session, declared={"GET /a", "GET /legacy"})

    assert verdict.unexpected == set(), "the forward leg must be silent, or this proves nothing"
    assert session.exitstatus != 0, "0 offenders against a floor of 1 must FAIL, not pass"
    assert any("floor" in line for line in session.config.reporter.lines)


def test_enforcement_leaves_a_clean_run_green(tmp_path, monkeypatch):
    """Control for both legs above: at the floor, with the gate armed, exit 0."""
    plugin = _plugin()
    session = _prepare(
        tmp_path,
        monkeypatch,
        hits=[("GET", "/a")],
        allowlist={"floor": 1, "uncovered": ["GET /legacy"]},
        enforce=True,
    )

    # min_declared=0: a 2-route synthetic surface, not a real app enumeration.
    verdict = plugin.finish_route_coverage(
        session, declared={"GET /a", "GET /legacy"}, min_declared=0
    )

    assert verdict.ok, verdict.errors
    assert session.exitstatus == 0
    assert any("enforcement=on" in line for line in session.config.reporter.lines)


def test_enforcement_reddens_the_run_when_it_cannot_compute_at_all(tmp_path, monkeypatch):
    """A guard asked to run and unable to is a failure, not a skip.

    Enforcement is opt-in, so reaching here means someone asked for the gate. If
    the route enumeration blows up, staying green would be the same "silently
    declined to run" failure this repo keeps shipping.
    """
    plugin = _plugin()
    session = _prepare(
        tmp_path,
        monkeypatch,
        hits=[("GET", "/a")],
        allowlist={"floor": 0, "uncovered": []},
        enforce=True,
    )
    monkeypatch.setattr(
        plugin.load_assertion_module(),
        "declared_routes",
        lambda: (_ for _ in ()).throw(ImportError("no main")),
    )

    assert plugin.finish_route_coverage(session) is None
    assert session.exitstatus != 0
    assert any("could not be computed" in line for line in session.config.reporter.lines)


def test_the_same_failure_stays_green_in_report_mode(tmp_path, monkeypatch):
    """Control for the test above: without the opt-in, an error is only reported."""
    plugin = _plugin()
    session = _prepare(
        tmp_path,
        monkeypatch,
        hits=[("GET", "/a")],
        allowlist={"floor": 0, "uncovered": []},
        enforce=False,
    )
    monkeypatch.setattr(
        plugin.load_assertion_module(),
        "declared_routes",
        lambda: (_ for _ in ()).throw(ImportError("no main")),
    )

    assert plugin.finish_route_coverage(session) is None
    assert session.exitstatus == 0
    assert any("error=ImportError" in line for line in session.config.reporter.lines)


def test_addopts_registers_the_recorder_early_enough():
    """The defect that made the whole instrument inert, pinned.

    The recorder installs at import time and only ``-p`` is imported during
    ``Config._preparse`` — early enough to wrap ``APIRoute.get_route_handler``
    before ``main`` builds any route. Without this the suite writes no
    ``hits-*.json`` at all and every reading is empty for the wrong reason.
    """
    ini = _pytest_ini()
    assert "-p tests.plugins.route_coverage" in ini.get("addopts", ""), (
        f"addopts no longer registers the route-coverage recorder: {ini.get('addopts')!r}"
    )


def test_pythonpath_still_makes_the_plugin_importable_at_preparse():
    """The coupling that decides whether pytest starts AT ALL.

    ``-p`` plugins are imported in ``Config._preparse`` via
    ``consider_preparse()``. The only thing that puts the rootdir on ``sys.path``
    before that point is ``_configure_python_path()``, i.e. this ini key — the
    rootdir insertion that comes from importing ``tests/conftest.py`` happens in
    ``pytest_load_initial_conftests``, thirty lines later. Drop ``"."`` here and
    the ``-p`` above becomes ``ImportError: Error importing plugin
    "tests.plugins.route_coverage"`` and no test in this repo runs.

    ``"src"`` must stay first so nothing at the repo root shadows a service module.
    """
    pythonpath = _pytest_ini().get("pythonpath", [])
    assert "." in pythonpath, (
        f"pythonpath={pythonpath!r} no longer contains '.', so "
        "`-p tests.plugins.route_coverage` cannot be imported and pytest will "
        "refuse to start. These two settings move together."
    )
    assert pythonpath.index("src") < pythonpath.index("."), (
        f"pythonpath={pythonpath!r}: 'src' must precede '.' so the repo root "
        "cannot shadow a service module"
    )


def _pytest_ini() -> dict:
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["pytest"]["ini_options"]


def test_the_recorder_is_actually_registered_in_this_very_run(pytestconfig):
    """Not the config file — the LIVE session. The two can disagree.

    ``pyproject.toml`` saying ``-p …`` proves nothing if the run overrode
    ``addopts``, and a run with no recorder produces an empty hit set that reads
    as "every route is uncovered".
    """
    from fastapi.routing import APIRoute

    from tests.plugins import route_coverage

    registered = pytestconfig.pluginmanager.get_plugin("tests.plugins.route_coverage")
    assert registered is not None, (
        "the route-coverage plugin is not registered in this session; the "
        "recorder wrote nothing and no reading is possible"
    )
    assert getattr(APIRoute, route_coverage._PATCH_FLAG, False), (
        "APIRoute.get_route_handler is unpatched — the recorder is installed but inert"
    )


def test_the_allowlist_is_seeded_and_its_floor_matches_its_entries():
    """The allowlist must be a real reading, and `floor` must equal len(uncovered).

    ⛔ THIS REPLACED a test asserting enforcement was OFF while the allowlist was
    the `{floor: 0, uncovered: []}` placeholder. That test was right for the day
    it was written and became a liability the moment the placeholder was the
    thing to fix: it FAILED CI if anyone turned the gate on, so the guard could
    not be enabled without deleting its own test — and it skipped itself once
    seeded, converting into permanent dead weight rather than into a check.

    The property that matters after seeding is the RATCHET INVARIANT: `floor` is
    the count of `uncovered`. If they drift, the ratchet stops meaning what its
    docstring says — floor > len makes every run red for the wrong reason; floor
    < len lets an offender be added without lowering anything, which is how a
    ratchet becomes a rug. Covering a route means deleting its entry AND lowering
    `floor` by one in the same commit; this test makes "same commit" enforceable.
    """
    grandfathered, floor = arc.load_allowlist(arc.DEFAULT_ALLOWLIST)

    # ⛔ THIS ASSERTION USED TO BE ``floor > 0``. Retired 2026-09-05, deliberately.
    #
    # ``floor > 0`` was never the property that mattered — it was a PROXY for "the
    # allowlist is still the un-seeded placeholder", and that inference was only
    # valid while 0 was unreachable by any other route. It is reachable now: the
    # eleven remaining entries were covered by real tests and the floor was lowered
    # 11 -> 0 in the same commit (see the allowlist header). Keeping the proxy would
    # mean the suite DEMANDS the gate be left weaker than it can be, which is the
    # opposite of what this file is for.
    #
    # The placeholder it feared is now caught by the gate itself rather than by a
    # proxy: with ``uncovered: []`` every unexercised route is ``unexpected``, so a
    # never-seeded allowlist reds the build on the first full CI run (measured: a
    # filtered run through this same gate reported 167 unexpected). What is asserted
    # instead, below, is the thing the proxy stood in for — that the gate can still
    # FAIL. A guard that cannot fail is the actual liability.
    # ⚠️ Synthetic floor/grandfathered set, NOT this repo's. The question here is
    # "can `evaluate` still fail?", which is a property of the GATE and must hold
    # at any floor. Passing the repo's live `floor` instead would make the
    # fully-covered leg unsatisfiable the moment anyone legitimately grandfathers
    # a route again: a fully-hit synthetic run has 0 uncovered, and `0 < floor`
    # trips the ratchet's lower leg, so the assertion would fail claiming "a
    # fully-covered run must pass" and blame the run rather than the floor.
    declared = {f"GET /r{i}" for i in range(arc.MIN_DECLARED_ROUTES + 1)}
    all_hit = arc.evaluate(declared, set(declared), grandfathered=set(), floor=0)
    assert all_hit.ok, f"a fully-covered run must pass, got: {all_hit}"

    missed = sorted(declared)[0]
    one_missing = arc.evaluate(declared, set(declared) - {missed}, grandfathered=set(), floor=0)
    assert not one_missing.ok, (
        f"THE GATE CANNOT FAIL. On a synthetic set with no grandfathered entries "
        f"and floor 0, a run leaving {missed!r} unexercised was still accepted. "
        f"That is the placeholder failure mode this test exists to catch, and it "
        f"is a property of the gate, independent of this repo's floor "
        f"(currently {floor})."
    )

    assert len(grandfathered) == floor, (
        f"floor is {floor} but `uncovered` holds {len(grandfathered)} entries. They "
        f"must move together: delete an entry AND lower floor by the same amount."
    )


# --------------------------------------------------------------------------- #
# Merge integrity under xdist.
#
# ⛔ WHY THESE EXIST. The allowlist carries ZERO entries at floor 0, so
# `unexpected` must come out exactly empty. Under `-n N` the reading is a union
# of N+1 files written by N+1 processes. If one file never lands, the union is
# short by everything that worker exercised and the gate reddens naming routes
# that ARE tested — a failure that reads as a code defect and is not one. The
# operator's cheapest escape from that is to re-grandfather the routes, i.e. to
# disarm a working gate over an instrument fault. These pin the alternative:
# name the merge, not the routes.
# --------------------------------------------------------------------------- #
def test_a_serial_run_expects_no_worker_files():
    """`numprocesses` is absent outside xdist, and must not be read as "1"."""
    plugin = _plugin()
    assert plugin.expected_worker_files(_FakeConfig(pathlib.Path("/nowhere"))) == 0


@pytest.mark.parametrize(
    ("numprocesses", "expected"),
    [(2, 2), (8, 8), (0, 0), (-1, 0), ("2", 2), ("auto", 0), (None, 0)],
)
def test_the_expected_worker_count_is_read_from_numprocesses(numprocesses, expected):
    """xdist resolves `auto` to an int in its own `pytest_configure`.

    An unresolvable value falls back to 0 ("not an xdist run") on purpose: a
    wrong guess here would redden a SERIAL run for a worker nobody asked for.
    """
    plugin = _plugin()
    config = _FakeConfig(pathlib.Path("/nowhere"), numprocesses=numprocesses)
    assert plugin.expected_worker_files(config) == expected


def test_a_complete_two_worker_merge_raises_no_complaint(tmp_path):
    plugin = _plugin()
    _write_worker(tmp_path, "controller", [])
    _write_worker(tmp_path, "gw0", [("GET", "/a")])
    _write_worker(tmp_path, "gw1", [("GET", "/b")])

    config = _FakeConfig(tmp_path, numprocesses=2)
    assert plugin.merge_integrity(config, tmp_path) == []


def test_a_worker_that_never_wrote_its_file_is_named_as_a_short_merge(tmp_path):
    """The whole point: `gw1` died, so its routes look untested. Say so."""
    plugin = _plugin()
    _write_worker(tmp_path, "controller", [])
    _write_worker(tmp_path, "gw0", [("GET", "/a")])

    config = _FakeConfig(tmp_path, numprocesses=2)
    complaints = plugin.merge_integrity(config, tmp_path)

    assert len(complaints) == 1
    assert "asked for 2 xdist worker(s)" in complaints[0]
    assert "only 1 per-worker hit file(s)" in complaints[0]


def test_a_corrupt_worker_file_is_named_rather_than_silently_skipped(tmp_path):
    """`merge_dir` swallows an unparseable file. That must not stay silent."""
    plugin = _plugin()
    _write_worker(tmp_path, "controller", [])
    _write_worker(tmp_path, "gw0", [("GET", "/a")])
    (tmp_path / "hits-gw1.json").write_text("{ this is not json")

    config = _FakeConfig(tmp_path, numprocesses=2)
    complaints = plugin.merge_integrity(config, tmp_path)

    assert any("did not parse" in c and "hits-gw1.json" in c for c in complaints)


def test_a_missing_run_directory_is_a_complaint_not_an_empty_reading(tmp_path):
    plugin = _plugin()
    complaints = plugin.merge_integrity(_FakeConfig(tmp_path), tmp_path / "absent")

    assert len(complaints) == 1
    assert "does not exist" in complaints[0]


def test_a_short_merge_reddens_the_run_and_blames_the_merge_not_the_routes(tmp_path, monkeypatch):
    """The load-bearing one.

    Two workers were requested, one file landed, and `/b` — which the dead worker
    exercised — is therefore missing from the union. The run must fail, and the
    failure must name the MERGE. If it named `/b` instead, the next operator
    grandfathers a route that has a test and the gate is disarmed for good.
    """
    plugin = _plugin()
    session = _prepare(
        tmp_path,
        monkeypatch,
        hits=[],
        allowlist={"floor": 0, "uncovered": []},
        enforce=True,
        numprocesses=2,
    )
    _write_worker(tmp_path / "run", "gw0", [("GET", "/a")])

    plugin.finish_route_coverage(session, declared={"GET /a", "GET /b"}, min_declared=0)

    assert session.exitstatus != 0, "a short merge left the run green"
    lines = session.config.reporter.lines
    assert any("only 1 per-worker hit file(s)" in line for line in lines), lines
    assert any("merge is incomplete" in line for line in lines), lines
    assert not any(line.startswith(plugin.UNCOVERED_PREFIX) for line in lines), (
        "the merge was short, so `GET /b` is an artefact of the missing file. "
        "Listing it as an uncovered route is what teaches the operator to "
        "grandfather a covered route and disarm the gate."
    )


def test_a_complete_merge_still_lets_a_genuinely_uncovered_route_fail(tmp_path, monkeypatch):
    """Control for the test above — otherwise the merge check could hide the gate.

    Same shape, but both worker files land. `GET /b` is now genuinely untested,
    so the run must fail naming THE ROUTE.
    """
    plugin = _plugin()
    session = _prepare(
        tmp_path,
        monkeypatch,
        hits=[],
        allowlist={"floor": 0, "uncovered": []},
        enforce=True,
        numprocesses=2,
    )
    _write_worker(tmp_path / "run", "gw0", [("GET", "/a")])
    _write_worker(tmp_path / "run", "gw1", [])

    plugin.finish_route_coverage(session, declared={"GET /a", "GET /b"}, min_declared=0)

    assert session.exitstatus != 0
    lines = session.config.reporter.lines
    assert any(f"{plugin.UNCOVERED_PREFIX} GET /b" in line for line in lines), lines
    assert not any("merge is incomplete" in line for line in lines), lines
