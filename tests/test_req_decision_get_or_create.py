"""Tests for the idempotent get-or-create path on ``POST /requirements`` and
``POST /decisions``.

AA-intake authors requirements + decisions during the
``project_start_request_analysis`` DOING stage. A BLOCKED→DOING re-run would
otherwise duplicate them because ``EntityService._generate_unique_slug``
auto-increments the slug (title, title-1, …). Mirroring the architecture-element
get-or-create (PR #63), each create endpoint is now get-or-create keyed on
``(tenant_id, project_id, title)`` — BUT ONLY when ``project_id`` is set. A
falsy ``project_id`` keeps pure create semantics for non-project callers.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from models.decisions import Decision
from models.requirements import Requirement
from tests.tenants import T


def _requirement_payload(
    *,
    title: str = "Users can reset their password",
    project_id: str | None = "gc-project",
) -> dict:
    payload: dict = {
        "title": title,
        "description": "As a user I can reset my password via email.",
        "effort": 3.0,
    }
    if project_id is not None:
        payload["project_id"] = project_id
    return payload


def _decision_payload(
    *,
    title: str = "Adopt Postgres as the primary datastore",
    project_id: str | None = "gc-project",
) -> dict:
    payload: dict = {
        "title": title,
        "context": "We need a relational store with strong consistency.",
        "decision": "Use Postgres 18 with psycopg 3.",
    }
    if project_id is not None:
        payload["project_id"] = project_id
    return payload


@pytest.mark.asyncio
class TestRequirementGetOrCreate:
    async def test_repost_same_key_updates_same_row(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        first = await authenticated_client.post("/requirements", json=_requirement_payload())
        assert first.status_code == 200, first.text
        first_id = first.json()["id"]

        # Re-POST identical (tenant, project_id, title) with a changed body →
        # update in place, same id, no duplicate.
        revised = _requirement_payload()
        revised["description"] = "As a user I can reset my password (revised)."
        second = await authenticated_client.post("/requirements", json=revised)
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first_id
        assert second.json()["description"] == "As a user I can reset my password (revised)."

        async with test_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Requirement)
                    .where(
                        Requirement.tenant_id == T("user-123"),
                        Requirement.project_id == "gc-project",
                        Requirement.title == "Users can reset their password",
                    )
                )
            ).scalar()
        assert count == 1

    async def test_repost_changed_content_reevaluates_and_snapshots(
        self, authenticated_client: AsyncClient, test_session_factory, monkeypatch
    ):
        """Bug #2: the sequential BLOCKED→DOING re-POST changes content, so the
        in-place update MUST re-evaluate quality (fresh ``last_evaluated_at``) AND
        record a new version snapshot — unlike the concurrent race-loss path,
        which correctly skips both (the winner already evaluated + snapshotted)."""
        from services import eval_registrations

        def _stub_heuristic(entity):
            return 0.5, [], []

        # Auto-eval heuristics are registered in app lifespan, which the ASGI
        # test transport does not run — register one directly so create/update
        # actually stamp last_evaluated_at.
        monkeypatch.setitem(eval_registrations.HEURISTICS, "requirement", _stub_heuristic)

        first = await authenticated_client.post("/requirements", json=_requirement_payload())
        assert first.status_code == 200, first.text
        rid = first.json()["id"]
        first_eval_at = first.json()["last_evaluated_at"]
        assert first_eval_at is not None, "create must run the heuristic once"

        versions_before = await authenticated_client.get(f"/requirements/{rid}/versions")
        assert versions_before.status_code == 200, versions_before.text
        assert len(versions_before.json()) == 1  # only the "created" snapshot

        # Re-POST the SAME (tenant, project_id, title) with genuinely changed
        # content (description + effort) → dedup onto the same row, but re-score
        # and snapshot the change.
        revised = _requirement_payload()
        revised["description"] = "Reset via email AND an SMS one-time code (revised)."
        revised["effort"] = 8.0
        second = await authenticated_client.post("/requirements", json=revised)
        assert second.status_code == 200, second.text
        assert second.json()["id"] == rid  # deduped onto the same row
        second_eval_at = second.json()["last_evaluated_at"]

        # (b) quality was RE-evaluated on the changed re-POST.
        assert second_eval_at is not None
        assert second_eval_at != first_eval_at, "changed re-POST must re-evaluate quality"

        # (a) the change landed a new version snapshot.
        versions_after = await authenticated_client.get(f"/requirements/{rid}/versions")
        assert versions_after.status_code == 200, versions_after.text
        assert len(versions_after.json()) >= 2, "changed re-POST must record a snapshot"

    async def test_repost_identical_content_records_no_new_snapshot(
        self, authenticated_client: AsyncClient, test_session_factory, monkeypatch
    ):
        """Bug #2 guard: an IDENTICAL-content re-POST must NOT add a snapshot —
        preserves the concurrent race-loss behavior (the winning racer already
        wrote the "created" snapshot; a redundant "updated" one is churn)."""
        first = await authenticated_client.post("/requirements", json=_requirement_payload())
        assert first.status_code == 200, first.text
        rid = first.json()["id"]

        second = await authenticated_client.post("/requirements", json=_requirement_payload())
        assert second.status_code == 200, second.text
        assert second.json()["id"] == rid

        versions = await authenticated_client.get(f"/requirements/{rid}/versions")
        assert versions.status_code == 200, versions.text
        assert len(versions.json()) == 1  # still only the "created" snapshot

    async def test_different_title_creates_distinct_row(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        a = await authenticated_client.post(
            "/requirements", json=_requirement_payload(title="Alpha requirement")
        )
        b = await authenticated_client.post(
            "/requirements", json=_requirement_payload(title="Beta requirement")
        )
        assert a.status_code == 200, a.text
        assert b.status_code == 200, b.text
        assert a.json()["id"] != b.json()["id"]

        async with test_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Requirement)
                    .where(
                        Requirement.tenant_id == T("user-123"),
                        Requirement.project_id == "gc-project",
                        Requirement.title.in_(["Alpha requirement", "Beta requirement"]),
                    )
                )
            ).scalar()
        assert count == 2

    async def test_no_project_id_always_creates(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """The project_id gate preserves pure-create semantics: two POSTs with
        the same title but NO project_id must produce TWO rows."""
        payload = _requirement_payload(project_id=None)
        first = await authenticated_client.post("/requirements", json=payload)
        second = await authenticated_client.post("/requirements", json=payload)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] != second.json()["id"]

        async with test_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Requirement)
                    .where(
                        Requirement.tenant_id == T("user-123"),
                        Requirement.project_id.is_(None),
                        Requirement.title == "Users can reset their password",
                    )
                )
            ).scalar()
        assert count == 2


@pytest.mark.asyncio
class TestDecisionGetOrCreate:
    async def test_repost_same_key_updates_same_row(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        first = await authenticated_client.post("/decisions", json=_decision_payload())
        assert first.status_code == 200, first.text
        first_id = first.json()["id"]

        revised = _decision_payload()
        revised["context"] = "We need a relational store (revised context)."
        second = await authenticated_client.post("/decisions", json=revised)
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first_id
        assert second.json()["context"] == "We need a relational store (revised context)."

        async with test_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Decision)
                    .where(
                        Decision.tenant_id == T("user-123"),
                        Decision.project_id == "gc-project",
                        Decision.title == "Adopt Postgres as the primary datastore",
                    )
                )
            ).scalar()
        assert count == 1

    async def test_different_title_creates_distinct_row(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        a = await authenticated_client.post(
            "/decisions", json=_decision_payload(title="Adopt Postgres")
        )
        b = await authenticated_client.post(
            "/decisions", json=_decision_payload(title="Adopt Redis for caching")
        )
        assert a.status_code == 200, a.text
        assert b.status_code == 200, b.text
        assert a.json()["id"] != b.json()["id"]

        async with test_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Decision)
                    .where(
                        Decision.tenant_id == T("user-123"),
                        Decision.project_id == "gc-project",
                        Decision.title.in_(["Adopt Postgres", "Adopt Redis for caching"]),
                    )
                )
            ).scalar()
        assert count == 2

    async def test_no_project_id_always_creates(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """The project_id gate preserves pure-create semantics: two POSTs with
        the same title but NO project_id must produce TWO rows."""
        payload = _decision_payload(project_id=None)
        first = await authenticated_client.post("/decisions", json=payload)
        second = await authenticated_client.post("/decisions", json=payload)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] != second.json()["id"]

        async with test_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Decision)
                    .where(
                        Decision.tenant_id == T("user-123"),
                        Decision.project_id.is_(None),
                        Decision.title == "Adopt Postgres as the primary datastore",
                    )
                )
            ).scalar()
        assert count == 2
