"""fr cockpit A4: acceptance criteria as items, a closure phase event, budget_currency.

1. ``pm_project_brief_versions.brief -> 'acceptance_criteria'`` was an id-less ``list[str]``.
   Every version (not only the current one: every version is read back through
   ``schemas.pm_intake.ProjectBrief``) is rewritten to items
   ``{id, text, status: 'unchecked', evidence: []}``. Ids are DETERMINISTIC —
   ``uuid5(CRITERION_NS, f"{project_id}:{n}:{text}")`` where ``n`` counts earlier equal texts in
   the same list — so one criterion keeps one id across all versions of its project and a
   re-run converts nothing twice (items are left as they are). ``checked_at`` /
   ``checked_by`` are omitted (null), as the app stores them.
2. ``pm_project_phase_events.kind varchar(16) NOT NULL DEFAULT 'phase'`` with CHECK
   ``kind IN ('phase', 'closure')``. Backfill: every ``closed`` project with a phase and no
   closure event gets one, ``from = to = current_phase_id``, ``at = updated_at`` (the last
   write, the best bound on when it closed), ``subject_id = 'migration:2026_10_25_0064'``.
3. ``pm_projects.budget_currency varchar(3) NULL`` with CHECK ``~ '^[A-Z]{3}$'`` (the code
   list is checked at the request edge, ``schemas.currency``). Backfilled from the older
   free-text ``currency`` where that is a valid ISO 4217 code (any case / padding).

No RLS change: both tables already carry the tenant policy and it is row-level.

Revision ID: 2026_10_25_0064
Revises: 2026_10_19_0063
Create Date: 2026-10-25
"""

import json
import uuid
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "2026_10_25_0064"
down_revision = "2026_10_19_0063"
branch_labels = None
depends_on = None

# Fixed namespace for criterion ids minted by this migration (never change it: ids derive
# from it, and re-runs must produce the same ids).
CRITERION_NS = uuid.UUID("0196a4c4-7a00-7000-8000-00000000a404")
BACKFILL_SUBJECT = "migration:2026_10_25_0064"

# Same set as ``src/schemas/currency.py`` (copied: a migration must not import app code
# that can change after it ships).
ISO_4217_CODES = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD
    BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP
    DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR
    IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD
    MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB
    PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD
    SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS
    VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XTS XUA
    XXX YER ZAR ZMW ZWG ZWL
    """.split()
)


def criteria_to_items(project_id: str, value: Any) -> Any:
    """The item form of one stored ``acceptance_criteria`` value (pure; idempotent).

    Strings become ``unchecked`` items with deterministic ids; items pass through unchanged.
    """
    if not isinstance(value, list):
        return value
    seen: dict[str, int] = {}
    out: list[Any] = []
    for item in value:
        if isinstance(item, str):
            n = seen.get(item, 0)
            seen[item] = n + 1
            cid = uuid.uuid5(CRITERION_NS, f"{project_id}:{n}:{item}")
            out.append({"id": str(cid), "text": item, "status": "unchecked", "evidence": []})
        else:
            out.append(item)
    return out


def items_to_criteria(value: Any) -> Any:
    """The downgrade: items back to their texts."""
    if not isinstance(value, list):
        return value
    return [item.get("text", "") if isinstance(item, dict) else item for item in value]


def convert_briefs(bind: sa.engine.Connection, *, down: bool = False) -> int:
    """Rewrite every brief version whose criteria need it; returns the rows changed."""
    rows = bind.execute(
        sa.text(
            "SELECT id, project_id, brief -> 'acceptance_criteria' AS ac "
            "FROM pm_project_brief_versions "
            "WHERE jsonb_typeof(brief -> 'acceptance_criteria') = 'array'"
        )
    ).all()
    changed = 0
    for row_id, project_id, ac in rows:
        new = items_to_criteria(ac) if down else criteria_to_items(str(project_id), ac)
        if new == ac:
            continue
        bind.execute(
            sa.text(
                "UPDATE pm_project_brief_versions "
                "SET brief = jsonb_set(brief, '{acceptance_criteria}', CAST(:ac AS jsonb)) "
                "WHERE id = :id"
            ),
            {"ac": json.dumps(new, ensure_ascii=False), "id": row_id},
        )
        changed += 1
    return changed


def backfill_closure_events(bind: sa.engine.Connection) -> int:
    result = bind.execute(
        sa.text(
            "INSERT INTO pm_project_phase_events "
            "(id, tenant_id, project_id, from_phase_id, to_phase_id, at, subject_id, kind) "
            "SELECT gen_random_uuid(), p.tenant_id, p.id, p.current_phase_id, "
            "       p.current_phase_id, coalesce(p.updated_at, now()), :subject, 'closure' "
            "FROM pm_projects p "
            "WHERE p.status = 'closed' AND p.current_phase_id IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM pm_project_phase_events e "
            "                  WHERE e.project_id = p.id AND e.kind = 'closure')"
        ),
        {"subject": BACKFILL_SUBJECT},
    )
    return result.rowcount or 0


def backfill_budget_currency(bind: sa.engine.Connection) -> int:
    result = bind.execute(
        sa.text(
            "UPDATE pm_projects SET budget_currency = upper(btrim(currency)) "
            "WHERE budget_currency IS NULL AND upper(btrim(currency)) = ANY(:codes)"
        ),
        {"codes": sorted(ISO_4217_CODES)},
    )
    return result.rowcount or 0


def upgrade() -> None:
    bind = op.get_bind()
    convert_briefs(bind)

    op.add_column(
        "pm_project_phase_events",
        sa.Column("kind", sa.String(16), nullable=False, server_default="phase"),
    )
    op.create_check_constraint(
        "ck_pm_project_phase_events_kind", "pm_project_phase_events", "kind IN ('phase', 'closure')"
    )
    backfill_closure_events(bind)

    op.add_column("pm_projects", sa.Column("budget_currency", sa.String(3), nullable=True))
    op.create_check_constraint(
        "ck_pm_projects_budget_currency_iso4217", "pm_projects", "budget_currency ~ '^[A-Z]{3}$'"
    )
    backfill_budget_currency(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_constraint("ck_pm_projects_budget_currency_iso4217", "pm_projects", type_="check")
    op.drop_column("pm_projects", "budget_currency")
    bind.execute(sa.text("DELETE FROM pm_project_phase_events WHERE kind = 'closure'"))
    op.drop_constraint("ck_pm_project_phase_events_kind", "pm_project_phase_events", type_="check")
    op.drop_column("pm_project_phase_events", "kind")
    convert_briefs(bind, down=True)
