"""A test whose ONLY assertion sits under an `if` verifies nothing when that
`if` is false — and nothing anywhere reports it.

`tests/integration/conftest.py` and `tests/test_integration_suite_can_fail.py`
between them close the *skip* mask: every spelling of `pytest.skip` /
`pytest.mark.skip` / `raise SkipTest`, plus a runtime backstop that rewrites a
skipped integration report into a failure. Both match SKIP SPELLINGS, and both
are scoped to ``tests/integration/``.

The `if` mask is the same defect wearing no marker at all:

    def test_leveling_actions_detail():
        result = level_resources(...)
        if result["conflicts_resolved"] > 0:      # false at runtime ⇒
            assert action["delay_days"] > 0       #   never evaluated

pytest reports **passed**. There is no skip line, no `s` in the progress dots,
no entry in the short test summary. The two guards above cannot see it: no skip
name appears, and both offenders live in top-level ``tests/``, outside the
directory they walk. This repo has already shipped a test that stayed green with
the function under test replaced by ``raise RuntimeError``.

``CLAUDE.md`` recorded, as settled fact, that "an `if` wrapping the only
assertion is the same mask and **no AST guard can see it**." The first half is
true. The second half is false, and this file is the refutation: an AST walk
that flags a ``def test_*`` where EVERY ``assert`` is nested inside an ``If``
found both offenders in seconds. Measured 2026-09-05 across all of ``tests/**``:
**2 offenders out of 2052 test functions examined.** Both are fixed in the same
commit that adds this file, so the guard ships at zero.

⛔ Do NOT "fix" a failure here by adding an allowlist entry. The allowlist is
pinned at its measured size, so adding one fails the ratchet leg as well. The
fix for a masked assertion is to assert the precondition — make the empty result
FAIL instead of silently passing — and then assert the real property
unconditionally.
"""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

# ---------------------------------------------------------------------------
# Scope decision, made deliberately and stated so the next reader need not guess
# ---------------------------------------------------------------------------
# ONLY `if` masks. An `assert` inside a `for`, `with`, `try`, `while` or `match`
# body CLEARS the test.
#
# Rationale: `if` is the construct whose *body may never execute while the test
# still reports success*. `with` and `try` bodies always run. A `for` over an
# empty sequence is the same failure mode in principle — but it is a DIFFERENT
# concern with a different fix and a much larger blast radius (parametrised
# sweeps over collections are idiomatic and overwhelmingly legitimate here), so
# folding it in would trade this guard's precision for noise. Loop-masked
# assertions are deliberately out of scope; they are somebody else's guard.
#
# `elif` / `else` DO mask: an `else:` branch is exactly as conditional as the
# `if:` it hangs off, and an earlier draft that checked only `node.body` would
# have let `if x: pass` / `else: assert y` straight through.

# A test is CLEARED by an unconditional `with pytest.raises(...)` even when it
# owns no unconditional `assert`: `raises` IS the assertion in that shape, and
# flagging it would be a pure false positive.
_RAISES_NAMES = {"raises", "warns", "deprecated_call"}

# Tests that are genuinely fine while looking masked — e.g. the real assertion
# lives in a helper this static walk cannot follow. Key: "<relpath>::<funcname>"
# (NOT the line number, which drifts on every edit above it). Every entry states
# WHY, because an exclusion with no recorded reason is where the worst case
# hides.
#
# Measured 2026-09-05: ZERO entries. Both known offenders were repaired rather
# than annotated.
ALLOWLIST: dict[str, str] = {}

# The allowlist is a ratchet, not a door.
MAX_ALLOWLISTED = 0

# Vacuity floor. It counts test functions EXAMINED, never violations found: a
# floor on violations is satisfied by zero, so it goes green when the walk is
# broken and sees nothing at all. This repo has shipped a vacuous leg inside the
# very gate built to catch vacuity — hence this. 2052 test functions exist
# today; 1900 leaves room for churn without letting the corpus quietly empty
# out.
MIN_TESTS_EXAMINED = 1900


def _test_files() -> list[pathlib.Path]:
    # rglob, not glob: a non-recursive walk misses a masked assertion parked one
    # directory deeper (tests/integration/, tests/contract/, tests/plugins/).
    return sorted(TESTS_DIR.rglob("*.py"))


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """child -> parent for the whole tree, so ancestry can be walked upward.

    A side table rather than an attribute stamped onto each node: `ast.AST` has
    no such field, so assigning one is a type error, and the map is discarded
    with the tree instead of outliving it.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_conditional(node: ast.AST, root: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True if `node` sits inside an `If` body or `else` anywhere below `root`."""
    current: ast.AST = node
    while current is not root:
        parent = parents.get(current)
        if parent is None:
            return False
        if isinstance(parent, ast.If) and any(
            child is current for child in parent.body + parent.orelse
        ):
            return True
        current = parent
    return False


def _is_raises_with(node: ast.AST) -> bool:
    """`with pytest.raises(...)` / `with raises(...)` — an assertion by another name."""
    if not isinstance(node, ast.With | ast.AsyncWith):
        return False
    for item in node.items:
        call = item.context_expr
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name in _RAISES_NAMES:
            return True
    return False


def scan_source(source: str, label: str) -> tuple[list[str], int]:
    """Return (offenders, tests_examined) for one module's source text.

    An offender is a ``def test_*`` (sync or async) that owns at least one
    ``assert`` and where EVERY ``assert`` it owns is nested inside an ``If``,
    with no unconditional ``pytest.raises`` block to stand in for one.

    Offenders are formatted ``<label>:<lineno> <function_name>`` so a developer
    can act on the failure message without re-running anything.

    A ``SyntaxError`` is raised, never swallowed: a walk that silently failed to
    parse a file would report "no offenders" for it forever.
    """
    tree = ast.parse(source, filename=label)
    parents = _parent_map(tree)

    offenders: list[str] = []
    examined = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        examined += 1

        asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
        if not asserts:
            # No assertion at all is a different defect with a different guard.
            continue

        if any(not _is_conditional(a, node, parents) for a in asserts):
            continue

        # Falls back to `pytest.raises` only AFTER every assert was found masked.
        if any(
            _is_raises_with(n) and not _is_conditional(n, node, parents)
            for n in ast.walk(node)
            if n is not node
        ):
            continue

        offenders.append(f"{label}:{node.lineno} {node.name}")

    return offenders, examined


def _walk() -> tuple[list[str], list[str], int]:
    """Return (offenders, allowlisted, tests_examined) across all of tests/**."""
    offenders: list[str] = []
    allowlisted: list[str] = []
    examined = 0

    for path in _test_files():
        rel = str(path.relative_to(REPO_ROOT))
        found, seen = scan_source(path.read_text(), rel)
        examined += seen
        for entry in found:
            func = entry.rsplit(" ", 1)[-1]
            if f"{rel}::{func}" in ALLOWLIST:
                allowlisted.append(entry)
            else:
                offenders.append(entry)

    return offenders, allowlisted, examined


def test_no_test_hides_its_only_assertion_under_an_if():
    """An `if` around the only assert turns a broken function into a green run."""
    offenders, _allowlisted, examined = _walk()

    # Vacuity floor FIRST: if the walk examined nothing, an empty offender list
    # means the guard is broken, not that the suite is clean.
    assert examined >= MIN_TESTS_EXAMINED, (
        f"guard walked only {examined} test functions (floor {MIN_TESTS_EXAMINED}) "
        f"— the walk is broken, so 'no offenders' proves nothing. "
        f"Files seen: {len(_test_files())}"
    )
    assert offenders == [], (
        "every assertion in these tests is nested under an `if`, so each one "
        "reports PASSED without evaluating a single assert whenever that "
        "condition is false. Assert the precondition (make the empty case FAIL) "
        "and then assert the property unconditionally. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_masked_assertion_allowlist_stays_at_its_measured_floor():
    """The allowlist is a ratchet, not a door."""
    _offenders, allowlisted, _examined = _walk()
    assert len(allowlisted) <= MAX_ALLOWLISTED, (
        f"{len(allowlisted)} allowlisted masked tests, ceiling {MAX_ALLOWLISTED}. "
        f"Repair the test rather than annotating it: {allowlisted}"
    )
    assert len(ALLOWLIST) <= MAX_ALLOWLISTED, (
        f"ALLOWLIST has {len(ALLOWLIST)} entries, ceiling {MAX_ALLOWLISTED}"
    )
    for key, reason in ALLOWLIST.items():
        assert reason.strip(), f"allowlist entry {key!r} has no stated reason"


def test_every_allowlist_entry_still_names_a_real_test():
    """A stale allowlist key is an exemption nobody can see is dead."""
    for key in ALLOWLIST:
        rel, _, func = key.partition("::")
        path = REPO_ROOT / rel
        assert path.exists(), f"allowlist names a file that is gone: {key}"
        assert f"def {func}(" in path.read_text(), (
            f"allowlist names a test that no longer exists: {key}"
        )


# ---------------------------------------------------------------------------
# Task 3: the detector's own killing mutation, pinned against SOURCE TEXT.
# Without this, a detector that matched nothing would satisfy the assertion
# above by finding zero of everything — and the two real offenders are fixed in
# this same commit, so they can no longer serve as the live proof.
# ---------------------------------------------------------------------------

_MASKED = """
def test_masked():
    result = compute()
    if result["items"]:
        assert result["items"][0] > 0
"""

_MASKED_ELSE = """
def test_masked_else():
    result = compute()
    if not result:
        pass
    else:
        assert result > 0
"""

_MASKED_ASYNC = """
async def test_masked_async():
    result = await compute()
    if result:
        assert result > 0
"""

_MASKED_NESTED = """
def test_masked_nested():
    for row in rows():
        if row.ok:
            with open("x") as fh:
                assert fh.read()
"""

_CLEAN = """
def test_clean():
    result = compute()
    assert result["items"], "precondition: the fixture must produce items"
    if result["items"]:
        assert result["items"][0] > 0
"""

_CLEAN_LOOP = """
def test_clean_loop():
    for row in rows():
        assert row.distance <= 1
"""

_CLEAN_RAISES = """
def test_clean_raises():
    with pytest.raises(ValueError):
        parse("nope")
    if extra:
        assert extra > 0
"""

_NO_ASSERT_AT_ALL = """
def test_no_assert():
    if thing:
        do_something()
"""


@pytest.mark.parametrize(
    ("source", "func"),
    [
        (_MASKED, "test_masked"),
        (_MASKED_ELSE, "test_masked_else"),
        (_MASKED_ASYNC, "test_masked_async"),
        (_MASKED_NESTED, "test_masked_nested"),
    ],
)
def test_the_detector_flags_a_masked_function(tmp_path, source, func):
    """Prove the detector can go RED on the shape it exists to catch."""
    module = tmp_path / f"{func}_module.py"
    module.write_text(source)

    offenders, examined = scan_source(module.read_text(), str(module))

    assert examined == 1, f"the detector did not even see {func}"
    assert len(offenders) == 1, f"detector missed the masked assertion in {func}: {offenders}"
    assert func in offenders[0]
    # The message must be actionable without re-running anything.
    assert str(module) in offenders[0]
    assert offenders[0].split(":")[-1].split(" ")[0].isdigit(), (
        f"offender line carries no line number: {offenders[0]!r}"
    )


@pytest.mark.parametrize(
    ("source", "func"),
    [
        (_CLEAN, "test_clean"),
        (_CLEAN_LOOP, "test_clean_loop"),
        (_CLEAN_RAISES, "test_clean_raises"),
        (_NO_ASSERT_AT_ALL, "test_no_assert"),
    ],
)
def test_the_detector_leaves_an_unmasked_function_alone(tmp_path, source, func):
    """A detector that flags everything is as useless as one that flags nothing."""
    module = tmp_path / f"{func}_module.py"
    module.write_text(source)

    offenders, examined = scan_source(module.read_text(), str(module))

    assert examined == 1, f"the detector did not even see {func}"
    assert offenders == [], f"false positive on {func}: {offenders}"


def test_a_syntax_error_fails_rather_than_reading_as_clean(tmp_path):
    """A file the walk cannot parse must NOT silently contribute zero offenders."""
    module = tmp_path / "broken_module.py"
    module.write_text("def test_x(:\n    assert True\n")
    with pytest.raises(SyntaxError):
        scan_source(module.read_text(), str(module))


@pytest.mark.parametrize(
    "name",
    ["test_architecture.py", "test_pm_crud.py", "test_integration_suite_can_fail.py"],
)
def test_the_corpus_the_guard_walks_is_really_there(name):
    """Pin files by name, so a rename cannot silently shrink the corpus."""
    assert (TESTS_DIR / name).exists(), f"{name} vanished from tests/"
