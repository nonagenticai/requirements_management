#!/usr/bin/env bash
# Assert that uv.lock ACTUALLY pins <package> at >= <min-version>.
#
# WHY THIS EXISTS
# ---------------
# `uv lock --upgrade-package <pkg>` exits 0 and prints a cheerful
# "Resolved N packages in 2.67s" EVEN WHEN IT CHANGED NOTHING AT ALL:
#
#   * <pkg> is not in the resolved dependency graph (typo, renamed
#     distribution, a package that only resolves on another platform, or a
#     direct-URL / git dependency) -> exit 0, uv.lock byte-identical, no warning.
#   * <pkg> IS in the graph but is a TRANSITIVE dependency whose version is
#     capped by some other package's constraint, and no pyproject constraint
#     was rewritten for it -> exit 0, uv.lock byte-identical, no warning.
#
# Measured on this repo with uv 0.10.12:
#
#   $ uv lock --upgrade-package this-package-does-not-exist-xyz
#   Resolved 151 packages in 2.67s
#   $ echo $?
#   0                            # <- and uv.lock's sha256 was unchanged
#
# So the EXIT CODE of `uv lock` is not evidence that a security bump landed.
# Neither is a changed uv.lock sha256: raising a pyproject floor to a version
# the lock ALREADY satisfies rewrites the `requires-dist` metadata block inside
# uv.lock (the file hash changes) while leaving the RESOLVED version untouched.
# The only sound assertion is on the resolved version. That is what this does.
#
# Usage:
#   scripts/assert-locked-version.sh <package> <min-version> [<lockfile>]
#
# Exit 0 only when <package> is present in the lock AND every resolved entry
# for it is >= <min-version>. Anything else (absent package, missing lockfile,
# unparseable version, resolved below the floor) is a hard failure -- "not
# found" is never "clean".
set -euo pipefail

# Emit every resolved version of $1 found in lockfile $2, one per line.
#
# uv.lock is TOML: top-level `[[package]]` tables carry column-0 `name = "..."`
# and `version = "..."`. Nested dependency-specifier tables (`[package.metadata]`,
# `[[package.dependencies]]`) also carry `name =`, but INDENTED, so anchoring on
# column 0 selects resolved packages only. Names are compared PEP 503-normalized
# (lowercase; runs of - _ . collapsed to a single -).
locked_versions() {
  local want="$1" lock="$2"
  awk -v want="$want" '
    function norm(s) {
      s = tolower(s)
      gsub(/[-_.]+/, "-", s)
      return s
    }
    function unquote(line,   v) {
      v = line
      sub(/^[a-z]+[ \t]*=[ \t]*"/, "", v)
      sub(/".*$/, "", v)
      return v
    }
    BEGIN { want = norm(want); name = ""; version = "" }
    /^\[\[package\]\]/ { name = ""; version = ""; next }
    /^name[ \t]*=/     { name = unquote($0); next }
    /^version[ \t]*=/  {
      version = unquote($0)
      if (name != "" && norm(name) == want) print version
      next
    }
  ' "$lock"
}

# Resolve an interpreter. Only `re`/`os`/`sys` are used below -- no tomllib, no
# `packaging` -- so any python3 works; the `uv run` fallback covers minimal CI
# containers that ship no system python at all.
_py() {
  if [[ -n "${ASSERT_LOCKED_VERSION_PYTHON:-}" ]]; then
    read -r -a _PY <<< "$ASSERT_LOCKED_VERSION_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    _PY=(python3)
  elif command -v python >/dev/null 2>&1; then
    _PY=(python)
  elif command -v uv >/dev/null 2>&1; then
    _PY=(uv run --no-project --quiet python)
  else
    echo "[assert-locked-version] no python interpreter available" >&2
    return 2
  fi
}

# Compare two PEP 440 versions. Exit 0 iff $1 >= $2.
#
# A lexicographic string compare is NOT good enough here: "2.13.0" < "2.9.0" as
# strings, which would silently pass a stale pin off as an upgrade.
version_ge() {
  local _PY=()
  _py || return 2
  A="$1" B="$2" "${_PY[@]}" - <<'PY'
import os
import re
import sys

# epoch! release [pre] [post] [dev] [+local]
_RE = re.compile(
    r"^\s*v?"
    r"(?:(?P<epoch>\d+)!)?"
    r"(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>\d*))?"
    r"(?:(?:[-_.]?post[-_.]?(?P<post>\d*))|(?:-(?P<post_implicit>\d+)))?"
    r"(?:[-_.]?dev[-_.]?(?P<dev>\d*))?"
    r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?\s*$",
    re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}


def key(text):
    m = _RE.match(text)
    if m is None:
        raise ValueError("not a PEP 440 version: %r" % (text,))
    release = [int(p) for p in m.group("release").split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()  # 1.2 == 1.2.0
    pre_l = m.group("pre_l")
    # A final release sorts ABOVE any pre-release of the same release tuple.
    pre = (1,) if pre_l is None else (0, _PRE_RANK[pre_l.lower()], int(m.group("pre_n") or 0))
    post_raw = m.group("post")
    if post_raw is None:
        post_raw = m.group("post_implicit")
    post = (0,) if post_raw is None else (1, int(post_raw or 0))
    dev_raw = m.group("dev")
    # A dev release sorts BELOW the corresponding non-dev release.
    dev = (1,) if dev_raw is None else (0, int(dev_raw or 0))
    return (int(m.group("epoch") or 0), tuple(release), pre, post, dev)


try:
    a = key(os.environ["A"])
    b = key(os.environ["B"])
except ValueError as exc:
    sys.stderr.write("[assert-locked-version] %s\n" % (exc,))
    sys.exit(2)
sys.exit(0 if a >= b else 1)
PY
}

assert_locked_version() {
  local pkg="${1:-}" min="${2:-}" lock="${3:-uv.lock}"

  if [[ -z "$pkg" || -z "$min" ]]; then
    echo "usage: assert-locked-version.sh <package> <min-version> [<lockfile>]" >&2
    return 2
  fi
  if [[ ! -f "$lock" ]]; then
    echo "[assert-locked-version] FAIL ${pkg}: lockfile '${lock}' does not exist" >&2
    return 1
  fi

  local versions
  versions="$(locked_versions "$pkg" "$lock")"

  # "Not in the lock" is the loudest form of the silent no-op, not a pass.
  if [[ -z "$versions" ]]; then
    echo "[assert-locked-version] FAIL ${pkg}: not present in ${lock} at all —" \
         "\`uv lock --upgrade-package ${pkg}\` exited 0 but resolved nothing." \
         "Check the distribution name, or whether it is a direct-URL/git dependency." >&2
    return 1
  fi

  local rc=0 v
  while IFS= read -r v; do
    [[ -z "$v" ]] && continue
    local cmp=0
    version_ge "$v" "$min" || cmp=$?
    if [[ "$cmp" == "2" ]]; then
      echo "[assert-locked-version] FAIL ${pkg}: cannot compare locked '${v}' to '${min}'" >&2
      rc=1
    elif [[ "$cmp" != "0" ]]; then
      echo "[assert-locked-version] FAIL ${pkg}: ${lock} resolves ${v}, below the requested ${min}." \
           "The bump did NOT land — uv exited 0 without moving it." >&2
      rc=1
    else
      echo "[assert-locked-version] ok ${pkg}: ${lock} resolves ${v} (>= ${min})"
    fi
  done <<< "$versions"

  return "$rc"
}

# Test seam: sourcing with ASSERT_LOCKED_VERSION_SOURCE_ONLY=1 loads the
# functions without running anything (mirrors gitops-production's
# rmt-drift-guard convention).
if [ "${ASSERT_LOCKED_VERSION_SOURCE_ONLY:-0}" = "1" ]; then
  # shellcheck disable=SC2317
  return 0 2>/dev/null || exit 0
fi

assert_locked_version "$@"
