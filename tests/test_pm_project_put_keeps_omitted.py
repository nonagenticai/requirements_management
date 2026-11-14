"""``PUT /pm/projects/{eid}``: every omitted field keeps its stored value.

Before the fix ``EntityService.update`` wrote ``payload.model_dump()`` for every field the
project path did not explicitly keep, so a PUT carrying only ``title`` reset ``status`` to
``"initiating"``, blanked ``created_by_user_id`` and nulled description / dates / budget /
programme_id. The contract is: omitted = kept; an explicit null clears.

All tests hit real PostgreSQL through the FastAPI test client.
"""

from uuid import uuid4

from httpx import AsyncClient

from tests.briefs import MIN_BRIEF
from tests.project_types import typed

# Fields that legitimately change on any successful write.
_VOLATILE = {"updated_at", "title"}


async def _seed(client: AsyncClient) -> tuple[str, dict]:
    resp = await client.post(
        "/pm/programmes", json={"title": "Keep-omitted programme", "status": "identifying"}
    )
    assert resp.status_code == 200, resp.text
    programme_id = resp.json()["id"]
    body = {
        "title": "Keep-omitted project",
        "description": "a description that must survive",
        "programme_id": programme_id,
        "tranche": "T1",
        "status": "executing",
        "start_date": "2026-01-05T00:00:00Z",
        "end_date": "2026-06-30T00:00:00Z",
        "budget": 125000.0,
        "currency": "EUR",
        "wip_limit": 3,
        "rework_limit": 2,
        "created_by_user_id": "user-creator-42",
        "requested_by": "ana",
        "brief": MIN_BRIEF,
    }
    resp = await client.post("/pm/projects", json=await typed(client, body))
    assert resp.status_code == 200, resp.text
    eid = resp.json()["id"]
    resp = await client.get(f"/pm/projects/{eid}")
    assert resp.status_code == 200, resp.text
    stored = resp.json()
    # The seed itself must hold the non-default values, or the test proves nothing.
    for field in (
        "description",
        "programme_id",
        "status",
        "start_date",
        "end_date",
        "budget",
        "wip_limit",
        "created_by_user_id",
    ):
        assert stored[field] not in (None, "", "initiating"), (field, stored[field])
    return eid, stored


async def test_title_only_put_keeps_every_other_field(authenticated_client: AsyncClient):
    eid, before = await _seed(authenticated_client)

    resp = await authenticated_client.put(f"/pm/projects/{eid}", json={"title": "Renamed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Renamed"

    after = (await authenticated_client.get(f"/pm/projects/{eid}")).json()
    assert after["title"] == "Renamed"
    changed = {
        k: (before[k], after.get(k))
        for k in before
        if k not in _VOLATILE and before[k] != after.get(k)
    }
    assert changed == {}, changed
    # Spelled out for the fields the bug reset, so a regression names them.
    assert after["status"] == "executing"
    assert after["created_by_user_id"] == "user-creator-42"
    assert after["description"] == "a description that must survive"
    assert after["budget"] == 125000.0
    assert after["programme_id"] == before["programme_id"]
    assert after["start_date"] == before["start_date"]
    assert after["end_date"] == before["end_date"]
    assert after["wip_limit"] == 3


async def test_explicit_null_clears_description(authenticated_client: AsyncClient):
    eid, before = await _seed(authenticated_client)

    resp = await authenticated_client.put(
        f"/pm/projects/{eid}", json={"title": before["title"], "description": None}
    )
    assert resp.status_code == 200, resp.text

    after = (await authenticated_client.get(f"/pm/projects/{eid}")).json()
    assert after["description"] is None
    # Only description was cleared.
    assert after["status"] == "executing"
    assert after["budget"] == 125000.0
    assert after["created_by_user_id"] == "user-creator-42"


async def test_title_is_still_required(authenticated_client: AsyncClient):
    eid, _ = await _seed(authenticated_client)
    resp = await authenticated_client.put(f"/pm/projects/{eid}", json={"status": "closing"})
    assert resp.status_code == 422, resp.text


async def test_legacy_full_put_still_writes_every_field(authenticated_client: AsyncClient):
    eid, before = await _seed(authenticated_client)
    full = {
        "title": "Fully replaced",
        "description": "new description",
        "programme_id": None,
        "tranche": None,
        "status": "closing",
        "start_date": "2027-02-01T00:00:00Z",
        "end_date": "2027-03-01T00:00:00Z",
        "budget": 1.5,
        "currency": "USD",
        "wip_limit": None,
        "rework_limit": 0,
        "created_by_user_id": "user-other-7",
        "requested_by": "bo",
    }
    resp = await authenticated_client.put(f"/pm/projects/{eid}", json=full)
    assert resp.status_code == 200, resp.text

    after = (await authenticated_client.get(f"/pm/projects/{eid}")).json()
    for field, value in full.items():
        if field.endswith("_date"):
            assert after[field].startswith(value[:10]), (field, after[field])
        else:
            assert after[field] == value, (field, after[field], value)


# ---------------------------------------------------------------------------
# origin_* = the creation identity: set at create, immutable afterwards.
# Omitted = kept; equal = accepted (no-op); different (null included) = 409.
# ---------------------------------------------------------------------------

_ORIGIN = ("origin_kind", "origin_project_id", "origin_workflow_run_id")


async def _seed_workflow_origin(client: AsyncClient) -> tuple[str, dict]:
    """A project created with a non-default origin identity (kind=workflow, run id set).
    R345: only ``origin_kind='project'`` carries an ``origin_project_id``."""
    body = {
        "title": "Origin child",
        "status": "executing",
        "origin_kind": "workflow",
        "origin_workflow_run_id": "n8n-run-1",
        "brief": MIN_BRIEF,
    }
    resp = await client.post("/pm/projects", json=await typed(client, body))
    assert resp.status_code == 200, resp.text
    eid = resp.json()["id"]
    stored = (await client.get(f"/pm/projects/{eid}")).json()
    # The seed must hold a non-default identity, or every test below proves nothing.
    assert stored["origin_kind"] == "workflow"
    assert stored["origin_workflow_run_id"] == "n8n-run-1"
    return eid, stored


async def _seed_project_origin(client: AsyncClient) -> tuple[str, dict]:
    """A project minted for a fr project (kind=project) via the POST get-or-create path."""
    body = {
        "title": "Origin child",
        "status": "executing",
        "origin_kind": "project",
        "origin_project_id": str(uuid4()),
        "brief": MIN_BRIEF,
    }
    resp = await client.post("/pm/projects", json=await typed(client, body))
    assert resp.status_code == 200, resp.text
    eid = resp.json()["id"]
    stored = (await client.get(f"/pm/projects/{eid}")).json()
    assert stored["origin_kind"] == "project"
    assert stored["origin_project_id"] == body["origin_project_id"]
    return eid, stored


def _origin(row: dict) -> dict:
    return {f: row[f] for f in _ORIGIN}


async def test_put_origin_kind_only_different_is_409(authenticated_client: AsyncClient):
    eid, before = await _seed_workflow_origin(authenticated_client)
    resp = await authenticated_client.put(
        f"/pm/projects/{eid}", json={"title": before["title"], "origin_kind": "tenant"}
    )
    assert resp.status_code == 409, resp.text
    assert "origin_immutable" in resp.text
    assert "origin_kind" in resp.text
    after = (await authenticated_client.get(f"/pm/projects/{eid}")).json()
    assert _origin(after) == _origin(before)


async def test_put_null_run_id_when_stored_is_409(authenticated_client: AsyncClient):
    eid, before = await _seed_workflow_origin(authenticated_client)
    resp = await authenticated_client.put(
        f"/pm/projects/{eid}",
        json={"title": before["title"], "origin_workflow_run_id": None},
    )
    assert resp.status_code == 409, resp.text
    assert "origin_immutable" in resp.text
    assert "origin_workflow_run_id" in resp.text
    after = (await authenticated_client.get(f"/pm/projects/{eid}")).json()
    assert _origin(after) == _origin(before)


async def test_put_full_round_trip_identical_origin_is_200_unchanged(
    authenticated_client: AsyncClient,
):
    eid, before = await _seed_workflow_origin(authenticated_client)
    resp = await authenticated_client.put(
        f"/pm/projects/{eid}",
        json={"title": before["title"], "status": before["status"], **_origin(before)},
    )
    assert resp.status_code == 200, resp.text
    after = (await authenticated_client.get(f"/pm/projects/{eid}")).json()
    assert _origin(after) == _origin(before)
    assert after["status"] == "executing"


async def test_put_omitted_origin_is_kept(authenticated_client: AsyncClient):
    eid, before = await _seed_workflow_origin(authenticated_client)
    resp = await authenticated_client.put(
        f"/pm/projects/{eid}", json={"title": before["title"], "description": "edited"}
    )
    assert resp.status_code == 200, resp.text
    after = (await authenticated_client.get(f"/pm/projects/{eid}")).json()
    assert after["description"] == "edited"
    assert _origin(after) == _origin(before)


async def test_post_get_or_create_finds_same_row_after_put(authenticated_client: AsyncClient):
    eid, before = await _seed_project_origin(authenticated_client)
    resp = await authenticated_client.put(
        f"/pm/projects/{eid}", json={"title": before["title"], "description": "edited"}
    )
    assert resp.status_code == 200, resp.text
    # A brief-less re-POST on the same identity must land on the existing row.
    resp = await authenticated_client.post(
        "/pm/projects",
        json=await typed(authenticated_client, {"title": before["title"], **_origin(before)}),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == eid
    listed = await authenticated_client.get(
        "/pm/projects", params={"origin_project_id": before["origin_project_id"]}
    )
    assert listed.status_code == 200, listed.text
    assert [p["id"] for p in listed.json()] == [eid]


async def test_repost_with_different_title_lands_on_existing_row(
    authenticated_client: AsyncClient,
):
    """R345: get-or-create is keyed on (tenant, origin_project_id) only — a re-POST for the
    same fr project under another title updates the existing row, never mints a second."""
    eid, before = await _seed_project_origin(authenticated_client)
    resp = await authenticated_client.post(
        "/pm/projects",
        json=await typed(authenticated_client, {**_origin(before), "title": "Renamed in fr"}),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == eid
    after = (await authenticated_client.get(f"/pm/projects/{eid}")).json()
    assert after["title"] == "Renamed in fr"
    assert _origin(after) == _origin(before)
    listed = await authenticated_client.get(
        "/pm/projects", params={"origin_project_id": before["origin_project_id"]}
    )
    assert [p["id"] for p in listed.json()] == [eid]


async def test_put_with_different_run_id_is_still_409(authenticated_client: AsyncClient):
    eid, before = await _seed_workflow_origin(authenticated_client)
    resp = await authenticated_client.put(
        f"/pm/projects/{eid}",
        json={**_origin(before), "title": before["title"], "origin_workflow_run_id": "n8n-run-2"},
    )
    assert resp.status_code == 409, resp.text
    assert "origin_immutable" in resp.text
    assert "origin_workflow_run_id" in resp.text
    after = (await authenticated_client.get(f"/pm/projects/{eid}")).json()
    assert after["origin_workflow_run_id"] == "n8n-run-1"
