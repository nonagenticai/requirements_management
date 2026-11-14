"""WS4: dedup requirements / decisions / resources on the deterministic AA
``stable_key`` (cardId + kind + ordinal), not the drift-prone LLM title.

The partitioned two-index design
(title index narrowed to ``stable_key IS NULL``, stable_key index on
``IS NOT NULL``) must keep the two disjoint so a stable-keyed create and a
title-fallback create with the SAME title never cross-collide → no HTTP 500.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from models.decisions import Decision
from models.pm import Resource
from models.requirements import Requirement
from tests.tenants import T


def _requirement_payload(*, title="Users can reset their password", stable_key=None):
    p: dict = {
        "title": title,
        "description": "As a user I can reset my password via email.",
        "effort": 3.0,
        "project_id": "gc-project",
    }
    if stable_key is not None:
        p["stable_key"] = stable_key
    return p


def _decision_payload(*, title="Adopt Postgres as the primary datastore", stable_key=None):
    p: dict = {
        "title": title,
        "context": "We need a relational store with strong consistency.",
        "decision": "Use Postgres 18 with psycopg 3.",
        "project_id": "gc-project",
    }
    if stable_key is not None:
        p["stable_key"] = stable_key
    return p


def _resource_payload(*, title="Senior Developer", resource_type="person", stable_key=None):
    p: dict = {"title": title, "resource_type": resource_type}
    if stable_key is not None:
        p["stable_key"] = stable_key
    return p


async def _count(session_factory, model, **filters) -> int:
    async with session_factory() as session:
        conds = [getattr(model, k) == v for k, v in filters.items()]
        return (
            await session.execute(select(func.count()).select_from(model).where(*conds))
        ).scalar()


@pytest.mark.asyncio
class TestRequirementStableKeyDedup:
    async def test_same_key_drifted_title_dedups_and_updates(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """Core AA re-run: same stable_key, DRIFTED title → one row, updated."""
        first = await authenticated_client.post(
            "/requirements",
            json=_requirement_payload(title="Reset password", stable_key="card-7:requirement:0"),
        )
        assert first.status_code == 200, first.text
        fid = first.json()["id"]

        second = await authenticated_client.post(
            "/requirements",
            json=_requirement_payload(
                title="Users may reset credentials via email", stable_key="card-7:requirement:0"
            ),
        )
        assert second.status_code == 200, second.text
        assert second.json()["id"] == fid
        assert second.json()["title"] == "Users may reset credentials via email"

        assert (
            await _count(
                test_session_factory,
                Requirement,
                tenant_id=T("user-123"),
                stable_key="card-7:requirement:0",
            )
            == 1
        )

    async def test_same_title_different_key_creates_two_rows(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """Same title + DIFFERENT stable_key → two distinct rows (no false dedup)."""
        a = await authenticated_client.post(
            "/requirements",
            json=_requirement_payload(title="Shared", stable_key="card-7:requirement:0"),
        )
        b = await authenticated_client.post(
            "/requirements",
            json=_requirement_payload(title="Shared", stable_key="card-7:requirement:1"),
        )
        assert a.status_code == 200, a.text
        assert b.status_code == 200, b.text
        assert a.json()["id"] != b.json()["id"]

    async def test_keyed_and_unkeyed_same_title_no_500(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """The disjoint-index guarantee: a stable-keyed row and a title-fallback
        row with the SAME title coexist without a 500 (they land in different
        partial indexes)."""
        keyed = await authenticated_client.post(
            "/requirements",
            json=_requirement_payload(title="Same title", stable_key="card-9:requirement:0"),
        )
        unkeyed = await authenticated_client.post(
            "/requirements", json=_requirement_payload(title="Same title")
        )
        assert keyed.status_code == 200, keyed.text
        assert unkeyed.status_code == 200, unkeyed.text
        assert keyed.json()["id"] != unkeyed.json()["id"]

    async def test_title_recovery_updates_null_key_row_not_older_keyed_sibling(
        self, authenticated_client: AsyncClient, test_session_factory, monkeypatch
    ):
        """Bug #4: the title-branch recovery re-SELECT must be filtered to
        ``stable_key IS NULL`` (mirroring the partial title index predicate).

        Otherwise, when an OLDER same-(project, title) row carries a NON-null
        stable_key — a row the title index never covered — the ordered
        ``limit(1)`` recovery picks that older keyed sibling and wrongly absorbs
        the update, instead of the stable_key-NULL row that actually collided.
        """
        # Row A: seeded via the STABLE_KEY branch → carries a stable_key, created
        # FIRST (so it sorts oldest under order_by(created_at, id)).
        a = await authenticated_client.post(
            "/requirements",
            json=_requirement_payload(title="Foo", stable_key="card-42:requirement:0"),
        )
        assert a.status_code == 200, a.text
        a_id = a.json()["id"]

        # Row B: seeded via the TITLE branch (no stable_key), SAME (project, title).
        # Disjoint indexes let it coexist with A without a 500.
        b_payload = _requirement_payload(title="Foo")
        b_payload["description"] = "Original B description."
        b = await authenticated_client.post("/requirements", json=b_payload)
        assert b.status_code == 200, b.text
        b_id = b.json()["id"]
        assert b_id != a_id

        # Re-POST via the TITLE branch with CHANGED content → collides the partial
        # title index (B occupies it) → recovery. Must update B, never A.
        revised = _requirement_payload(title="Foo")
        revised["description"] = "Revised B description via title recovery."
        third = await authenticated_client.post("/requirements", json=revised)
        assert third.status_code == 200, third.text
        assert third.json()["id"] == b_id, "recovery must update the stable_key-NULL row (B)"

        async with test_session_factory() as session:
            row_a = await session.get(Requirement, UUID(a_id))
            row_b = await session.get(Requirement, UUID(b_id))

        # Row A (the keyed sibling) is UNTOUCHED — key intact, description intact.
        assert row_a is not None
        assert row_a.stable_key == "card-42:requirement:0"
        assert row_a.description == "As a user I can reset my password via email."
        # Row B absorbed the update.
        assert row_b is not None
        assert row_b.description == "Revised B description via title recovery."

    async def test_no_stable_key_falls_back_to_title_dedup(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """Without a stable_key the older (project, title) dedup still holds."""
        a = await authenticated_client.post(
            "/requirements", json=_requirement_payload(title="Legacy title dedup")
        )
        b = await authenticated_client.post(
            "/requirements", json=_requirement_payload(title="Legacy title dedup")
        )
        assert a.status_code == 200 and b.status_code == 200
        assert a.json()["id"] == b.json()["id"]


@pytest.mark.asyncio
class TestDecisionStableKeyDedup:
    async def test_same_key_drifted_title_dedups(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        first = await authenticated_client.post(
            "/decisions",
            json=_decision_payload(title="Use Postgres", stable_key="card-3:decision:0"),
        )
        assert first.status_code == 200, first.text
        fid = first.json()["id"]
        second = await authenticated_client.post(
            "/decisions",
            json=_decision_payload(
                title="Adopt Postgres 18 as the datastore", stable_key="card-3:decision:0"
            ),
        )
        assert second.status_code == 200, second.text
        assert second.json()["id"] == fid
        assert (
            await _count(
                test_session_factory,
                Decision,
                tenant_id=T("user-123"),
                stable_key="card-3:decision:0",
            )
            == 1
        )


@pytest.mark.asyncio
class TestResourceStableKeyDedup:
    async def test_same_key_drifted_title_dedups(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """Resources have no project_id and a legitimately non-unique title;
        stable_key (tenant-scoped) is the only safe dedup handle."""
        first = await authenticated_client.post(
            "/pm/resources",
            json=_resource_payload(title="Senior Developer", stable_key="card-2:resource:0"),
        )
        assert first.status_code in (200, 201), first.text
        fid = first.json()["id"]
        second = await authenticated_client.post(
            "/pm/resources",
            json=_resource_payload(title="Sr. Software Engineer", stable_key="card-2:resource:0"),
        )
        assert second.status_code in (200, 201), second.text
        assert second.json()["id"] == fid
        assert (
            await _count(
                test_session_factory,
                Resource,
                tenant_id=T("user-123"),
                stable_key="card-2:resource:0",
            )
            == 1
        )

    async def test_non_unique_title_without_key_creates_two(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """Without a stable_key, resources are pure-create — a legitimately
        repeated title (two 'Senior Developer's) must NOT collapse."""
        a = await authenticated_client.post(
            "/pm/resources", json=_resource_payload(title="Senior Developer")
        )
        b = await authenticated_client.post(
            "/pm/resources", json=_resource_payload(title="Senior Developer")
        )
        assert a.status_code in (200, 201) and b.status_code in (200, 201)
        assert a.json()["id"] != b.json()["id"]
