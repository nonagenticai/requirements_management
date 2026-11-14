#!/usr/bin/env bash
# Auto-resolve pip-audit findings for this repo:
#   - fix available  → bump pyproject constraint (if direct), uv lock --upgrade-package, uv sync
#   - no fix         → append CVE to .pip-audit-ignore with timestamped comment
# Re-runs pip-audit until clean or no further progress is possible.
# Does NOT git commit/push — review the diff before pushing.
set -euo pipefail

MAX_PASSES="${MAX_PASSES:-10}"
PIP_AUDIT_VERSION="${PIP_AUDIT_VERSION:-2.10.0}"
IGNORE_FILE=".pip-audit-ignore"
PYPROJECT="pyproject.toml"
AUTOFIX_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSERT_LOCKED_VERSION="${ASSERT_LOCKED_VERSION:-$AUTOFIX_SCRIPT_DIR/assert-locked-version.sh}"

build_ignore_args() {
  ignore_args=()
  [[ -f "$IGNORE_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [[ -z "$line" ]] && continue
    ignore_args+=(--ignore-vuln "$line")
  done < "$IGNORE_FILE"
}

run_audit_json() {
  build_ignore_args
  uv export --frozen --no-emit-project --all-extras --all-groups \
    --format requirements-txt --no-hashes --quiet 2>/dev/null \
  | uvx --quiet --from "pip-audit==${PIP_AUDIT_VERSION}" \
      pip-audit -r /dev/stdin --disable-pip --no-deps --format json \
      ${ignore_args[@]+"${ignore_args[@]}"} 2>/dev/null || true
}

is_direct_dep() {
  PKG="$1" python3 - <<'PY'
import os, re, sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open("pyproject.toml", "rb") as f:
    data = tomllib.load(f)
deps = list(data.get("project", {}).get("dependencies", []))
for grp in data.get("project", {}).get("optional-dependencies", {}).values():
    deps.extend(grp)
for grp in data.get("dependency-groups", {}).values():
    deps.extend(grp)
pkg = os.environ["PKG"].lower()
for d in deps:
    name = re.split(r"[\s\[<>=!~;]", d, 1)[0].strip().lower()
    if name == pkg:
        sys.exit(0)
sys.exit(1)
PY
}

bump_constraint() {
  PKG="$1" FIX="$2" python3 - <<'PY'
import os, re, sys
pkg = os.environ["PKG"]; fix = os.environ["FIX"]
with open("pyproject.toml") as f:
    text = f.read()
# Match  "<pkg>"  or  "<pkg>[extras]<spec>"  inside dependency string lists.
pat = re.compile(
    rf'("{re.escape(pkg)}(\[[^\]]+\])?)([<>=!~][^"]*)?(")',
    re.IGNORECASE,
)
def repl(m):
    return f'{m.group(1)}>={fix}{m.group(4)}'
new, n = pat.subn(repl, text, count=1)
if n == 0:
    sys.exit(2)
with open("pyproject.toml", "w") as f:
    f.write(new)
PY
}

record_ignore() {
  local vuln_id="$1" pkg="$2" note="$3"
  touch "$IGNORE_FILE"
  if ! grep -qxF "$vuln_id" <(awk '{print $1}' "$IGNORE_FILE"); then
    printf '%s  # %s %s (%s)\n' "$vuln_id" "$pkg" "$(date -I)" "$note" >> "$IGNORE_FILE"
  fi
}

# Attempt to land a fix for $pkg at >=$fix. Returns 0 only when the bump is
# PROVEN to be in uv.lock; on any other outcome it restores pyproject.toml +
# uv.lock and records the CVE in $IGNORE_FILE.
#
# The `assert_locked_version` step is load-bearing, not belt-and-braces:
# `uv lock --upgrade-package <pkg>` exits 0 and prints "Resolved N packages"
# even when it resolved nothing at all — when <pkg> is absent from the graph
# (typo / renamed distribution / direct-URL or git dependency), or when <pkg>
# is transitive and some other package's constraint caps it and `is_direct_dep`
# therefore rewrote no pyproject floor. Before this check, both cases were
# reported as a successful security bump and the .bak snapshots were deleted:
# the CVE stayed live in the lockfile with nothing anywhere saying so.
#
# Note the check asserts the RESOLVED VERSION, not "did uv.lock change". A
# changed uv.lock is not evidence either: raising a pyproject floor to a
# version the lock already satisfies rewrites uv.lock's `requires-dist`
# metadata (the file hash moves) while the resolved version stays put.
apply_upgrade() {
  local pkg="$1" vuln_id="$2" fix="$3"
  local reason=""

  # Snapshot for rollback if the resolver refuses the bump.
  cp "$PYPROJECT" "${PYPROJECT}.autofix.bak"
  cp uv.lock uv.lock.autofix.bak
  if is_direct_dep "$pkg"; then
    bump_constraint "$pkg" "$fix" || echo "  (could not rewrite ${PYPROJECT} for $pkg, relying on uv lock)"
  fi

  if ! uv lock --upgrade-package "$pkg" --quiet 2>/tmp/uv-lock-err; then
    echo "  resolver rejected upgrade — reverting and adding to ignore"
    sed -n 's/^/  | /p' /tmp/uv-lock-err | head -5
    reason="resolver conflict prevents fix"
  elif ! "$ASSERT_LOCKED_VERSION" "$pkg" "$fix"; then
    echo "  uv lock exited 0 but the bump never landed — reverting and adding to ignore"
    reason="uv lock silently no-op'd the fix"
  fi

  if [[ -n "$reason" ]]; then
    mv "${PYPROJECT}.autofix.bak" "$PYPROJECT"
    mv uv.lock.autofix.bak uv.lock
    record_ignore "$vuln_id" "$pkg" "$reason to >=$fix"
    return 1
  fi

  rm -f "${PYPROJECT}.autofix.bak" uv.lock.autofix.bak
  return 0
}

main() {
if [[ ! -f "$PYPROJECT" ]]; then
  echo "[autofix] no pyproject.toml in $(pwd) — skipping" >&2
  exit 1
fi
if [[ ! -f uv.lock ]]; then
  echo "[autofix] no uv.lock in $(pwd) — skipping" >&2
  exit 1
fi
if [[ ! -x "$ASSERT_LOCKED_VERSION" ]]; then
  echo "[autofix] lock-bump guard missing at $ASSERT_LOCKED_VERSION — refusing to run" >&2
  exit 1
fi

# Track (pkg, vuln_id) we've already attempted to upgrade.
# If the same pair shows up in a later pass, uv refused the bump (transitive
# constraint), so fall through to ignore.
declare -A attempted_upgrades=()

for pass in $(seq 1 "$MAX_PASSES"); do
  json="$(run_audit_json)"
  if [[ -z "$json" || "$json" == "null" ]]; then
    echo "[pass $pass] clean"
    exit 0
  fi

  vulns="$(JSON="$json" python3 - <<'PY'
import json, os, re, sys
try:
    d = json.loads(os.environ.get("JSON", "") or "{}")
except Exception:
    sys.exit(0)
# Treat pre-release fix versions (rc/a/b/dev/post) as "no stable fix" — we do
# not auto-bump to pre-releases. Operator decides whether to opt in.
#
# NB: keep apostrophes and backticks out of this heredoc. It sits inside a
# $( ) command substitution, and bash 3.2 — which is /bin/bash on every Mac in
# the fleet, and the shell GitHub Actions uses for run: steps on the macOS
# runners — scans the substitution for quote characters before it processes
# the heredoc. A single apostrophe in a comment here made the whole file
# unparseable on those hosts: bash exited 2 with "unexpected EOF while looking
# for matching", i.e. the autofix script could never run there at all.
# scripts/test-assert-locked-version.sh asserts this file parses under
# /bin/bash so the regression cannot come back silently.
PRE_RE = re.compile(r"(?i)(?:rc|a\d|b\d|alpha|beta|dev|pre)")
for dep in d.get("dependencies", []):
    for v in dep.get("vulns", []):
        fixes = [f for f in (v.get("fix_versions") or []) if not PRE_RE.search(f)]
        print("\t".join([dep.get("name", ""), v.get("id", ""), fixes[0] if fixes else ""]))
PY
  )"

  if [[ -z "$vulns" ]]; then
    echo "[pass $pass] clean"
    exit 0
  fi

  progressed=0
  declare -A seen_pkgs=()
  while IFS=$'\t' read -r pkg vuln_id fix; do
    [[ -z "$pkg" ]] && continue
    if [[ -n "$fix" ]]; then
      if [[ -n "${seen_pkgs[$pkg]:-}" ]]; then
        continue  # already upgraded this pass
      fi
      seen_pkgs[$pkg]=1
      key="$pkg|$vuln_id"
      if [[ -n "${attempted_upgrades[$key]:-}" ]]; then
        echo "[pass $pass] $pkg $vuln_id → upgrade refused by resolver (transitive pin), ignoring"
        record_ignore "$vuln_id" "$pkg" "transitive pin blocks fix to >=$fix"
        progressed=1
        continue
      fi
      attempted_upgrades[$key]=1
      echo "[pass $pass] $pkg $vuln_id → upgrading to >=$fix"
      apply_upgrade "$pkg" "$vuln_id" "$fix" || true
      progressed=1
    else
      echo "[pass $pass] $pkg $vuln_id → no fix, ignoring"
      record_ignore "$vuln_id" "$pkg" "no fix available"
      progressed=1
    fi
  done <<< "$vulns"

  if [[ "$progressed" == "0" ]]; then
    echo "[pass $pass] no progress, bailing"
    exit 1
  fi
  uv sync --quiet
done

echo "[autofix] hit MAX_PASSES=$MAX_PASSES, still dirty"
exit 1
}

# Test seam: sourcing with PIP_AUDIT_AUTOFIX_SOURCE_ONLY=1 loads the functions
# (apply_upgrade, record_ignore, is_direct_dep, bump_constraint) without running
# the audit loop, so they can be driven directly against fixture repos.
if [ "${PIP_AUDIT_AUTOFIX_SOURCE_ONLY:-0}" = "1" ]; then
  # shellcheck disable=SC2317
  return 0 2>/dev/null || exit 0
fi

main "$@"
