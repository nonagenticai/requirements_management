"""``GET /architecture/elements/{eid}/children`` must be bounded.

The defect these tests pin down: the endpoint declared **no** ``limit``
parameter at all (verified against the live production OpenAPI spec — its only
declared parameters were ``eid`` and one ``$ref``) and its query carried no
``LIMIT``. One fan-out parent therefore loaded its entire child set into a
``512Mi`` / ``replicas: 1`` pod, while the sibling list endpoint on the same
resource (``GET /architecture/elements``) has capped at
``Query(100, ge=1, le=1000)`` all along.

The bound here is deliberately IDENTICAL to that sibling. Matching it is the
whole argument for the change: a different default on one of two list endpoints
over the same table is a new inconsistency, not a fix.

KILLING MUTATIONS — one per test, so a later edit cannot quietly undo the bound:

  * drop ``.limit(limit)`` from the query but keep the parameter declared ->
    ``test_default_limit_is_applied_to_the_query`` fails. That is the failure
    mode this file exists for: an endpoint that ADVERTISES a cap in its schema
    while still reading the whole table is strictly worse than one that
    advertises nothing, because the OpenAPI spec then certifies a bound that
    does not exist.
  * widen ``le=1000`` (or drop it) -> ``test_limit_above_the_ceiling_is_422``
    fails: 1001 is accepted and the ceiling is gone.
  * drop ``ge=1`` -> ``test_limit_below_one_is_422`` fails. ``limit=0`` and
    ``limit=-1`` are separate cases on purpose: SQL treats ``LIMIT 0`` as a
    legal empty page, so a missing ``ge=1`` shows up as 200-with-no-rows rather
    than as an error, and only the negative value would trip the database.
  * drop ``.limit(...)`` AND the parameter together -> the explicit-limit test
    fails first (a query string FastAPI does not declare is ignored, so all
    seeded rows come back).

Seeding goes straight through ``test_session`` rather than the POST route:
these tests need more rows than the cap, and 105 create calls each run the
write-time acyclicity guard plus quality scoring. The rows are ordinary — no
guard is being bypassed, only the cost.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid7

import pytest
from httpx import AsyncClient

from models.architecture import ArchitectureElement
from tests.tenants import T

# Must match ``limit``'s declared default in the router. Written out rather
# than imported so that changing the router's default makes these tests fail
# instead of following along silently.
_DEFAULT_LIMIT = 1000
_CEILING = 1000

_TENANT = T("user-123")
_TENANT_NAME = "user-123"


def _row(slug: str, *, element_id: UUID, parent_id: UUID | None) -> ArchitectureElement:
    """Build one ordinary child row for the fan-out parent."""
    return ArchitectureElement(
        id=element_id,
        slug=slug,
        title=slug,
        description="bounded-children fixture",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_TENANT,
        visibility="tenant",
        project_id="bounded-children",
        layer="application",
        element_type="application_component",
        properties={},
        parent_id=parent_id,
    )


async def _seed_parent_with_children(test_session: Any, count: int) -> UUID:
    """Create one parent and ``count`` direct children; return the parent id."""
    parent_id = uuid7()
    test_session.add(_row("bounded-children-parent", element_id=parent_id, parent_id=None))
    for i in range(count):
        test_session.add(_row(f"bounded-child-{i:04d}", element_id=uuid7(), parent_id=parent_id))
    await test_session.commit()
    return parent_id


@pytest.mark.asyncio
async def test_more_children_than_limit_returns_exactly_limit(
    authenticated_client: AsyncClient, test_session
) -> None:
    """An explicit ``limit`` caps the page at exactly that many items."""
    parent_id = await _seed_parent_with_children(test_session, 25)

    resp = await authenticated_client.get(
        f"/architecture/elements/{parent_id}/children", params={"limit": 10}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 10, f"expected exactly 10 items, got {len(body)}"
    # Every item really is a child of this parent — a cap that leaked rows from
    # elsewhere would also be 10 items long.
    assert {c["parent_id"] for c in body} == {str(parent_id)}
    # The caller can see it was truncated: the count header reports the whole
    # set, not the page.
    assert resp.headers["X-Total-Count"] == "25", resp.headers


@pytest.mark.asyncio
async def test_a_caller_that_passes_no_limit_is_not_silently_truncated(
    authenticated_client: AsyncClient, test_session
) -> None:
    """No query string ⇒ the whole child set, up to the ceiling.

    ⚠️ This endpoint's default is the CEILING (1000), not the sibling list
    endpoint's 100, and that asymmetry is deliberate — see the router docstring.
    The live caller is ``n8n-nodes-requirements``'s
    ``listArchitectureElementChildren``, which sends no query string and has no
    pagination-follow loop. A default of 100 would have handed it a silently
    truncated child set: an availability fix traded for a confidently wrong
    answer, which is the exact defect the rest of this PR removes.

    So the property under test is NOT "the default truncates". It is "a caller
    that asks for nothing still gets everything it used to get".
    """
    seeded = 120  # comfortably over the sibling's default of 100, under the ceiling
    parent_id = await _seed_parent_with_children(test_session, seeded)

    resp = await authenticated_client.get(f"/architecture/elements/{parent_id}/children")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == seeded, (
        f"seeded {seeded} children and passed no limit; the caller must still receive "
        f"all of them (the sibling's default of 100 would have cut this to 100), "
        f"got {len(body)}"
    )
    assert resp.headers["X-Total-Count"] == str(seeded), resp.headers


@pytest.mark.asyncio
async def test_the_declared_default_is_the_ceiling(
    authenticated_client: AsyncClient, test_session
) -> None:
    """The bound is real and visible in the schema, not just in the handler.

    Pins both halves: the parameter is DECLARED (so a client can see the bound)
    and its default equals the ceiling (so the asymmetry above is intentional and
    survives a later "tidy the defaults to match" edit).
    """
    spec = (await authenticated_client.get("/openapi.json")).json()
    params = {
        p["name"]: p
        for path, item in spec["paths"].items()
        if path.endswith("/children")
        for method, op in item.items()
        if isinstance(op, dict)
        for p in op.get("parameters", [])
        if p.get("name")
    }
    assert "limit" in params, f"the bound is not declared in the schema: {list(params)}"
    schema = params["limit"]["schema"]
    assert schema.get("default") == _DEFAULT_LIMIT == _CEILING, schema
    assert schema.get("maximum") == _CEILING, schema


@pytest.mark.asyncio
async def test_limit_above_the_ceiling_is_422(
    authenticated_client: AsyncClient, test_session
) -> None:
    """``limit=1001`` is refused — the ``le=1000`` ceiling matches the sibling."""
    parent_id = await _seed_parent_with_children(test_session, 1)

    resp = await authenticated_client.get(
        f"/architecture/elements/{parent_id}/children", params={"limit": _CEILING + 1}
    )

    assert resp.status_code == 422, resp.text
    # The ceiling itself is still accepted — the bound is le, not lt.
    ok = await authenticated_client.get(
        f"/architecture/elements/{parent_id}/children", params={"limit": _CEILING}
    )
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_limit", [0, -1])
async def test_limit_below_one_is_422(
    authenticated_client: AsyncClient, test_session, bad_limit: int
) -> None:
    """``limit=0`` and ``limit=-1`` are both refused by the ``ge=1`` bound."""
    parent_id = await _seed_parent_with_children(test_session, 2)

    resp = await authenticated_client.get(
        f"/architecture/elements/{parent_id}/children", params={"limit": bad_limit}
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_the_page_is_ordered_in_sql_not_by_luck(
    authenticated_client: AsyncClient, test_session
) -> None:
    """The paged query must carry an ORDER BY on the primary key.

    ⛔ THIS TEST EXISTS BECAUSE ITS MUTANT SURVIVED. An adversarial sweep deleted
    ``.order_by(ArchitectureElement.id)`` from the children query and every other
    test in this file still passed — including the paging test, because with a
    small fixture Postgres happens to return heap order, which happens to match
    insertion order, which happens to match id order.

    That is luck, not a contract. ``LIMIT``/``OFFSET`` without a unique ordering
    is an arbitrary subset the planner may vary between identical requests, so
    page 2 could repeat or skip rows from page 1 — and the docstring on the
    endpoint claims "paging is stable". The property is only visible in the SQL,
    so that is what this asserts.
    """
    from sqlalchemy import event

    parent_id = await _seed_parent_with_children(test_session, 3)

    statements: list[str] = []
    engine = test_session.get_bind()

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        resp = await authenticated_client.get(
            f"/architecture/elements/{parent_id}/children", params={"limit": 2}
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert resp.status_code == 200, resp.text  # control: the call really ran

    paged = [
        s
        for s in statements
        if "FROM architecture_elements" in s and "LIMIT" in s.upper() and "count(" not in s.lower()
    ]
    assert paged, f"no paged SELECT was captured; the listener is blind: {statements}"
    assert all("ORDER BY" in s.upper() for s in paged), (
        f"the paged SELECT reached Postgres with no ORDER BY, so the page is an "
        f"arbitrary subset: {paged}"
    )
    assert all("architecture_elements.id" in s for s in paged), (
        f"the paged SELECT orders by something other than the primary key: {paged}"
    )
