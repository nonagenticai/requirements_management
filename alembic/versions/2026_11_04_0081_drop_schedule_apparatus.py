"""Drop the PM schedule apparatus: activities, buffers, schedule snapshots (R352).

George 2026-11-04: RM no longer mirrors Planka cards as ``pm_activities`` (R309) and no longer
schedules (CPM / CCPM, buffers, durations, schedule snapshots and their diff, what-if, the
``/pm/tools`` endpoints). Card ``requires`` stays where it lives (Planka / n8n). No live n8n
flow called these routes; fr only read activities and dependencies.

Upgrade, in order:

1. ``DELETE`` every ``architecture_relationships`` row with a ``pm_activities`` end. Those rows
   were the PM scheduling dependencies (``/pm/dependencies``) and any other link to an
   activity; the table is polymorphic (no FK), so they would otherwise survive as edges to
   rows that no longer exist.
2. Drop ``pm_risks.related_activity_ids`` (a JSONB list of activity ids; nothing to point
   at any more).
3. ``DROP TABLE`` ``pm_buffers``, ``pm_schedule_snapshots`` and ``pm_activities``. On head
   ``2026_11_04_0080`` no other table holds a foreign key to any of them (``pm_card_ledger``
   is keyed by tenant + card and references no table; ``pm_activities`` has only its own
   ``parent_activity_id`` self-FK), and each table's RLS policies
   (``tenant_isolation``, and ``system_published_read`` on ``pm_activities``) go with it.

``quality_evaluations`` and ``entity_version_history`` rows for activities and buffers are kept
on purpose: they are polymorphic history that outlives its entity by design
(``models/quality_evaluation.py``).

Forward-only: the downgrade raises. The rows are gone and the code that read them is deleted.

Revision ID: 2026_11_04_0081
Revises: 2026_11_04_0080
Create Date: 2026-11-04
"""

from alembic import op

revision = "2026_11_04_0081"
down_revision = "2026_11_04_0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM architecture_relationships "
        "WHERE source_entity_type = 'pm_activities' OR target_entity_type = 'pm_activities'"
    )
    op.execute("ALTER TABLE pm_risks DROP COLUMN IF EXISTS related_activity_ids")
    op.execute("DROP TABLE IF EXISTS pm_buffers")
    op.execute("DROP TABLE IF EXISTS pm_schedule_snapshots")
    op.execute("DROP TABLE IF EXISTS pm_activities")


def downgrade() -> None:
    raise NotImplementedError(
        "2026_11_04_0081 is forward-only: the PM schedule apparatus (pm_activities, pm_buffers, "
        "pm_schedule_snapshots) and its rows were dropped (R352)."
    )
