"""KEEP-OUT: the SYSTEM tenant must never read another tenant's data.

User ruling 2026-10-02. The system tenant's identity was, on the (since-removed,
R351 (7)) tokenless in-mesh path, an ASSERTED fact (``DEFAULT_TENANT_ID``
fallback / bare ``X-Tenant-Id``), so any read widening keyed on it is a
cross-tenant leak (the
0045 ``system_superadmin_read`` incident, dropped by 0046). This file fails if
a future change re-opens it, at three layers:

1. migrations: the NET set of RLS policies after every CREATE/DROP, and the
   predicate of any non-tenant-isolation survivor;
2. ``scope()``: SQL shape for the system tenant;
3. HTTP: the system caller sees own rows only; a tokenless caller is 401
   whatever tenant it names (or none).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from auth.middleware import AuthMiddleware
from fleet_auth import clear_license_cache
from models.principles import Principle
from repositories.visibility import is_inherited_visible, scope
from settings import get_settings
from tests.tenants import T

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
SYSTEM = get_settings().system_tenant_id
CUSTOMER = "0195f48b-d000-7000-8000-000000000100"

# EXPLICIT allowlist of policy names that may survive the migration chain.
TENANT_ISOLATION_FAMILY = frozenset(
    {
        "tenant_isolation",
        "tenant_isolation_read",
        "tenant_isolation_ins",
        "tenant_isolation_upd",
        "tenant_isolation_del",
    }
)
# The ONLY policy allowed to admit rows of a tenant other than the caller's: the
# system tenant's PUBLISHED tenant-visible rows (read-only inheritance).
ALLOWED_CROSS_TENANT_POLICIES = frozenset({"system_published_read"})
ALLOWED_POLICIES = TENANT_ISOLATION_FAMILY | ALLOWED_CROSS_TENANT_POLICIES

_EVENT = re.compile(
    r"(?P<op>CREATE|DROP)\s+POLICY\s+(?:IF\s+EXISTS\s+)?"
    r"(?P<name>\{[A-Za-z_]+\}|\"?[A-Za-z_][A-Za-z0-9_]*\"?|\{policy\})",
    re.IGNORECASE,
)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _upgrade_source(path: Path) -> tuple[str, dict[str, str]]:
    src = path.read_text()
    tree = ast.parse(src)
    consts = _module_constants(tree)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return ast.get_source_segment(src, node) or "", consts
    return "", consts


def _events_in(upgrade_src: str, consts: dict[str, str]) -> list[tuple[str, str]]:
    """Ordered (CREATE|DROP, policy-name) events found in an upgrade() body."""
    events: list[tuple[str, str]] = []
    for m in _EVENT.finditer(upgrade_src):
        name = m.group("name").strip('"')
        if name.startswith("{"):
            name = consts.get(name.strip("{}"), "")
        if name and name.upper() not in {"ON"}:
            events.append((m.group("op").upper(), name))
    return events


def net_policies() -> dict[str, str]:
    """Policy name -> migration file that (last) created it, net of later DROPs."""
    live: dict[str, str] = {}
    for path in sorted(VERSIONS.glob("2*.py")):
        src, consts = _upgrade_source(path)
        for op, name in _events_in(src, consts):
            if op == "CREATE":
                live[name] = path.name
            else:
                live.pop(name, None)
    return live


_TENANT_CONJUNCT = re.compile(
    r"(?:^|\bAND\s+)\(?\s*tenant_id\s*=\s*'[0-9a-fA-F-]{36}'(?:::uuid)?\s*\)?(?:\s+AND\b|\s*$)",
    re.IGNORECASE,
)
_FORBIDDEN_TOKENS = re.compile(r"\b(?:OR|TRUE|IS\s+NOT\s+NULL)\b", re.IGNORECASE)


_GUC_CALL = re.compile(
    r"current_setting\(\s*'[a-z_][a-z0-9_.]*'(?:::text)?\s*,\s*true\s*\)", re.IGNORECASE
)


def predicate_admits_by_caller_alone(predicate: str) -> bool:
    """True unless the predicate is a plain AND-chain anchored on ``tenant_id = '<uuid>'``.

    Fails CLOSED. Accepted only when (a) one top-level AND conjunct pins
    ``tenant_id`` to a literal uuid and (b) it contains no ``OR``, ``TRUE`` or
    ``IS NOT NULL`` — so the 0045 shape (``current_setting(...) = '<sys>'``) and a
    vacuous ``tenant_id IS NOT NULL OR true`` are both rejected; a bare mention
    of the word ``tenant_id`` is NOT a fence.
    """
    pred = " ".join(predicate.split())
    # The ONLY sanctioned use of the literal ``true``: current_setting's
    # missing_ok argument. Neutralise exactly that call shape before scanning, so
    # ``current_setting('app.current_tenant', true)`` is not mistaken for a
    # vacuous boolean while a real ``OR true`` / ``= true`` still is.
    scan = _GUC_CALL.sub("GUC()", pred)
    if _FORBIDDEN_TOKENS.search(scan):
        return True
    return _TENANT_CONJUNCT.search(pred) is None


# Exact pin for every surviving cross-tenant policy: changing a predicate (even
# to one that still mentions tenant_id) must be a reviewed edit of this table.
PINNED_PREDICATES = {
    "system_published_read": (
        f"tenant_id = '{SYSTEM}'::uuid AND visibility = 'tenant' AND version_status = 'PUBLISHED' "
        "AND current_setting('app.current_tenant', true) IN ("
        "'0195f48b-d000-7000-8000-000000000001', "
        "'0195f48b-d000-7000-8000-000000000002', "
        "'0195f48b-d000-7000-8000-000000000003', "
        "'0195f48b-d000-7000-8000-000000000004', "
        "'0195f48b-d000-7000-8000-000000000005', "
        "'019de842-c92a-7e37-8fdd-eb0d8c5396f9')"
    ),
}


def _evaluated_predicate(fname: str) -> str:
    """The migration module's evaluated ``_PREDICATE`` (f-strings resolved)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"_mig_{fname[:-3]}", VERSIONS / fname)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return " ".join(str(mod._PREDICATE).split())


def _surviving_predicates() -> dict[str, str]:
    """Evaluated ``_PREDICATE`` for each surviving non-isolation policy's file."""
    return {
        name: _evaluated_predicate(fname)
        for name, fname in net_policies().items()
        if name not in TENANT_ISOLATION_FAMILY
    }


# ── 1. migrations ──────────────────────────────────────────────────────────


def test_detector_sees_the_0045_policy_and_0046_drop() -> None:
    """Positive control: the parser is not blind — it finds 0045's create and 0046's drop."""
    s45, c45 = _upgrade_source(VERSIONS / "2026_10_01_0045_system_superadmin_read_policy.py")
    s46, c46 = _upgrade_source(VERSIONS / "2026_10_02_0046_drop_system_superadmin_read.py")
    assert ("CREATE", "system_superadmin_read") in _events_in(s45, c45)
    assert ("DROP", "system_superadmin_read") in _events_in(s46, c46)
    assert "system_published_read" in net_policies()
    assert "tenant_isolation" in net_policies()


def test_net_policy_set_is_only_the_allowed_names() -> None:
    surviving = set(net_policies())
    unexpected = surviving - ALLOWED_POLICIES
    assert not unexpected, (
        f"migration chain leaves RLS policies outside the keep-out allowlist: "
        f"{sorted(unexpected)} (from {[net_policies()[n] for n in sorted(unexpected)]}). "
        "No policy may admit another tenant's rows (system tenant must never read "
        "other tenants' data)."
    )
    assert "system_superadmin_read" not in surviving


def test_surviving_non_isolation_policy_predicates_are_tenant_scoped() -> None:
    preds = _surviving_predicates()
    assert "system_published_read" in preds  # the control: it IS found and checked
    for name, pred in preds.items():
        assert not predicate_admits_by_caller_alone(pred), (
            f"policy {name} admits rows by caller identity alone: {pred}"
        )


def test_surviving_cross_tenant_predicates_match_their_pin_exactly() -> None:
    preds = _surviving_predicates()
    assert set(preds) == set(PINNED_PREDICATES), (
        f"surviving cross-tenant policies {sorted(preds)} != pinned {sorted(PINNED_PREDICATES)}"
    )
    for name, pred in preds.items():
        assert pred == PINNED_PREDICATES[name], f"{name} predicate changed: {pred!r}"


@pytest.mark.parametrize(
    "bad",
    [
        f"current_setting('app.current_tenant', true) = '{SYSTEM}'",  # the 0045 shape
        "tenant_id IS NOT NULL OR true",  # the vacuous mutant from the #192 review
        f"tenant_id = '{SYSTEM}'::uuid OR true",
        f"tenant_id = '{SYSTEM}'::uuid OR visibility = 'tenant'",
        "tenant_id IS NOT NULL",
        "tenant_id = tenant_id",
        "true",
        # the GUC-call exemption must not open a hole:
        f"tenant_id = '{SYSTEM}'::uuid AND current_setting('app.current_tenant', true) IS NOT NULL",
        f"tenant_id = '{SYSTEM}'::uuid AND current_setting('app.current_tenant', true) IN ('x') OR true",
        f"tenant_id = '{SYSTEM}'::uuid AND (current_setting('app.current_tenant', true) = 'x') = true",
        "current_setting('app.current_tenant', true) IN ('x')",  # no tenant_id anchor at all
    ],
)
def test_detector_rejects_vacuous_and_caller_only_predicates(bad: str) -> None:
    """Negative controls: each must be rejected — mentioning tenant_id is not a fence."""
    assert predicate_admits_by_caller_alone(bad), bad


def test_detector_accepts_the_real_tenant_anchored_shape() -> None:
    assert not predicate_admits_by_caller_alone(PINNED_PREDICATES["system_published_read"])


def test_pin_rejects_a_widened_system_published_read() -> None:
    """A predicate that still passes the shape rule but differs from the pin fails the pin."""
    widened = f"tenant_id = '{SYSTEM}'::uuid AND visibility = 'tenant'"
    assert not predicate_admits_by_caller_alone(widened)
    assert widened != PINNED_PREDICATES["system_published_read"]


# Every migration file that touches RLS (policy DDL, RLS toggles, BYPASSRLS,
# row_security) is pinned by NAME + SHA-256 of its full text. A new such file —
# whatever its name sorts as (alembic orders by down_revision, not filename) —
# or any edit to a pinned one (e.g. re-creating system_published_read with a
# different USING while leaving _PREDICATE untouched) fails until reviewed and
# re-pinned here. This replaced a filename-cutoff rule a validator bypassed on
# #192 with a file named to sort before the cutoff.
_DANGER_TOKENS = (
    r"\b(?:CREATE|ALTER|DROP)\s+POLICY\b"
    r"|\b(?:ENABLE|DISABLE|NO\s+FORCE|FORCE)\s+ROW\s+LEVEL\s+SECURITY\b"
    r"|\bBYPASSRLS\b|\brow_security\b|\bSUPERUSER\b|\bSET\s+(?:LOCAL\s+|SESSION\s+)?ROLE\b"
    # A view / owner change / grant / SECURITY DEFINER function runs as its owner
    # and so reads past RLS: same class, different spelling.
    r"|\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\b"
    # GRANT / OWNER TO are SQL-shaped and upper-case only: prose such as
    # "client-credentials grant" must not trip the scan.
    r"|(?-i:\bOWNER\s+TO\b)|(?-i:\bGRANT\s+\S+(?:\s+\S+)?\s+(?:ON|TO)\b)"
    r"|\bSECURITY\s+DEFINER\b"
)
_RLS_TOUCH = re.compile(_DANGER_TOKENS, re.IGNORECASE)


def _normalized_variants(text: str) -> tuple[str, str]:
    """(raw, normalized). Normalized joins adjacent string literals ('CREATE ' 'POLICY')
    and turns escaped newlines/tabs into spaces, so a keyword split across literals or
    an embedded \\n still matches. \\s+ in the tokens already spans real newlines."""
    norm = re.sub(r"(['\"])\s*\+?\s*\1", " ", text)  # "a" "b" / 'a' + 'b' -> a b
    norm = norm.replace("\\n", " ").replace("\\t", " ")
    return text, norm


def _touches_rls(text: str) -> bool:
    return any(_RLS_TOUCH.search(v) for v in _normalized_variants(text))


PINNED_RLS_MIGRATIONS: dict[str, str] = {
    # Re-pinned 2026-11-01 (chore/remove-kag-common): both files had their RLS
    # DDL INLINED (frozen from kag_common.database.rls 0.37.0) so a historical
    # migration no longer depends on an installed package. Reviewed: the emitted
    # SQL is byte-identical to what the library produced for these revisions.
    "2026_07_02_0030_rls_tenant_isolation_backstop.py": "e8156f9dfcda062d54ff1c54fc9a3b6050214426200bb6bfd5eae0e6a5ae6b5e",  # pragma: allowlist secret
    "2026_07_09_0031_principle_conflicts.py": "4854d651e2b9516f5459b0ba4474407a3f7e7e8c3274e23a4ccce93c478d4a3d",  # pragma: allowlist secret
    "2026_08_24_0042_tenant_id_is_a_uuid.py": "2bb9cd6e8b844efacea8b3045a77d9271e67206f31a8d2bcfdaa39f56d92b917",  # pragma: allowlist secret
    "2026_09_23_0043_quality_evaluation_history.py": "d2e07d3729685d7ad75343d5a01a273a86e2ed6fb78570e82810c298f2761aba",  # pragma: allowlist secret
    "2026_09_27_0044_system_published_read_policy.py": "8ef565ad3855667900973dcdaacb92bed415d5dbfa7aafb533505cb273a14224",  # pragma: allowlist secret
    "2026_10_01_0045_system_superadmin_read_policy.py": "4396400349223a11b74d9dccb93004a641ba1b68b8c555253af46f1ef78a4517",  # pragma: allowlist secret
    "2026_10_02_0046_drop_system_superadmin_read.py": "62fb7f420095d5fbdbb0e8c3222bbe21ace8b78fb3824f88be6b6f394bbf0a35",  # pragma: allowlist secret
    "2026_10_02_0047_narrow_system_published_read.py": "61b185872ab589e280e1ab59affe625c4af8034a249144e6b7b6d866edcabe23",  # pragma: allowlist secret
    "2026_10_12_0050_pm_catalogue.py": "d4d3d2689f07029c6f9d94034f2ac1d53ba6ec729390cd596df9dc6c0e10ba00",  # pragma: allowlist secret
    "2026_10_12_0051_pm_catalogue_tier1.py": "ee0e1e55899d076354142e4a887c521efd9a87cfcba2dcc16e5b504cf61400aa",  # pragma: allowlist secret
    "2026_10_12_0052_pm_catalogue_tier2.py": "89fc0928c36e0fbb13c3e6f37e298c47a9efa01b1d0ccf983f834e95f248e93c",  # pragma: allowlist secret
    "2026_10_13_0053_pm_project_type_phase.py": "c5646137f0bafdddc2cb95a0b054d7a88caa497e187cf91126de88caa9f2b6d9",  # pragma: allowlist secret
    "2026_10_13_0056_pm_project_intake.py": "9e44f1e873b28a73f9bf23676ad244c6b0f178e4b6e5535a6a01151269abc6d6",  # pragma: allowlist secret
    "2026_10_15_0059_pm_project_capabilities.py": "04767cc1a96d62dff3b735f8a6cc041aacaf585ca5acb30c7848261c0fa566b6",  # pragma: allowlist secret
    "2026_10_19_0062_pm_coach_actions.py": "6b018ad7035e3e09a7b84ba0c32e5b4b012de7aea236d1c2511e647b99cc62e1",  # pragma: allowlist secret
    "2026_10_27_0068_idempotency_keys_drop_title_identity.py": "9cebc63fc1be39b78b1b51706162272525e90ed68a4359551bd18b7a188d8d6c",  # pragma: allowlist secret
    "2026_10_27_0069_modeling_models_and_view_occurrences.py": "6c96fe07d9bcd29a5f4b2fcbc6a3366598907694d599e9c20bbbdbc0520ad80e",  # pragma: allowlist secret
    "2026_10_27_0070_modeling_runs_links_changesets_reviews.py": "27dc140be72c22838132fe615a70d18c94476ec7fd624be10b26305c3e389f51",  # pragma: allowlist secret
    "2026_10_27_0073_uml_storage.py": "1a1a38cc53156c6201e77ebb4d3cb7639fbe48fa8235fa658f89859ca0267aff",  # pragma: allowlist secret
    "2026_11_04_0080_pm_card_ledger.py": "76be7ad87d3cf5736765273bd2841295c3244ab8ac81b9b45c3f17c218d907e5",  # pragma: allowlist secret
}


def rls_touching_migrations() -> dict[str, str]:
    """name -> sha256 for every alembic file (ANY name) whose text touches RLS."""
    import hashlib

    out = {}
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text()
        if _touches_rls(text):
            out[path.name] = hashlib.sha256(text.encode()).hexdigest()
    return out


def test_rls_touching_migrations_are_exactly_the_reviewed_pins() -> None:
    found = rls_touching_migrations()
    assert "2026_09_27_0044_system_published_read_policy.py" in found  # control: detector sees
    new = sorted(set(found) - set(PINNED_RLS_MIGRATIONS))
    assert not new, f"unreviewed migration(s) touching RLS: {new} — review and pin name+sha256"
    changed = sorted(
        n for n in found if n in PINNED_RLS_MIGRATIONS and found[n] != PINNED_RLS_MIGRATIONS[n]
    )
    assert not changed, f"pinned RLS migration(s) edited: {changed} — review and re-pin"
    gone = sorted(set(PINNED_RLS_MIGRATIONS) - set(found))
    assert not gone, f"pinned RLS migration(s) missing/renamed: {gone}"


REPO = VERSIONS.parents[1]
_POLICY_DDL = re.compile(_DANGER_TOKENS, re.IGNORECASE)
_RLS_LOOSENING = re.compile(
    r"\bALTER\s+POLICY\b|\b(?:DISABLE|NO\s+FORCE)\s+ROW\s+LEVEL\s+SECURITY\b|\bBYPASSRLS\b",
    re.IGNORECASE,
)
_CODE_SUFFIXES = {
    ".py",
    ".sql",
    ".sh",
    ".yaml",
    ".yml",
    ".toml",
    ".j2",
    ".tmpl",
    ".cfg",
    ".ini",
    ".conf",
    ".txt",
    ".json",
}
# Policy DDL outside alembic/versions is never allowed: a startup hook, seed
# script or SQL file could otherwise (re)define a policy the migration-chain
# checks never see. Reviewed exceptions go here as repo-relative paths.
POLICY_DDL_OUTSIDE_MIGRATIONS_ALLOWLIST: set[str] = {
    # Reviewed 2026-11-01 (chore/remove-kag-common). src/fleet_auth.py RENDERS
    # policy statement strings (upgrade_statements/downgrade_statements); it
    # never EXECUTES them — the only runtime listener it installs is
    # register_tenant_guc, which emits `set_config('app.current_tenant', …)`.
    # The two migrations that own the policy DDL now carry frozen INLINE copies
    # and do not import this module, so the migration chain cannot be altered by
    # editing it. The consumers of the builders are tests, which use them to
    # install the SAME policy production runs. Vendored from kag_common 0.37.0,
    # which lived outside this repo and was therefore invisible to this scan.
    "src/fleet_auth.py",
}
# ALTER POLICY / disabling RLS inside a migration bypasses the CREATE-based
# net-set and predicate checks entirely; each one must be a reviewed pin.
PINNED_RLS_LOOSENING: set[tuple[str, str]] = {
    # 0042 drops the policies and DISABLEs RLS only to retype tenant_id
    # (text -> uuid), then re-enables + re-forces it and re-creates the policies
    # in the same upgrade() — checked by test_0042_re_enables_rls below.
    ("2026_08_24_0042_tenant_id_is_a_uuid.py", "DISABLE ROW LEVEL SECURITY"),
}


def _code_files_outside_migrations() -> list[Path]:
    out = []
    for path in REPO.rglob("*"):
        rel = path.relative_to(REPO)
        if not path.is_file() or path.suffix not in _CODE_SUFFIXES:
            continue
        top = rel.parts[0]
        if top in {"tests", "docs", ".git", ".venv", "node_modules", ".worktrees"}:
            continue
        if rel.parts[:2] == ("alembic", "versions"):
            continue
        out.append(path)
    return out


def policy_ddl_outside_migrations() -> list[str]:
    """Whole-file scan (not line by line: a multi-line CREATE⏎POLICY must match)."""
    hits = []
    for path in _code_files_outside_migrations():
        rel = str(path.relative_to(REPO))
        if rel in POLICY_DDL_OUTSIDE_MIGRATIONS_ALLOWLIST:
            continue
        text = path.read_text(errors="ignore")
        for variant in _normalized_variants(text):
            m = _POLICY_DDL.search(variant)
            if m:
                hits.append(f"{rel}: {' '.join(m.group(0).split())}")
                break
    return hits


def rls_loosening_in_migrations() -> list[tuple[str, str]]:
    hits = []
    for path in sorted(VERSIONS.glob("2*.py")):
        src, _ = _upgrade_source(path)
        for m in _RLS_LOOSENING.finditer(src):
            hits.append((path.name, m.group(0).upper()))
    return hits


def test_scan_covers_the_code_tree() -> None:
    """Control: the scan really walks src/ (not an empty file list)."""
    files = {str(p.relative_to(REPO)) for p in _code_files_outside_migrations()}
    assert "src/repositories/visibility.py" in files


def test_no_policy_ddl_outside_migrations() -> None:
    hits = policy_ddl_outside_migrations()
    assert not hits, (
        "RLS policy DDL outside alembic/versions (review + allowlist, or move it):\n"
        + "\n".join(hits)
    )


def test_no_unreviewed_alter_policy_or_rls_disable_in_migrations() -> None:
    unreviewed = [h for h in rls_loosening_in_migrations() if h not in PINNED_RLS_LOOSENING]
    assert not unreviewed, (
        f"ALTER POLICY / RLS disable in migrations needs a reviewed pin: {unreviewed}"
    )


def test_0042_re_enables_rls() -> None:
    """The one pinned DISABLE must be followed, in the same upgrade(), by ENABLE and
    by re-creating ``tenant_isolation`` (prod connects as the non-owner
    ``requirements_app``, so ENABLE without FORCE still binds it)."""
    src, _ = _upgrade_source(VERSIONS / "2026_08_24_0042_tenant_id_is_a_uuid.py")
    i = src.upper().index("DISABLE ROW LEVEL SECURITY")
    tail = src[i:].upper()
    assert "ENABLE ROW LEVEL SECURITY" in tail
    assert "CREATE POLICY TENANT_ISOLATION" in tail


def test_prose_does_not_trip_the_scan() -> None:
    for prose in (
        "Keycloak token endpoint for the client-credentials grant",
        "we grant access to the owner to review",
        "the policy of this repo is documented elsewhere",
    ):
        assert not _touches_rls(prose), prose


@pytest.mark.parametrize(
    "line",
    [
        "ALTER POLICY tenant_isolation ON principles USING (tenant_id IS NOT NULL OR true)",
        "alter policy system_published_read on requirements with check (true)",
        "CREATE POLICY sneaky ON principles FOR SELECT USING (true)",
        "ALTER TABLE principles DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE principles NO FORCE ROW LEVEL SECURITY",
        "ALTER ROLE requirements_app BYPASSRLS",
        "CREATE\n    POLICY sneaky ON principles USING (true)",  # multi-line (real newline)
        'op.execute("CREATE " "POLICY sneaky ON principles USING (true)")',  # split literals
        "op.execute('ALTER ' + 'POLICY tenant_isolation ON principles USING (true)')",
        'sql = "CREATE\\nPOLICY sneaky ON principles USING (true)"',  # escaped \n
        "CREATE OR REPLACE VIEW all_principles AS SELECT * FROM principles",
        "ALTER TABLE principles OWNER TO requirements_app",
        "GRANT requirements_owner TO requirements_app",
        "CREATE FUNCTION f() RETURNS SETOF principles LANGUAGE sql SECURITY DEFINER AS $$ SELECT * FROM principles $$",
        "SET ROLE postgres",
        "SET row_security = off",
    ],
)
def test_ddl_detector_controls(line: str) -> None:
    """Negative controls: every policy-loosening / RLS-bypassing form is recognised."""
    assert _touches_rls(line), line


# ── 2. scope() SQL shape ───────────────────────────────────────────────────


def _where_sql(stmt) -> str:
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    parts = re.split(r"\sWHERE\s", sql, maxsplit=1)
    assert len(parts) == 2, f"no WHERE clause at all (cross-tenant read): {sql}"
    return parts[1]


_TENANT_EQ = re.compile(r"\(?\s*(?:\w+\.)?tenant_id\s*=\s*'([0-9a-fA-F-]{32,36})'\s*\)?")


def _top_level(sql: str, op: str) -> list[str]:
    """Split ``sql`` on ``op`` (AND/OR) only at parenthesis depth 0."""
    parts, depth, buf, i = [], 0, [], 0
    token = f" {op} "
    while i < len(sql):
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and sql[i : i + len(token)].upper() == token:
            parts.append("".join(buf))
            buf, i = [], i + len(token)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


@pytest.mark.parametrize("include_system", [True, False])
def test_scope_for_system_tenant_is_tenant_predicated(include_system: bool) -> None:
    stmt = scope(
        select(Principle),
        Principle,
        tenant_id=SYSTEM,
        subject_id=SYSTEM,
        include_system=include_system,
        system_tenant_id=SYSTEM,
        platform_tenant_ids={SYSTEM},
    )
    where = _where_sql(stmt)
    # The system tenant never widens: at the TOP level its WHERE is an AND-chain
    # with an own-tenant conjunct. A top-level OR is a widening arm (that is how
    # a cross-tenant read is spelled); ORs nested inside the own-tenant AND (the
    # tenant/user visibility choice) are fine.
    conjuncts = _top_level(where, "AND")
    assert len(_top_level(where, "OR")) == 1, f"system scope widens at top level: {where}"
    own = SYSTEM.replace("-", "").lower()
    pinned = [
        m.group(1).replace("-", "").lower()
        for c in conjuncts
        if (m := _TENANT_EQ.fullmatch(c.strip())) is not None
    ]
    assert own in pinned, f"no top-level own-tenant conjunct: {where}"


def test_scope_for_customer_and_platform_stay_tenant_predicated() -> None:
    for tenant in (CUSTOMER, T("platform-x")):
        stmt = scope(
            select(Principle),
            Principle,
            tenant_id=tenant,
            subject_id=tenant,
            include_system=True,
            system_tenant_id=SYSTEM,
            platform_tenant_ids={T("platform-x")},
        )
        where = _where_sql(stmt)
        assert any(
            re.fullmatch(r"\(?\s*(?:\w+\.)?tenant_id\s*=\s*'[0-9a-fA-F-]{32,36}'\s*\)?", c.strip())
            for side in _top_level(where, "OR")
            for c in _top_level(side.strip().strip("()"), "AND")
        ), f"no tenant-pinned arm: {where}"


# ── 3. HTTP: system caller sees own rows only ──────────────────────────────


def _p(slug: str, tenant: str) -> Principle:
    return Principle(
        slug=slug,
        title=f"P {slug}",
        statement=f"S {slug}.",
        tenant_id=tenant,
        tenant_name="keepout-probe",
        subject_id=tenant,
        visibility="tenant",
        rationale=None,
        implications=[],
        tags=[],
        active=True,
        version_status="PUBLISHED",
    )


@pytest.mark.tenant_isolation
async def test_system_tenant_licence_sees_only_system_rows(test_client, test_session) -> None:
    sys_row, cust_row = _p("keepout-sys", SYSTEM), _p("keepout-cust", CUSTOMER)
    test_session.add_all([sys_row, cust_row])
    await test_session.commit()
    await test_session.refresh(sys_row)
    await test_session.refresh(cust_row)
    await test_session.commit()

    test_client.headers = {"X-Tenant-Id": SYSTEM}
    resp = await test_client.get("/principles", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    ids = {i["id"] for i in resp.json()}
    assert str(sys_row.id) in ids
    assert str(cust_row.id) not in ids
    assert (await test_client.get(f"/principles/{cust_row.id}")).status_code == 404

    # customer: own rows only (never the system tenant's, never widened)
    test_client.headers = {"X-Tenant-Id": CUSTOMER}
    resp = await test_client.get("/principles", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    ids = {i["id"] for i in resp.json()}
    assert ids == {str(cust_row.id)}


# ── 3b. No token ⇒ 401: the system tenant cannot be ASSERTED ──────────────


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/requirements/echo")
    async def _echo(request: Request) -> JSONResponse:
        return JSONResponse({"tenant_id": request.state.user.tenant_id})

    return app


@pytest.fixture(autouse=True)
def _fresh() -> None:
    clear_license_cache()


async def _get(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get("/requirements/echo", headers=headers)


@pytest.mark.tokenless
@pytest.mark.parametrize("hdr", [None, "", "   ", SYSTEM, CUSTOMER])
async def test_tokenless_caller_is_refused_whatever_tenant_it_names(hdr) -> None:
    """No default-tenant fallback and no header-asserted tenant: 401, from loopback."""
    resp = await _get({} if hdr is None else {"X-Tenant-Id": hdr})
    assert resp.status_code == 401, resp.text


def test_top_level_detector_controls() -> None:
    """The splitter must see a top-level OR and ignore a nested one."""
    nested = "t.tenant_id = 'abc' AND (v = 'tenant' OR v = 'user')"
    widened = "t.tenant_id = 'abc' OR t.tenant_id = 'def'"
    assert len(_top_level(nested, "OR")) == 1
    assert len(_top_level(widened, "OR")) == 2


# ── 4. is_inherited_visible (id-loaded rows, e.g. relationship endpoints) ──


class _Row:
    def __init__(
        self, tenant_id: str, visibility: str = "tenant", version_status: str = "PUBLISHED"
    ):
        self.tenant_id = tenant_id
        self.visibility = visibility
        self.version_status = version_status


@pytest.mark.parametrize(
    "owner",
    [CUSTOMER, "019de842-c92a-7e37-8fdd-eb0d8c5396f9", "0195f48b-d000-7000-8000-000000000003"],
)
@pytest.mark.parametrize("status", ["PUBLISHED", "DRAFT"])
def test_system_caller_cannot_see_another_tenants_row_by_id(owner: str, status: str) -> None:
    """relationship_service validates edge endpoints with this: the SYSTEM tenant
    must not 'see' a customer's / platform tenant's element (validator finding on #192)."""
    row = _Row(owner, version_status=status)
    assert not is_inherited_visible(
        row, tenant_id=SYSTEM, system_tenant_id=SYSTEM, platform_tenant_ids={SYSTEM}
    )
    assert not is_inherited_visible(row, tenant_id=SYSTEM, system_tenant_id=SYSTEM)


def test_is_inherited_visible_controls() -> None:
    """Positive controls: own rows are visible; a PLATFORM caller inherits a published system row."""
    assert is_inherited_visible(
        _Row(SYSTEM, version_status="DRAFT"), tenant_id=SYSTEM, system_tenant_id=SYSTEM
    )
    platform = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"
    assert is_inherited_visible(
        _Row(SYSTEM), tenant_id=platform, system_tenant_id=SYSTEM, platform_tenant_ids={platform}
    )
    assert not is_inherited_visible(
        _Row(SYSTEM), tenant_id=CUSTOMER, system_tenant_id=SYSTEM, platform_tenant_ids={platform}
    )
