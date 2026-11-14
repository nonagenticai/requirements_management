"""DB-enforced dedup for the synthetic-Artifact code_binding upsert.

Migration ``2026_07_18_0036`` adds the synthetic's identity ``(container,
component_id)`` to ``architecture_elements`` (``synthetic_binding_container`` /
``synthetic_binding_component_id``) plus two partial unique indexes
(``uq_arch_elements_synthetic_binding_{tenant,user}``). These make
"one synthetic per (tenant, project, code-unit)" a DB-enforced invariant, so the
concurrent double-mint race PR #73 had to tolerate (two synthetic elements sharing
a ``coding_component`` binding) can no longer exist. The create route routes the
synthetic branch through ``upsert_synthetic_by_code_binding``, which optimistically
inserts and, on the unique violation, rolls back and recovers the survivor by its
committed identity columns.

Test/alembic split: the pytest suite builds schema via ``Base.metadata.create_all``
(``tests/conftest.py``), NOT alembic — so these tests exercise the indexes via the
ORM model ``__table_args__`` dual-declaration; the alembic migration is the
production/CI path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid7

import pytest
import sqlalchemy.exc
from httpx import AsyncClient
from sqlalchemy import func, select

from models.architecture import ArchitectureElement, ArchitectureElementRealisationBinding
from tests.tenants import T

_TENANT = T("user-123")
_CONTAINER = "fr"
_COMPONENT_ID = "fr.frontend.duplicate-binding"
_PROJECT_ID = "synth-race-project"


def _synthetic_element(
    *,
    title: str,
    created_at: datetime,
    container: str | None = _CONTAINER,
    component_id: str | None = _COMPONENT_ID,
    project_id: str | None = _PROJECT_ID,
    visibility: str = "tenant",
    subject_id: str = _TENANT,
) -> ArchitectureElement:
    """Build a synthetic Artifact row (mirrors svc.create defaults), with the
    identity columns populated the way ``upsert_synthetic_by_code_binding`` sets
    them via ``extra_columns``."""
    return ArchitectureElement(
        id=uuid7(),
        slug=f"{title.lower().replace(' ', '-')}-{uuid7().hex[:8]}",
        title=title,
        description="seeded synthetic",
        tenant_id=_TENANT,
        tenant_name=_TENANT,
        subject_id=subject_id,
        visibility=visibility,
        project_id=project_id,
        layer="technology",
        element_type="artifact",
        properties={},
        status="draft",
        is_synthetic=True,
        synthetic_binding_container=container,
        synthetic_binding_component_id=component_id,
        created_at=created_at,
    )


async def _count_synthetics_bound(session, *, container=_CONTAINER, component_id=_COMPONENT_ID):
    """Count synthetic elements bound to (container, component_id) via the
    canonical binding table (the observable end state)."""
    return (
        await session.execute(
            select(func.count())
            .select_from(ArchitectureElement)
            .join(
                ArchitectureElementRealisationBinding,
                ArchitectureElementRealisationBinding.element_id == ArchitectureElement.id,
            )
            .where(
                ArchitectureElement.tenant_id == _TENANT,
                ArchitectureElement.is_synthetic.is_(True),
                ArchitectureElementRealisationBinding.container == container,
                ArchitectureElementRealisationBinding.component_id == component_id,
            )
        )
    ).scalar()


async def test_db_rejects_duplicate_tenant_synthetic(test_session_factory):
    """The tenant partial unique index rejects a SECOND synthetic on the same
    (tenant, project, code-unit) — the invariant PR #73 could only tolerate."""
    base = datetime.now(timezone.utc)
    async with test_session_factory() as session:
        session.add(_synthetic_element(title="Dup A", created_at=base))
        await session.commit()

        session.add(_synthetic_element(title="Dup B", created_at=base + timedelta(seconds=5)))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.flush()
        await session.rollback()


async def test_null_project_synthetics_dedup(test_session_factory):
    """COALESCE(project_id,'') collapses NULLs — two tenant-wide (null-project)
    synthetics on the same code-unit collide (differs from the WS1 title index,
    which leaves null-project rows unconstrained)."""
    base = datetime.now(timezone.utc)
    async with test_session_factory() as session:
        session.add(_synthetic_element(title="Null Proj A", created_at=base, project_id=None))
        await session.commit()

        session.add(
            _synthetic_element(
                title="Null Proj B", created_at=base + timedelta(seconds=5), project_id=None
            )
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.flush()
        await session.rollback()


async def test_user_visibility_two_subjects_coexist(test_session_factory):
    """The user-visibility index keys on subject_id, so two distinct subjects may
    each own a private synthetic on the same code-unit."""
    base = datetime.now(timezone.utc)
    async with test_session_factory() as session:
        session.add(
            _synthetic_element(
                title="User A", created_at=base, visibility="user", subject_id="subject-A"
            )
        )
        session.add(
            _synthetic_element(
                title="User B",
                created_at=base + timedelta(seconds=1),
                visibility="user",
                subject_id="subject-B",
            )
        )
        # No violation: different subject_id → different index key.
        await session.commit()

        count = (
            await session.execute(
                select(func.count())
                .select_from(ArchitectureElement)
                .where(
                    ArchitectureElement.tenant_id == _TENANT,
                    ArchitectureElement.is_synthetic.is_(True),
                    ArchitectureElement.visibility == "user",
                    ArchitectureElement.synthetic_binding_container == _CONTAINER,
                    ArchitectureElement.synthetic_binding_component_id == _COMPONENT_ID,
                )
            )
        ).scalar()
        assert count == 2


async def test_route_sequential_reaccept_is_idempotent(authenticated_client: AsyncClient):
    """Sequential re-accept: first POST creates (201), second finds the survivor
    via the binding-table fast path and updates in place (200) — exactly one
    synthetic bound to the code-unit."""
    payload = {
        "title": "Synthetic Artifact",
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "project_id": _PROJECT_ID,
        "properties": {"code_binding": {"container": _CONTAINER, "component_id": _COMPONENT_ID}},
    }

    first = await authenticated_client.post("/architecture/elements", json=payload)
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    second = await authenticated_client.post("/architecture/elements", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first_id


async def test_route_recovers_survivor_on_index_collision(
    authenticated_client: AsyncClient, test_session_factory
):
    """Exercises the IntegrityError recovery branch deterministically (no
    concurrency): seed a winner with identity columns set but NO binding row, so
    the route's binding-table fast path MISSES, svc.create collides on the
    identity index, and recovery resolves the winner by its identity columns."""
    base = datetime.now(timezone.utc)
    async with test_session_factory() as session:
        winner = _synthetic_element(title="Race Winner", created_at=base)
        session.add(winner)  # identity cols set, no binding row attached
        await session.commit()
        winner_id = str(winner.id)

    payload = {
        "title": "Race Loser (re-accepted)",
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "project_id": _PROJECT_ID,
        "properties": {"code_binding": {"container": _CONTAINER, "component_id": _COMPONENT_ID}},
    }
    resp = await authenticated_client.post("/architecture/elements", json=payload)

    assert resp.status_code == 200, resp.text  # recovered → update, not 201/500
    assert resp.json()["id"] == winner_id

    # No duplicate minted: the winner is now the single synthetic bound to the
    # code-unit (the route wrote its binding onto the survivor).
    async with test_session_factory() as session:
        assert await _count_synthetics_bound(session) == 1


async def test_route_concurrent_double_post_yields_one(authenticated_client: AsyncClient):
    """True concurrent double-mint: two overlapping POSTs resolve to a single
    synthetic (one 201 + one 200), never a 500 and never a duplicate."""
    payload = {
        "title": "Concurrent Artifact",
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "project_id": "synth-concurrent-project",
        "properties": {
            "code_binding": {"container": _CONTAINER, "component_id": "fr.frontend.concurrent"}
        },
    }
    raw = await asyncio.gather(
        authenticated_client.post("/architecture/elements", json=payload),
        authenticated_client.post("/architecture/elements", json=payload),
        return_exceptions=True,
    )
    # Both requests must have completed without raising (no 500-as-exception).
    for r in raw:
        if isinstance(r, BaseException):
            raise AssertionError(f"concurrent POST raised: {r!r}")
    responses = [r for r in raw if not isinstance(r, BaseException)]
    codes = sorted(r.status_code for r in responses)
    assert 500 not in codes, codes  # never a MultipleResultsFound 500
    assert codes.count(201) <= 1, codes  # at most one fresh insert
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1, f"expected a single resolved synthetic, got {ids}"


# ─────────────────────────────────────────────────────────────────────────────
# The upsert's UPDATE branches must refuse a parent cycle like PUT does
# ─────────────────────────────────────────────────────────────────────────────


def _cycle_payload(component_id: str, parent_id: str | None = None) -> dict:
    payload = {
        "title": "Synthetic cycle root",
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "project_id": _PROJECT_ID,
        "properties": {"code_binding": {"container": _CONTAINER, "component_id": component_id}},
    }
    if parent_id is not None:
        payload["parent_id"] = parent_id
    return payload


async def _child_of(client: AsyncClient, parent_id: str) -> dict:
    resp = await client.post(
        "/architecture/elements",
        json={
            "title": "Synthetic cycle child",
            "layer": "technology",
            "element_type": "artifact",
            "project_id": _PROJECT_ID,
            "parent_id": parent_id,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _assert_cycle_refused(client: AsyncClient, resp, root_id: str, child: dict) -> None:
    # Same refusal as the regular update path (PUT under its own descendant).
    put = await client.put(
        f"/architecture/elements/{root_id}",
        json={**_cycle_payload("unused"), "is_synthetic": False, "parent_id": child["id"]},
    )
    assert put.status_code == 422, put.text
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == put.json()["error"]["code"], (resp.text, put.text)
    message = resp.json()["error"]["message"]
    assert root_id in message and child["id"] in message, message
    reread = await client.get(f"/architecture/elements/{root_id}")
    assert reread.json()["parent_id"] is None, reread.text  # nothing persisted


async def test_route_reaccept_under_own_descendant_is_refused(authenticated_client: AsyncClient):
    """Fast path: re-sending a synthetic whose parent is its own descendant is a cycle."""
    payload = _cycle_payload("fr.frontend.cycle-fast")
    root = await authenticated_client.post("/architecture/elements", json=payload)
    assert root.status_code == 201, root.text
    child = await _child_of(authenticated_client, root.json()["id"])
    resp = await authenticated_client.post(
        "/architecture/elements", json={**payload, "parent_id": child["id"]}
    )
    await _assert_cycle_refused(authenticated_client, resp, root.json()["id"], child)


async def test_recovered_survivor_under_own_descendant_is_refused(
    authenticated_client: AsyncClient, test_session_factory
):
    """IntegrityError recovery branch: the survivor is updated in place, so the same
    cycle check must run with ITS id."""
    component = "fr.frontend.cycle-recovery"
    async with test_session_factory() as session:
        winner = _synthetic_element(
            title="Cycle Winner", created_at=datetime.now(timezone.utc), component_id=component
        )
        session.add(winner)  # identity cols set, no binding row → fast path misses
        await session.commit()
        winner_id = str(winner.id)
    child = await _child_of(authenticated_client, winner_id)
    resp = await authenticated_client.post(
        "/architecture/elements", json=_cycle_payload(component, parent_id=child["id"])
    )
    await _assert_cycle_refused(authenticated_client, resp, winner_id, child)
