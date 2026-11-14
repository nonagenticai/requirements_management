"""R345: one ``pm_projects`` row per fr project, enforced by the database.

DB level (the model mirror of migration ``2026_11_03_0078``, at the ``INSERT`` boundary):
``uq_pm_projects_tenant_origin_project`` is lifetime unique on ``(tenant_id,
origin_project_id)`` for ``origin_kind='project'`` — another title, a closed row and another
visibility all still collide; ``ck_pm_projects_origin_project`` ties ``origin_project_id`` to
``origin_kind='project'``; ``ck_pm_projects_origin_kind`` closes the domain.

API level: ``POST /pm/projects`` is a get-or-create on that key; a survivor the caller cannot
reach (another visibility) is 409 ``origin_project_exists`` (without its id), never a 500.

Distinct slugs keep ``uq_pm_projects_tenant_slug`` from firing first and masking the index.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy.exc
from httpx import AsyncClient

from models.pm import Project
from tests.briefs import MIN_BRIEF
from tests.project_types import typed
from tests.tenants import T


def _row(
    *,
    slug: str,
    origin_kind: str = "project",
    origin_project_id=None,
    tenant: str = "user-123",
    **kw,
) -> Project:
    return Project(
        slug=slug,
        title=kw.pop("title", "Customer onboarding platform"),
        tenant_id=T(tenant),
        origin_kind=origin_kind,
        origin_project_id=origin_project_id,
        **kw,
    )


async def _add_ok(factory, row: Project) -> None:
    async with factory() as session:
        session.add(row)
        await session.commit()


async def _add_rejected(factory, row: Project, constraint: str) -> None:
    async with factory() as session:
        session.add(row)
        with pytest.raises(sqlalchemy.exc.IntegrityError, match=constraint):
            await session.commit()


@pytest.mark.parametrize(
    "second",
    [
        {"title": "Another title"},
        {"title": "Closed one", "status": "closed"},
        {"title": "Private one", "visibility": "user", "subject_id": "alice"},
    ],
    ids=["other-title", "closed", "user-visibility"],
)
async def test_second_row_for_the_same_fr_project_is_rejected(test_session_factory, second):
    origin = uuid4()
    await _add_ok(test_session_factory, _row(slug="first", origin_project_id=origin))
    await _add_rejected(
        test_session_factory,
        _row(slug="second", origin_project_id=origin, **second),
        "uq_pm_projects_tenant_origin_project",
    )


async def test_same_fr_project_id_in_another_tenant_is_allowed(test_session_factory):
    origin = uuid4()
    await _add_ok(test_session_factory, _row(slug="t1", origin_project_id=origin))
    await _add_ok(test_session_factory, _row(slug="t2", origin_project_id=origin, tenant="other"))


async def test_project_row_without_origin_project_id_is_rejected(test_session_factory):
    await _add_rejected(
        test_session_factory,
        _row(slug="no-origin", origin_project_id=None),
        "ck_pm_projects_origin_project",
    )


@pytest.mark.parametrize("kind", ["tenant", "workflow"])
async def test_non_project_row_with_origin_project_id_is_rejected(test_session_factory, kind):
    await _add_rejected(
        test_session_factory,
        _row(slug=f"{kind}-with-origin", origin_kind=kind, origin_project_id=uuid4()),
        "ck_pm_projects_origin_project",
    )


async def test_unknown_origin_kind_is_rejected(test_session_factory):
    await _add_rejected(
        test_session_factory,
        _row(slug="bogus-kind", origin_kind="bogus"),
        "ck_pm_projects_origin_kind",
    )


async def test_tenant_rows_without_origin_are_unconstrained(test_session_factory):
    await _add_ok(test_session_factory, _row(slug="plain-a", origin_kind="tenant"))
    await _add_ok(test_session_factory, _row(slug="plain-b", origin_kind="tenant"))


# ---------------------------------------------------------------------------
# POST /pm/projects
# ---------------------------------------------------------------------------


def _body(origin: str, **kw) -> dict:
    return {
        "title": "Minted",
        "origin_kind": "project",
        "origin_project_id": origin,
        "brief": MIN_BRIEF,
        **kw,
    }


async def test_repost_for_the_same_fr_project_is_idempotent(authenticated_client: AsyncClient):
    c = authenticated_client
    origin = str(uuid4())
    first = await c.post("/pm/projects", json=await typed(c, _body(origin)))
    assert first.status_code == 200, first.text
    again = await c.post("/pm/projects", json=await typed(c, _body(origin, title="Renamed")))
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    listed = await c.get("/pm/projects", params={"origin_project_id": origin})
    assert [p["id"] for p in listed.json()] == [first.json()["id"]]


async def test_closed_project_still_owns_its_fr_project(authenticated_client: AsyncClient):
    c = authenticated_client
    origin = str(uuid4())
    first = await c.post("/pm/projects", json=await typed(c, _body(origin)))
    assert first.status_code == 200, first.text
    eid = first.json()["id"]
    closed = await c.put(f"/pm/projects/{eid}", json={"title": "Minted", "status": "closed"})
    assert closed.status_code == 200, closed.text
    again = await c.post("/pm/projects", json=await typed(c, _body(origin, title="New go")))
    assert again.status_code == 200, again.text
    assert again.json()["id"] == eid
    listed = await c.get("/pm/projects", params={"origin_project_id": origin})
    assert [p["id"] for p in listed.json()] == [eid]


@pytest.mark.parametrize("with_brief", [True, False])
async def test_unreachable_survivor_is_409_without_its_id(
    authenticated_client: AsyncClient, with_brief: bool
):
    """The fr project's row is tenant-visible; a ``user``-visibility POST cannot update it
    in place (its survivor lookup is subject-scoped), so it is a 409 — not a 500 and not a
    second row. The survivor's id is not disclosed: the caller cannot read that row."""
    c = authenticated_client
    origin = str(uuid4())
    first = await c.post("/pm/projects", json=await typed(c, _body(origin)))
    assert first.status_code == 200, first.text
    body = _body(origin, title="Private copy")
    if not with_brief:
        del body["brief"]
    resp = await c.post("/pm/projects", json=await typed(c, body), headers={"X-Visibility": "user"})
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "origin_project_exists"
    assert first.json()["id"] not in resp.text
    listed = await c.get("/pm/projects", params={"origin_project_id": origin})
    assert [p["id"] for p in listed.json()] == [first.json()["id"]]


async def test_workflow_origin_with_project_id_is_422(authenticated_client: AsyncClient):
    c = authenticated_client
    body = _body(str(uuid4()), origin_kind="workflow", origin_workflow_run_id="n8n-run-1")
    resp = await c.post("/pm/projects", json=await typed(c, body))
    assert resp.status_code == 422, resp.text
    assert "origin_project_id_requires_origin_kind_project" in resp.text
