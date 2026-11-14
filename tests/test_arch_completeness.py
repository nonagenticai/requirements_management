"""Endpoint-level coverage for ``POST /architecture/adm-completeness``.

This endpoint had no test coverage prior to this file, which is exactly
why the ``Principle.project_id`` latent (``_count_entities`` calling a
non-existent column on the Principle model → ``AttributeError`` → 500)
lived undetected from commit 1870458 through P4.5. The cases below
exercise the Principle code path on TOGAF Phase A (``requires_principles
= True``) so any future regression — re-routing Principle through
``_count_entities``, dropping the principles_count from the response
contract, etc. — fails loud at CI time, not in production.
"""

from __future__ import annotations

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.principles import Principle
from tests.tenants import T

_TENANT = T("user-123")  # matches the tenant header in ``authenticated_client``
_TENANT_NAME = "User 123"
_SUBJECT = T("user-123")
_PROJECT = "adm-completeness-test-project"


def _principle(slug: str) -> Principle:
    return Principle(
        slug=slug,
        title=slug,
        statement="seed",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
    )


@pytest_asyncio.fixture
async def _two_tenant_principles(test_session: AsyncSession) -> None:
    test_session.add_all([_principle("prin-a"), _principle("prin-b")])
    await test_session.commit()


async def test_phase_a_no_principles_reports_missing(
    authenticated_client: AsyncClient,
) -> None:
    """Phase A requires principles; with none seeded, ``"principles"`` is missing."""
    resp = await authenticated_client.post(
        "/architecture/tools/adm-completeness",
        json={"phase": "A", "project_id": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "A"
    assert "principles" in body["missing"]
    assert "define architecture principles" in body["recommendations"]
    # No ``principles defined (N)`` string should be in passed when count is 0.
    assert not any("principles defined" in entry for entry in body["passed"])


async def test_phase_a_with_principles_reports_passed(
    authenticated_client: AsyncClient, _two_tenant_principles: None
) -> None:
    """Phase A with 2 tenant-scope Principles → ``"principles defined (2)"`` in passed.

    Counts MUST come from the tenant-only counter — Principle has no
    ``project_id`` column, so the historical ``_count_entities`` path
    would have raised ``AttributeError`` and produced a 500 here.
    """
    resp = await authenticated_client.post(
        "/architecture/tools/adm-completeness",
        json={"phase": "A", "project_id": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "principles defined (2)" in body["passed"]
    assert "principles" not in body["missing"]


async def test_phase_b_principles_path_unaffected(
    authenticated_client: AsyncClient,
) -> None:
    """Phase B does not require principles — endpoint must still 200.

    Smoke that the Principle code path runs unconditionally (the count is
    computed for every phase, just not asserted on for B), so a future
    refactor that gates the call behind ``requires_principles`` won't
    silently re-introduce the latent on Phase A/H.
    """
    resp = await authenticated_client.post(
        "/architecture/tools/adm-completeness",
        json={"phase": "B", "project_id": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "B"
    # B never lists principles in missing or passed (requires_principles=False).
    assert "principles" not in body["missing"]
    assert not any("principles defined" in entry for entry in body["passed"])
