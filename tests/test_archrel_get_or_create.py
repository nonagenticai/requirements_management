"""Tests for the idempotent get-or-create path on ``POST /architecture/relationships``.

AA-intake fans out architecture assets during expansion. A BLOCKED→DOING re-run would
otherwise duplicate them, so a relationship updates in place on its **edge tuple**
``(tenant_id, project_id, source_entity_type, source_id, target_entity_type, target_id,
relationship_type)`` — a relationship has no meaningful title — gated on ``project_id`` so
non-project callers keep pure-create semantics.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from models.architecture import ArchitectureRelationship
from tests.tenants import T


def _relationship_payload(
    *,
    source_id: str,
    target_id: str,
    project_id: str | None = "gc-project",
    relationship_type: str = "traceability",
) -> dict:
    # requirements↔requirements skips ArchiMate metamodel validation (that only
    # runs when BOTH endpoints are architecture_elements), so the ids need not
    # reference real rows.
    payload: dict = {
        "source_entity_type": "requirements",
        "source_id": source_id,
        "target_entity_type": "requirements",
        "target_id": target_id,
        "relationship_type": relationship_type,
        "name": "traces to",
    }
    if project_id is not None:
        payload["project_id"] = project_id
    return payload


@pytest.mark.asyncio
class TestArchitectureRelationshipGetOrCreate:
    async def test_repost_same_edge_updates_same_row(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        src, tgt = str(uuid4()), str(uuid4())
        first = await authenticated_client.post(
            "/architecture/relationships",
            json=_relationship_payload(source_id=src, target_id=tgt),
        )
        assert first.status_code == 200, first.text
        first_id = first.json()["id"]

        # Re-POST the SAME edge tuple with a changed label → update in place.
        revised = _relationship_payload(source_id=src, target_id=tgt)
        revised["name"] = "traces to (revised)"
        second = await authenticated_client.post("/architecture/relationships", json=revised)
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first_id

        async with test_session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ArchitectureRelationship).where(
                            ArchitectureRelationship.tenant_id == T("user-123"),
                            ArchitectureRelationship.project_id == "gc-project",
                            ArchitectureRelationship.source_id == src,
                            ArchitectureRelationship.target_id == tgt,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(row) == 1
        assert row[0].name == "traces to (revised)"

    async def test_different_edge_creates_distinct_row(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        src = str(uuid4())
        tgt_a, tgt_b = str(uuid4()), str(uuid4())
        a = await authenticated_client.post(
            "/architecture/relationships",
            json=_relationship_payload(source_id=src, target_id=tgt_a),
        )
        b = await authenticated_client.post(
            "/architecture/relationships",
            json=_relationship_payload(source_id=src, target_id=tgt_b),
        )
        assert a.status_code == 200, a.text
        assert b.status_code == 200, b.text
        assert a.json()["id"] != b.json()["id"]

        async with test_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(ArchitectureRelationship)
                    .where(
                        ArchitectureRelationship.tenant_id == T("user-123"),
                        ArchitectureRelationship.project_id == "gc-project",
                        ArchitectureRelationship.source_id == src,
                    )
                )
            ).scalar()
        assert count == 2

    async def test_no_project_id_always_creates(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """The project_id gate preserves pure-create semantics: two POSTs of the
        same edge with NO project_id must produce TWO rows."""
        src, tgt = str(uuid4()), str(uuid4())
        payload = _relationship_payload(source_id=src, target_id=tgt, project_id=None)
        first = await authenticated_client.post("/architecture/relationships", json=payload)
        second = await authenticated_client.post("/architecture/relationships", json=payload)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] != second.json()["id"]

        async with test_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(ArchitectureRelationship)
                    .where(
                        ArchitectureRelationship.tenant_id == T("user-123"),
                        ArchitectureRelationship.source_id == src,
                        ArchitectureRelationship.target_id == tgt,
                    )
                )
            ).scalar()
        assert count == 2
