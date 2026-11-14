"""CONTAINMENT: drop ``system_superadmin_read`` (added by ``2026_10_01_0045``).

⛔ WHY — a live confidentiality regression, measured 2026-10-02 within the hour
of 0045 shipping. 0045 keyed the super-admin SELECT policy on the CALLER's
tenant GUC alone (``app.current_tenant = <system uuid>``). But the tenant a
request runs as is not always an authenticated fact here:

* ``src/auth/middleware.py`` admits a trusted-network peer (``TRUSTED_CIDRS``
  covers the whole k3s pod CIDR) with NO token and takes its tenant from a bare
  ``X-Tenant-Id`` header — or, with no header at all, from ``DEFAULT_TENANT_ID``;
* production sets ``DEFAULT_TENANT_ID`` to the SYSTEM tenant
  ``0195f48b-d000-7000-8000-000000000001`` and leaves
  ``AUTH_TOKENLESS_IDENTITY_MODE`` unset (= ``observe``: tokenless accepted).

So any in-mesh caller that sends no credential at all became the system tenant,
and after 0045 the system tenant reads EVERY tenant's rows. Measured as
``requirements_app`` (NOBYPASSRLS) with the GUC set to the system tenant:
requirements 556 -> 1,249 rows across 8 tenants, principles 1,194 -> 1,704.
Customer data must never be readable by a caller whose identity was asserted,
not proven (TENANCY strict separation).

This migration restores the exact pre-0045 read surface: ``tenant_isolation``
and ``system_published_read`` are untouched; only the 0045 policy goes. The
super-admin read itself (G-B) must come back gated on an AUTHENTICATED
principal (licence token + super-admin role), never on the tenant GUC alone —
a separate, reviewed change.

Forward-only: 0045 stays in history; ``downgrade`` re-creates its policy so the
chain stays reversible for tests, but production must never run it.

Revision ID: 2026_10_02_0046
Revises: 2026_10_01_0045
Create Date: 2026-10-02
"""

from alembic import op

revision = "2026_10_02_0046"
down_revision = "2026_10_01_0045"
branch_labels = None
depends_on = None

POLICY = "system_superadmin_read"
_SYSTEM_TENANT = "0195f48b-d000-7000-8000-000000000001"
_PREDICATE = f"current_setting('app.current_tenant', true) = '{_SYSTEM_TENANT}'"

# Exactly 0045's TABLES — pinned by tests/test_contain_superadmin_0046.py.
TABLES = (
    "principles",
    "requirements",
    "decisions",
    "toc_trees",
    "toc_nodes",
    "architecture_elements",
    "architecture_relationships",
    "architecture_element_realisation_bindings",
    "architecture_views",
    "architecture_standards",
    "pm_programmes",
    "pm_projects",
    "pm_activities",
    "pm_resources",
    "pm_buffers",
    "pm_schedule_snapshots",
    "pm_risks",
    "pm_benefits",
    "principle_conflicts",
    "quality_evaluations",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} AS PERMISSIVE FOR SELECT USING ({_PREDICATE})"
        )
