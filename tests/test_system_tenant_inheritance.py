"""System-tenant READ inheritance (owner design, n8n_custom_nodes SPEC.md:106-118).

Every PLATFORM tenant (``settings.platform_tenant_ids``) reads its own rows PLUS
the system tenant's PUBLISHED, tenant-visible rows (``is_system: true`` in
responses); a CUSTOMER tenant reads its own rows only (owner rule 2026-09-30);
writes stay own-tenant.

The DB policy ``system_published_read`` is narrowed to the platform tenants by
``2026_10_02_0047``; the RLS tests below pin that a customer GUC sees no system row.

Two layers are exercised, because either alone can pass while the feature is
broken:

* the APP layer (``scope(include_system=True)`` on the list/get routes, the
  ``can_mutate`` tenant check, SQL slug-shadowing) — driven over HTTP. The test
  DSN is a superuser, so RLS is NOT in play here. ``tests/test_visibility_matrix.py``
  drives the same APP layer, same caveat;
* the RLS layer (``system_published_read`` from ``2026_09_27_0044``, narrowed by ``2026_10_02_0047``;
  the cancelled ``system_superadmin_read`` of 0045 was dropped by 0046) — driven as
  a NON-superuser role with the real ``tenant_isolation`` policy installed.
  Production connects as ``requirements_app`` (NOBYPASSRLS); without those
  policies every inherited/super-admin row is silently filtered back out, which
  no superuser test can see. The negative controls drop a policy and require
  the row it alone admitted to vanish.

G-B (cross-tenant super-admin read) is CANCELLED (user ruling 2026-10-02): the system
tenant reads only its own rows; see ``test_system_tenant_sees_only_its_own_rows``.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db import Base
from fleet_auth import downgrade_statements, tenant_table_names, upgrade_statements
from models.principles import Principle
from repositories.visibility import can_mutate
from settings import Settings, get_settings
from tests.tenants import T

pytestmark = [pytest.mark.tenant_isolation, pytest.mark.security]

SYSTEM = get_settings().system_tenant_id
TENANT_B = T("system-inheritance-tenant-b")
TENANT_C = T("system-inheritance-tenant-c")
CUSTOMER = "0195f48b-d000-7000-8000-000000000100"
N8N_DEV = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"


def _principle(slug: str, tenant: str, status: str, visibility: str = "tenant") -> Principle:
    return Principle(
        slug=slug,
        title=f"Principle {slug}",
        statement=f"Statement for {slug}.",
        tenant_id=tenant,
        tenant_name="system-inheritance-probe",
        subject_id=tenant,
        visibility=visibility,
        rationale=None,
        implications=[],
        tags=[],
        active=True,
        version_status=status,
    )


@pytest_asyncio.fixture
async def seeded(test_session: AsyncSession) -> dict[str, Principle]:
    rows = {
        "sys_pub": _principle("sys-published", SYSTEM, "PUBLISHED"),
        "sys_draft": _principle("sys-draft", SYSTEM, "DRAFT"),
        "sys_deprecated": _principle("sys-deprecated", SYSTEM, "DEPRECATED"),
        "sys_user_private": _principle("sys-user-private", SYSTEM, "PUBLISHED", "user"),
        "sys_shadowed": _principle("shared-slug", SYSTEM, "PUBLISHED"),
        "b_shadowing": _principle("shared-slug", TENANT_B, "DRAFT"),
        "b_own": _principle("b-own", TENANT_B, "DRAFT"),
    }
    test_session.add_all(rows.values())
    await test_session.commit()
    for r in rows.values():
        await test_session.refresh(r)
    # refresh() autobegins a transaction holding AccessShare on principles; end
    # it so later DDL (DROP/CREATE POLICY) is not blocked by an idle-in-tx
    # session. expire_on_commit=False keeps the loaded attributes usable.
    await test_session.commit()
    return rows


def _client_as(test_client, tenant: str):
    test_client.headers = {"X-Tenant-Id": tenant}
    return test_client


@pytest.fixture
def b_is_platform(monkeypatch):
    """Make TENANT_B a PLATFORM tenant for this test only.

    Since 2026-09-30 only platform tenants inherit system rows; TENANT_B is a
    customer by default. ``is_platform_tenant`` reads the setting per call, so
    patching the cached instance is enough.
    """
    s = get_settings()
    monkeypatch.setattr(s, "platform_tenant_ids", f"{s.platform_tenant_ids},{TENANT_B}")
    assert TENANT_B in s.platform_tenant_id_set


# ── customer fence (owner rule 2026-09-30): a non-platform tenant inherits NOTHING ──


async def test_customer_tenant_never_sees_a_published_system_row(test_client, seeded) -> None:
    assert TENANT_B not in get_settings().platform_tenant_id_set
    client = _client_as(test_client, TENANT_B)
    resp = await client.get("/principles", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()}
    assert ids == {str(seeded["b_shadowing"].id), str(seeded["b_own"].id)}
    assert resp.headers["X-Total-Count"] == "2"
    for key in ("sys_pub", "sys_shadowed"):
        one = await client.get(f"/principles/{seeded[key].id}")
        assert one.status_code == 404, one.text


# ── (a) + (d): list/get widen to PUBLISHED system rows; own rows shadow by slug ──


@pytest.mark.usefixtures("b_is_platform")
async def test_tenant_sees_own_plus_published_system_rows(test_client, seeded) -> None:
    client = _client_as(test_client, TENANT_B)
    resp = await client.get("/principles", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    by_id = {item["id"]: item for item in resp.json()}

    expected = {str(seeded[k].id) for k in ("sys_pub", "b_shadowing", "b_own")}
    assert set(by_id) == expected, sorted(item["slug"] for item in by_id.values())
    # Counted in SQL, so the header agrees with the page (not trimmed post-LIMIT).
    assert resp.headers["X-Total-Count"] == "3"

    assert by_id[str(seeded["sys_pub"].id)]["is_system"] is True
    assert by_id[str(seeded["b_own"].id)]["is_system"] is False
    assert by_id[str(seeded["b_shadowing"].id)]["is_system"] is False

    one = await client.get(f"/principles/{seeded['sys_pub'].id}")
    assert one.status_code == 200, one.text
    assert one.json()["is_system"] is True


@pytest.mark.usefixtures("b_is_platform")
async def test_own_row_shadows_system_row_of_same_slug(test_client, seeded) -> None:
    client = _client_as(test_client, TENANT_B)
    resp = await client.get("/principles", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    shared = [item for item in resp.json() if item["slug"] == "shared-slug"]
    assert [item["id"] for item in shared] == [str(seeded["b_shadowing"].id)]

    # Shadowing is in SQL: paging one row at a time still yields exactly 3
    # distinct rows and never the shadowed system twin.
    seen: list[str] = []
    for offset in range(4):
        page = await client.get("/principles", params={"limit": 1, "offset": offset})
        assert page.status_code == 200, page.text
        assert page.headers["X-Total-Count"] == "3"
        seen.extend(item["id"] for item in page.json())
    assert len(seen) == 3 and len(set(seen)) == 3
    assert str(seeded["sys_shadowed"].id) not in seen


# ── (b) negative control: unpublished / user-private system rows stay hidden ──


@pytest.mark.usefixtures("b_is_platform")
@pytest.mark.parametrize("key", ["sys_draft", "sys_deprecated", "sys_user_private"])
async def test_unpublished_system_rows_are_invisible(test_client, seeded, key) -> None:
    client = _client_as(test_client, TENANT_B)
    listed = await client.get("/principles", params={"limit": 100})
    assert listed.status_code == 200, listed.text
    assert str(seeded[key].id) not in {item["id"] for item in listed.json()}

    one = await client.get(f"/principles/{seeded[key].id}")
    assert one.status_code == 404, one.text


# ── (c) writes never widen ──


@pytest.mark.usefixtures("b_is_platform")
async def test_tenant_cannot_mutate_a_system_row(test_client, test_session, seeded) -> None:
    client = _client_as(test_client, TENANT_B)
    # Capture plain values up front: after expire_all() any attribute access on
    # the ORM object would lazy-load outside a greenlet (MissingGreenlet).
    target_id = seeded["sys_pub"].id
    original_title = seeded["sys_pub"].title
    assert original_title == "Principle sys-published"

    patched = await client.patch(f"/principles/{target_id}", json={"title": "hijacked"})
    assert patched.status_code == 404, patched.text
    deleted = await client.delete(f"/principles/{target_id}")
    assert deleted.status_code == 404, deleted.text

    test_session.expire_all()
    title = (
        await test_session.execute(select(Principle.title).where(Principle.id == target_id))
    ).scalar_one()  # scalar_one also proves the row was not deleted
    assert title == original_title
    await test_session.commit()


def test_can_mutate_refuses_a_row_outside_the_active_tenant() -> None:
    row = SimpleNamespace(tenant_id=SYSTEM, visibility="tenant", subject_id=SYSTEM)
    assert can_mutate(row, subject_id=TENANT_B, tenant_id=TENANT_B) is False
    # Positive control: same row, own tenant → mutable.
    assert can_mutate(row, subject_id=SYSTEM, tenant_id=SYSTEM) is True
    # Case/format of the uuid string must not matter (the column compares as uuid).
    assert can_mutate(row, subject_id=SYSTEM, tenant_id=SYSTEM.upper()) is True


# ── (e) the system tenant itself: own rows ONLY (user ruling 2026-10-02) ──
#
# G-B (cross-tenant super-admin read) is CANCELLED: the system tenant must never
# read another tenant's data. Its identity is, on the tokenless path, an
# asserted fact (DEFAULT_TENANT_ID fallback), so a cross-tenant read keyed on it
# is a confidentiality hole. ``scope`` gives it the ordinary own-tenant scope.


async def test_system_tenant_sees_only_its_own_rows(test_client, seeded) -> None:
    client = _client_as(test_client, SYSTEM)
    resp = await client.get("/principles", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()}
    # Own tenant-visible rows (any status) — never TENANT_B's rows. The
    # user-visibility row is owned by subject_id=SYSTEM here, so it is the
    # caller's own and stays visible only when the caller subject matches.
    own_tenant_visible = {
        str(seeded[k].id) for k in ("sys_pub", "sys_draft", "sys_deprecated", "sys_shadowed")
    }
    assert own_tenant_visible <= ids
    assert str(seeded["b_own"].id) not in ids
    assert str(seeded["b_shadowing"].id) not in ids
    one = await client.get(f"/principles/{seeded['b_own'].id}")
    assert one.status_code == 404, one.text


# ── RLS layer: the migration's policy, as a non-superuser ──

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "2026_10_02_0047_narrow_system_published_read.py"
)
_WORKER = os.getenv("PYTEST_XDIST_WORKER", "") or "serial"
PROBE_ROLE = f"sys_inherit_probe_{_WORKER}"
PROBE_SCHEMA = f"test_{_WORKER}" if os.getenv("PYTEST_XDIST_WORKER") else "public"
TABLE = "principles"


def _migration():
    spec = importlib.util.spec_from_file_location("_mig_0047", _MIGRATION)
    assert spec and spec.loader, _MIGRATION
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_freezes_the_configured_system_tenant() -> None:
    mig = _migration()
    assert mig._SYSTEM_TENANT == SYSTEM, (
        "SYSTEM_TENANT_ID and the RLS policy's frozen uuid disagree; inherited rows "
        "would read as absent in production."
    )
    assert TABLE in mig.TABLES


def test_migration_freezes_the_configured_platform_set() -> None:
    """Settings default and the migration's frozen literals must change together."""
    mig = _migration()
    default = Settings.model_fields["platform_tenant_ids"].default
    assert set(mig._PLATFORM_TENANTS) == {p.strip() for p in default.split(",")}
    assert len(set(mig._PLATFORM_TENANTS)) == len(mig._PLATFORM_TENANTS)


@pytest_asyncio.fixture
async def rls_on_principles(test_engine) -> AsyncIterator[SimpleNamespace]:
    mig = _migration()
    others = set(tenant_table_names(Base.metadata)) - {TABLE}
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PROBE_ROLE}') "
                f'THEN CREATE ROLE "{PROBE_ROLE}" NOLOGIN NOBYPASSRLS; END IF; END $$'
            )
        )
        await conn.execute(text(f'GRANT USAGE ON SCHEMA "{PROBE_SCHEMA}" TO "{PROBE_ROLE}"'))
        await conn.execute(text(f'GRANT SELECT, UPDATE ON {TABLE} TO "{PROBE_ROLE}"'))
        for stmt in upgrade_statements(Base.metadata, exclude=others):
            await conn.execute(text(stmt))
        await conn.execute(
            text(
                f"CREATE POLICY {mig.POLICY} ON {TABLE} AS PERMISSIVE FOR SELECT "
                f"USING ({mig._PREDICATE})"
            )
        )
    try:
        yield mig
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text(f"DROP POLICY IF EXISTS {mig.POLICY} ON {TABLE}"))
            for stmt in downgrade_statements(Base.metadata, exclude=others):
                await conn.execute(text(stmt))


async def _visible_slugs_as(test_engine, tenant: str) -> set[str]:
    async with test_engine.begin() as conn:
        await conn.execute(text(f'SET LOCAL ROLE "{PROBE_ROLE}"'))
        await conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": tenant})
        rows = await conn.execute(text(f"SELECT slug, tenant_id::text FROM {TABLE}"))
        return {f"{slug}@{'sys' if tid == SYSTEM else 'own'}" for slug, tid in rows.all()}


async def test_rls_customer_guc_sees_no_system_row(test_engine, rls_on_principles, seeded) -> None:
    """TENANT_B is a customer: own rows only, every system row filtered out by RLS."""
    assert await _visible_slugs_as(test_engine, TENANT_B) == {"shared-slug@own", "b-own@own"}
    assert await _visible_slugs_as(test_engine, CUSTOMER) == set()


async def test_rls_platform_guc_sees_published_system_rows_and_nothing_else(
    test_engine, rls_on_principles, seeded
) -> None:
    visible = await _visible_slugs_as(test_engine, N8N_DEV)
    # Only PUBLISHED + visibility='tenant' system rows; no draft/deprecated/user rows.
    assert visible == {"sys-published@sys", "shared-slug@sys"}


@pytest.mark.parametrize("guc", ["", None])
async def test_rls_unset_or_empty_guc_sees_nothing(
    test_engine, rls_on_principles, seeded, guc
) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text(f'SET LOCAL ROLE "{PROBE_ROLE}"'))
        if guc is not None:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :t, true)"), {"t": guc}
            )
        rows = await conn.execute(text(f"SELECT slug FROM {TABLE}"))
        assert rows.all() == []


async def test_rls_never_lets_another_tenant_update_a_system_row(
    test_engine, rls_on_principles, seeded
) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text(f'SET LOCAL ROLE "{PROBE_ROLE}"'))
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"), {"t": N8N_DEV}
        )
        result = await conn.execute(
            text(f"UPDATE {TABLE} SET title = 'hijacked' WHERE id = :id"),
            {"id": str(seeded["sys_pub"].id)},
        )
        assert result.rowcount == 0


async def test_rls_negative_control_without_the_policy_hides_system_rows(
    test_engine, rls_on_principles, seeded
) -> None:
    """Proves the policy — not a superuser bypass — is what admitted them above."""
    mig = rls_on_principles
    async with test_engine.begin() as conn:
        # Fail fast on a lock regression instead of hanging the suite.
        await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        await conn.execute(text(f"DROP POLICY {mig.POLICY} ON {TABLE}"))
    try:
        visible = await _visible_slugs_as(test_engine, N8N_DEV)
        assert visible == set()
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            await conn.execute(text(f"DROP POLICY IF EXISTS {mig.POLICY} ON {TABLE}"))
            await conn.execute(
                text(
                    f"CREATE POLICY {mig.POLICY} ON {TABLE} AS PERMISSIVE FOR SELECT "
                    f"USING ({mig._PREDICATE})"
                )
            )
