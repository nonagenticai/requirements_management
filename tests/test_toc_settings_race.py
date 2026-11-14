"""Regression: concurrent first-time TocSettings upserts must not 500.

``upsert_settings`` used to do ``_settings_row`` → ``session.add`` →
``commit`` with no guard. Two concurrent first-time writes for the same
``(tenant_id, project_id)`` both see ``None`` → both insert → the second
commit raises ``IntegrityError`` on ``uq_toc_settings_tenant_project`` →
uncaught → 500. The fix catches the IntegrityError, rolls back, re-fetches
the winner's row, and applies this call's settings on it.
"""

import asyncio

from sqlalchemy import func, select

from models.toc import TocSettings
from services.toc_service import upsert_settings
from tests.tenants import T


async def _count_rows(session, tenant_id: str, project_id: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(TocSettings)
            .where(
                TocSettings.tenant_id == tenant_id,
                TocSettings.project_id == project_id,
            )
        )
    ).scalar_one()


async def test_toc_settings_concurrent_first_write_no_500(test_session_factory):
    """Two independent sessions racing the same first-time upsert: neither
    raises (no 500) and exactly one row survives."""
    tenant_id = T("race-tenant-1")
    project_id = "race-project-1"

    async with test_session_factory() as session_a, test_session_factory() as session_b:
        # True concurrency: two independent AsyncSessions (each its own
        # connection) both attempt the first-time insert under asyncio.gather.
        # One commit wins; the loser hits uq_toc_settings_tenant_project and
        # must recover in-place rather than 500.
        results = await asyncio.gather(
            upsert_settings(session_a, tenant_id, project_id, {"src": "a"}),
            upsert_settings(session_b, tenant_id, project_id, {"src": "b"}),
            return_exceptions=True,
        )

    # Neither call raised — an IntegrityError leaking out is the 500 we fixed.
    for r in results:
        assert not isinstance(r, BaseException), f"upsert_settings raised: {r!r}"
    assert all(isinstance(r, TocSettings) for r in results)

    # Exactly one row exists for the (tenant, project) after the race.
    async with test_session_factory() as verify:
        count = await _count_rows(verify, tenant_id, project_id)
    assert count == 1
