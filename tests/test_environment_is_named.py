"""`ENVIRONMENT` decides two things, and production has been answering neither.

Why this file exists
--------------------
Measured 2026-09-04 against the live Deployment `requirements` in ns
`requirements`: `ENVIRONMENT` appears in **neither** the explicit `env:` list
(15 entries) **nor** the single `envFrom` secret `requirements-api-env` (keys:
CODING_SERVICE_TOKEN, DATABASE_URL, JWT_PUBLIC_KEY, POSTGRES_PASSWORD_REQUIREMENTS,
RUNTIME_DATABASE_URL, SERVICE_CLIENT_ID, SERVICE_CLIENT_SECRET). `printenv` in
the running pod confirms it. Two consequences, both real:

* `Settings.model_post_init` early-returns for anything that is not exactly
  `"production"`, so `_REQUIRED_IN_PRODUCTION` — which then guarded four fields
  (now three: `database_url`, `license_introspect_url`, `otlp_traces_endpoint`;
  `worker_management_url` left with the LLM lane, 2026-10-26) — **had never
  executed in production**. A guard that
  silently declines to run looks exactly like a guard that ran and passed.
* Every production log line carries `environment=development`. That is the field
  an operator filters on, so production traffic has been sitting in the
  development bucket. Verbatim from Loki, a real production line:
  `{"entity_type": "decision", ..., "event": "eval_dispatcher.start",
  "service": "requirements-management", "environment": "development", ...}`

⛔ The trap this file mainly exists to stop
-------------------------------------------
`src/main.py` reads `os.getenv("ENVIRONMENT", "")` while `src/settings.py`
declares `default="development"`. That asymmetry LOOKS like sloppiness and the
obvious tidy-up is to route both through `settings`. **Doing so is a security
regression**, which is why the asymmetry is now commented in `main.py` and
pinned here.

`LEAK_EXCEPTION_DETAILS` is true when `ENVIRONMENT` is `development`/`dev`.
Sourcing it from `settings.environment` would make an ABSENT variable resolve to
`"development"` — and absent is precisely the production state measured above.
The tidy version therefore switches exception-detail leaking **on in
production**. Absent must mean "do not leak", never "assume development".
"""

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from logging_config import add_service_context  # noqa: E402
from settings import Settings  # noqa: E402


def _leak_flag_getenv_defaults() -> list[str | None]:
    """Every `os.getenv("ENVIRONMENT", <default>)` default in main.py's leak flag.

    Structural rather than behavioural on purpose, and the reason is worth
    stating: `LEAK_EXCEPTION_DETAILS` is a module-level constant evaluated at
    import time, and importing `main` constructs the entire FastAPI application
    (every router, the middleware stack, the engines). Re-importing it under a
    patched environment to observe one boolean would be a far heavier and more
    fragile test than reading the expression that produces it.
    """
    tree = ast.parse((SRC / "main.py").read_text())
    defaults: list[str | None] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "LEAK_EXCEPTION_DETAILS" for t in node.targets
        ):
            continue
        for call in ast.walk(node.value):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "getenv"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == "ENVIRONMENT"
            ):
                if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
                    value = call.args[1].value
                    defaults.append(value if isinstance(value, str) else None)
                else:
                    defaults.append(None)
    return defaults


def _leak_flag_membership_values() -> set[str]:
    """The literal set that main.py's leak flag tests `ENVIRONMENT` against.

    Read together with `_leak_flag_getenv_defaults()`: neither half is the
    invariant on its own. "Default is empty" is satisfied by a set that contains
    the empty string, and "the set is {development, dev}" is satisfied by a
    default of "development". Only the PAIR — default not in set — actually says
    that an absent variable does not enable leaking.
    """
    tree = ast.parse((SRC / "main.py").read_text())
    values: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "LEAK_EXCEPTION_DETAILS" for t in node.targets
        ):
            continue
        for cmp_node in ast.walk(node.value):
            if not isinstance(cmp_node, ast.Compare):
                continue
            if not any(isinstance(op, ast.In) for op in cmp_node.ops):
                continue
            for comparator in cmp_node.comparators:
                if isinstance(comparator, ast.Set | ast.List | ast.Tuple):
                    for elt in comparator.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            values.add(elt.value)
    return values


def test_the_leak_flag_treats_an_absent_environment_as_not_development():
    """An ABSENT `ENVIRONMENT` must not enable exception-detail leaking.

    Kill-mutation: change `os.getenv("ENVIRONMENT", "")` in main.py to
    `os.getenv("ENVIRONMENT", "development")` — or route it through
    `settings.environment`, whose default IS `"development"` — and this fails.
    That mutant is not hypothetical tidying: it is the exact refactor a reader
    reaches for after noticing the two defaults disagree.
    """
    defaults = _leak_flag_getenv_defaults()
    assert defaults, (
        "LEAK_EXCEPTION_DETAILS no longer reads os.getenv('ENVIRONMENT', ...) directly; if it now "
        "comes from settings, an ABSENT variable resolves to 'development' and leaking is ON in "
        "production, which is the state measured on the live pod 2026-09-04"
    )
    assert all(d == "" for d in defaults), (
        f"the ENVIRONMENT default in main.py's leak flag is {defaults!r}; it must be '' so that a "
        "missing variable means 'do not leak' rather than 'assume development'"
    )
    # ⛔ THE DEFAULT ALONE IS NOT THE INVARIANT — an adversarial review defeated
    # the first version of this test by leaving the default as `""` and adding
    # `""` to the membership set instead, which makes the flag TRUE when
    # ENVIRONMENT is absent (the measured production state) with 5 tests passing.
    # The property that actually matters is the pair: the default must not be a
    # member of the set it is tested against.
    members = _leak_flag_membership_values()
    assert members, (
        "could not find the `... in {…}` membership test in main.py's leak flag; if the shape "
        "changed, re-establish what an ABSENT ENVIRONMENT now resolves to"
    )
    for d in defaults:
        assert d not in members, (
            f"main.py's leak flag compares ENVIRONMENT (default {d!r}) against {sorted(members)!r} "
            "— the default is IN that set, so an absent variable switches "
            "LEAK_EXCEPTION_DETAILS ON, which is exactly the production state measured 2026-09-04"
        )


def test_the_production_guard_actually_raises_on_a_missing_required_setting():
    """The guard works — it has simply never been asked to run in production.

    Establishing this is what licenses the claim that arming `ENVIRONMENT`
    is worth doing: if the guard were broken, arming it would achieve nothing.
    """
    with pytest.raises(RuntimeError) as exc:
        Settings(
            environment="production",
            database_url="",
            license_introspect_url="http://lic",
            otlp_traces_endpoint="http://otlp",
        )
    assert "database_url" in str(exc.value)


def test_the_guard_covers_three_fields_including_database_url():
    """Pins the guarded set.

    `database_url` is named explicitly because it was once omitted from the docs —
    the single setting whose absence is least survivable. `worker_management_url`
    left the set with the LLM lane (2026-10-26).
    """
    assert set(Settings._REQUIRED_IN_PRODUCTION) == {
        "database_url",
        "license_introspect_url",
        "otlp_traces_endpoint",
    }


def test_the_guard_is_dormant_when_environment_is_unset():
    """The measured production behaviour, pinned as a fact rather than a wish.

    This is NOT an endorsement: it documents that an unset `ENVIRONMENT` boots a
    pod with every required setting empty and no complaint. Should the default
    ever change so this raises, that is a deliberate decision and this test
    should be updated with it — not deleted to make a red suite green.
    """
    s = Settings(
        database_url="",
        license_introspect_url="",
        otlp_traces_endpoint="",
    )
    assert s.environment == "development"


def test_the_log_label_comes_from_settings_not_from_os_getenv(monkeypatch):
    """The `environment` log field must read `settings`, not the raw variable.

    ⚠️ THIS TEST WAS VACUOUS ON ITS FIRST WRITING AND THE MUTANT SURVIVED. The
    first version set `ENVIRONMENT=production` and asserted the label said
    `production` — but `os.getenv("ENVIRONMENT", "development")` returns
    `production` in that situation too. Both implementations agree whenever the
    variable is SET, so the assertion could not distinguish them, and reverting
    the fix left the suite green.

    The discriminator is the case where they DISAGREE: the variable absent, and
    `settings` carrying a value that did not come from `os.environ`. The fixed
    code reports what settings says; the `os.getenv` version reports its own
    hardcoded default.

    Kill-mutation: restore
    `event_dict["environment"] = os.getenv("ENVIRONMENT", "development")`
    in `src/logging_config.py` — this then reads "development" and fails.
    """
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    class _Stub:
        environment = "staging-from-settings"

    monkeypatch.setattr("settings.get_settings", lambda: _Stub())
    assert add_service_context(None, "info", {})["environment"] == "staging-from-settings"
