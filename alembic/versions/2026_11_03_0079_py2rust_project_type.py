"""Project type ``py2rust`` (Python -> Rust rewrite) with its own phases, EXISTING tenants.

George 2026-11-03: a Python -> Rust conversion is its own project type with its own phases and
card types: ``setup`` -> ``plan`` -> ``port`` -> ``parity`` -> ``cutover``, one ``rewrite_*`` card
type per work phase (rewrite_plan, rewrite_slice, rewrite_parity_qa, rewrite_cutover_readiness)
plus the PM phase card types (project_phase_start, phase_coverage_check, project_phase_closure)
in every work phase, and ``it``'s setup placement (project_setup, project_start_request_analysis
as the entry). The code default (``src/data/pm_catalogue_defaults.json``) reaches a tenant whose
catalogue is imported after this ships; this migration gives it to every tenant that already
holds a ``kata`` project type (the same tenants 0077 targets: the v4 catalogue).

ADDITIVE ONLY (no-seeding rule, George 2026-09-28): no existing row is changed or replaced, and
no card type is created. A target tenant is skipped with a NOTICE when it lacks a ``setup``
phase or any card type the type places (prod 2026-11-03: only the conveyor tenant
5f868d42-03f8-51f0-a93c-3e83070dc196 holds all four rewrite_* types; the other kata tenants hold
rewrite_plan alone), or when it already holds a project type named ``py2rust`` that is not this
migration's (its own row is kept, never re-placed; decided by the type INSERT's ON CONFLICT, so a
type the app creates concurrently skips the tenant rather than aborting the migration). Per
remaining tenant: each missing phase (plan/port/parity/cutover) is created unless the tenant has a phase by that name (its own is reused); the type is created with
its ordered phases, its placements and the project-type-wide WIP limit of 1 that every default
project type gets. Every created row has a DETERMINISTIC id, ``uuid5(ROW_NS,
"<tenant_id>:<kind>:<name>")``, which marks it as this migration's. Re-running changes nothing.

Downgrade touches ONLY rows carrying this migration's ids. A created type that a project,
recipe, board template or another WIP limit references is in use: it is kept with its phases and
a NOTICE names it. Every other created type is deleted (its phase selections, placements and its
own WIP limit cascade); then each created phase that nothing references any more is deleted.

No RLS change: row-level tenant policy already covers the tables.

Revision ID: 2026_11_03_0079
Revises: 2026_11_03_0078
Create Date: 2026-11-03
"""

import uuid

import sqlalchemy as sa

from alembic import op

revision = "2026_11_03_0079"
down_revision = "2026_11_03_0078"
branch_labels = None
depends_on = None

MARKER_TYPE = "kata"  # the tenants 0077 targets
# Namespace of the deterministic ids this migration gives the rows it creates (fixed forever).
ROW_NS = uuid.UUID("9d3c1a52-2026-5103-8079-0079b2a5f00d")

# Frozen copies of the entries in pm_catalogue_defaults.json at this revision (a test pins them
# equal, so drift between the migration and the default is a visible failure).
_EXIT = (
    "project_phase_closure released after an approved phase sign-off; it starts the next "
    "phase (R88, R92)"
)
PHASES: list[dict] = [
    {
        "name": "plan",
        "title": "Plan",
        "description": (
            "Plan a language rewrite: the target architecture and an ordered slice list, drafted "
            "from the source repos at pinned SHAs and confirmed by the user (George 2026-11-03)."
        ),
        "output": "a confirmed rewrite plan (rewrite_plan)",
        "exit_rule": _EXIT,
    },
    {
        "name": "port",
        "title": "Port",
        "description": (
            "Port the planned slices to the target language, one slice per card (George "
            "2026-11-03)."
        ),
        "output": "one merged pull request per slice of the confirmed plan (rewrite_slice)",
        "exit_rule": _EXIT,
    },
    {
        "name": "parity",
        "title": "Parity",
        "description": (
            "Prove the rewrite equals its sources at the pinned SHAs: schema, API and behaviour, "
            "computed without judgement (George 2026-11-03)."
        ),
        "output": "a published parity report (rewrite_parity_qa)",
        "exit_rule": _EXIT,
    },
    {
        "name": "cutover",
        "title": "Cutover",
        "description": (
            "Collect the cutover evidence; the cutover itself is a session action (George "
            "2026-11-03)."
        ),
        "output": "the published G4 cutover evidence (rewrite_cutover_readiness)",
        "exit_rule": "project_phase_closure released after an approved phase sign-off (R88, R92)",
    },
]
_PM = ["phase_coverage_check", "project_phase_closure", "project_phase_start"]
PROJECT_TYPE: dict = {
    "name": "py2rust",
    "title": "Python → Rust rewrite",
    "description": (
        "A rewrite of one or more Python source repositories, pinned to SHAs, into one Rust "
        "service: plan the slices, port them, prove parity with the sources, and collect the "
        "cutover evidence (py2rust RS-D1..RS-D6, George 2026-11-03)."
    ),
    "phases": ["setup", "plan", "port", "parity", "cutover"],
    "card_types": {
        "setup": ["project_setup", "project_start_request_analysis"],
        "plan": [*_PM, "rewrite_plan"],
        "port": [*_PM, "rewrite_slice"],
        "parity": [*_PM, "rewrite_parity_qa"],
        "cutover": [*_PM, "rewrite_cutover_readiness"],
    },
    "entry_card_types": {"setup": ["project_start_request_analysis"]},
}
WIP_LIMIT = 1
CARD_TYPE_NAMES = sorted({n for v in PROJECT_TYPE["card_types"].values() for n in v})


def row_id(tenant_id, kind: str, name: str) -> uuid.UUID:
    """The id this migration gives the ``kind`` row ``name`` it creates for ``tenant_id``."""
    return uuid.uuid5(ROW_NS, f"{tenant_id}:{kind}:{name}")


def _notice(msg: str) -> None:
    # print(), not a logger: alembic's logging here never reaches stderr (see 0042).
    print(f"NOTICE 0079: {msg}", flush=True)


def add_project_type(bind: sa.engine.Connection) -> dict[str, int]:
    """Create the type where it is missing; returns counts of what was created."""
    out = {"types": 0, "phases": 0, "placed": 0, "skipped": 0}
    tenants = bind.execute(
        sa.text(
            "SELECT DISTINCT ON (tenant_id) tenant_id, tenant_name, subject_id, visibility "
            "FROM pm_project_types WHERE name = :marker ORDER BY tenant_id, id"
        ),
        {"marker": MARKER_TYPE},
    ).all()
    for t in tenants:
        tid = t.tenant_id
        cts = dict(
            bind.execute(
                sa.text(
                    "SELECT name, id FROM pm_card_types WHERE tenant_id = :t AND name = ANY(:n)"
                ),
                {"t": tid, "n": CARD_TYPE_NAMES},
            ).all()
        )
        setup = bind.execute(
            sa.text("SELECT id FROM pm_project_phases WHERE tenant_id = :t AND name = 'setup'"),
            {"t": tid},
        ).scalar()
        missing = sorted(set(CARD_TYPE_NAMES) - set(cts)) + ([] if setup else ["phase setup"])
        if missing:
            _notice(f"skipped tenant {tid}: it lacks {', '.join(missing)}")
            out["skipped"] += 1
            continue
        owner = {
            "tenant_id": tid,
            "tenant_name": t.tenant_name,
            "subject_id": t.subject_id,
            "visibility": t.visibility,
        }
        # The type first, before any other row: ON CONFLICT (not a prior SELECT) decides, so a
        # py2rust type the app creates concurrently skips the tenant instead of aborting the
        # whole migration. Nothing else is written for a tenant whose type is not ours.
        type_id = row_id(tid, "project_type", PROJECT_TYPE["name"])
        created_type = bind.execute(
            sa.text(
                "INSERT INTO pm_project_types (id, tenant_id, tenant_name, subject_id, visibility, "
                "name, title, description) VALUES (:id, :tenant_id, :tenant_name, :subject_id, "
                ":visibility, :name, :title, :description) "
                "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id"
            ),
            {
                **owner,
                "id": type_id,
                "name": PROJECT_TYPE["name"],
                "title": PROJECT_TYPE["title"],
                "description": PROJECT_TYPE["description"],
            },
        ).scalar()
        if created_type is None:
            existing = bind.execute(
                sa.text("SELECT id FROM pm_project_types WHERE tenant_id = :t AND name = :n"),
                {"t": tid, "n": PROJECT_TYPE["name"]},
            ).scalar()
            if existing is not None and uuid.UUID(str(existing)) == type_id:
                continue  # a re-run: ours, already complete
            _notice(
                f"skipped tenant {tid}: it already holds its own {PROJECT_TYPE['name']!r} type "
                f"({existing}); kept, never re-placed"
            )
            out["skipped"] += 1
            continue
        out["types"] += 1
        phase_ids = {"setup": setup}
        for p in PHASES:
            created = bind.execute(
                sa.text(
                    "INSERT INTO pm_project_phases (id, tenant_id, tenant_name, subject_id, "
                    "visibility, name, title, description, output, exit_rule) VALUES (:id, "
                    ":tenant_id, :tenant_name, :subject_id, :visibility, :name, :title, "
                    ":description, :output, :exit_rule) "
                    "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id"
                ),
                {**owner, **p, "id": row_id(tid, "phase", p["name"])},
            ).scalar()
            out["phases"] += created is not None
            phase_ids[p["name"]] = (
                created
                or bind.execute(
                    sa.text("SELECT id FROM pm_project_phases WHERE tenant_id = :t AND name = :n"),
                    {"t": tid, "n": p["name"]},
                ).scalar()
            )
        for pos, name in enumerate(PROJECT_TYPE["phases"]):
            bind.execute(
                sa.text(
                    "INSERT INTO pm_project_type_phases (project_type_id, phase_id, position, "
                    "tenant_id) VALUES (:pt, :ph, :pos, :t)"
                ),
                {"pt": type_id, "ph": phase_ids[name], "pos": pos, "t": tid},
            )
        for phase, names in PROJECT_TYPE["card_types"].items():
            entry = PROJECT_TYPE["entry_card_types"].get(phase, [])
            for n in names:
                bind.execute(
                    sa.text(
                        "INSERT INTO pm_project_type_card_types (project_type_id, phase_id, "
                        "card_type_id, tenant_id, is_entry) VALUES (:pt, :ph, :ct, :t, :entry)"
                    ),
                    {
                        "pt": type_id,
                        "ph": phase_ids[phase],
                        "ct": cts[n],
                        "t": tid,
                        "entry": n in entry,
                    },
                )
                out["placed"] += 1
        bind.execute(
            sa.text(
                "INSERT INTO pm_wip_limits (id, tenant_id, tenant_name, subject_id, visibility, "
                "project_type_id, wip_limit) VALUES (:id, :tenant_id, :tenant_name, :subject_id, "
                ":visibility, :pt, :limit)"
            ),
            {
                **owner,
                "id": row_id(tid, "wip_limit", PROJECT_TYPE["name"]),
                "pt": type_id,
                "limit": WIP_LIMIT,
            },
        )
    return out


def remove_project_type(bind: sa.engine.Connection) -> dict[str, int]:
    """Downgrade: remove only this migration's rows; keep (and report) a type in use."""
    out = {"types": 0, "phases": 0, "kept": 0}
    rows = bind.execute(
        sa.text("SELECT id, tenant_id FROM pm_project_types WHERE name = :n"),
        {"n": PROJECT_TYPE["name"]},
    ).all()
    ours = [
        r
        for r in rows
        if uuid.UUID(str(r.id)) == row_id(r.tenant_id, "project_type", PROJECT_TYPE["name"])
    ]
    tenants = []
    for r in ours:
        own_wip = row_id(r.tenant_id, "wip_limit", PROJECT_TYPE["name"])
        if bind.execute(
            sa.text(
                "SELECT 1 WHERE EXISTS (SELECT 1 FROM pm_projects WHERE project_type_id = :pt) "
                "OR EXISTS (SELECT 1 FROM pm_recipes WHERE project_type_id = :pt) "
                "OR EXISTS (SELECT 1 FROM pm_board_templates WHERE project_type_id = :pt) "
                "OR EXISTS (SELECT 1 FROM pm_wip_limits WHERE project_type_id = :pt "
                "AND id <> :own)"
            ),
            {"pt": r.id, "own": own_wip},
        ).first():
            _notice(
                f"downgrade: kept {PROJECT_TYPE['name']} ({r.id}) of tenant {r.tenant_id} and its "
                "phases: a project, recipe, board template or WIP limit references it"
            )
            out["kept"] += 1
            continue
        # Cascades: its phase selections, their placements and its own WIP limit.
        out["types"] += bind.execute(
            sa.text("DELETE FROM pm_project_types WHERE id = :pt"), {"pt": r.id}
        ).rowcount
        tenants.append(r.tenant_id)
    for tid in tenants:
        for p in PHASES:
            out["phases"] += bind.execute(
                sa.text(
                    "DELETE FROM pm_project_phases f WHERE f.id = :id "
                    "AND NOT EXISTS (SELECT 1 FROM pm_project_type_phases x WHERE x.phase_id = f.id) "
                    "AND NOT EXISTS (SELECT 1 FROM pm_recipes x WHERE x.phase_id = f.id) "
                    "AND NOT EXISTS (SELECT 1 FROM pm_wip_limits x WHERE x.phase_id = f.id) "
                    "AND NOT EXISTS (SELECT 1 FROM pm_milestone_kinds x WHERE x.phase_id = f.id) "
                    "AND NOT EXISTS (SELECT 1 FROM pm_gates x WHERE x.phase_id = f.id) "
                    "AND NOT EXISTS (SELECT 1 FROM pm_projects x WHERE x.current_phase_id = f.id) "
                    "AND NOT EXISTS (SELECT 1 FROM pm_project_phase_events x "
                    "WHERE x.from_phase_id = f.id OR x.to_phase_id = f.id)"
                ),
                {"id": row_id(tid, "phase", p["name"])},
            ).rowcount
    return out


def upgrade() -> None:
    add_project_type(op.get_bind())


def downgrade() -> None:
    remove_project_type(op.get_bind())
