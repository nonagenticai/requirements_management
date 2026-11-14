"""The integration suite must be able to go RED. This guard is why.

Three independent mechanisms had made ``tests/integration/`` incapable of
reporting a failure, and every one of them looked like a passing suite:

1. **Deselected.** ``pyproject.toml`` carried ``addopts = "-v -m 'not
   integration'"`` and nothing anywhere passed ``-m integration``. The 86 tests
   had never run in CI.
2. **Could not authenticate.** The client pointed at ``requirements.example.com``
   with a hardcoded licence token that the licence server rejects
   (``403 {"error": "license_invalid", "reason": "token_invalid"}``, measured
   2026-09-04).
3. **19 masking skips.** ``if r.status_code not in (200, 201):
   pytest.skip(...)`` — so the suite reported success precisely when the thing
   under test was broken.

Measured on ``main`` at ab51ac0, running the suite as it then stood:
**7 passed, 39 failed, 40 skipped**. The 7 passes were ``test_health.py``, whose
endpoints are auth-exempt. Not one of the other 79 had ever reached a business
endpoint, and because of (1) nobody ever saw the 39 failures.

This guard defends (1) and (3). (2) is structural — the suite now runs
in-process by default (``tests/integration/conftest.py``).

⛔ Do NOT "fix" a failure here by adding an escape. The escape list is pinned at
its measured size, so adding one fails the ratchet leg as well.
"""

import ast
import pathlib
import tomllib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"

# A skip may survive ONLY with an explicit inline escape naming a reason:
#     pytest.skip("...")  # integration-skip-ok: <reason>
# Measured 2026-09-04: ZERO. The floor below stops that from drifting upward
# quietly — a ratchet that can only be loosened is not a ratchet.
# Every spelling of "do not run this" that an adversarial review reached for:
# the plain call, the aliased import, the marks, and unittest's exception.
_SKIP_NAMES = {"skip", "skipif", "SkipTest"}

ESCAPE_MARKER = "integration-skip-ok:"
MAX_ESCAPES = 0

# Vacuity floor. It counts test functions EXAMINED, never violations found: a
# floor on violations is satisfied by zero, so it goes green when the walk is
# broken and sees nothing at all. 86 tests exist today; 80 leaves room for a
# little churn without letting the corpus quietly empty out.
MIN_TESTS_EXAMINED = 80


def _integration_files() -> list[pathlib.Path]:
    # rglob, not glob: a non-recursive walk misses a skip parked one
    # directory deeper, which an adversarial review used as a bypass.
    return sorted(INTEGRATION_DIR.rglob("*.py"))


def _walk() -> tuple[list[str], list[str], int]:
    """Return (violations, escapes, tests_examined) across tests/integration."""
    violations: list[str] = []
    escapes: list[str] = []
    tests_examined = 0

    for path in _integration_files():
        source = path.read_text()
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test_"
            ):
                tests_examined += 1
                continue

            # `raise unittest.SkipTest` (no parentheses) is a bare attribute, not
            # a Call, so it must be matched separately or it walks straight past.
            if isinstance(node, ast.Raise) and node.exc is not None:
                exc = node.exc
                name = (
                    exc.attr
                    if isinstance(exc, ast.Attribute)
                    else exc.id
                    if isinstance(exc, ast.Name)
                    else None
                )
                if name in _SKIP_NAMES:
                    where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                    line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                    (escapes if ESCAPE_MARKER in line else violations).append(where)
                    continue

            # `@pytest.mark.skip` WITHOUT parentheses is a bare Attribute — neither
            # a Call nor a Raise. An adversarial audit measured pytest reporting
            # `1 skipped` while this walk returned no violations at all.
            if isinstance(node, ast.Attribute) and node.attr in _SKIP_NAMES:
                where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                bucket = escapes if ESCAPE_MARKER in line else violations
                if where not in bucket:
                    bucket.append(where)
                continue

            if not isinstance(node, ast.Call):
                continue

            # pytest.skip(...) / skip(...) — match on the attribute or the bare
            # name, so `from pytest import skip` cannot slip past a name check.
            func = node.func
            # `pytest.skip(...)` / bare `skip(...)` (aliased import), and also
            # `pytest.mark.skip(...)` / `pytest.mark.skipif(...)`, which are
            # MARKS rather than calls to skip. An adversarial review defeated an
            # earlier version of this guard with a one-line module-level
            # `pytestmark = [..., pytest.mark.skipif(True, reason=...)]`.
            is_skip = (isinstance(func, ast.Attribute) and func.attr in _SKIP_NAMES) or (
                isinstance(func, ast.Name) and func.id in _SKIP_NAMES
            )
            if not is_skip:
                continue

            where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if ESCAPE_MARKER in line:
                escapes.append(where)
            else:
                violations.append(where)

    return violations, escapes, tests_examined


def test_no_masking_skips_in_the_integration_suite():
    """A skip converts real breakage into a green run. None may survive."""
    violations, _escapes, tests_examined = _walk()

    # Vacuity floor FIRST: if the walk found nothing to examine, an empty
    # violations list means the guard is broken, not that the suite is clean.
    assert tests_examined >= MIN_TESTS_EXAMINED, (
        f"guard walked only {tests_examined} integration tests (floor "
        f"{MIN_TESTS_EXAMINED}) — the walk is broken, so 'no violations' proves "
        f"nothing. Files seen: {[p.name for p in _integration_files()]}"
    )
    assert violations == [], (
        "pytest.skip() in tests/integration turns real breakage into a green "
        f"run. Assert on the status and body instead. Offenders: {violations}"
    )


def test_skip_escapes_stay_at_their_measured_floor():
    """The escape hatch is a ratchet, not a door."""
    _violations, escapes, _examined = _walk()
    assert len(escapes) <= MAX_ESCAPES, (
        f"{len(escapes)} escapes, ceiling {MAX_ESCAPES}. Fix the test rather "
        f"than annotating it: {escapes}"
    )


def test_the_guard_can_actually_see_a_skip():
    """Self-test: prove the AST walk detects the thing it exists to detect.

    Without this, a walk that silently matched nothing would satisfy the
    violations assertion above by finding zero of everything.
    """
    tree = ast.parse('import pytest\ndef test_x():\n    pytest.skip("boom")\n')
    found = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "skip"
    ]
    assert found, "the guard's own matcher cannot see pytest.skip — it proves nothing"

    aliased = ast.parse('from pytest import skip\ndef test_x():\n    skip("boom")\n')
    found_alias = [
        n
        for n in ast.walk(aliased)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "skip"
    ]
    assert found_alias, "an aliased `skip` import would slip past the guard"


def test_the_runtime_backstop_rewrites_a_skipped_report():
    """Self-test for layer 2, which otherwise has no test at all.

    The AST walk above has a self-test; the runtime backstop did not, and CI's
    zero-skip runs never exercise it — so a backstop that silently stopped
    working would look exactly like a healthy suite. This drives the real hook
    with a stubbed report.

    ⚠️ It asserts on `when="setup"` deliberately. The first version of the hook
    only inspected `when == "call"`, and a `@pytest.mark.skip` is decided during
    SETUP — so the bypass an adversarial audit planted produced exit 0 both
    serially and under `-n 2`.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_integration_conftest", INTEGRATION_DIR / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _Report:
        def __init__(self, nodeid, when, skipped, reason="Skipped: bypass"):
            self.nodeid = nodeid
            self.when = when
            self.skipped = skipped
            self.outcome = "skipped" if skipped else "passed"
            # Real shape of a skipped report's longrepr: (path, lineno, reason).
            self.longrepr = ("tests/integration/test_x.py", 1, reason) if skipped else None

    def drive(report):
        gen = module.pytest_runtest_makereport(item=None, call=None)
        next(gen)
        try:
            gen.send(report)
        except StopIteration:
            pass
        return report

    for when in ("setup", "call"):
        r = drive(_Report("tests/integration/test_x.py::test_y", when, True))
        assert r.outcome == "failed", (
            f"a skipped integration test at phase {when!r} was not rewritten to failed"
        )
        assert "SKIPPED in in-process mode" in str(r.longrepr)

    # Controls: it must NOT rewrite anything else, or it would fail honest runs.
    r = drive(_Report("tests/test_other.py::test_y", "call", True))
    assert r.outcome == "skipped", "a NON-integration skip was rewritten — false positive"

    r = drive(_Report("tests/integration/test_x.py::test_y", "call", False))
    assert r.outcome == "passed", "a passing test was rewritten"


def test_integration_marker_is_not_deselected():
    """`addopts` must not exclude the marker, or none of the above matters.

    This is mechanism (1) from the module docstring. A perfectly red-able suite
    that CI never selects is exactly as useful as no suite at all.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    addopts = pyproject["tool"]["pytest"]["ini_options"].get("addopts", "")
    assert "not integration" not in addopts.replace('"', "").replace("'", ""), (
        "pyproject addopts deselects the integration marker, so these 86 tests "
        f"never run: addopts={addopts!r}"
    )


@pytest.mark.parametrize(
    "name", ["test_versions.py", "test_bulk_delete.py", "test_quality_priority.py"]
)
def test_the_corpus_the_guard_walks_is_really_there(name):
    """Pin the files by name, so a rename cannot silently shrink the corpus."""
    assert (INTEGRATION_DIR / name).exists(), f"{name} vanished from tests/integration"
