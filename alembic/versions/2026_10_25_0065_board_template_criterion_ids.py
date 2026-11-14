"""Board templates: ``criterion_ids`` in the Relations group (fr cockpit R-CK10).

``criterion_ids`` is a NEW card custom field: the brief acceptance-criterion ids a card serves
(a JSON list or comma list, like ``required_by``), stamped by the cap.*.plan / cap.*.create
flows and read by name by ct.project_closure.doing. The code default
(``src/data/pm_catalogue_defaults.json``) only reaches tenants whose catalogue is imported
AFTER this ships — the import is create-if-missing and never touches an existing row — so
every existing ``pm_board_templates`` row gets the field here.

ADDITIVE ONLY (no-seeding rule, George 2026-09-28): a row is never replaced by the code
default. Each row keeps its own current lists, labels and groups; the one change is
``criterion_ids`` inserted into its OWN ``Relations`` group, right after ``required_by``
(else appended). A row that already carries ``criterion_ids`` in any group, or has no
``Relations`` group, is left as it is (no group is invented). Re-running changes nothing.

Downgrade removes ``criterion_ids`` from every Relations group (the field did not exist before).

No RLS change: row-level tenant policy already covers the table.

Revision ID: 2026_10_25_0065
Revises: 2026_10_25_0064
Create Date: 2026-10-25
"""

import json
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "2026_10_25_0065"
down_revision = "2026_10_25_0064"
branch_labels = None
depends_on = None

FIELD = "criterion_ids"
GROUP = "Relations"
AFTER = "required_by"


def add_field(groups: Any) -> Any | None:
    """The groups with ``criterion_ids`` added to Relations, or None when nothing changes."""
    if not isinstance(groups, list):
        return None
    if any(FIELD in (g.get("fields") or []) for g in groups if isinstance(g, dict)):
        return None
    for i, g in enumerate(groups):
        if isinstance(g, dict) and g.get("name") == GROUP:
            fields = list(g.get("fields") or [])
            at = fields.index(AFTER) + 1 if AFTER in fields else len(fields)
            fields.insert(at, FIELD)
            out = list(groups)
            out[i] = {**g, "fields": fields}
            return out
    return None


def remove_field(groups: Any) -> Any | None:
    """The groups without ``criterion_ids`` in Relations, or None when nothing changes."""
    if not isinstance(groups, list):
        return None
    changed = False
    out = []
    for g in groups:
        if isinstance(g, dict) and g.get("name") == GROUP and FIELD in (g.get("fields") or []):
            g = {**g, "fields": [f for f in g["fields"] if f != FIELD]}
            changed = True
        out.append(g)
    return out if changed else None


def rewrite_templates(bind: sa.engine.Connection, down: bool = False) -> int:
    rows = bind.execute(
        sa.text(
            "SELECT id, custom_field_groups FROM pm_board_templates "
            "WHERE custom_field_groups IS NOT NULL"
        )
    ).all()
    fn = remove_field if down else add_field
    changed = 0
    for row_id, groups in rows:
        new = fn(groups)
        if new is None:
            continue
        bind.execute(
            sa.text(
                "UPDATE pm_board_templates "
                "SET custom_field_groups = CAST(:g AS jsonb), updated_at = now() WHERE id = :id"
            ),
            {"g": json.dumps(new, ensure_ascii=False), "id": row_id},
        )
        changed += 1
    return changed


def upgrade() -> None:
    rewrite_templates(op.get_bind())


def downgrade() -> None:
    rewrite_templates(op.get_bind(), down=True)
