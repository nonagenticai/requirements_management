"""Card types ``change_principle_development`` + ``change_decision_development``, EXISTING tenants.

R326 (George 2026-10-30) overturned R258 for these two change_management asset-revision types:
fr mints both (revising an existing principle / decision), and with neither in the tenant's
catalogue fr stamped ``card_group ''`` on every such card. The code default
(``src/data/pm_catalogue_defaults.json``, regenerated from the v4 design) only reaches a tenant
whose catalogue is imported AFTER this ships: ``import-defaults`` creates missing card types but
places card types only into a project type it creates itself. So every existing tenant gets them
here.

ADDITIVE ONLY (no-seeding rule, George 2026-09-28): no existing row is changed or replaced.
Only a tenant that holds BOTH a ``change_management`` capability AND a card type named
``change_requirement_development`` is touched: the two types are its siblings, and a tenant
without the sibling never adopted that part of the catalogue (prod 2026-10-30: 5 tenants hold the
capability but no sibling; they get nothing). Per such tenant, each type it lacks BY NAME is
created under that capability with the frozen definition below (a tenant that already has a row
by that name keeps its own). A created row gets a DETERMINISTIC id, ``uuid5(ROW_NS,
"<tenant_id>:<name>")``, which marks it as this migration's (a tenant's own row has a uuidv7 id).
Each created row is then placed wherever the tenant placed its sibling
``change_requirement_development`` (same project type, same phase, never an entry type; only
rows created here are placed), so the composition matches the default ``it`` matrix without
assuming the tenant's ids. Re-running changes nothing.

Downgrade touches ONLY rows carrying this migration's id, never a tenant's own row of either
name. A created row that a recipe item or WIP limit references is in use: it is kept WITH its
placements and a NOTICE names it (RM keeps no card table keyed to card types, so those are the
only references). Every other created row loses its placements and is deleted.

No RLS change: row-level tenant policy already covers the tables.

Revision ID: 2026_10_30_0076
Revises: 2026_10_30_0075
Create Date: 2026-10-30
"""

import json
import uuid

import sqlalchemy as sa

from alembic import op

revision = "2026_10_30_0076"
down_revision = "2026_10_30_0075"
branch_labels = None
depends_on = None

CAPABILITY = "change_management"
SIBLING = "change_requirement_development"
# Namespace of the deterministic ids this migration gives the rows it creates (fixed forever).
ROW_NS = uuid.UUID("4d1c0a76-2026-5030-8326-0076c4a7e5d0")

_OUTPUTS = [
    "changes_artifact_id",
    "reviewed_changes_artifact_id",
    "published_artifact_id",
    "written_ids",
]
_ACCEPTANCE = [
    "READY: every_ask_cites_the_brief",
    "DOING: every_change_cites_a_source",
    "DOING: every_ask_has_a_change",
    "QA: every_revise_addressed",
    "QA: capability_acceptance_met",
    "DONE: artifact_is_qa_version",
]
# Frozen copy of the two entries in pm_catalogue_defaults.json at this revision (a test pins
# them equal, so drift between the migration and the default is a visible failure).
CARD_TYPES: list[dict] = [
    {
        "name": "change_principle_development",
        "title": "Change principle development",
        "description": "Defining overarching program principles and guidelines.",
        "inputs": [],
        "outputs": _OUTPUTS,
        "acceptance": _ACCEPTANCE,
        "rules": [],
        "human_task": False,
        "definition_of_done": None,
    },
    {
        "name": "change_decision_development",
        "title": "Change decision development",
        "description": "Documenting key program-level strategic decisions.",
        "inputs": [],
        "outputs": _OUTPUTS,
        "acceptance": _ACCEPTANCE,
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
    created: list = []
    caps = bind.execute(
        sa.text(
            "SELECT k.id, k.tenant_id, k.tenant_name, k.subject_id, k.visibility "
            "FROM pm_capabilities k WHERE k.name = :cap AND EXISTS ("
            "SELECT 1 FROM pm_card_types s WHERE s.tenant_id = k.tenant_id AND s.name = :sibling)"
        ),
        {"cap": CAPABILITY, "sibling": SIBLING},
    ).all()
    for cap_id, tenant_id, tenant_name, subject_id, visibility in caps:
        for ct in CARD_TYPES:
            res = bind.execute(
                sa.text(
                    "INSERT INTO pm_card_types (id, tenant_id, tenant_name, subject_id, "
                    "visibility, name, title, description, capability_id, inputs, outputs, "
                    "acceptance, rules, human_task, definition_of_done) "
                    "VALUES (:id, :tenant_id, :tenant_name, :subject_id, :visibility, :name, "
                    ":title, :description, :cap_id, CAST(:inputs AS jsonb), "
                    "CAST(:outputs AS jsonb), CAST(:acceptance AS jsonb), CAST(:rules AS jsonb), "
                    ":human_task, :dod) "
                    "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id"
                ),
                {
                    "id": row_id(tenant_id, ct["name"]),
                    "tenant_id": tenant_id,
                    "tenant_name": tenant_name,
                    "subject_id": subject_id,
                    "visibility": visibility,
                    "name": ct["name"],
                    "title": ct["title"],
                    "description": ct["description"],
                    "cap_id": cap_id,
                    "inputs": _j(ct["inputs"]),
                    "outputs": _j(ct["outputs"]),
                    "acceptance": _j(ct["acceptance"]),
                    "rules": _j(ct["rules"]),
                    "human_task": ct["human_task"],
                    "dod": ct["definition_of_done"],
                },
            )
            created.extend(res.scalars().all())
    if not created:
        return {"created": 0, "placed": 0}
    # Place each row created HERE wherever its tenant placed the sibling (same project type +
    # phase). A tenant's own pre-existing row of the same name is never placed by this.
    res = bind.execute(
        sa.text(
            "INSERT INTO pm_project_type_card_types "
            "(project_type_id, phase_id, card_type_id, tenant_id, is_entry) "
            "SELECT p.project_type_id, p.phase_id, n.id, p.tenant_id, false "
            "FROM pm_project_type_card_types p "
            "JOIN pm_card_types s ON s.id = p.card_type_id AND s.name = :sibling "
            "JOIN pm_card_types n ON n.tenant_id = s.tenant_id AND n.id = ANY(:ids) "
            "ON CONFLICT DO NOTHING"
        ),
        {"sibling": SIBLING, "ids": created},
    )
    return {"created": len(created), "placed": res.rowcount or 0}


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
        # print(), not a logger: alembic's logging here never reaches stderr (see 0042).
        print(
            f"NOTICE 0076 downgrade: kept {r.name} ({r.id}) of tenant {r.tenant_id} and its "
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
