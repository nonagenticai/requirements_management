"""Every bounded loader obeys the same three-part contract — checked structurally.

⛔ WHY A STRUCTURAL GUARD AND NOT NINE MORE BEHAVIOURAL TESTS
--------------------------------------------------------------
An adversarial mutation sweep against this PR left **15 survivors**, and every one
of them was in a loader whose table is EMPTY in production (``architecture_views``
and ``architecture_standards`` hold 0 rows) or otherwise unseeded by the
behavioural tests:

  * dropping ``.raise_if_truncated()`` from ``_load_views``, ``_load_standards``,
    ``integration_tools._load_requirements`` and ``llm_tools._load_relationships``
    changed nothing any test could see — so four of nine loaders could silently
    ship a truncated whole-model verdict, which is the exact defect this PR
    exists to prevent;
  * swapping the ELEMENTS cap for the RELATIONSHIPS cap on four loaders was
    likewise invisible;
  * and six loaders accepted a NON-UNIQUE ``order_by``. ``fetch_bounded``'s own
    docstring calls the ordering REQUIRED, because ``LIMIT`` without a unique
    ``ORDER BY`` is a non-deterministic subset — the same request may answer
    differently each time. That property was pinned in exactly one place out of
    ten.

Seeding over-cap fixtures for all nine would mean inventing rows for tables that
have never held any, and would still not pin the ORDERING. The property those
mutants violate is a property of the CALL, so this asserts the call.

This is a lint over the AST, deliberately: it is not a substitute for the
behavioural tests in ``test_arch_input_bounds.py`` / ``test_sibling_input_bounds.py``
(which prove the 422 actually reaches the client for the loaders that can be
seeded), it is the thing that stops the other four drifting out of the contract
unobserved.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_ROUTERS = pathlib.Path(__file__).resolve().parent.parent / "src" / "routers"

# The cap each loader MUST use, keyed by the model it orders on. A loader that
# selects relationships and caps on the element budget is a real defect: the two
# populations differ by ~4x in production (2,699 vs 11,681 for the largest
# tenant), so the wrong constant is either a bound that never binds or one that
# refuses a tenant it should serve.
_EXPECTED_CAP = {
    "ArchitectureElement": "DEFAULT_MAX_INPUT_ELEMENTS",
    "ArchitectureRelationship": "DEFAULT_MAX_INPUT_RELATIONSHIPS",
    # Views, standards and requirements are node-like populations that enter the
    # same coverage matrices as elements, and are deliberately governed by the
    # element budget rather than a third constant nobody would keep in step.
    "ArchitectureView": "DEFAULT_MAX_INPUT_ELEMENTS",
    "Standard": "DEFAULT_MAX_INPUT_ELEMENTS",
    "Requirement": "DEFAULT_MAX_INPUT_ELEMENTS",
}

# ⚠️ ONE MODEL, TWO LEGITIMATE CAPS — keyed by module, checked before the map above.
#
# `architecture_elements.batch-delete` caps on `_BATCH_DELETE_MAX` (500) because that path DELETES one
# committed transaction per element and the endpoint has already told callers
# 500 is the most it accepts. The `DEFAULT_MAX_INPUT_*` ceilings are sized ~2x
# the largest tenant for READ-ONLY analysis, where loading the whole model is
# the point. Holding a destructive path to a read ceiling three orders of
# magnitude larger is not a stricter check, it is the wrong one.
_EXPECTED_CAP_BY_MODULE = {
    "architecture_elements.py": {"ArchitectureElement": "_BATCH_DELETE_MAX"},
}

# A vacuity floor. If the collector finds nothing, every assertion below passes
# for the wrong reason — the same failure mode this repo has shipped before.
# Raised 9 → 20 when `pm_tools` (10 loaders) and `llm_tools._load_wbs_tree`
# adopted the helper: a floor left at the old count would let every one of those
# eleven be reverted with the guard still green.
# Lowered 20 → 18 (2026-10-26) when the LLM routers (`llm_tools`,
# `governance_architecture`) were deleted with their loaders; 18 is the exact
# count measured after the removal, so the floor is still tight.
# Lowered 18 → 8 (2026-11-04, R352) when `pm_tools` (10 loaders) was deleted with the
# schedule apparatus; 8 is the exact count measured after the removal.
_MIN_CALL_SITES = 8


def _call_sites() -> list[tuple[str, int, ast.Call]]:
    found: list[tuple[str, int, ast.Call]] = []
    for path in sorted(_ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "fetch_bounded":
                found.append((path.name, node.lineno, node))
    return found


def test_the_collector_sees_every_loader() -> None:
    """Vacuity floor: an empty walk must fail, not pass."""
    sites = _call_sites()
    assert len(sites) >= _MIN_CALL_SITES, (
        f"found only {len(sites)} fetch_bounded call site(s) under {_ROUTERS}; "
        f"expected at least {_MIN_CALL_SITES}. Either loaders were removed or this "
        f"collector is broken — and a broken collector makes every other assertion "
        f"in this file vacuous."
    )


@pytest.mark.parametrize("filename,lineno,call", _call_sites(), ids=lambda v: str(v))
def test_every_bounded_call_orders_by_a_unique_column(
    filename: str, lineno: int, call: ast.Call
) -> None:
    """``order_by`` must be a model's primary key, never a non-unique column.

    ``LIMIT`` without a unique ordering returns an arbitrary subset that Postgres
    is free to vary between identical requests. A refusal that depends on which
    rows the planner felt like returning is not a bound, it is a coin toss.
    """
    kwargs = {kw.arg: kw.value for kw in call.keywords}
    order_by = kwargs.get("order_by")
    assert order_by is not None, f"{filename}:{lineno} passes no order_by"
    assert isinstance(order_by, ast.Attribute), (
        f"{filename}:{lineno} order_by is not a <Model>.<column> attribute"
    )
    assert order_by.attr == "id", (
        f"{filename}:{lineno} orders by {order_by.attr!r}, which is not the primary "
        f"key. Only a unique column makes the LIMIT prefix reproducible."
    )


@pytest.mark.parametrize("filename,lineno,call", _call_sites(), ids=lambda v: str(v))
def test_every_bounded_call_uses_the_cap_for_its_population(
    filename: str, lineno: int, call: ast.Call
) -> None:
    """The cap must match the model being loaded — see ``_EXPECTED_CAP``.

    The per-module override wins where it exists, because one model can front
    two genuinely different populations — see ``_EXPECTED_CAP_BY_MODULE``.
    """
    kwargs = {kw.arg: kw.value for kw in call.keywords}
    order_by, cap = kwargs.get("order_by"), kwargs.get("cap")
    assert isinstance(order_by, ast.Attribute), f"{filename}:{lineno} malformed order_by"
    model = getattr(order_by.value, "id", None)
    module_overrides = _EXPECTED_CAP_BY_MODULE.get(filename, {})
    assert model in module_overrides or model in _EXPECTED_CAP, (
        f"{filename}:{lineno} loads {model!r}, which has no declared cap. Add it to "
        f"_EXPECTED_CAP deliberately rather than letting a new population inherit "
        f"whichever constant was nearest."
    )
    expected = module_overrides.get(model) or _EXPECTED_CAP[model]
    assert isinstance(cap, ast.Name), f"{filename}:{lineno} cap is not a named constant"
    assert cap.id == expected, (
        f"{filename}:{lineno} loads {model} but caps on {cap.id}; expected {expected}."
    )


def test_every_bounded_result_is_refused_when_truncated() -> None:
    """Each loader must call ``.raise_if_truncated()`` on what it fetched.

    Fetching with a bound and then NOT refusing hands the analysis an arbitrary
    prefix of the model and reports the verdict as complete — a 200 carrying a
    failure, which is the shape this repo has now removed three times.
    """
    offenders: list[str] = []
    checked = 0
    for path in sorted(_ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if "fetch_bounded" not in ast.dump(fn):
                continue
            checked += 1
            # ⚠️ STRUCTURAL, not a substring. This assertion used to read
            # `"raise_if_truncated" not in ast.dump(fn)`, and an adversarial sweep
            # walked straight through it: `bounded.raise_if_truncated` — the same
            # name with the parentheses deleted — is an `Attribute` that evaluates
            # to a bound method and throws it away, yet the substring is still
            # present, so the guard stayed GREEN while the refusal was disarmed.
            # Two deleted characters silently un-bounded `_load_views`,
            # `_load_standards` and `integration_tools._load_requirements`, which
            # are precisely the loaders with no behavioural over-cap test (their
            # tables hold 0 rows in production) — i.e. the ones this guard exists
            # for. A name being MENTIONED is not the same as it being CALLED.
            called = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "raise_if_truncated"
                for node in ast.walk(fn)
            )
            if not called:
                offenders.append(f"{path.name}:{fn.lineno} {fn.name}")
    assert checked >= _MIN_CALL_SITES, (
        f"only {checked} function(s) call fetch_bounded; the walk is broken."
    )
    assert not offenders, (
        "these loaders bound their query but never refuse the truncated result, so "
        "they would analyse an arbitrary prefix and report it as whole:\n  "
        + "\n  ".join(offenders)
    )
