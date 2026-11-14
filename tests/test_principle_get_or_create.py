"""Tests for the idempotent get-or-create path on ``POST /principles``.

AA-intake authors principles during the ``project_start_request_analysis`` DOING
stage. A BLOCKED->DOING re-run would otherwise duplicate them:
``EntityService._generate_unique_slug`` auto-increments the slug (title,
title-1, ...), so the ``(tenant_id, slug)`` unique constraint never fires and a
fresh row lands on every re-run.

Mirroring the requirement/decision get-or-create (PR #64), the create endpoint is
now get-or-create -- but ONLY for **project-scoped** creates. The siblings gate on
a truthy ``project_id``; ``Principle`` has no ``project_id`` column (it is
tenant-wide by design), so the equivalent gate here is ``origin_project_id``,
which the AA-intake ``Create Principle`` node always sets.

An **un-scoped** create (no ``origin_project_id`` -- the general API) keeps its
pre-existing behaviour and still mints a distinct slug, so hand-authored
principles that legitimately share a title are unaffected. That is pinned by
``test_unscoped_create_still_duplicates`` below (and by the pre-existing
``091f684:tests/test_coverage_86.py`` slug-collision tests).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from models.principles import Principle
from tests.tenants import T

PROJECT_A = str(uuid4())
# Titles are made unique per test-run: the suite shares one database, and a
# principle title colliding with another test file's row would make the
# get-or-create match a foreign row (and the row-count assertions flaky).
_DEFAULT_TITLE = f"Prefer managed services over self-hosting {uuid4().hex[:8]}"


def _uniq(stem: str) -> str:
    return f"{stem} {uuid4().hex[:8]}"


def _principle_payload(
    *,
    title: str = _DEFAULT_TITLE,
    origin_project_id: str | None = PROJECT_A,
) -> dict:
    body: dict = {
        "title": title,
        "statement": "We use managed services unless a hard requirement forbids it.",
        "rationale": "Reduces operational burden on a small platform team.",
        "implications": ["Vendor evaluation is part of every design review."],
        "tags": ["platform"],
    }
    if origin_project_id is not None:
        # snake_case ON THE WIRE: the In-schemas declare no camelCase alias, so
        # `originProjectId` would be silently DROPPED. The n8n nodes already map
        # their camelCase params to `body.origin_project_id`, which is what
        # AA-intake actually sends. `origin_kind` must be "project" too --
        # OriginFieldsInMixin.check_origin_consistency rejects an
        # origin_project_id under the default origin_kind="tenant".
        body["origin_project_id"] = origin_project_id
        body["origin_kind"] = "project"
    return body


@pytest.mark.asyncio
class TestPrincipleGetOrCreate:
    async def test_repost_same_key_updates_same_row(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """A project-scoped re-POST of the same title updates in place."""
        first = await authenticated_client.post("/principles", json=_principle_payload())
        assert first.status_code == 200, first.text
        first_id = first.json()["id"]

        revised = _principle_payload()
        revised["statement"] = "We use managed services unless cost forbids it (revised)."
        second = await authenticated_client.post("/principles", json=revised)
        assert second.status_code == 200, second.text

        # Same row, updated in place -- no duplicate.
        assert second.json()["id"] == first_id
        assert (
            second.json()["statement"]
            == "We use managed services unless cost forbids it (revised)."
        )

        async with test_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Principle)
                    .where(
                        Principle.tenant_id == T("user-123"),
                        Principle.title == _DEFAULT_TITLE,
                    )
                )
            ).scalar()
        assert count == 1

    async def test_different_title_creates_distinct_row(self, authenticated_client: AsyncClient):
        """Same project, different title -> genuinely distinct principles."""
        a = await authenticated_client.post(
            "/principles", json=_principle_payload(title=_uniq("Alpha principle"))
        )
        b = await authenticated_client.post(
            "/principles", json=_principle_payload(title=_uniq("Beta principle"))
        )
        assert a.status_code == 200, a.text
        assert b.status_code == 200, b.text
        assert a.json()["id"] != b.json()["id"]

    async def test_unscoped_create_still_duplicates(self, authenticated_client: AsyncClient):
        """No origin_project_id (the general API) -> unchanged legacy behaviour.

        The dedup is deliberately gated on origin_project_id so hand-authored
        principles that legitimately share a title still each get their own row
        (with an auto-incremented slug). Guards the semantics that
        ``091f684:tests/test_coverage_86.py``'s slug-collision tests encode.
        """
        title = _uniq("Unscoped shared title")
        first = await authenticated_client.post(
            "/principles", json=_principle_payload(title=title, origin_project_id=None)
        )
        second = await authenticated_client.post(
            "/principles", json=_principle_payload(title=title, origin_project_id=None)
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

        # Distinct rows, distinct slugs -- NOT deduped.
        assert first.json()["id"] != second.json()["id"]
        assert first.json()["slug"] != second.json()["slug"]

    async def test_different_project_same_title_creates_distinct_row(
        self, authenticated_client: AsyncClient
    ):
        """The key includes origin_project_id -- two projects may share a title."""
        title = _uniq("Cross-project shared title")
        a = await authenticated_client.post(
            "/principles", json=_principle_payload(title=title, origin_project_id=str(uuid4()))
        )
        b = await authenticated_client.post(
            "/principles", json=_principle_payload(title=title, origin_project_id=str(uuid4()))
        )
        assert a.status_code == 200, a.text
        assert b.status_code == 200, b.text
        assert a.json()["id"] != b.json()["id"]
