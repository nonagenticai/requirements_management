"""RLS: let the SYSTEM tenant READ every tenant's rows (super-admin, TENANCY §2.1).

Owner design (n8n_custom_nodes ``AA_IMPLEMENTATION/SPEC.md:106-118``; G-B,
PR #187, same day as #185's visibility matrix): the SYSTEM+IMPROVEMENT tenant
``0195f48b-d000-7000-8000-000000000001`` does not merely *inherit* its own
PUBLISHED rows into other tenants (that is ``system_published_read``,
``2026_09_27_0044``) — when the system tenant is the ACTIVE caller on a read
route, it sees EVERY tenant's rows, any ``visibility``/``version_status``, no
narrowing at all. ``repositories/visibility.py::scope`` already implements
this: ``if include_system and same_tenant(tenant_id, system_tenant_id): return
stmt`` — the super-admin branch emits NO WHERE clause whatsoever.

⛔ WHY THIS MIGRATION EXISTS — the app-level change alone is INERT in prod,
the same failure class as ``2026_09_27_0044``. Production connects as
``requirements_app`` (NOT owner, NOBYPASSRLS, measured: ``rolsuper=false``,
``rolbypassrls=false``). With no app-level WHERE, Postgres falls back entirely
to RLS, and the only two policies in play are ``tenant_isolation`` (own tenant
only) and ``system_published_read`` (any tenant reading the SYSTEM tenant's
PUBLISHED rows) — neither admits "the SYSTEM tenant reading tenant B's DRAFT
row". Measured on prod: 1,194 of 1,694 rows visible when
``app.current_tenant`` is the system tenant — every other tenant's row is
silently dropped, HTTP 200, no error. Only a non-superuser probe can see this
(the HTTP test suite connects as a superuser and passes either way — see
``tests/test_system_tenant_inheritance.py``'s module docstring, same caveat).

Shape: a THIRD, PERMISSIVE policy ``FOR SELECT`` only, keyed on the CALLER
(the GUC), not on any column of the row — it has no ``tenant_id`` condition at
all, because the system tenant must see rows it does not own. Permissive
policies OR together, so SELECT sees ``own OR system-published OR
(caller-is-system)``. INSERT/UPDATE/DELETE are untouched: those commands are
governed solely by ``tenant_isolation``, so the system tenant still cannot
WRITE to another tenant's row at the DB layer — ``can_mutate`` is the app-level
mirror of that fence (``repositories/visibility.py``'s module docstring:
"Mutations never widen").

Fail-closed GUC, same shape as ``kag_common.database.rls._GUC_TEXT``: with
``app.current_tenant`` unset, ``current_setting(…, true)`` returns NULL and
``NULL = '<uuid>'`` is NULL (falsy) — the policy admits nothing rather than
raising. No ``NULLIF`` needed here (unlike the fleet helper) because this
predicate never compares the GUC to the empty string, only to a non-empty
literal, so an empty GUC already fails closed without it.

⛔ FROZEN: the system uuid is a literal, not rendered from
``settings``/``Base.metadata`` — same reasoning as ``2026_09_27_0044`` and
``2026_07_02_0030``. ``SYSTEM_TENANT_ID`` in ``src/settings.py`` MUST equal
``_SYSTEM_TENANT`` below; changing one without the other makes the system
tenant's super-admin read silently narrow back to its own rows.

Tables: every table carrying the strict ``tenant_isolation`` policy (NOT NULL
``tenant_id``) — the FULL backstop set from ``2026_07_02_0030``/
``2026_08_24_0042``, not the narrower ``version_status``-only set
``2026_09_27_0044`` used (this policy has no publication-state condition, so
it is not limited to tables that have one). Excluded, deliberately:
``toc_settings`` (nullable ``tenant_id`` — carries the fleet helper's
"global-or-own" policy set, named ``tenant_isolation_read``/``_ins``/``_upd``/
``_del``, not the literal name ``tenant_isolation`` this migration keys off);
``conflict_reconcile_watermark``/``conflict_reconcile_failure`` (operational
cursors, deliberately kept OUTSIDE the tenant-isolation backstop entirely —
see ``2026_08_24_0042``'s docstring); ``entity_version_history`` (no
``tenant_id`` column) and ``tenant_id_crosswalk`` (not tenant data).

Revision ID: 2026_10_01_0045
Revises: 2026_09_27_0044
Create Date: 2026-10-01
"""

from alembic import op

revision = "2026_10_01_0045"
down_revision = "2026_09_27_0044"
branch_labels = None
depends_on = None

POLICY = "system_superadmin_read"
_SYSTEM_TENANT = "0195f48b-d000-7000-8000-000000000001"
_PREDICATE = f"current_setting('app.current_tenant', true) = '{_SYSTEM_TENANT}'"

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
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} AS PERMISSIVE FOR SELECT USING ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
