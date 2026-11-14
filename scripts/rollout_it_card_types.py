"""One-off R228 rollout: give every tenant's ``it`` project type all v4 card types.

Why a script: ``POST /pm/catalogue/import-defaults`` creates missing card types but sets a
project type's matrix only when it CREATES the project type, so tenants that already have
``it`` keep the old seven-type matrix. This script, per tenant:

1. reads the tenant's ``it`` composition (``GET /pm/project-types/{id}/card-types``);
2. (apply only) runs ``POST /pm/catalogue/import-defaults`` so the 56 new card types exist;
3. ``PUT /pm/project-types/{id}/card-types`` with, per phase, the CURRENT card types UNION the
   defaults' ``it`` placement (``src/data/pm_catalogue_defaults.json``). Union, not replace: a
   card type a tenant placed itself is kept. ``entry_card_type_ids`` / ``output`` /
   ``exit_rule`` are omitted, i.e. unchanged;
4. re-reads the composition and fails unless every default placement is present.

Idempotent: a tenant already complete gets no PUT. ``--dry-run`` (the DEFAULT) does GETs only
and prints the per-phase diff; nothing is written until ``--apply`` is given.

Auth: each tenant's OWN requirements licence, sent as ``Authorization: Bearer <token>`` (RM
#222; tokenless ``X-Tenant-Id`` is being closed, and a licence acts only for its own tenant,
so one token per tenant). Tokens are read from files and never printed. ``--tokens`` is a file
of ``<tenant_uuid> <path-to-token-file>`` lines; tenant 5f868d42 (the conveyor tenant) defaults
to ``~/.config/nonagentic/n8n-local-licences/requirements.out``. A tenant with no token is
reported and makes the run exit 1 (never silently skipped). A token file is either the bare
token or a ``provision_service_token.py`` transcript (its ``token :`` line is used, and its
``tenant_id :`` line must equal the tenant, else the tenant is refused). ⛔ Do NOT mint a
missing token with ``provision_service_token.py``: it ROTATES the subject (``revoked
previous``), killing the live credential that subject's service uses, and credential rotation
is scheduled for 2026-12-01 only. Use the tenant's existing requirements licence holder's
token; if none is at hand, leave that tenant out and report it.

Usage::

    uv run python scripts/rollout_it_card_types.py [--base-url URL] [--tokens FILE] \\
        [--tenant UUID ...] [--apply]

``--base-url`` defaults to ``https://api.nonagentic.ai/requirements`` (the gateway path the
conveyor's requirements credential uses).
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[1] / "src/data/pm_catalogue_defaults.json"
TENANTS = (
    "5f868d42-03f8-51f0-a93c-3e83070dc196",
    "0195f48b-d000-7000-8000-000000000001",
    "0195f48b-d000-7000-8000-000000000002",
    "0195f48b-d000-7000-8000-000000000003",
    "0195f48b-d000-7000-8000-000000000004",
    "0195f48b-d000-7000-8000-000000000005",
)
DEFAULT_TOKEN_FILES = {
    TENANTS[0]: Path.home() / ".config/nonagentic/n8n-local-licences/requirements.out",
}
IT = "it"


class RM:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def call(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(  # noqa: S310 — operator-supplied https base URL
            f"{self.base}{path}", data=data, method=method, headers=self.headers
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise SystemExit(f"{method} {path}: HTTP {e.code} {detail}") from None


def _tokens(path: Path | None) -> dict[str, Path]:
    files = dict(DEFAULT_TOKEN_FILES)
    if path:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                tenant, file = line.split(maxsplit=1)
                files[tenant] = Path(file).expanduser()
    return files


def _read_token(file: Path, tenant: str) -> str | None:
    """The bare token, or the ``token :`` line of a provisioning transcript (tenant-checked)."""
    text = file.read_text()
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("token", "tenant_id"):
            fields[key.strip()] = value.split()[0] if value.split() else ""
    if "token" not in fields:
        return text.strip() or None
    if fields.get("tenant_id") not in (None, tenant):
        print(f"{tenant}: {file} is another tenant's licence, refused", file=sys.stderr)
        return None
    return fields["token"] or None


def _desired() -> dict[str, list[str]]:
    (it,) = [t for t in json.loads(DEFAULTS.read_text())["project_types"] if t["name"] == IT]
    return it["card_types"]


def _composition(rm: RM, it_id: str) -> dict[str, dict]:
    comp = rm.call("GET", f"/pm/project-types/{it_id}/card-types")
    return {
        p["name"]: {
            "phase_id": p["phase_id"],
            "card_types": {ct["name"] for cap in p["capabilities"] for ct in cap["card_types"]},
        }
        for p in comp["phases"]
    }


def rollout(rm: RM, tenant: str, desired: dict[str, list[str]], apply: bool) -> bool:
    types = {t["name"]: t for t in rm.call("GET", "/pm/project-types")}
    if IT not in types:
        print(f"{tenant}: no `{IT}` project type — import-defaults creates it with the full matrix")
        if apply:
            rm.call("POST", "/pm/catalogue/import-defaults", {})
            types = {t["name"]: t for t in rm.call("GET", "/pm/project-types")}
        else:
            return True
    it_id = types[IT]["id"]
    current = _composition(rm, it_id)
    missing_phases = sorted(set(desired) - set(current))
    if missing_phases:
        print(
            f"{tenant}: `{IT}` lacks phases {missing_phases}; fix the phases first", file=sys.stderr
        )
        return False

    diff = {p: sorted(set(names) - current[p]["card_types"]) for p, names in desired.items()}
    extra = {
        p: sorted(current[p]["card_types"] - set(desired.get(p, ())))
        for p in current
        if current[p]["card_types"] - set(desired.get(p, ()))
    }
    for phase in desired:
        print(f"{tenant}: {phase}: +{len(diff[phase])} {diff[phase]}")
    if extra:
        print(f"{tenant}: kept (tenant-placed, not in defaults): {extra}")
    if not any(diff.values()):
        print(f"{tenant}: complete, nothing to do")
        return True
    if not apply:
        return True

    rm.call("POST", "/pm/catalogue/import-defaults", {})
    ids: dict[str, str] = {}
    offset = 0
    while True:
        page = rm.call("GET", f"/pm/card-types?limit=1000&offset={offset}")
        ids.update({c["name"]: c["id"] for c in page})
        if len(page) < 1000:
            break
        offset += 1000
    wanted = {p: current[p]["card_types"] | set(desired.get(p, ())) for p in current}
    unknown = sorted({n for names in wanted.values() for n in names} - set(ids))
    if unknown:
        print(f"{tenant}: card types not in the tenant after import: {unknown}", file=sys.stderr)
        return False
    body = {
        "phases": [
            {"phase_id": current[p]["phase_id"], "card_type_ids": [ids[n] for n in sorted(names)]}
            for p, names in wanted.items()
        ]
    }
    rm.call("PUT", f"/pm/project-types/{it_id}/card-types", body)

    after = _composition(rm, it_id)
    short = {
        p: sorted(set(names) - after[p]["card_types"])
        for p, names in desired.items()
        if set(names) - after[p]["card_types"]
    }
    if short:
        print(f"{tenant}: VERIFY FAILED, still missing {short}", file=sys.stderr)
        return False
    print(f"{tenant}: applied and verified ({sum(len(v) for v in desired.values())} placements)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base-url", default="https://api.nonagentic.ai/requirements")
    ap.add_argument("--tokens", type=Path, help="file of '<tenant_uuid> <token-file>' lines")
    ap.add_argument("--tenant", action="append", help="limit to these tenants (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run, GETs only)")
    args = ap.parse_args()

    desired = _desired()
    tokens = _tokens(args.tokens)
    ok = True
    print(("APPLY" if args.apply else "DRY RUN") + f" against {args.base_url}")
    for tenant in args.tenant or TENANTS:
        file = tokens.get(tenant)
        if not file or not file.is_file():
            print(
                f"{tenant}: NO TOKEN ({file or 'not configured'}), not rolled out", file=sys.stderr
            )
            ok = False
            continue
        token = _read_token(file, tenant)
        if not token:
            ok = False
            continue
        ok &= rollout(RM(args.base_url, token), tenant, desired, args.apply)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
