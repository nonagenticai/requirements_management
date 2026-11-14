"""Every ``Depends(get_session)`` must pass ``scope="function"``.

⛔ WHAT BREAKS WITHOUT IT

``get_session`` (``src/db.py``) is an ``async`` generator dependency: it yields a
session out of ``AsyncSessionLocal`` and only closes it when the generator is
finalised.  FastAPI's default dependency scope keeps that generator alive until
**after** the response has been sent and after every ``BackgroundTask`` attached
to the request has run.  So the pooled connection is held across the response
send and across arbitrary background work — the request looks finished, the pool
slot is not.  ``scope="function"`` finalises the dependency when the *handler*
returns, which is the point the connection is actually no longer needed.

This is the same pool-starvation family as ``test_eval_does_not_hold_db_across_llm``
and ``test_readiness_survives_pool_saturation``: the engine takes SQLAlchemy's
defaults, the Deployment runs one replica, and a starved pool becomes a dropped
EndpointSlice rather than a slow response.

WHAT THIS FILE PINS

1. Every ``Depends(get_session)`` call site under ``src/`` passes
   ``scope="function"`` (leg 1).
2. Prose mentions of ``Depends(get_session)`` in comments and docstrings are NOT
   call sites (leg 2).  AST parsing gives this for free — it is asserted anyway
   so the property is pinned rather than incidental.
3. ``get_session`` is never imported under a different name (leg 3).  An alias
   would rename the callee and slip straight past leg 1's name check.  A regex
   guard in this repo has already been defeated by exactly this once, which is
   why the whole file is AST-based and not a ``grep``.
4. The extractor itself works, proven against a synthetic AST (leg 4) rather
   than assumed.

RATCHET: drop ``scope="function"`` from any single call site and leg 1 goes red
naming ``file:line``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# ``src/`` as laid out on disk.  pyproject sets ``pythonpath = ["src"]``, so the
# package root is one level up from ``tests/``.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

REQUIRED_SCOPE = "function"
SESSION_DEP = "get_session"


# ─────────────────────────────────────────────────────────────────────────────
# The extractor.  Module-level and importable so leg 4 can drive it directly
# instead of trusting it.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Site:
    """One ``Depends(get_session, ...)`` call site found in the AST."""

    file: str
    line: int
    scope: str | None
    """The constant value of the ``scope=`` keyword, or None if absent/non-constant."""

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def compliant(self) -> bool:
        return self.scope == REQUIRED_SCOPE


def _is_depends(func: ast.expr) -> bool:
    """True for ``Depends(...)`` and for ``fastapi.Depends(...)``.

    Only the trailing name is checked on the attribute form: the module may be
    bound as ``fastapi``, ``fa``, or anything else, and the guard should not
    care.  A false positive here is harmless (it would be a call named
    ``Depends`` taking ``get_session``, which is the thing we want checked).
    """
    if isinstance(func, ast.Name):
        return func.id == "Depends"
    if isinstance(func, ast.Attribute):
        return func.attr == "Depends"
    return False


def find_session_depends(path: Path, source: str | None = None) -> list[Site]:
    """Return every ``Depends(get_session, ...)`` call site in one Python file.

    ``source`` is an override used by the self-test so it can feed synthetic
    code without writing a temp file.  ``path`` is then only a label.
    """
    text = source if source is not None else path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))

    try:
        rel = str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        rel = str(path)

    sites: list[Site] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_depends(node.func):
            continue
        # ``Depends`` takes its dependency as the first positional argument OR as
        # the ``dependency=`` keyword. BOTH call forms are recognised here, so a
        # site written either way is checked for its scope; neither form can
        # drop out of the population silently.
        dependency: ast.expr | None = node.args[0] if node.args else None
        if dependency is None:
            for kw in node.keywords:
                if kw.arg == "dependency":
                    dependency = kw.value
                    break
        if not (isinstance(dependency, ast.Name) and dependency.id == SESSION_DEP):
            continue

        scope: str | None = None
        for kw in node.keywords:
            if kw.arg == "scope" and isinstance(kw.value, ast.Constant):
                scope = kw.value.value if isinstance(kw.value.value, str) else None
        sites.append(Site(file=rel, line=node.lineno, scope=scope))

    # ``ast.walk`` is breadth-first, so raw order is nesting depth, not source
    # order. Sort so failure output names sites in the order a reader will find
    # them in the file.
    return sorted(sites, key=lambda s: s.line)


def find_aliased_imports(path: Path, source: str | None = None) -> list[str]:
    """Return ``file:line name`` for any import of ``get_session`` under an alias.

    ``from db import get_session as gs`` renames the callee, so every
    ``Depends(gs)`` call site becomes invisible to :func:`find_session_depends`.
    Reported as its own failure because the remedy is different: un-alias the
    import, do not add a scope.
    """
    text = source if source is not None else path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))

    try:
        rel = str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        rel = str(path)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == SESSION_DEP and alias.asname not in (None, SESSION_DEP):
                    offenders.append(f"{rel}:{node.lineno} as {alias.asname}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # e.g. ``import db.get_session as gs`` — unusual, still caught.
                if alias.name.split(".")[-1] == SESSION_DEP and alias.asname not in (
                    None,
                    SESSION_DEP,
                ):
                    offenders.append(f"{rel}:{node.lineno} as {alias.asname}")
    return offenders


def _src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _all_sites() -> list[Site]:
    sites: list[Site] = []
    for f in _src_files():
        sites.extend(find_session_depends(f))
    return sites


# ─────────────────────────────────────────────────────────────────────────────
# Leg 1 — the behaviour.
# ─────────────────────────────────────────────────────────────────────────────
def test_every_depends_get_session_passes_scope_function() -> None:
    sites = _all_sites()
    violations = [s.location for s in sites if not s.compliant]

    assert not violations, (
        f"{len(violations)} of {len(sites)} `Depends(get_session)` call sites do not pass "
        f'`scope="{REQUIRED_SCOPE}"`. Without it FastAPI holds the pooled DB connection '
        "across the response send and across every BackgroundTask attached to the request, "
        "so the pool slot outlives the work. Offending sites:\n  " + "\n  ".join(violations)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Leg 1b — the vacuity floor.
#
# ⛔ THIS FLOOR IS ON SITES EXAMINED, NEVER ON VIOLATIONS FOUND. Do not
# "simplify" it into a floor on violations: a floor on violations is satisfied by
# zero violations *by construction* and can therefore never go red — it would be
# a check that cannot fail. The number below is the denominator leg 1 divides
# into. If the glob breaks, `src/` moves, the routers are renamed, or the AST
# walk stops matching the call shape, leg 1 passes vacuously over an empty list
# and ONLY this assertion notices.
# ─────────────────────────────────────────────────────────────────────────────
MINIMUM_CALL_SITES = 174
"""176 sites across 27 router files measured 2026-09-04; floored at 170 for churn."""


def test_the_guard_still_finds_the_call_sites_it_exists_to_check() -> None:
    sites = _all_sites()
    assert len(sites) >= MINIMUM_CALL_SITES, (
        f"only {len(sites)} `Depends(get_session)` call sites were found under {SRC}, "
        f"expected at least {MINIMUM_CALL_SITES}. This guard has lost sight of the code "
        "it polices — leg 1 above is now passing over an (almost) empty list. Fix the "
        "discovery, do not lower the floor."
    )
    # And the files must actually have been read, not silently skipped.
    assert len(_src_files()) > 0, f"no Python files found under {SRC}"


# ─────────────────────────────────────────────────────────────────────────────
# Leg 2 — comments and docstrings are not call sites.
#
# AST parsing gives this for free. Pinned anyway so it is a property of the
# guard rather than an accident of the current implementation: a future
# "just grep it, it's simpler" rewrite fails here.
# ─────────────────────────────────────────────────────────────────────────────
# Leg 2b — prose is excluded because the extractor is AST-based, not textual.
#
# ⚠️ This used to pin (file, line) pairs. That is brittle for no benefit: an
# unrelated edit above a comment shifts its line number and the leg goes RED
# claiming the comment "is gone", which is a stale fixture, not a finding.
# (It happened — src/agents/runner.py:88 moved to :99.) The fixture is now
# DERIVED: every textual occurrence that the AST walk did NOT report must sit on
# a comment line or inside a string literal. That cannot go stale, and the floor
# below stops it going vacuous.
MINIMUM_PROSE_MENTIONS = 3


def _string_literal_lines(tree: ast.AST) -> set[int]:
    """Every line covered by a string constant (docstrings included)."""
    covered: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            covered.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return covered


def test_prose_mentions_of_depends_get_session_are_not_reported_as_call_sites() -> None:
    reported = {(s.file, s.line) for s in _all_sites()}
    prose: list[str] = []

    for path in _src_files():
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        literal_lines = _string_literal_lines(ast.parse(text))
        for i, line in enumerate(text.splitlines(), start=1):
            if f"Depends({SESSION_DEP}" not in line:
                continue
            if (rel, i) in reported:
                continue  # a real AST call site, not prose
            assert line.lstrip().startswith("#") or i in literal_lines, (
                f"{rel}:{i} contains `Depends({SESSION_DEP}` but the AST walk did not "
                "report it, and it is neither a comment nor inside a string literal. "
                "The extractor is silently dropping a real call site."
            )
            prose.append(f"{rel}:{i}")

    assert len(prose) >= MINIMUM_PROSE_MENTIONS, (
        f"only {len(prose)} prose mentions of `Depends({SESSION_DEP}` found, expected at "
        f"least {MINIMUM_PROSE_MENTIONS}. Without them this leg proves nothing about "
        f"comments being excluded. Found: {prose}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Leg 3 — alias defence.
# ─────────────────────────────────────────────────────────────────────────────
def test_get_session_is_never_imported_under_an_alias() -> None:
    offenders: list[str] = []
    for f in _src_files():
        offenders.extend(find_aliased_imports(f))

    assert not offenders, (
        f"`{SESSION_DEP}` is imported under an alias:\n  "
        + "\n  ".join(offenders)
        + f"\n\nAn alias renames the callee, so every `Depends(<alias>)` built from it is "
        f"INVISIBLE to the scope check above — the guard would report zero violations "
        f"while the defect ships. Import `{SESSION_DEP}` under its own name."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Leg 4 — the extractor is proven, not assumed.
#
# Without this, every leg above rests on an unverified belief that
# find_session_depends() can see a violation at all.
# ─────────────────────────────────────────────────────────────────────────────
SYNTHETIC = '''\
"""A docstring mentioning Depends(get_session) that must NOT be counted."""
from fastapi import Depends
from db import get_session

# A comment mentioning Depends(get_session) that must NOT be counted.


async def compliant(session=Depends(get_session, scope="function")):
    return session


async def offender(session=Depends(get_session)):
    return session


async def wrong_scope(session=Depends(get_session, scope="request")):
    return session


async def keyword_form_offender(session=Depends(dependency=get_session)):
    return session


async def not_our_dependency(x=Depends(get_current_user)):
    return x
'''


def test_the_extractor_reports_exactly_the_non_compliant_synthetic_sites() -> None:
    sites = find_session_depends(Path("synthetic.py"), source=SYNTHETIC)

    # 5 real call sites: 4 on get_session, 1 on get_current_user (not matched).
    assert [s.line for s in sites] == [8, 12, 16, 20], (
        f"expected call sites on lines 8, 12, 16, 20 of the synthetic module, got "
        f"{[s.line for s in sites]} — the extractor is matching the wrong nodes "
        "(prose on lines 1 and 5, or the unrelated dependency on line 24)"
    )

    violations = [s for s in sites if not s.compliant]
    assert len(violations) == 3, (
        f"expected exactly 3 violations in the synthetic module, got {len(violations)}"
    )
    assert violations[0].line == 12 and violations[0].scope is None, (
        "the bare `Depends(get_session)` on line 12 was not reported as a violation — "
        "the extractor cannot see the defect it exists to catch"
    )
    assert violations[1].line == 16 and violations[1].scope == "request", (
        'the `scope="request"` site on line 16 was not reported — the extractor is '
        "checking for the PRESENCE of a scope keyword, not its VALUE"
    )
    assert violations[2].line == 20 and violations[2].scope is None, (
        "the keyword-form `Depends(dependency=get_session)` on line 20 was not reported "
        "as a violation — the extractor only sees the POSITIONAL call form, so rewriting "
        "a site to the keyword form would remove it from the population silently"
    )
    assert sites[0].compliant and sites[0].scope == "function", (
        "the compliant site on line 8 was reported as a violation — the extractor "
        "would fail leg 1 even on correct code"
    )
    assert violations[0].location == "synthetic.py:12"


def test_the_alias_detector_sees_an_alias() -> None:
    """The alias defence must itself be provably able to fire."""
    assert find_aliased_imports(
        Path("synthetic.py"), source="from db import get_session as gs\n"
    ) == ["synthetic.py:1 as gs"]
    # And must not fire on the honest import.
    assert find_aliased_imports(Path("synthetic.py"), source="from db import get_session\n") == []
