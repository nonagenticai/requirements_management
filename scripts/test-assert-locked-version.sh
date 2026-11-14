#!/usr/bin/env bash
# Unit + wiring tests for the uv-lock bump guard.
#
# Two halves, both required:
#   1. assert-locked-version.sh in isolation — does it fail on a lock that did
#      not actually move, and pass on one that did?
#   2. pip-audit-autofix.sh's apply_upgrade() driven against a stub `uv` — does
#      the guard actually FIRE from the production code path, and does a real
#      bump still go through (green half)?
#
# Plain bash, no framework — mirrors gitops-production's
# .github/actions/rmt-drift-guard/test-rmt-drift-guard.sh convention.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD="$REPO_ROOT/scripts/assert-locked-version.sh"
AUTOFIX="$REPO_ROOT/scripts/pip-audit-autofix.sh"

PASSED=0
fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { PASSED=$((PASSED + 1)); echo "  ok  $*"; }

# macOS ships `shasum`, most Linux images ship `sha256sum`; require one.
if command -v sha256sum >/dev/null 2>&1; then
  sha256_of() { sha256sum "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
  sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }
else
  fail "neither sha256sum nor shasum available"
fi

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

[[ -x "$GUARD" ]]   || fail "guard not executable: $GUARD"
[[ -x "$AUTOFIX" ]] || fail "autofix not executable: $AUTOFIX"

# Both scripts must PARSE under every bash that will run them. /bin/bash on
# macOS is 3.2.57, which is what GitHub Actions uses for `run:` steps on the
# self-hosted mac runners and what `#!/usr/bin/env bash` resolves to on
# mini1/mini2. Its command-substitution parser scans for quote characters
# before processing a heredoc, so one apostrophe inside the `$(python3 - <<PY
# ... PY)` block in pip-audit-autofix.sh made the entire file unparseable
# there — bash exited 2 without executing a line. That failure is invisible to
# any check that only looks at an exit code being non-zero, which is exactly
# what the guard is for.
for _sh in /bin/bash "$(command -v bash || true)"; do
  [[ -n "$_sh" && -x "$_sh" ]] || continue
  _ver="$("$_sh" --version 2>/dev/null | head -1)"
  "$_sh" -n "$AUTOFIX" || fail "$AUTOFIX does not parse under $_sh — $_ver"
  "$_sh" -n "$GUARD"   || fail "$GUARD does not parse under $_sh — $_ver"
  ok "parses under $_sh (${_ver#GNU bash, })"
done

# ---------------------------------------------------------------------------
# Fixture lockfile. Deliberately includes the shapes that break naive parsers:
#   * an indented `name =` inside [package.metadata] / [[package.dependencies]]
#     (must NOT be mistaken for a resolved package)
#   * a non-normalized distribution name (en_core_web_sm)
#   * the same package resolved twice under different platform markers
# ---------------------------------------------------------------------------
LOCK="$TMPROOT/uv.lock"
cat > "$LOCK" <<'TOML'
version = 1
requires-python = ">=3.14"

[[package]]
name = "torch"
version = "2.9.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "filelock" },
]

[[package]]
name = "cryptography"
version = "50.0.0"
source = { registry = "https://pypi.org/simple" }

[package.metadata]
requires-dist = [
    { name = "cryptography", specifier = ">=46.0.5" },
]

[[package]]
name = "en_core_web_sm"
version = "3.8.0"
source = { url = "https://example.invalid/en_core_web_sm-3.8.0-py3-none-any.whl" }

[[package]]
name = "urllib3"
version = "2.7.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "urllib3"
version = "2.5.0"
source = { registry = "https://pypi.org/simple" }
TOML

expect_guard() {
  local want="$1" pkg="$2" min="$3" lock="${4:-$LOCK}" desc="$5"
  local rc=0
  "$GUARD" "$pkg" "$min" "$lock" >/dev/null 2>&1 || rc=$?
  [[ "$rc" == "$want" ]] || fail "$desc — expected exit $want, got $rc"
  ok "$desc (exit $rc)"
}

echo "== assert-locked-version.sh =="

# Resolved strictly above the floor.
expect_guard 0 cryptography 46.0.5 "$LOCK" "locked 50.0.0 >= floor 46.0.5"
# Resolved exactly at the floor.
expect_guard 0 cryptography 50.0.0 "$LOCK" "locked 50.0.0 >= floor 50.0.0"
# Resolved BELOW the floor: this is the transitive silent no-op — `uv lock
# --upgrade-package torch` exits 0, changes nothing, torch stays vulnerable.
expect_guard 1 torch 2.13.0 "$LOCK" "locked 2.9.0 < floor 2.13.0 must FAIL"
# ... and the same pair proves the compare is numeric, not lexicographic:
# as strings "2.9.0" > "2.13.0", so a string compare would have passed it.
expect_guard 0 torch 2.9.0 "$LOCK" "locked 2.9.0 >= floor 2.9.0 (numeric, not lexicographic)"
# Absent from the graph entirely — a typo'd or renamed distribution. uv lock
# exits 0 having resolved nothing. "Not found" must never be "clean".
expect_guard 1 this-package-does-not-exist "1.0.0" "$LOCK" "package absent from lock must FAIL"
# PEP 503 name normalization: underscores in the lock, hyphens on the CLI.
expect_guard 0 en-core-web-sm 3.8.0 "$LOCK" "PEP 503 normalization matches en_core_web_sm"
expect_guard 1 en-core-web-sm 3.9.0 "$LOCK" "normalized name still compares its version"
# Two resolved entries under different markers; one is below the floor.
expect_guard 1 urllib3 2.7.0 "$LOCK" "one of two resolved entries below floor must FAIL"
expect_guard 0 urllib3 2.5.0 "$LOCK" "both resolved entries at/above floor passes"
# Pre-releases sort below the final release of the same number.
PRELOCK="$TMPROOT/pre.lock"
printf '[[package]]\nname = "cryptography"\nversion = "50.0.0rc1"\n' > "$PRELOCK"
expect_guard 1 cryptography 50.0.0 "$PRELOCK" "50.0.0rc1 < 50.0.0 must FAIL"
expect_guard 0 cryptography 50.0.0rc1 "$PRELOCK" "50.0.0rc1 >= 50.0.0rc1 passes"
# Fail closed on nonsense rather than treating it as satisfied.
BADLOCK="$TMPROOT/bad.lock"
printf '[[package]]\nname = "weird"\nversion = "not-a-version"\n' > "$BADLOCK"
expect_guard 1 weird 1.0.0 "$BADLOCK" "unparseable locked version must FAIL"
expect_guard 1 torch 2.0.0 "$TMPROOT/nope.lock" "missing lockfile must FAIL"

# An indented `name = "torch"` inside a [package.metadata] block must not be
# read as a resolved package. Prove it by asking for a package that ONLY
# appears indented: it must be reported absent, not silently matched.
NESTLOCK="$TMPROOT/nested.lock"
cat > "$NESTLOCK" <<'TOML'
[[package]]
name = "mlx-backend"
version = "1.0.0"

[package.metadata]
requires-dist = [
    { name = "nested-only", specifier = ">=9.9.9" },
]
TOML
expect_guard 1 nested-only 1.0.0 "$NESTLOCK" "indented dependency-specifier name is not a resolved package"

# ---------------------------------------------------------------------------
# Wiring: drive the REAL apply_upgrade() from pip-audit-autofix.sh.
# A guard that exists but is never reached is a gate that cannot fail, so this
# half runs production code, not a reimplementation of it.
# ---------------------------------------------------------------------------
echo "== pip-audit-autofix.sh apply_upgrade() =="

REAL_UV="$(command -v uv || true)"

make_fixture_repo() {
  local dir="$1"
  mkdir -p "$dir/bin"
  cat > "$dir/pyproject.toml" <<'TOML'
[project]
name = "fixture"
version = "0.0.0"
dependencies = [
    "cryptography>=46.0.5",
]
TOML
  # `torch` is deliberately NOT a direct dependency: is_direct_dep() returns
  # false, no pyproject floor is rewritten, and `uv lock --upgrade-package
  # torch` is free to be a no-op. That is the exact real-world shape.
  cat > "$dir/uv.lock" <<'TOML'
version = 1

[[package]]
name = "torch"
version = "2.9.0"
source = { registry = "https://pypi.org/simple" }
TOML
  # Some runners ship a python3 predating tomllib (3.11); is_direct_dep() needs
  # it. Route python3 through uv's interpreter when the ambient one is too old,
  # so this test executes fully everywhere instead of quietly degrading.
  if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import tomllib' >/dev/null 2>&1; then
    [[ -n "$REAL_UV" ]] || fail "no python3 with tomllib, and no uv to borrow an interpreter from"
    cat > "$dir/bin/python3" <<EOF
#!/usr/bin/env bash
exec "$REAL_UV" run --no-project python "\$@"
EOF
    chmod +x "$dir/bin/python3"
  fi
}

# RED half: `uv lock` exits 0 and touches nothing — the silent no-op.
NOOP="$TMPROOT/noop"
make_fixture_repo "$NOOP"
cat > "$NOOP/bin/uv" <<'SH'
#!/usr/bin/env bash
# Reproduces the measured behaviour of `uv lock --upgrade-package <pkg>` for a
# package it cannot move: prints a success line, changes nothing, exits 0.
if [[ "${1:-}" == "lock" ]]; then
  echo "Resolved 151 packages in 2.67s"
  exit 0
fi
exit 0
SH
chmod +x "$NOOP/bin/uv"

LOCK_SHA_BEFORE="$(sha256_of "$NOOP/uv.lock")"
PROJ_SHA_BEFORE="$(sha256_of "$NOOP/pyproject.toml")"

rc=0
(
  cd "$NOOP"
  export PATH="$NOOP/bin:$PATH"
  export PIP_AUDIT_AUTOFIX_SOURCE_ONLY=1
  # shellcheck source=/dev/null
  source "$AUTOFIX"
  unset PIP_AUDIT_AUTOFIX_SOURCE_ONLY
  apply_upgrade torch PYSEC-2026-9999 2.13.0
) >"$TMPROOT/noop.out" 2>&1 || rc=$?

[[ "$rc" != "0" ]] || fail "apply_upgrade returned 0 for a uv lock that changed nothing (the silent no-op shipped)"
# A non-zero exit is NOT on its own proof the guard fired: a bash parse error
# in the sourced script also exits non-zero (2), having executed nothing.
# Require the guard's own message, so the RED half rests on behaviour rather
# than on a status code anything could have produced.
grep -q 'the bump never landed' "$TMPROOT/noop.out" \
  || fail "apply_upgrade exited $rc but never reached the guard. Captured output:
$(cat "$TMPROOT/noop.out")"
ok "apply_upgrade rejects a uv lock that exited 0 without moving the version (exit $rc, guard reported it)"

[[ "$(sha256_of "$NOOP/uv.lock")" == "$LOCK_SHA_BEFORE" ]] \
  || fail "uv.lock was not restored after the rejected bump"
[[ "$(sha256_of "$NOOP/pyproject.toml")" == "$PROJ_SHA_BEFORE" ]] \
  || fail "pyproject.toml was not restored after the rejected bump"
ok "pyproject.toml and uv.lock restored byte-identical"

[[ -f "$NOOP/.pip-audit-ignore" ]] || fail "no .pip-audit-ignore written for the rejected bump"
grep -q 'PYSEC-2026-9999' "$NOOP/.pip-audit-ignore" \
  || fail "CVE not recorded in .pip-audit-ignore — the finding vanished silently"
grep -q "no-op" "$NOOP/.pip-audit-ignore" \
  || fail ".pip-audit-ignore does not say WHY the bump was rejected"
ok "CVE recorded in .pip-audit-ignore with the real reason"

[[ ! -f "$NOOP/pyproject.toml.autofix.bak" && ! -f "$NOOP/uv.lock.autofix.bak" ]] \
  || fail "rollback snapshots left behind"
ok "no .autofix.bak residue"

# GREEN half: `uv lock` genuinely lands the bump. Without this, the guard could
# pass its RED test by simply always failing.
REAL="$TMPROOT/real"
make_fixture_repo "$REAL"
cat > "$REAL/bin/uv" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "lock" ]]; then
  # A resolver that actually moved torch to 2.13.0.
  printf 'version = 1\n\n[[package]]\nname = "torch"\nversion = "2.13.0"\nsource = { registry = "https://pypi.org/simple" }\n' > uv.lock
  echo "Resolved 151 packages in 3.01s"
  exit 0
fi
exit 0
SH
chmod +x "$REAL/bin/uv"

rc=0
(
  cd "$REAL"
  export PATH="$REAL/bin:$PATH"
  export PIP_AUDIT_AUTOFIX_SOURCE_ONLY=1
  # shellcheck source=/dev/null
  source "$AUTOFIX"
  unset PIP_AUDIT_AUTOFIX_SOURCE_ONLY
  apply_upgrade torch PYSEC-2026-9999 2.13.0
) >"$TMPROOT/real.out" 2>&1 || rc=$?

[[ "$rc" == "0" ]] || fail "apply_upgrade rejected a bump that DID land (guard is over-strict): $(cat "$TMPROOT/real.out")"
ok "apply_upgrade accepts a bump that genuinely landed"
[[ ! -f "$REAL/.pip-audit-ignore" ]] || fail "a landed bump must not be written off into .pip-audit-ignore"
ok "no spurious .pip-audit-ignore entry for a landed bump"
grep -q '2.13.0' "$REAL/uv.lock" || fail "fixture uv.lock was not actually updated"
ok "uv.lock carries the new version"

echo "PASS ($PASSED assertions)"
