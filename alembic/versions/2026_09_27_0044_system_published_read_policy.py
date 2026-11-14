"""RLS: let every tenant READ the system tenant's PUBLISHED, tenant-visible rows.

Owner design (n8n_custom_nodes ``AA_IMPLEMENTATION/SPEC.md:106-118``): the
SYSTEM+IMPROVEMENT tenant ``0195f48b-d000-7000-8000-000000000001`` holds system
assets; every other tenant reads its own rows plus the system tenant's
PUBLISHED rows; writes are always own-tenant only.

⛔ WHY THIS MIGRATION EXISTS — the app-level change alone is INERT in prod.
``tenant_isolation`` (``2026_07_02_0030``, retyped by ``2026_08_24_0042``) is
``tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid``
and production connects as ``requirements_app`` (NOT owner, NOBYPASSRLS), so
RLS is enforcing. ``scope(include_system=True)`` would emit the widened WHERE
and Postgres would silently filter every system row back out: HTTP 200, zero
inherited rows, no error. The test suite connects as a superuser and would pass
either way — only a non-superuser probe can tell the difference
(``tests/test_system_tenant_inheritance.py::test_rls_*``).

Shape: a SECOND, PERMISSIVE policy ``FOR SELECT`` only. Permissive policies OR
together, so SELECT sees ``own OR system-published``. INSERT/UPDATE/DELETE are
untouched: UPDATE/DELETE must still satisfy ``tenant_isolation`` (the only
policy that applies to those commands), so a system row stays unwritable from
another tenant at the DB layer too — the backstop to ``can_mutate``'s tenant
check. DRAFT/DEPRECATED and ``visibility='user'`` system rows stay invisible.

⛔ FROZEN: the system uuid and the table list are literals, not rendered from
``settings``/``Base.metadata`` (same reasoning as ``2026_07_02_0030``'s frozen
predicate). ``SYSTEM_TENANT_ID`` in ``src/settings.py`` MUST equal
``_SYSTEM_TENANT`` below; changing one without the other makes inherited rows
read as absent.

Tables: every tenant table carrying ``version_status`` (the publication
state). Tables WITHOUT it inherit nothing and get no policy:
``architecture_relationships``, ``architecture_element_realisation_bindings``,
``pm_buffers``, ``pm_schedule_snapshots``, ``principle_conflicts``,
``toc_settings``, ``quality_evaluation``/history, reconciler bookkeeping.

Revision ID: 2026_09_27_0044
Revises: 2026_09_23_0043
Create Date: 2026-09-27
"""

from alembic import op

revision = "2026_09_27_0044"
down_revision = "2026_09_23_0043"
branch_labels = None
depends_on = None

POLICY = "system_published_read"
_SYSTEM_TENANT = "0195f48b-d000-7000-8000-000000000001"
_PREDICATE = (
    f"tenant_id = '{_SYSTEM_TENANT}'::uuid "
    "AND visibility = 'tenant' "
    "AND version_status = 'PUBLISHED'"
)

TABLES = (
    "principles",
    "requirements",
    "decisions",
    "toc_trees",
    "toc_nodes",
    "architecture_elements",
    "architecture_views",
    "architecture_standards",
    "pm_programmes",
    "pm_projects",
    "pm_activities",
    "pm_resources",
    "pm_risks",
    "pm_benefits",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} AS PERMISSIVE FOR SELECT USING ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
