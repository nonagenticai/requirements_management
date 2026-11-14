"""RLS: narrow ``system_published_read`` to PLATFORM tenants only.

George's rule (2026-09-30): customers never see system data. The app layer has
enforced that since #186 (``repositories.visibility.may_inherit_system`` /
``settings.platform_tenant_ids``), but ``system_published_read`` (created by
``2026_09_27_0044``) still admitted EVERY tenant to the system tenant's
PUBLISHED, tenant-visible rows at the DB layer. This migration closes that gap:
the policy now additionally requires the caller's ``app.current_tenant`` to be
one of the platform tenants.

Fails CLOSED: ``current_setting('app.current_tenant', true)`` is NULL (GUC never
set) or '' (reset), and ``NULL IN (...)`` is NULL, ``'' IN (...)`` is false —
neither admits a row. Only an exact platform uuid string does.

⛔ FROZEN literals, like 0044's system uuid: ``_SYSTEM_TENANT`` and
``_PLATFORM_TENANTS`` are NOT rendered from ``settings``. They MUST equal
``SYSTEM_TENANT_ID`` and the ``platform_tenant_ids`` default in
``src/settings.py``; change settings and this policy (a NEW migration) together,
or a platform tenant reads as a customer at the DB layer (silent empty reads).
Pinned by ``tests/test_system_tenant_inheritance.py``.

Only SELECT is affected; writes stay own-tenant via ``tenant_isolation``.

Revision ID: 2026_10_02_0047
Revises: 2026_10_02_0046
Create Date: 2026-10-02
"""

from alembic import op

revision = "2026_10_02_0047"
down_revision = "2026_10_02_0046"
branch_labels = None
depends_on = None

POLICY = "system_published_read"
_SYSTEM_TENANT = "0195f48b-d000-7000-8000-000000000001"
_PLATFORM_TENANTS = (
    "0195f48b-d000-7000-8000-000000000001",  # system
    "0195f48b-d000-7000-8000-000000000002",  # workflow-improvement
    "0195f48b-d000-7000-8000-000000000003",  # execution
    "0195f48b-d000-7000-8000-000000000004",  # prompt-improvement
    "0195f48b-d000-7000-8000-000000000005",  # procedure-improvement
    "019de842-c92a-7e37-8fdd-eb0d8c5396f9",  # n8n-dev (AA conveyor)
)
_PLATFORM_LIST = ", ".join(f"'{t}'" for t in _PLATFORM_TENANTS)
_PREDICATE = (
    f"tenant_id = '{_SYSTEM_TENANT}'::uuid "
    "AND visibility = 'tenant' "
    "AND version_status = 'PUBLISHED' "
    f"AND current_setting('app.current_tenant', true) IN ({_PLATFORM_LIST})"
)
# The 0044 predicate, restored by downgrade().
_PREVIOUS_PREDICATE = (
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
    # DDL stays inline: tests/test_keepout_no_cross_tenant_read.py parses the
    # CREATE/DROP POLICY events out of upgrade()'s own source.
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} AS PERMISSIVE FOR SELECT USING ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} AS PERMISSIVE FOR SELECT "
            f"USING ({_PREVIOUS_PREDICATE})"
        )
