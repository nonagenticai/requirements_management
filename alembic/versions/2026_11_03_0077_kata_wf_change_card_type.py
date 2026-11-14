"""Card type ``wf_change`` on the ``kata`` project type, EXISTING tenants (Kata WP5).

George 2026-11-03 (n8n-designs design/choices/v4/kata/wp5-change-replay.html): ``wf_change`` is
the ONE execution-phase kata card type (capability ``workflow_engineering``, KATA-ONLY). The code
default (``src/data/pm_catalogue_defaults.json``) only reaches a tenant whose catalogue is
imported AFTER this ships: ``import-defaults`` creates missing card types but places card types
only into a project type it creates itself. So every tenant that already holds a ``kata`` project
type gets it here.

ADDITIVE ONLY (no-seeding rule, George 2026-09-28): no existing row is changed or replaced.
Only a tenant that holds a project type named ``kata`` that SELECTS an ``execution`` phase AND a
``workflow_engineering`` capability is touched (prod 2026-11-03: two such tenants, the conveyor
tenant and the kata replay sandbox; both kata types select setup/preliminary/execution). A tenant
missing either is skipped with a NOTICE: its kata predates the kata card types, and adding a
capability or a phase selection is not this migration's to decide. Per such tenant, ``wf_change``
is created under that capability with the frozen definition below unless the tenant already has
a row by that name (it keeps its own). A created row gets a DETERMINISTIC id, ``uuid5(ROW_NS,
"<tenant_id>:wf_change")``, which marks it as this migration's (a tenant's own row has a uuidv7
id). Each created row is placed in its tenant's ``kata`` type, ``execution`` phase, never an entry
type; only rows created here are placed. Re-running changes nothing.

Downgrade touches ONLY rows carrying this migration's id, never a tenant's own ``wf_change``. A
created row that a recipe item or WIP limit references is in use: it is kept WITH its placements
and a NOTICE names it (RM keeps no card table keyed to card types, so those are the only
references). Every other created row loses its placements and is deleted.

No RLS change: row-level tenant policy already covers the tables.

Revision ID: 2026_11_03_0077
Revises: 2026_10_30_0076
Create Date: 2026-11-03
"""

import json
import uuid

import sqlalchemy as sa

from alembic import op

revision = "2026_11_03_0077"
down_revision = "2026_10_30_0076"
branch_labels = None
depends_on = None

CAPABILITY = "workflow_engineering"
PROJECT_TYPE = "kata"
PHASE = "execution"
# Namespace of the deterministic ids this migration gives the rows it creates (fixed forever).
ROW_NS = uuid.UUID("5b2e7f30-2026-5103-8077-0077ca7a0c4e")

# Frozen copy of the entry in pm_catalogue_defaults.json at this revision (a test pins it equal,
# so drift between the migration and the default is a visible failure).
CARD_TYPES: list[dict] = [
    {
        "name": "wf_change",
        "title": "Wf change",
        "description": (
            "applies one op list to a copy of the failing flow, proves it by replay, publishes "
            "it and releases the cord"
        ),
        "inputs": [],
        "outputs": [
            "op_list",
            "copy_workflow_id",
            "copy_version_id",
            "replay_report_artifact_id",
            "live_version_id",
        ],
        "acceptance": [
            "READY: cord_engaged_with_the_kata_as_holder",
            "READY: evidence_artifact_present",
            "READY: every_op_is_in_the_vocabulary",
            "DOING: every_op_applied",
            "DOING: copy_passes_lint",
            "DOING: diff_touches_only_the_op_nodes",
            "DOING: replay_ran",
            "QA: three_replays_ran",
            "QA: two_of_three_replays_pass_without_error",
            "QA: original_failing_check_passes",
            "DONE: live_flow_read_back_matches_the_copy",
            "DONE: only_the_kata_nodes_changed",
            "DONE: cord_released",
            "DONE: blocked_cards_moved_back_and_commented",
            "DONE: kata_closed_in_fr",
        ],
        "rules": [],
        "human_task": False,
        "definition_of_done": None,
    },
]
NAMES = tuple(c["name"] for c in CARD_TYPES)


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def row_id(tenant_id, name: str) -> uuid.UUID:
    """The id this migration gives the ``name`` row it creates for ``tenant_id``."""
    return uuid.uuid5(ROW_NS, f"{tenant_id}:{name}")


def add_card_types(bind: sa.engine.Connection) -> dict[str, int]:
    """Create the missing rows and placements; returns ``{"created": n, "placed": m}``."""
    targets = bind.execute(
        sa.text(
            "SELECT t.id AS type_id, t.tenant_id, tp.phase_id, k.id AS cap_id, k.tenant_name, "
            "k.subject_id, k.visibility "
            "FROM pm_project_types t "
            "LEFT JOIN pm_project_type_phases tp ON tp.project_type_id = t.id AND tp.phase_id IN ("
            "SELECT f.id FROM pm_project_phases f WHERE f.tenant_id = t.tenant_id "
            "AND f.name = :phase) "
            "LEFT JOIN pm_capabilities k ON k.tenant_id = t.tenant_id AND k.name = :cap "
            "WHERE t.name = :ptype"
        ),
        {"phase": PHASE, "cap": CAPABILITY, "ptype": PROJECT_TYPE},
    ).all()
    created = placed = 0
    for t in targets:
        if t.phase_id is None or t.cap_id is None:
            # print(), not a logger: alembic's logging here never reaches stderr (see 0042).
            print(
                f"NOTICE 0077: skipped tenant {t.tenant_id}: its {PROJECT_TYPE!r} type "
                f"{'selects no ' + PHASE + ' phase' if t.phase_id is None else ''}"
                f"{' and ' if t.phase_id is None and t.cap_id is None else ''}"
                f"{'has no ' + CAPABILITY + ' capability' if t.cap_id is None else ''}",
                flush=True,
            )
            continue
        for ct in CARD_TYPES:
            new = (
                bind.execute(
                    sa.text(
                        "INSERT INTO pm_card_types (id, tenant_id, tenant_name, subject_id, "
                        "visibility, name, title, description, capability_id, inputs, outputs, "
                        "acceptance, rules, human_task, definition_of_done) "
                        "VALUES (:id, :tenant_id, :tenant_name, :subject_id, :visibility, :name, "
                        ":title, :description, :cap_id, CAST(:inputs AS jsonb), "
                        "CAST(:outputs AS jsonb), CAST(:acceptance AS jsonb), "
                        "CAST(:rules AS jsonb), :human_task, :dod) "
                        "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id"
                    ),
                    {
                        "id": row_id(t.tenant_id, ct["name"]),
                        "tenant_id": t.tenant_id,
                        "tenant_name": t.tenant_name,
                        "subject_id": t.subject_id,
                        "visibility": t.visibility,
                        "name": ct["name"],
                        "title": ct["title"],
                        "description": ct["description"],
                        "cap_id": t.cap_id,
                        "inputs": _j(ct["inputs"]),
                        "outputs": _j(ct["outputs"]),
                        "acceptance": _j(ct["acceptance"]),
                        "rules": _j(ct["rules"]),
                        "human_task": ct["human_task"],
                        "dod": ct["definition_of_done"],
                    },
                )
                .scalars()
                .all()
            )
            if not new:
                continue  # the tenant's own row of that name: kept, never placed by this
            created += 1
            # Place the row created HERE in the kata type's execution phase (never an entry).
            placed += (
                bind.execute(
                    sa.text(
                        "INSERT INTO pm_project_type_card_types "
                        "(project_type_id, phase_id, card_type_id, tenant_id, is_entry) "
                        "VALUES (:pt, :ph, :ct, :tenant_id, false) ON CONFLICT DO NOTHING"
                    ),
                    {"pt": t.type_id, "ph": t.phase_id, "ct": new[0], "tenant_id": t.tenant_id},
                ).rowcount
                or 0
            )
    return {"created": created, "placed": placed}


def remove_card_types(bind: sa.engine.Connection) -> dict[str, int]:
    """Downgrade: remove only this migration's rows; keep (and report) any that are in use."""
    rows = bind.execute(
        sa.text("SELECT id, tenant_id, name FROM pm_card_types WHERE name = ANY(:names)"),
        {"names": list(NAMES)},
    ).all()
    ours = [r.id for r in rows if uuid.UUID(str(r.id)) == row_id(r.tenant_id, r.name)]
    if not ours:
        return {"unplaced": 0, "deleted": 0, "kept": 0}
    used = bind.execute(
        sa.text(
            "SELECT c.id, c.tenant_id, c.name FROM pm_card_types c WHERE c.id = ANY(:ids) AND ("
            "EXISTS (SELECT 1 FROM pm_recipe_items r WHERE r.card_type_id = c.id) OR "
            "EXISTS (SELECT 1 FROM pm_wip_limits w WHERE w.card_type_id = c.id))"
        ),
        {"ids": ours},
    ).all()
    for r in used:
        print(
            f"NOTICE 0077 downgrade: kept {r.name} ({r.id}) of tenant {r.tenant_id} and its "
            "placements: a recipe item or WIP limit references it",
            flush=True,
        )
    used_ids = {r.id for r in used}
    drop = [i for i in ours if i not in used_ids]
    if not drop:
        return {"unplaced": 0, "deleted": 0, "kept": len(used)}
    unplaced = bind.execute(
        sa.text("DELETE FROM pm_project_type_card_types WHERE card_type_id = ANY(:ids)"),
        {"ids": drop},
    ).rowcount
    deleted = bind.execute(
        sa.text("DELETE FROM pm_card_types WHERE id = ANY(:ids)"), {"ids": drop}
    ).rowcount
    return {"unplaced": unplaced or 0, "deleted": deleted or 0, "kept": len(used)}


def upgrade() -> None:
    add_card_types(op.get_bind())


def downgrade() -> None:
    remove_card_types(op.get_bind())
