"""Pins for three things an adversarial review reverted with every test still green.

A mutation that survives is a coverage gap, not luck. All three below were made,
all three passed 19/19, and all three matter:

1. *(Removed 2026-10-26 with the background LLM eval lane, whose
   `eval_dispatcher.start` / `.end` lines it pinned.)*
2. **`READY_BUDGET_S` 3.0 -> 60.0.** The readiness handler must answer well inside
   the kubelet's `timeoutSeconds: 5`; a 60s budget re-creates the original outage
   with extra steps. The entire premise was unpinned.
3. **The `await session.commit()` release point -> `pass`.** The connection is
   then held across a serial per-container fetch loop (30s each). (Its sibling in
   the since-removed governance proposals router went with the LLM lane.) There is no behavioural test for
   this anywhere — it needs a real Postgres, and the repo's DB-backed tests skip
   locally — so it is asserted structurally, at the call site.

Structural assertions are the right tool here and not a cop-out: in each case the
DIFFERENCE IS INVISIBLE TO BEHAVIOUR until the pool is full, which is exactly when
no test is watching.

Sections 4-6 (added 2026-09-03) extend the same argument to the ENGINES themselves:
the stale-connection settings this repo was the fleet outlier for, the shared pool
spec every engine must unpack, and pool_watch's coverage. (The owner engine these
once also pinned was removed with the conflict reconciler, 2026-10-26.)
"""

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


def _tree(rel: str) -> ast.Module:
    return ast.parse((SRC / rel).read_text())


def _fn(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


# ── 2. the readiness budget must fit inside the kubelet's probe timeout ───────


def test_the_readiness_budget_fits_inside_the_probe_timeout():
    from routers import health

    # readinessProbe.timeoutSeconds is 5 in the Deployment. The budget must leave
    # room for connect + query + serialisation, not merely be under the wire.
    assert 0 < health.READY_BUDGET_S <= 4.0, (
        f"READY_BUDGET_S is {health.READY_BUDGET_S}; the kubelet gives up at 5s, so a "
        "larger budget re-creates the original outage with extra steps"
    )


# ── 3. the two release points ─────────────────────────────────────────────────


def _commits_before(fn, marker: str) -> bool:
    """True iff an `await <x>.commit()` appears before the first call to `marker`."""
    commit_line = None
    marker_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "commit" and commit_line is None:
                commit_line = node.lineno
            if node.func.attr == marker and marker_line is None:
                marker_line = node.lineno
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == marker
            and marker_line is None
        ):
            marker_line = node.lineno
    return commit_line is not None and marker_line is not None and commit_line < marker_line


def test_conformance_releases_the_connection_before_the_upstream_loop():
    fn = _fn(_tree("routers/architecture_tools.py"), "conformance_endpoint")
    assert _commits_before(fn, "fetch_code_units"), (
        "no `await session.commit()` before the fetch_code_units loop — the pooled "
        "connection is held across one 30s-timeout call per bound container"
    )


# ── 4. stale-connection hygiene, on every engine ──────────────────────────────
# ⛔ These pin STANDARDISATION, not the cause. The stale-socket hypothesis (a
# pgbouncer roll poisoning the pool) was REFUTED by measurement on 2026-09-03:
# 9 QueuePool episodes in a 13h window with zero pgbouncer rolls. They exist
# because this repo was the fleet outlier — `secrets/models.py`, `skill_builder`
# and `improvement-loop-service` all set exactly these two. The assertion shape
# (`pool._pre_ping` / `pool._recycle`) is copied from the fleet precedent at
# `secrets/tests/unit/test_models.py:238-245`.


def test_the_runtime_engine_pre_pings_and_recycles():
    import db

    assert db.engine.pool._pre_ping is True
    assert db.engine.pool._recycle == 3600


def test_the_health_engine_pre_pings_and_recycles():
    """The probe's own connection must not be the one dead socket in the pod."""
    import db

    assert db.health_engine.pool._pre_ping is True
    assert db.health_engine.pool._recycle == 3600


# ── 5. the shared pool spec must carry the hygiene settings ──────────────────


def _pool_kwargs_keys() -> set[str]:
    """The keys of the module-level `_POOL_KWARGS` dict literal in db.py."""
    for node in ast.walk(_tree("db.py")):
        if (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and isinstance(getattr(node, "value", None), ast.Dict)
            and any(
                isinstance(t, ast.Name) and t.id == "_POOL_KWARGS"
                for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
            )
        ):
            assert isinstance(node.value, ast.Dict)
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("no module-level `_POOL_KWARGS` dict literal in db.py")


def test_the_shared_pool_spec_carries_the_hygiene_settings():
    assert {"pool_size", "max_overflow", "pool_timeout", "pool_pre_ping", "pool_recycle"} <= (
        _pool_kwargs_keys()
    ), (
        "_POOL_KWARGS lost a key; every engine that unpacks it silently reverts to a "
        "SQLAlchemy default nobody reviewed"
    )


# ── 6. every engine must be observable ────────────────────────────────────────


def test_every_engine_is_registered_with_pool_watch():
    labels = {
        node.args[1].value
        for node in ast.walk(_tree("db.py"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "install"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pool_watch"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
    }
    assert labels == {"runtime", "health"}, (
        f"pool_watch covers {labels or 'nothing'}; an unwatched engine's checkouts are "
        "anonymous, which is the condition that made the holder unfindable in the first place"
    )
    # Deliberately an EQUALITY, not a subset: adding an engine must break this
    # test, so the author has to decide to watch it rather than silently ship a
    # pool whose checkouts are anonymous. (`owner` and `eval` left this set with
    # the LLM lane, 2026-10-26.)


# ── `max_overflow=0` must be settable ────────────────────────────────────────
# `_env_num` refused 0 for every setting, so `*_MAX_OVERFLOW=0` was silently
# replaced with the default. Found by an adversarial review, 2026-09-04. The rejection is still CORRECT for
# POOL_SIZE, where 0 means an unbounded pool — hence the negative control below,
# without which this pair would pass by simply removing the validation.


def test_max_overflow_zero_is_accepted_because_the_module_advises_it(monkeypatch):
    """A bounded, non-growing pool is a legitimate setting."""
    import db

    monkeypatch.delenv("X_OVERFLOW_PROBE", raising=False)
    assert db._env_num("X_OVERFLOW_PROBE", 4, int, allow_zero=True) == 4  # unset -> default
    monkeypatch.setenv("X_OVERFLOW_PROBE", "0")
    assert db._env_num("X_OVERFLOW_PROBE", 4, int, allow_zero=True) == 0


def test_pool_size_zero_is_still_refused_and_still_says_why(monkeypatch):
    """NEGATIVE CONTROL — deleting the validation would pass the test above.

    0 means an UNBOUNDED pool for pool_size, and `pool_is_saturated` would still
    take its ceiling from the 0: a pool that can never fill behind a gauge that
    thinks it always is.
    """
    import db

    monkeypatch.setenv("X_SIZE_PROBE", "0")
    assert db._env_num("X_SIZE_PROBE", 5, int) == 5  # falls back, loudly


def test_negative_values_are_refused_even_where_zero_is_allowed(monkeypatch):
    import db

    monkeypatch.setenv("X_NEG_PROBE", "-1")
    assert db._env_num("X_NEG_PROBE", 4, int, allow_zero=True) == 4


# ── The REAL call site must carry allow_zero, not just `_env_num` ─────────────
# An adversarial review found the tests above pin only `_env_num` itself: removing
# `allow_zero=True` from the production call sites left the whole pool suite
# green, so the change's stated purpose had no guard at all. These two pin the
# call site — one structurally, one behaviourally in a fresh interpreter (the
# module builds its engines at import, so it cannot be re-imported in-process).


def test_both_max_overflow_call_sites_pass_allow_zero() -> None:
    """Structural pin. Kills: deleting `allow_zero=True` from the call site."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "db.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    found: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_env_num"):
            continue
        if not (node.args and isinstance(node.args[0], ast.Constant)):
            continue
        name = node.args[0].value
        if not isinstance(name, str) or not name.endswith("MAX_OVERFLOW"):
            continue
        found[name] = any(
            k.arg == "allow_zero" and getattr(k.value, "value", False) is True
            for k in node.keywords
        )

    assert set(found) == {"DB_MAX_OVERFLOW"}, (
        f"expected exactly the one MAX_OVERFLOW setting, found {sorted(found)}"
    )
    missing = sorted(n for n, ok in found.items() if not ok)
    assert not missing, (
        f"{missing} no longer pass allow_zero=True, so `0` is silently discarded again."
    )


def test_max_overflow_zero_reaches_the_module_constants() -> None:
    """Behavioural pin, in a fresh interpreter.

    `db` builds its engines at import, so `importlib.reload` would create real
    engines as a side effect. A subprocess is the honest way to observe the
    module constants under a different environment.
    """
    import json
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "DB_MAX_OVERFLOW": "0",
    }
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,json; sys.path.insert(0,'src'); import db; "
            "print(json.dumps([db.MAX_OVERFLOW]))",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [0], (
        f"MAX_OVERFLOW did not reach 0: {out.stdout!r}"
    )
