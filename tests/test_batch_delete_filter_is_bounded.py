"""`batch-delete`'s two branches must agree on how much one call may destroy.

`POST /architecture/elements/batch-delete` takes exactly one of `ids` or
`filter`. The `ids` branch has been capped at `_BATCH_DELETE_MAX` (500) by a
`field_validator` since it shipped. The `filter` branch had no cap at all: the
schema's `_whitelist_filter` rejects an unknown key but never requires a present
one, so `{"filter": {}}` is a well-formed request that selects EVERY element in
the tenant — 2,703 on the largest live one — and feeds them to a per-id loop
whose `EntityService.delete` ends in `session.commit()`. That is N separate
transactions, so a failure partway through leaves the tenant's architecture in a
state no rollback recovers.

There are TWO defects here, and the bound only fixes one of them. Bounding the
filter branch makes `{"filter": {}}` a 422 on a tenant over the cap — and leaves
it a 200 that wipes the tenant when the tenant is under it (measured: 3 elements
seeded, `deleted: 3`, list then empty). So the empty filter is now rejected
outright with 400, alongside the existing "exactly one of `ids` or `filter`"
guard it belongs with.

Three orders of magnitude between two branches of one endpoint is the defect.
The fix bounds the filter branch with the SAME `_BATCH_DELETE_MAX`, not with one
of `repositories.bounded`'s `DEFAULT_MAX_INPUT_*` ceilings (6000/24000) — those
are sized ~2x the largest tenant for read-only whole-graph ANALYSIS, where
loading everything is the point and the worst outcome is a slow request.

What is pinned here:

0. **An empty filter is refused at all sizes**, with the rows still present
   afterwards — the case the bound alone answers differently depending on how
   big the tenant happens to be.
1. **The refusal deletes nothing.** Over the cap is 422 AND the rows are still
   there afterwards. A status-only assertion would pass just as happily if the
   endpoint deleted 501 elements and then reported an error, which is the strictly
   worse failure — a destructive no-op is recoverable, a partial destruction is
   not.
2. **The control.** Under the cap still behaves exactly as before. Without it,
   "bounded" is indistinguishable from "broken": an endpoint that deletes nothing
   at all also never trips a cap.
3. **The `ids` branch is untouched**, so this change cannot be read as having
   moved the number that was already there.
4. **The `route_class` is wired.** `InputTooLarge` is a plain `ValueError`;
   only `_BoundedInputRoute` renders it as 422, and it is applied PER-ROUTER. A
   correct `fetch_bounded` call on a router without it ships a 500 — "this
   service is broken" for a request the server understood and refused on purpose.

The cap is monkeypatched down to 5 rather than seeded past. Seeding 501 elements
through the create endpoint would make every CI run pay a real cost to exercise
one comparison, and the constant is the thing under test, not the row count.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from pydantic import ValidationError

from routers import architecture_elements
from routers.architecture_tools import BoundedInputRoute
from tests.tenants import T

_URL = "/architecture/elements/batch-delete"
_PROJECT = "batch-delete-filter-cap"
_SMALL_CAP = 5

_BASE_ELEMENT = {
    "description": "Seeded for the batch-delete filter bound",
    "layer": "technology",
    "element_type": "node",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": _PROJECT,
}


async def _seed(client: AsyncClient, count: int) -> None:
    """Create `count` distinct technology-layer elements in `_PROJECT`."""
    for i in range(count):
        resp = await client.post(
            "/architecture/elements",
            json={**_BASE_ELEMENT, "title": f"filter-cap element {i:02d}"},
        )
        assert resp.status_code in (200, 201), resp.text


async def _count(client: AsyncClient) -> int:
    """How many elements the tenant can see in `_PROJECT`.

    Read from the `X-Total-Count` header rather than `len(body)` so the answer
    is the true total and not a page of it.
    """
    resp = await client.get("/architecture/elements", params={"project_id": _PROJECT, "limit": 1})
    assert resp.status_code == 200, resp.text
    return int(resp.headers["X-Total-Count"])


async def test_over_cap_filter_is_refused_and_deletes_nothing(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """422 naming the total and the limit — and every row survives.

    The survival assertion is the load-bearing one. `raise_if_truncated()` fires
    BEFORE the delete loop, so the refusal is atomic by construction; asserting
    only the status code would not tell the difference between that and a loop
    that committed 5 deletions and then raised.
    """
    monkeypatch.setattr(architecture_elements, "_BATCH_DELETE_MAX", _SMALL_CAP)
    await _seed(authenticated_client, _SMALL_CAP + 1)
    before = await _count(authenticated_client)
    assert before == _SMALL_CAP + 1, before

    resp = await authenticated_client.post(_URL, json={"filter": {"layer": "technology"}})

    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "input_too_large", error
    # `main._http_exception_to_response` flattens a dict detail to `code` +
    # `message` and drops every sibling key, so the two numbers the operator has
    # to act on reach the wire only inside the message text.
    assert str(_SMALL_CAP + 1) in error["message"], error
    assert str(_SMALL_CAP) in error["message"], error
    assert "architecture elements matching filter" in error["message"], error

    assert await _count(authenticated_client) == before


async def test_empty_filter_is_refused(authenticated_client: AsyncClient) -> None:
    """`{"filter": {}}` is 400, and the tenant is still there afterwards.

    Deliberately run UNDER the cap and with the real 500 in place, because that
    is where the bound cannot help: `{}` passes `_whitelist_filter` (no unknown
    keys, because no keys), passes the mutual-exclusion guard, matched every
    element in the tenant, and on a tenant of three that is a 200 reporting
    `deleted: 3` — a silent wipe. Bounding the branch made the SAME request 422
    on a large tenant and 200-wipe on a small one; only rejecting the empty
    filter makes the endpoint answer the same way in both directions.

    400, not 422, to match the mutual-exclusion guard it sits beside: this is a
    malformed intent — the caller named no criteria — not a well-formed request
    the server is refusing on size.

    The survival assertion is the load-bearing half. A 400 that had already
    committed the deletions would satisfy a status-only check.
    """
    await _seed(authenticated_client, 3)
    assert await _count(authenticated_client) == 3

    resp = await authenticated_client.post(_URL, json={"filter": {}})

    assert resp.status_code == 400, resp.text
    assert "at least one criterion" in str(resp.json()), resp.text
    assert await _count(authenticated_client) == 3


async def test_an_empty_ids_list_is_still_a_no_op_not_a_400(
    authenticated_client: AsyncClient,
) -> None:
    """Empty `ids` and empty `filter` are deliberately NOT symmetric.

    `{"ids": []}` is "delete these zero things" — the caller enumerated the set
    and it was empty — and stays a 200 no-op (pinned in
    `test_batch_delete_does_not_500.py` for a separate reason: it used to be a
    500). `{"filter": {}}` is "delete, without saying what". Pinned here so the
    new guard cannot be widened into the ids branch by someone tidying the two
    into agreement.
    """
    resp = await authenticated_client.post(_URL, json={"ids": []})

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 0, resp.text


async def test_under_cap_filter_still_deletes(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: at or under the cap, the branch behaves exactly as before."""
    monkeypatch.setattr(architecture_elements, "_BATCH_DELETE_MAX", _SMALL_CAP)
    await _seed(authenticated_client, _SMALL_CAP)
    assert await _count(authenticated_client) == _SMALL_CAP

    resp = await authenticated_client.post(_URL, json={"filter": {"layer": "technology"}})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == _SMALL_CAP, body
    assert len(body["ids"]) == _SMALL_CAP, body
    assert await _count(authenticated_client) == 0


async def test_ids_branch_is_unchanged(authenticated_client: AsyncClient) -> None:
    """500 ids is still accepted by the schema; 501 is still a 422.

    The boundary is asserted against `BatchDeleteRequest` directly rather than
    over HTTP: a 500-id request would drive 500 tenant-scoped lookups for ids
    that do not exist, which is real CI time spent on a validator that never
    touches the database.
    """
    ids = [UUID(f"00000000-0000-0000-0000-{i:012d}") for i in range(501)]

    assert architecture_elements._BATCH_DELETE_MAX == 500
    accepted = architecture_elements.BatchDeleteRequest(ids=ids[:500])
    assert accepted.ids is not None and len(accepted.ids) == 500

    with pytest.raises(ValidationError, match="max 500 ids per call"):
        architecture_elements.BatchDeleteRequest(ids=ids)

    resp = await authenticated_client.post(_URL, json={"ids": [str(i) for i in ids]})
    assert resp.status_code == 422, resp.text


async def test_over_cap_is_422_not_500(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `route_class` is wired — the half of the fix that is easy to drop.

    `fetch_bounded` raises `InputTooLarge`, a plain `ValueError` that
    `repositories/**` deliberately keeps framework-free. Nothing in this router
    catches it; only `_BoundedInputRoute` maps it, and a `route_class` is
    per-router, not inherited from the sibling that defines it. Remove
    `route_class=` from `architecture_elements.router` and this goes red twice —
    on the structural pin and on the status code.
    """
    assert architecture_elements.router.route_class is BoundedInputRoute

    batch_delete_routes = [
        r
        for r in architecture_elements.router.routes
        if isinstance(r, APIRoute) and r.path == "/batch-delete"
    ]
    assert len(batch_delete_routes) == 1, batch_delete_routes
    assert isinstance(batch_delete_routes[0], BoundedInputRoute)

    monkeypatch.setattr(architecture_elements, "_BATCH_DELETE_MAX", _SMALL_CAP)
    await _seed(authenticated_client, _SMALL_CAP + 1)

    resp = await authenticated_client.post(_URL, json={"filter": {"layer": "technology"}})

    assert resp.status_code != 500, resp.text
    assert resp.status_code == 422, resp.text


async def test_one_tenants_volume_cannot_refuse_another_tenants_delete(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound must count only the caller's own rows.

    An adversarial sweep deleted `.where(ArchitectureElement.tenant_id == tenant_id)`
    from the filter query and NOTHING went red — not even the existing
    `test_batch_delete_tenant_isolated`. That test is about the `ids` branch, and
    it passes for a different reason than the query: `EntityService.delete`
    re-checks ownership per id and 404s, so no row of another tenant is ever
    actually destroyed.

    What the unscoped query DOES change is who gets refused. `fetch_bounded`
    counts the rows the SELECT matched, so without the tenant predicate a large
    neighbour's elements are counted against a small tenant's cap, and that
    tenant is answered 422 for a delete that would have touched one row. That is
    a cross-tenant denial of service reachable by doing nothing but existing, and
    no assertion in the suite could see it.

    Seeded so the two tenants are only distinguishable if the predicate is there:
    the neighbour alone exceeds the cap, the caller alone is far under it.
    """
    monkeypatch.setattr(architecture_elements, "_BATCH_DELETE_MAX", _SMALL_CAP)
    loud = {"X-Tenant-Id": T("tenant-filter-cap-loud")}
    quiet = {"X-Tenant-Id": T("tenant-filter-cap-quiet")}

    for i in range(_SMALL_CAP + 1):
        resp = await test_client.post(
            "/architecture/elements",
            json={**_BASE_ELEMENT, "title": f"loud neighbour {i:02d}"},
            headers=loud,
        )
        assert resp.status_code in (200, 201), resp.text

    mine = await test_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "quiet tenant's only element"},
        headers=quiet,
    )
    assert mine.status_code in (200, 201), mine.text

    resp = await test_client.post(_URL, json={"filter": {"project_id": _PROJECT}}, headers=quiet)
    assert resp.status_code == 200, (
        "the quiet tenant owns 1 matching row against a cap of "
        f"{_SMALL_CAP}; a refusal here means the bound counted the neighbour's "
        f"rows too. body={resp.text}"
    )
    assert resp.json()["deleted"] == 1, resp.text

    # And the neighbour still has everything: the scoped delete took only its own.
    survived = await test_client.get(
        "/architecture/elements", params={"project_id": _PROJECT, "limit": 1}, headers=loud
    )
    assert survived.status_code == 200, survived.text
    assert int(survived.headers["X-Total-Count"]) == _SMALL_CAP + 1
