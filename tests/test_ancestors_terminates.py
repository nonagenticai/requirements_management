"""``GET /architecture/elements/{eid}/ancestors`` must always terminate.

The defect these tests pin down: the ancestors CTE was ``UNION ALL`` with a
``depth`` column that was computed and never filtered on, and no path column.
``parent_id`` had no write-time guard either, so two ordinary writes
(A.parent=B, B.parent=A) plus one GET recursed forever. The service runs
``replicas: 1``, so the connection was never returned.

Every test here bounds its own wall clock with ``asyncio.wait_for``. That is
deliberate: the regression's signature is a HANG, and a test that hangs does not
report a failure — it takes the whole suite with it. The bound converts the
regression into a red test.

KILLING MUTATIONS — each verified by actually applying it and observing RED.
Read the first note before trusting the obvious version of this table:

  ⚠️ ``revert-cycle-break`` alone (drop ``AND NOT (e.id = ANY(up.path))``, keep
  the depth cap) does **NOT** make the two-node cycle test fail, and an earlier
  version of this docstring claimed it did. Measured: all tests still pass,
  because the depth cap independently stops the recursion at 64 rows and the
  ``deepest_parent in visited`` check then still answers 409. The two read
  guards are genuinely redundant *for termination*, which is the point of
  having both — but it means a test that only asserts "409 on a cycle" cannot
  tell them apart and must not claim to.

  * ``revert-cycle-break`` -> ``test_cycle_is_cheap_not_merely_finite`` fails.
    That test raises the cap far above the cycle length, which is the only
    condition under which the two guards give observably different behaviour:
    with the path predicate a cycle costs as many rows as the cycle is long;
    without it, a cycle costs the whole cap. The other cycle tests keep
    passing under this mutation, by design — see the note above.
  * ``drop-depth-cap`` (drop ``AND up.depth < :max_depth``) ->
    ``test_depth_cap_truncates_with_headers`` gets the full chain and no
    headers, and fails.
  * ``drop-write-guard`` (remove the ``assert_parent_acyclic`` call from the
    PUT route) -> ``test_write_time_cycle_is_refused_and_not_persisted`` gets a
    200 and a persisted cycle, and fails.
  * both read guards removed -> the original defect: unbounded recursion, the
    ``asyncio.wait_for`` bound trips, and every cycle test fails.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from models.architecture import ArchitectureElement
from repositories import hierarchy
from tests.tenants import T

# Any hang is the regression. Ten seconds is far above the real cost of these
# queries (single-digit ms) and far below a suite timeout.
_BOUND_SECONDS = 10.0

_TENANT = T("user-123")
_TENANT_NAME = "user-123"

_BASE_ELEMENT: dict[str, Any] = {
    "description": "ancestors-termination fixture",
    "layer": "application",
    "element_type": "application_component",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "ancestors-termination",
}


def _row(slug: str, *, element_id: uuid.UUID, parent_id: uuid.UUID | None) -> ArchitectureElement:
    """Build a row directly, bypassing the router's write-time guard.

    Built directly on purpose: the whole point is to reproduce data that the
    write guard now refuses but that already exists in the table (rows written
    before the guard landed, or by any other writer). The read guards exist for
    exactly that population.
    """
    return ArchitectureElement(
        id=element_id,
        slug=slug,
        title=slug,
        description="cycle fixture",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_TENANT,
        visibility="tenant",
        project_id="ancestors-termination",
        layer="application",
        element_type="application_component",
        properties={},
        parent_id=parent_id,
    )


async def _create(client: AsyncClient, title: str, parent_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {**_BASE_ELEMENT, "title": title}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = await client.post("/architecture/elements", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_two_node_cycle_returns_409_and_does_not_hang(
    authenticated_client: AsyncClient, test_session
) -> None:
    """A real 2-node cycle answers 409 within a bounded wall clock.

    KILLING MUTATION: ``revert-cycle-break`` (drop ``NOT (e.id = ANY(up.path))``
    from the recursive arm) -> the CTE recurses forever, ``asyncio.wait_for``
    trips, and this test fails instead of hanging the suite.
    """
    a_id = uuid.uuid4()
    b_id = uuid.uuid4()
    test_session.add(_row("cycle-a", element_id=a_id, parent_id=b_id))
    test_session.add(_row("cycle-b", element_id=b_id, parent_id=a_id))
    await test_session.commit()

    resp = await asyncio.wait_for(
        authenticated_client.get(f"/architecture/elements/{a_id}/ancestors"),
        timeout=_BOUND_SECONDS,
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    # Repo-standard error envelope, not a bare dict.
    assert set(body.keys()) == {"error"}, body
    assert body["error"]["status"] == 409, body
    message = body["error"]["message"]
    # Assert on the ids actually created, not on shape.
    assert str(a_id) in message and str(b_id) in message, message


@pytest.mark.asyncio
async def test_self_loop_returns_409(authenticated_client: AsyncClient, test_session) -> None:
    """A one-node cycle (A.parent = A) is a cycle, not an empty ancestor list.

    The pre-fix query filtered the anchor row out in SQL (``WHERE id <> :eid``),
    so a self-loop produced zero rows and would have answered ``200 []`` — a
    silent wrong answer rather than a hang. Pinned separately because the
    two-node test cannot see it.
    """
    a_id = uuid.uuid4()
    test_session.add(_row("selfloop-a", element_id=a_id, parent_id=a_id))
    await test_session.commit()

    resp = await asyncio.wait_for(
        authenticated_client.get(f"/architecture/elements/{a_id}/ancestors"),
        timeout=_BOUND_SECONDS,
    )

    assert resp.status_code == 409, resp.text
    assert str(a_id) in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_cycle_is_cheap_not_merely_finite(
    authenticated_client: AsyncClient, test_session, monkeypatch
) -> None:
    """A cycle must cost the CYCLE's length, not the depth cap's.

    This is the only test that separates the two read guards. With the cap set
    far above the cycle length, the depth cap is still a correct *termination*
    guard — it would eventually stop — but stopping after 50,000 recursive rows
    (each carrying a ``uuid[]`` path one element longer than the last, so the
    work is quadratic) is not a usable answer on a ``replicas: 1`` service. The
    path predicate stops at the repeat, i.e. after 2 rows.

    KILLING MUTATION: ``revert-cycle-break`` -> the recursion runs to the cap,
    the wall-clock bound trips, and this test fails. The other cycle tests do
    NOT fail under that mutation, which is exactly why this one exists.
    """
    a_id = uuid.uuid4()
    b_id = uuid.uuid4()
    test_session.add(_row("deepcap-a", element_id=a_id, parent_id=b_id))
    test_session.add(_row("deepcap-b", element_id=b_id, parent_id=a_id))
    await test_session.commit()

    monkeypatch.setattr(hierarchy, "ANCESTORS_MAX_DEPTH", 50_000)

    resp = await asyncio.wait_for(
        authenticated_client.get(f"/architecture/elements/{a_id}/ancestors"),
        timeout=_BOUND_SECONDS,
    )

    assert resp.status_code == 409, resp.text
    assert str(a_id) in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_acyclic_chain_still_returns_full_ancestry(
    authenticated_client: AsyncClient,
) -> None:
    """CONTROL — must pass identically before and after the fix.

    Without this, a guard that answered 409 (or []) for EVERY input would make
    the cycle tests above pass vacuously. Asserts the exact ids created, in
    nearest-first order.
    """
    n1 = await _create(authenticated_client, "control depth 1")
    n2 = await _create(authenticated_client, "control depth 2", parent_id=n1["id"])
    n3 = await _create(authenticated_client, "control depth 3", parent_id=n2["id"])
    n4 = await _create(authenticated_client, "control depth 4", parent_id=n3["id"])
    n5 = await _create(authenticated_client, "control depth 5", parent_id=n4["id"])

    resp = await asyncio.wait_for(
        authenticated_client.get(f"/architecture/elements/{n5['id']}/ancestors"),
        timeout=_BOUND_SECONDS,
    )

    assert resp.status_code == 200, resp.text
    chain = resp.json()
    assert [c["id"] for c in chain] == [n4["id"], n3["id"], n2["id"], n1["id"]]
    # A complete answer must NOT claim truncation.
    assert "X-Ancestors-Truncated" not in resp.headers, dict(resp.headers)


@pytest.mark.asyncio
async def test_depth_cap_truncates_with_headers(
    authenticated_client: AsyncClient, monkeypatch
) -> None:
    """A chain longer than the cap returns 200 + truncation headers, not an error.

    KILLING MUTATION: ``drop-depth-cap`` (remove ``up.depth < :max_depth``) ->
    the full 5-deep ancestry comes back and no headers are set; both assertions
    below fail.

    The chain is built BEFORE the cap is shortened, so this exercises the READ
    cap only. The write-time cap is a separate knob (a function default bound at
    def time) and is deliberately not disturbed here.
    """
    n1 = await _create(authenticated_client, "capped depth 1")
    n2 = await _create(authenticated_client, "capped depth 2", parent_id=n1["id"])
    n3 = await _create(authenticated_client, "capped depth 3", parent_id=n2["id"])
    n4 = await _create(authenticated_client, "capped depth 4", parent_id=n3["id"])
    n5 = await _create(authenticated_client, "capped depth 5", parent_id=n4["id"])

    # Cap of 3 => anchor (n5) + 2 ancestors walked: n4, n3.
    monkeypatch.setattr(hierarchy, "ANCESTORS_MAX_DEPTH", 3)

    resp = await asyncio.wait_for(
        authenticated_client.get(f"/architecture/elements/{n5['id']}/ancestors"),
        timeout=_BOUND_SECONDS,
    )

    assert resp.status_code == 200, resp.text
    # Body stays a bare list — no envelope. The AA conveyor reads it positionally.
    chain = resp.json()
    assert isinstance(chain, list), chain
    assert [c["id"] for c in chain] == [n4["id"], n3["id"]]
    assert resp.headers["X-Ancestors-Truncated"] == "true"
    assert resp.headers["X-Ancestors-Depth-Limit"] == "3"


@pytest.mark.asyncio
async def test_write_time_cycle_is_refused_and_not_persisted(
    authenticated_client: AsyncClient,
) -> None:
    """Re-parenting an element under its own descendant is refused with 422.

    NOTE the verb is PUT, not PATCH — this router exposes no PATCH route.

    KILLING MUTATION: ``drop-write-guard`` -> the PUT returns 200, the cycle
    lands, and the re-read below sees ``parent_id == child['id']``.
    """
    root = await _create(authenticated_client, "writeguard root")
    child = await _create(authenticated_client, "writeguard child", parent_id=root["id"])

    resp = await authenticated_client.put(
        f"/architecture/elements/{root['id']}",
        json={**_BASE_ELEMENT, "title": "writeguard root", "parent_id": child["id"]},
    )

    assert resp.status_code == 422, resp.text
    message = resp.json()["error"]["message"]
    assert str(root["id"]) in message and str(child["id"]) in message, message

    # The cycle must NOT be persisted: root keeps its original (null) parent.
    reread = await authenticated_client.get(f"/architecture/elements/{root['id']}")
    assert reread.status_code == 200, reread.text
    assert reread.json()["parent_id"] is None, reread.text

    # And the endpoint that the cycle would have hung still answers.
    ancestors = await asyncio.wait_for(
        authenticated_client.get(f"/architecture/elements/{child['id']}/ancestors"),
        timeout=_BOUND_SECONDS,
    )
    assert ancestors.status_code == 200, ancestors.text
    assert [c["id"] for c in ancestors.json()] == [root["id"]]


@pytest.mark.asyncio
async def test_write_time_self_parent_is_refused(authenticated_client: AsyncClient) -> None:
    """The degenerate case: an element cannot be made its own parent."""
    el = await _create(authenticated_client, "selfparent element")

    resp = await authenticated_client.put(
        f"/architecture/elements/{el['id']}",
        json={**_BASE_ELEMENT, "title": "selfparent element", "parent_id": el["id"]},
    )

    assert resp.status_code == 422, resp.text
    assert str(el["id"]) in resp.json()["error"]["message"]

    reread = await authenticated_client.get(f"/architecture/elements/{el['id']}")
    assert reread.json()["parent_id"] is None, reread.text


@pytest.mark.asyncio
async def test_create_under_a_preexisting_cycle_is_refused(
    authenticated_client: AsyncClient, test_session
) -> None:
    """POST is a write path too — it must not attach a subtree to cyclic data.

    Covers the fan-out branch that the PUT tests cannot reach: the create route
    checks with ``element_id=None``, which catches a parent whose OWN ancestor
    chain is already cyclic.
    """
    a_id = uuid.uuid4()
    b_id = uuid.uuid4()
    test_session.add(_row("precycle-a", element_id=a_id, parent_id=b_id))
    test_session.add(_row("precycle-b", element_id=b_id, parent_id=a_id))
    await test_session.commit()

    resp = await authenticated_client.post(
        "/architecture/elements",
        json={**_BASE_ELEMENT, "title": "child of a cycle", "parent_id": str(a_id)},
    )

    assert resp.status_code == 422, resp.text
    assert str(a_id) in resp.json()["error"]["message"]

    # Nothing was written.
    found = (
        (
            await test_session.execute(
                select(ArchitectureElement).where(
                    ArchitectureElement.tenant_id == _TENANT,
                    ArchitectureElement.title == "child of a cycle",
                )
            )
        )
        .scalars()
        .all()
    )
    assert found == [], found
