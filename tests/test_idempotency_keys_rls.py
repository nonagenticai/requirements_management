"""RLS on ``idempotency_keys`` holds at runtime, for a role RLS actually applies to.

Every other idempotency test connects as the suite's superuser, which bypasses RLS
unconditionally — so migration ``2026_10_27_0068``'s ``tenant_isolation`` policy (USING +
WITH CHECK) had never been exercised. This file installs THAT migration's predicate (read
from the migration module, not retyped), drops each transaction onto a NOLOGIN NOBYPASSRLS
role with ``SET LOCAL ROLE`` (transaction-scoped: pgbouncer transaction pooling forbids a
session-level SET), and binds the tenant the way the app does: ``tenant_id_var`` + the
``register_tenant_guc`` begin-listener on ``db.engine`` under ``TENANT_RLS_MODE=enforce``.

Positive controls keep it honest: tenant A sees and purges ITS rows through the same role,
so a zero for tenant B's rows means "filtered", not "nothing works".
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy.exc
from sqlalchemy import func, select, text

from db import AsyncSessionLocal, tenant_id_var
from models.idempotency import IdempotencyKey
from services.idempotency import Idempotency
from tests.tenants import T

pytestmark = [pytest.mark.tenant_isolation, pytest.mark.security]

TENANT_A = T("idem-rls-tenant-a")
TENANT_B = T("idem-rls-tenant-b")
_WORKER = os.getenv("PYTEST_XDIST_WORKER", "") or "serial"
ROLE = f"idem_rls_probe_{_WORKER}"
SCHEMA = f"test_{_WORKER}" if os.getenv("PYTEST_XDIST_WORKER") else "public"
MIGRATION = next(
    (Path(__file__).resolve().parent.parent / "alembic" / "versions").glob("2026_10_27_0068_*.py")
)


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0068", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def rls_enforced(test_engine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    m = _migration()
    table, predicate = m._TABLE, m._PREDICATE
    assert table == IdempotencyKey.__tablename__
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') "
                f'THEN CREATE ROLE "{ROLE}" NOLOGIN NOBYPASSRLS; END IF; END $$'
            )
        )
        await conn.execute(text(f'GRANT USAGE ON SCHEMA "{SCHEMA}" TO "{ROLE}"'))
        await conn.execute(text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{ROLE}"'))
        await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        await conn.execute(
            text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING ({predicate}) WITH CHECK ({predicate})"
            )
        )
    monkeypatch.setenv("TENANT_RLS_MODE", "enforce")
    yield
    async with test_engine.begin() as conn:
        await conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        await conn.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))


def _row(tenant: str, key: str, *, expired: bool = False) -> IdempotencyKey:
    return IdempotencyKey(
        tenant_id=tenant,
        subject_id="rls-subject",
        key=key,
        method="POST",
        route="/architecture/elements",
        request_hash="h",
        status_code=200,
        created_at=datetime.now(UTC) - timedelta(hours=25 if expired else 0),
    )


@pytest_asyncio.fixture
async def seeded(rls_enforced: None, test_session_factory) -> dict[str, str]:
    """Seeded as the superuser (no policy applies), so the rows exist regardless."""
    rows = {
        "a_live": _row(TENANT_A, "a-live"),
        "a_old": _row(TENANT_A, "a-old", expired=True),
        "b_live": _row(TENANT_B, "b-live"),
        "b_old": _row(TENANT_B, "b-old", expired=True),
    }
    async with test_session_factory() as session:
        session.add_all(rows.values())
        await session.commit()
        total = (await session.execute(select(func.count()).select_from(IdempotencyKey))).scalar()
    assert total == 4  # the superuser sees every tenant's rows
    return {name: str(r.id) for name, r in rows.items()}


class _AsTenant:
    """One app-engine transaction with the tenant GUC bound and the restricted role."""

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant

    async def __aenter__(self):
        self.token = tenant_id_var.set(self.tenant)
        self.session = AsyncSessionLocal()
        # First statement opens the transaction -> register_tenant_guc binds the GUC.
        await self.session.execute(text(f'SET LOCAL ROLE "{ROLE}"'))
        bound, bypass = (
            await self.session.execute(
                text(
                    "SELECT current_setting('app.current_tenant', true), "
                    "(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user)"
                )
            )
        ).one()
        assert bound == self.tenant, f"tenant GUC not bound by the app mechanism: {bound!r}"
        assert bypass is False, "the probe role bypasses RLS; this test would prove nothing"
        return self.session

    async def __aexit__(self, *exc):
        await self.session.rollback()
        await self.session.close()
        tenant_id_var.reset(self.token)


async def _ids(test_session_factory) -> set[str]:
    async with test_session_factory() as s:
        return {str(i) for i in (await s.execute(select(IdempotencyKey.id))).scalars()}


async def test_tenant_a_cannot_read_update_or_delete_tenant_b_rows(
    seeded: dict[str, str], test_session_factory
):
    async with _AsTenant(TENANT_A) as s:
        visible = {str(i) for i in (await s.execute(select(IdempotencyKey.id))).scalars()}
        assert visible == {seeded["a_live"], seeded["a_old"]}  # own rows: non-zero
        b_ids = [seeded["b_live"], seeded["b_old"]]
        upd = await s.execute(
            text("UPDATE idempotency_keys SET status_code = 500 WHERE id = ANY(:ids)"),
            {"ids": b_ids},
        )
        dele = await s.execute(
            text("DELETE FROM idempotency_keys WHERE id = ANY(:ids)"), {"ids": b_ids}
        )
        assert (upd.rowcount, dele.rowcount) == (0, 0)
        own = await s.execute(
            text("UPDATE idempotency_keys SET status_code = 201 WHERE id = :id"),
            {"id": seeded["a_live"]},
        )
        assert own.rowcount == 1  # the same statement works on A's own row
        await s.commit()
    async with test_session_factory() as s:
        b = (
            await s.execute(
                select(IdempotencyKey.status_code).where(IdempotencyKey.tenant_id == TENANT_B)
            )
        ).scalars()
        assert list(b) == [200, 200]


async def test_insert_or_move_into_another_tenant_violates_with_check(seeded: dict[str, str]):
    async with _AsTenant(TENANT_A) as s:
        with pytest.raises(sqlalchemy.exc.ProgrammingError, match="row-level security"):
            await s.execute(
                text(
                    "INSERT INTO idempotency_keys "
                    "(id, tenant_id, subject_id, key, method, route, request_hash) "
                    "VALUES (:id, :t, 's', 'k', 'POST', '/x', 'h')"
                ),
                {"id": uuid4(), "t": TENANT_B},
            )
    async with _AsTenant(TENANT_A) as s:
        with pytest.raises(sqlalchemy.exc.ProgrammingError, match="row-level security"):
            await s.execute(
                text("UPDATE idempotency_keys SET tenant_id = :t WHERE id = :id"),
                {"t": TENANT_B, "id": seeded["a_live"]},
            )


async def test_expired_key_purge_as_tenant_a_never_touches_tenant_b(
    seeded: dict[str, str], test_session_factory
):
    # The real claim path (bounded tenant GC included), as tenant A under RLS.
    async with _AsTenant(TENANT_A) as s:
        idem = Idempotency(
            s,
            route="/architecture/elements",
            tenant_id=TENANT_A,
            subject_id="rls-subject",
            visibility="tenant",
            key=f"fresh-{uuid4()}",
            body={"x": 1},
        )
        assert await idem.begin() is None
        await s.commit()
    remaining = await _ids(test_session_factory)
    assert seeded["a_old"] not in remaining  # positive control: the purge ran
    assert {seeded["b_live"], seeded["b_old"]} <= remaining
    # Even an unscoped purge (an app-level filter bug) cannot reach tenant B.
    async with _AsTenant(TENANT_A) as s:
        await s.execute(
            text("DELETE FROM idempotency_keys WHERE created_at < now() - interval '24 hours'")
        )
        await s.commit()
    assert {seeded["b_live"], seeded["b_old"]} <= await _ids(test_session_factory)
