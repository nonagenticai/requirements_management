"""Backfill the ``BLOCKED-<card_id>`` decisions minted by n8n's blocked-escalate.

Until n8n_custom_nodes#2126, ``patterns/blocked-escalate`` called createDecision
with title ``BLOCKED-<card_id>``, no project_id and no stable_key, so every
re-block minted a new unbound row (8,245 rows / 3,534 cards on 2026-10-08).
#2126 mints ``Blocked: <card name>`` with project_id + stable_key
``blocked:<card_id>`` (one decision per card). This script brings the existing
rows to the same shape, in three SEPARATE phases, each applied on its own:

  titles    title := ``Blocked: <card name | card_id>`` (sliced to 200, exactly
            #2126's expression). Every candidate row.
  bind      the survivor of each (tenant, card) — a row already keyed
            ``blocked:<card_id>`` if there is one, else the newest candidate — gets
            project_id := the card's newest project uuid (never the system
            ``…0001``, never a non-uuid; #2126's rule) and stable_key :=
            ``blocked:<card_id>``. Skipped when the survivor already carries the
            key (bound by an earlier run, or minted by #2126 — that row IS the
            survivor, so phases can run apart and new BLOCKED- rows can arrive
            in between without a second row ever getting the key).
  collapse  every other ``proposed`` candidate of the card becomes
            ``superseded`` with superseded_by := the survivor's slug.

Default is a DRY RUN: counts plus sample before→after rows, no writes.
``--apply <phase>`` writes. Every write first stores the old AND new values
in ``<backup-schema>.decisions_blocked_backfill`` (PK id+phase, first write
wins, so a rerun never overwrites the originals). ``--restore <phase|all>``
puts the old values back, but only on rows whose current value is still what
the backfill wrote — anything edited since is reported and left alone.

Card names come from a JSON file ``{"<card_id>": "<name>"}`` (``--card-names``),
exported read-only from the kanban DB; a card missing from it keeps its id,
as #2126 does. Rows whose context does not parse are reported, never touched.

Run as the table owner (RLS on ``decisions`` is ENABLE, not FORCE).

Usage::

    DATABASE_URL=postgresql+psycopg://... uv run python scripts/backfill_blocked_decisions.py \\
        --card-names names.json [--apply titles|bind|collapse] [--restore titles|bind|collapse|all]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

PHASES = ("titles", "bind", "collapse")
# Columns each phase owns, in the order they are backed up / restored.
PHASE_COLUMNS: dict[str, tuple[str, ...]] = {
    "titles": ("title",),
    "bind": ("project_id", "stable_key"),
    "collapse": ("status", "superseded_by"),
}
SYSTEM_PROJECT = "00000000-0000-0000-0000-000000000001"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
CARD_RE = re.compile(r"^card=([0-9]+);")
PROJECT_RE = re.compile(r"project=([0-9a-fA-F-]{36})")
BATCH = 500
DEFAULT_BACKUP_SCHEMA = "backfill_backup"


@dataclass
class Row:
    id: str
    tenant_id: str
    slug: str
    title: str
    project_id: str | None
    stable_key: str | None
    status: str
    superseded_by: str | None
    created_at: Any
    context: str
    visibility: str = "tenant"
    card: str | None = None
    project: str | None = None


@dataclass
class Plan:
    candidates: int = 0
    unparseable: list[str] = field(default_factory=list)
    # rows whose shape the plan cannot safely transform: non-tenant visibility
    # (the #2126 lookup and the unique indexes are visibility='tenant' only),
    # or bound to a project without a stable_key (a retitle could collide on
    # uq_decisions_tenant_project_title). Reported, never touched.
    skipped_shape: list[str] = field(default_factory=list)
    # phase -> list of (row id, {column: (old, new)})
    changes: dict[str, list[tuple[str, dict[str, tuple[Any, Any]]]]] = field(
        default_factory=lambda: {p: [] for p in PHASES}
    )


def valid_project(value: str | None) -> str | None:
    if value and UUID_RE.match(value) and value.lower() != SYSTEM_PROJECT:
        return value
    return None


def target_title(card: str, names: dict[str, str]) -> str:
    return ("Blocked: " + (names.get(card) or card))[:200]


def compute_plan(rows: list[Row], keyed: dict[tuple[str, str], str], names: dict[str, str]) -> Plan:
    """Pure: from candidate rows (+ existing #2126 rows keyed by (tenant, stable_key)
    → slug) compute every phase's per-row column changes."""
    plan = Plan(candidates=len(rows))
    groups: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        m = CARD_RE.match(r.context or "")
        if not m:
            plan.unparseable.append(r.id)
            continue
        if r.visibility != "tenant" or (r.project_id is not None and r.stable_key is None):
            plan.skipped_shape.append(r.id)
            continue
        r.card = m.group(1)
        pm = PROJECT_RE.search(r.context or "")
        r.project = valid_project(pm.group(1) if pm else None)
        groups.setdefault((r.tenant_id, r.card), []).append(r)

    for (tenant, card), members in sorted(groups.items()):
        members.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        title = target_title(card, names)
        for r in members:
            if r.title != title:
                plan.changes["titles"].append((r.id, {"title": (r.title, title)}))

        key = f"blocked:{card}"
        existing_slug = keyed.get((tenant, key))
        already = next((m for m in members if m.stable_key == key), None)
        survivor = None if existing_slug else (already or members[0])
        survivor_slug = existing_slug or (survivor.slug if survivor else members[0].slug)
        if survivor is not None and survivor.stable_key != key:
            project = next((m.project for m in members if m.project), None)
            if project:
                plan.changes["bind"].append(
                    (
                        survivor.id,
                        {
                            "project_id": (survivor.project_id, project),
                            "stable_key": (survivor.stable_key, key),
                        },
                    )
                )
        for r in members:
            if r is survivor or r.status != "proposed":
                continue
            plan.changes["collapse"].append(
                (
                    r.id,
                    {
                        "status": (r.status, "superseded"),
                        "superseded_by": (r.superseded_by, survivor_slug),
                    },
                )
            )
    return plan


def backup_table(schema: str) -> str:
    if not re.match(r"^[a-z_][a-z0-9_]*$", schema):
        raise ValueError(f"bad backup schema name: {schema!r}")
    return f"{schema}.decisions_blocked_backfill"


async def backup_exists(conn: AsyncConnection, schema: str) -> bool:
    res = await conn.execute(
        text("SELECT to_regclass(:t) IS NOT NULL"), {"t": backup_table(schema)}
    )
    return bool(res.scalar())


async def ensure_backup(conn: AsyncConnection, schema: str) -> None:
    table = backup_table(schema)
    await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    await conn.execute(
        text(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                id uuid NOT NULL,
                phase text NOT NULL,
                old_values jsonb NOT NULL,
                new_values jsonb NOT NULL,
                backed_up_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (id, phase)
            )"""
        )
    )


async def load(conn: AsyncConnection, schema: str) -> tuple[list[Row], dict[tuple[str, str], str]]:
    clause = "d.title LIKE 'BLOCKED-%'"
    if await backup_exists(conn, schema):
        clause += f" OR d.id IN (SELECT id FROM {backup_table(schema)})"
    res = await conn.execute(
        text(
            f"""SELECT d.id::text, d.tenant_id::text, d.slug, d.title, d.project_id, d.stable_key,
                       d.status, d.superseded_by, d.created_at, d.context, d.visibility
                FROM decisions d WHERE {clause}"""
        )
    )
    rows = [Row(*r) for r in res.fetchall()]
    ids = {r.id for r in rows}
    res = await conn.execute(
        text(
            "SELECT id::text, tenant_id::text, stable_key, slug FROM decisions "
            "WHERE stable_key LIKE 'blocked:%'"
        )
    )
    # Only rows the backfill did NOT produce count as an existing #2126 survivor.
    keyed = {(t, k): s for (i, t, k, s) in res.fetchall() if i not in ids}
    return rows, keyed


async def apply_phase(conn: AsyncConnection, plan: Plan, phase: str, schema: str) -> int:
    """Write one phase, BATCH rows per committed transaction. ``conn`` must not
    be inside a transaction (each batch commits on its own)."""
    async with conn.begin():
        await ensure_backup(conn, schema)
    table = backup_table(schema)
    cols = PHASE_COLUMNS[phase]
    changes = plan.changes[phase]
    for start in range(0, len(changes), BATCH):
        async with conn.begin():
            for row_id, diff in changes[start : start + BATCH]:
                old = {c: diff[c][0] for c in cols}
                new = {c: diff[c][1] for c in cols}
                await conn.execute(
                    text(
                        f"INSERT INTO {table} (id, phase, old_values, new_values) "
                        "VALUES (CAST(:id AS uuid), :phase, CAST(:old AS jsonb), CAST(:new AS jsonb)) "
                        "ON CONFLICT (id, phase) DO NOTHING"
                    ),
                    {"id": row_id, "phase": phase, "old": json.dumps(old), "new": json.dumps(new)},
                )
                sets = ", ".join(f"{c} = :{c}" for c in cols)
                await conn.execute(
                    text(
                        f"UPDATE decisions SET {sets}, updated_at = now() WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": row_id, **new},
                )
    return len(changes)


async def restore_phase(conn: AsyncConnection, phase: str, schema: str) -> tuple[int, list[str]]:
    """Put old values back where the row still carries exactly what we wrote.
    One transaction. ``conn`` must not be inside a transaction."""
    restored, skipped = 0, []
    async with conn.begin():
        if not await backup_exists(conn, schema):
            return 0, []
        table = backup_table(schema)
        cols = PHASE_COLUMNS[phase]
        res = await conn.execute(
            text(f"SELECT id::text, old_values, new_values FROM {table} WHERE phase = :p"),
            {"p": phase},
        )
        for row_id, old, new in res.fetchall():
            match = " AND ".join(f"{c} IS NOT DISTINCT FROM :new_{c}" for c in cols)
            sets = ", ".join(f"{c} = :old_{c}" for c in cols)
            params: dict[str, Any] = {"id": row_id}
            for c in cols:
                params[f"old_{c}"] = old.get(c)
                params[f"new_{c}"] = new.get(c)
            r = await conn.execute(
                text(
                    f"UPDATE decisions SET {sets}, updated_at = now() "
                    f"WHERE id = CAST(:id AS uuid) AND {match}"
                ),
                params,
            )
            if r.rowcount:
                restored += 1
            else:
                skipped.append(row_id)
    return restored, skipped


def summarize(plan: Plan, rows: list[Row], samples: int = 5) -> dict[str, Any]:
    by_id = {r.id: r for r in rows}
    out: dict[str, Any] = {
        "candidates": plan.candidates,
        "unparseable": len(plan.unparseable),
        "unparseable_ids": plan.unparseable[:20],
        "skipped_shape": len(plan.skipped_shape),
        "skipped_shape_ids": plan.skipped_shape[:20],
        "cards": len({(r.tenant_id, r.card) for r in rows if r.card}),
    }
    for phase in PHASES:
        ch = plan.changes[phase]
        out[phase] = {
            "rows": len(ch),
            "samples": [
                {
                    "id": i,
                    "slug": by_id[i].slug,
                    **{c: {"before": o, "after": n} for c, (o, n) in d.items()},
                }
                for i, d in ch[:samples]
            ],
        }
    return out


async def run(args: argparse.Namespace) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    names: dict[str, str] = {}
    if args.card_names:
        with open(args.card_names) as fh:
            names = {str(k): str(v) for k, v in json.load(fh).items()}
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            if args.restore:
                order = list(reversed(PHASES)) if args.restore == "all" else [args.restore]
                report = {}
                for phase in order:
                    n, skipped = await restore_phase(conn, phase, args.backup_schema)
                    report[phase] = {
                        "restored": n,
                        "skipped_edited_since": len(skipped),
                        "skipped_ids": skipped[:20],
                    }
                print(json.dumps({"restore": report}, indent=2, default=str))
                return 0
            async with conn.begin():
                # The read never writes; on the dry run nothing else runs at all.
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                rows, keyed = await load(conn, args.backup_schema)
            plan = compute_plan(rows, keyed, names)
            summary = summarize(plan, rows)
            summary["card_names_loaded"] = len(names)
            cards = {r.card for r in rows if r.card}
            summary["cards_with_name"] = sum(1 for c in cards if names.get(c))
            summary["titles_named_samples"] = [
                {"id": i, "before": d["title"][0], "after": d["title"][1]}
                for i, d in plan.changes["titles"]
                if not d["title"][1].endswith(d["title"][0].removeprefix("BLOCKED-"))
            ][:5]
            if args.apply:
                summary["applied"] = {
                    args.apply: await apply_phase(conn, plan, args.apply, args.backup_schema)
                }
            else:
                summary["dry_run"] = True
            print(json.dumps(summary, indent=2, default=str))
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Backfill the BLOCKED-<card_id> decisions minted by blocked-escalate."
    )
    p.add_argument("--card-names", help='JSON file {"<card_id>": "<card name>"}')
    g = p.add_mutually_exclusive_group()
    g.add_argument("--apply", choices=PHASES, help="write ONE phase (default: dry run)")
    g.add_argument("--restore", choices=(*PHASES, "all"), help="undo a phase from the backup table")
    p.add_argument("--backup-schema", default=DEFAULT_BACKUP_SCHEMA)
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
