"""Modeling lifecycle review fixes (2026-10-27), over HTTP against real Postgres.

One test (or more) per review finding; each fails on the code before the fix:

1. model visibility: owner-only, refused over another subject's rows / links;
2. a link narrower than its source model belongs to no model (never in its manifest);
3. change-set apply / reject re-read the set under lock (two real sessions);
4. a duplicate edge is a CS-OP-002 finding; a constraint at apply is 422, not 500;
5. a pruned cross-model trace link bumps ITS model's head;
6. a title / description PATCH bumps the head;
7. If-Match on another subject's private row is 404, never a 412 leaking a head;
8. force delete removes edges ending on deleted cross-domain edges and their links;
9. ETag is the target model's head when one write bumps two models;
10. PATCH ignores approval fields on architecture rows;
11. an out-of-range exchange colour is a VALUE-INVALID loss and is dropped;
12. a private model's owner reviews and publishes its revisions alone.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from models.architecture import ArchitectureElement, ArchitectureRelationship
from models.modeling import ModelingChangeSet, ModelingModel, ModelingTraceLink
from schemas.architecture import ArchitectureElementIn
from services import architecture_element_service
from services import change_sets as cs_svc
from tests.test_modeling_lifecycle import (
    ALICE,
    BOB,
    OTHER_TENANT,
    TENANT,
    element,
    etag,
    hdrs,
    head_of,
    integrity,
    new_model,
    ok,
    populated,
    relationship,
)
from tests.test_modeling_lifecycle_part2 import _requirement, code, propose, review, run, seal
from tools.architecture.exchange import import_exchange

pytestmark = pytest.mark.asyncio


async def link(client: AsyncClient, source: tuple[str, str], target: tuple[str, str], **kw: Any):
    return await client.post(
        "/modeling/trace-links",
        json={
            "source": {"kind": source[0], "id": source[1]},
            "target": {"kind": target[0], "id": target[1]},
            "link_type": "traces_to",
        },
        headers=kw.get("headers") or hdrs(),
    )


async def get_link(client: AsyncClient, link_id: str, headers=None):
    return await client.get(f"/modeling/trace-links/{link_id}", headers=headers or hdrs())


async def patch(client: AsyncClient, mid: str, body: dict, headers=None):
    return await client.patch(f"/modeling/models/{mid}", json=body, headers=headers or hdrs())


async def manifest_of(client: AsyncClient, mid: str) -> dict[str, Any]:
    rev = await seal(client, mid)
    return (
        await ok(
            await client.get(
                f"/modeling/models/{mid}/revisions/{rev['revision_id']}", headers=hdrs()
            )
        )
    )["manifest"]


# --- 1. visibility change: owner only, never over foreign rows ------------------------


async def test_only_the_owner_may_make_a_model_private_and_only_over_own_rows(
    test_client: AsyncClient, test_session_factory
) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    mine = await ok(await element(test_client, mid))
    bobs = await ok(await element(test_client, mid, _headers=hdrs(BOB)))

    member = await patch(test_client, mid, {"visibility": "user"}, headers=hdrs(BOB))
    assert member.status_code == 403 and code(member) == "model_visibility_owner_only"

    head = await head_of(test_client, mid)
    foreign = await patch(test_client, mid, {"visibility": "user"})
    assert foreign.status_code == 409 and code(foreign) == "visibility_change_foreign_rows"
    assert await head_of(test_client, mid) == head
    # Nothing moved: Bob still reads his row and the model.
    await ok(await test_client.get(f"/architecture/elements/{mine['id']}", headers=hdrs(BOB)))
    assert (await ok(await test_client.get(f"/modeling/models/{mid}", headers=hdrs(BOB))))[
        "visibility"
    ] == "tenant"
    await ok(await test_client.delete(f"/architecture/elements/{bobs['id']}", headers=hdrs(BOB)))

    # A trace link Bob owns on the model blocks it too.
    req = await _requirement(test_session_factory)
    bob_link = await ok(
        await link(test_client, ("element", mine["id"]), ("requirement", req), headers=hdrs(BOB)),
        201,
    )
    foreign = await patch(test_client, mid, {"visibility": "user"})
    assert foreign.status_code == 409 and code(foreign) == "visibility_change_foreign_rows"
    await ok(await test_client.delete(f"/modeling/trace-links/{bob_link['id']}", headers=hdrs(BOB)))

    own_link = await ok(await link(test_client, ("element", mine["id"]), ("requirement", req)), 201)
    assert own_link["visibility"] == "tenant"
    resp = await patch(test_client, mid, {"visibility": "user"})
    moved = await ok(resp)
    assert moved["visibility"] == "user"
    # The link moved with its rows, and stays in the (now private) model.
    got = await ok(await get_link(test_client, own_link["id"]))
    assert got["visibility"] == "user" and got["model_id"] == mid
    assert (await get_link(test_client, own_link["id"], headers=hdrs(BOB))).status_code == 404
    assert (
        await test_client.get(f"/architecture/elements/{mine['id']}", headers=hdrs(BOB))
    ).status_code == 404
    assert [t["id"] for t in (await manifest_of(test_client, mid))["trace_links"]] == [
        own_link["id"]
    ]


async def test_a_foreign_tenants_row_blocks_a_model_becoming_private(
    test_client: AsyncClient, test_session_factory
) -> None:
    """The TENANT half of the clause: same subject, another tenant's row.

    ``subject_id`` equals the owner's, so only ``tenant_id != model.tenant_id`` can refuse
    it — the subject clause alone would let a foreign tenant's row become the owner's
    private content.
    """
    m = await new_model(test_client)
    mid = m["id"]
    await ok(await element(test_client, mid))
    async with test_session_factory() as s:
        foreign = ArchitectureElement(
            slug=f"probe-{uuid.uuid4().hex[:12]}",
            title="foreign row",
            layer="business",
            element_type="business_actor",
            tenant_id=OTHER_TENANT,
            tenant_name="review-fixes-other",
            subject_id=ALICE,
            visibility="tenant",
            model_id=uuid.UUID(mid),
        )
        s.add(foreign)
        await s.commit()
        foreign_id = foreign.id

    head = await head_of(test_client, mid)
    refused = await patch(test_client, mid, {"visibility": "user"})
    assert refused.status_code == 409 and code(refused) == "visibility_change_foreign_rows"
    assert "architecture_elements" in refused.json()["error"]["message"]
    assert await head_of(test_client, mid) == head
    assert (await ok(await test_client.get(f"/modeling/models/{mid}", headers=hdrs())))[
        "visibility"
    ] == "tenant"

    # Non-vacuous: with the foreign row gone the same call succeeds (an own-tenant row
    # is not what refused it).
    async with test_session_factory() as s:
        await s.execute(delete(ArchitectureElement).where(ArchitectureElement.id == foreign_id))
        await s.commit()
    assert (await ok(await patch(test_client, mid, {"visibility": "user"})))["visibility"] == "user"


# --- 2. a link narrower than its source model ------------------------------------------


async def test_a_private_link_on_a_shared_model_is_in_no_manifest(
    test_client: AsyncClient, test_session_factory
) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    shared = await ok(await element(test_client, mid))
    private_el = await ok(await element(test_client, None, _headers=hdrs(visibility="user")))
    req = await _requirement(test_session_factory)
    head = await head_of(test_client, mid)

    to_private = await ok(
        await link(test_client, ("element", shared["id"]), ("element", private_el["id"])), 201
    )
    chosen = await ok(
        await link(
            test_client,
            ("element", shared["id"]),
            ("requirement", req),
            headers=hdrs(visibility="user"),
        ),
        201,
    )
    for row in (to_private, chosen):
        assert row["visibility"] == "user" and row["model_id"] is None
    assert await head_of(test_client, mid) == head  # not the shared model's content
    assert (await manifest_of(test_client, mid))["trace_links"] == []

    # Kept consistent across visibility changes: private model → both rejoin it ...
    await ok(await patch(test_client, mid, {"visibility": "user"}))
    for row in (to_private, chosen):
        assert (await ok(await get_link(test_client, row["id"])))["model_id"] == mid
    assert {t["id"] for t in (await manifest_of(test_client, mid))["trace_links"]} == {
        to_private["id"],
        chosen["id"],
    }
    # ... shared again → the link to a private row stays private and leaves the model.
    await ok(await patch(test_client, mid, {"visibility": "tenant"}))
    back = await ok(await get_link(test_client, to_private["id"]))
    assert back["visibility"] == "user" and back["model_id"] is None
    widened = await ok(await get_link(test_client, chosen["id"]))
    assert widened["visibility"] == "tenant" and widened["model_id"] == mid
    assert [t["id"] for t in (await manifest_of(test_client, mid))["trace_links"]] == [chosen["id"]]


# --- 3. change-set apply / reject under lock ------------------------------------------


async def _noop_change_set(client: AsyncClient) -> tuple[str, str]:
    """A proposed set that changes nothing (same title): its apply never moves the head."""
    w = await populated(client)
    mid = w["model"]["id"]
    ops = [
        {
            "op": "update",
            "kind": "element",
            "id": w["actor"]["id"],
            "attributes": {"title": w["actor"]["title"]},
        }
    ]
    cs = await ok(await propose(client, mid, ops, await head_of(client, mid)), 201)
    assert cs["status"] == "proposed", cs
    return mid, cs["id"]


async def _hold_model_lock(session, mid: str) -> None:
    await session.execute(
        select(ModelingModel.id).where(ModelingModel.id == uuid.UUID(mid)).with_for_update()
    )


async def test_apply_never_overwrites_a_concurrent_reject(
    test_client: AsyncClient, test_session_factory
) -> None:
    mid, csid = await _noop_change_set(test_client)
    async with test_session_factory() as other:
        # Another request holds the model lock and rejects the set meanwhile.
        await _hold_model_lock(other, mid)
        apply = asyncio.create_task(
            test_client.post(f"/modeling/change-sets/{csid}/apply", headers=hdrs())
        )
        await asyncio.sleep(1.0)
        assert not apply.done(), "apply did not wait for the model lock"
        await other.execute(
            update(ModelingChangeSet)
            .where(ModelingChangeSet.id == uuid.UUID(csid))
            .values(
                status="rejected",
                reject_reason="no",
                rejected_by=BOB,
                rejected_at=datetime.now(UTC),
            )
        )
        await other.commit()
    resp = await asyncio.wait_for(apply, timeout=30)
    assert resp.status_code == 409 and code(resp) == "change_set_not_proposed", resp.text
    got = await ok(await test_client.get(f"/modeling/change-sets/{csid}", headers=hdrs()))
    assert got["status"] == "rejected"


async def test_reject_never_overwrites_a_concurrent_apply(
    test_client: AsyncClient, test_session_factory
) -> None:
    mid, csid = await _noop_change_set(test_client)
    async with test_session_factory() as other:
        await _hold_model_lock(other, mid)
        await other.execute(
            update(ModelingChangeSet)
            .where(ModelingChangeSet.id == uuid.UUID(csid))
            .values(status="applied", applied_by=BOB, applied_at=datetime.now(UTC))
        )
        reject = asyncio.create_task(
            test_client.post(
                f"/modeling/change-sets/{csid}/reject", json={"reason": "x"}, headers=hdrs()
            )
        )
        await asyncio.sleep(1.0)
        assert not reject.done(), "reject did not wait for the model lock"
        await other.commit()
    resp = await asyncio.wait_for(reject, timeout=30)
    assert resp.status_code == 409 and code(resp) == "change_set_not_open", resp.text
    got = await ok(await test_client.get(f"/modeling/change-sets/{csid}", headers=hdrs()))
    assert got["status"] == "applied"


async def test_two_concurrent_applies_of_a_no_op_set_one_wins(
    test_client: AsyncClient, test_session_factory
) -> None:
    mid, csid = await _noop_change_set(test_client)
    async with test_session_factory() as other:
        await _hold_model_lock(other, mid)
        applies = [
            asyncio.create_task(
                test_client.post(f"/modeling/change-sets/{csid}/apply", headers=hdrs())
            )
            for _ in range(2)
        ]
        await asyncio.sleep(1.0)
        assert not any(a.done() for a in applies)
        await other.commit()
    results = await asyncio.wait_for(asyncio.gather(*applies), timeout=30)
    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]


# --- 4. duplicate edges ----------------------------------------------------------------


def _pair_ops() -> list[dict[str, Any]]:
    return [
        {
            "op": "create",
            "kind": "element",
            "id": "a",
            "attributes": {"title": "Actor", "element_type": "business_actor"},
        },
        {
            "op": "create",
            "kind": "element",
            "id": "r",
            "attributes": {"title": "Role", "element_type": "business_role"},
        },
        {
            "op": "create",
            "kind": "relationship",
            "id": "ar",
            "attributes": {"relationship_type": "assignment", "source_id": "a", "target_id": "r"},
        },
    ]


def _dup_op(a: str, r: str) -> list[dict[str, Any]]:
    return [
        {
            "op": "create",
            "kind": "relationship",
            "id": "dup",
            "attributes": {"relationship_type": "assignment", "source_id": a, "target_id": r},
        }
    ]


async def _project_model_with_edge(client: AsyncClient) -> tuple[str, dict[str, str]]:
    m = await new_model(client, project_id=f"p-{uuid.uuid4().hex[:6]}")
    mid = m["id"]
    cs = await ok(await propose(client, mid, _pair_ops(), 0), 201)
    applied = await ok(await client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs()))
    return mid, applied["id_map"]


async def test_a_duplicate_edge_is_an_invalid_change_set(test_client: AsyncClient) -> None:
    mid, ids = await _project_model_with_edge(test_client)
    head = await head_of(test_client, mid)
    cs = await ok(await propose(test_client, mid, _dup_op(ids["a"], ids["r"]), head), 201)
    assert cs["status"] == "invalid"
    assert [f["rule_id"] for f in cs["blocking_findings"]] == ["CS-OP-002"]
    assert cs["blocking_findings"][0]["evidence"]["duplicate_of"] == ids["ar"]
    assert "CS-OP-002" in {f["rule_id"] for f in cs["findings"]}
    # Within one set, too.
    m2 = await new_model(test_client, project_id=f"p-{uuid.uuid4().hex[:6]}")
    twice = await ok(
        await propose(test_client, m2["id"], [*_pair_ops(), *_dup_op("a", "r")], 0), 201
    )
    assert twice["status"] == "invalid"
    assert {f["rule_id"] for f in twice["blocking_findings"]} == {"CS-OP-002"}
    # Without a project the database allows it; no CS-OP-002.
    m3 = await new_model(test_client)
    free = await ok(
        await propose(test_client, m3["id"], [*_pair_ops(), *_dup_op("a", "r")], 0), 201
    )
    assert "CS-OP-002" not in {f["rule_id"] for f in free["blocking_findings"]}


async def test_a_constraint_violation_at_apply_is_422_not_500(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid, ids = await _project_model_with_edge(test_client)
    head = await head_of(test_client, mid)
    # Simulate an evaluation that cannot see the conflict (e.g. a row written since).
    monkeypatch.setattr(cs_svc, "_check_duplicate_edge", lambda *a, **k: None)
    cs = await ok(await propose(test_client, mid, _dup_op(ids["a"], ids["r"]), head), 201)
    assert cs["status"] == "proposed"
    resp = await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    findings = integrity(resp)
    assert [f["rule_id"] for f in findings] == ["CS-OP-003"]
    assert findings[0]["evidence"]["constraint"] == "uq_arch_rels_tenant_edge"
    assert findings[0]["path"] == "/ops/0"
    got = await ok(await test_client.get(f"/modeling/change-sets/{cs['id']}", headers=hdrs()))
    assert got["status"] == "proposed" and await head_of(test_client, mid) == head


# --- 5 + 9. cross-model prune bumps; ETag is the target model's ------------------------


async def test_deleting_a_link_end_bumps_the_links_model_and_etag_is_the_targets(
    test_client: AsyncClient,
) -> None:
    a = await new_model(test_client, title="A")  # uuid7: A's id sorts before B's
    b = await new_model(test_client, title="B")
    a1 = await ok(await element(test_client, a["id"]))
    b1 = await ok(await element(test_client, b["id"]))
    for _ in range(3):  # B's head far from A's
        await ok(await element(test_client, b["id"]))
    cross = await ok(await link(test_client, ("element", b1["id"]), ("element", a1["id"])), 201)
    assert cross["model_id"] == b["id"]
    head_a, head_b = await head_of(test_client, a["id"]), await head_of(test_client, b["id"])
    assert head_a != head_b + 1

    resp = await test_client.delete(
        f"/architecture/elements/{a1['id']}", headers=hdrs(if_match=head_a)
    )
    await ok(resp)
    assert (await get_link(test_client, cross["id"])).status_code == 404
    assert await head_of(test_client, b["id"]) == head_b + 1  # finding 5
    assert etag(resp) == head_a + 1 == await head_of(test_client, a["id"])  # finding 9


# --- 6. header PATCH bumps the head -----------------------------------------------------


async def test_title_and_description_patch_bump_the_head(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    first = await manifest_of(test_client, mid)
    resp = await patch(test_client, mid, {"title": "Renamed"}, headers=hdrs(if_match=0))
    assert (await ok(resp))["head"] == 1 and etag(resp) == 1
    resp = await patch(test_client, mid, {"description": "d"}, headers=hdrs(if_match=1))
    assert (await ok(resp))["head"] == 2 and etag(resp) == 2
    stale = await patch(test_client, mid, {"description": "e"}, headers=hdrs(if_match=1))
    assert stale.status_code == 412 and code(stale) == "stale_head"
    same = await patch(test_client, mid, {"title": "Renamed"})
    assert (await ok(same))["head"] == 2  # no change, no bump
    second = await manifest_of(test_client, mid)
    assert second["model"]["title"] == "Renamed" and first["model"]["title"] == "M"
    revs = await ok(await test_client.get(f"/modeling/models/{mid}/revisions", headers=hdrs()))
    assert [r["head"] for r in revs] == [2, 0]


# --- 7. If-Match never leaks a private head ---------------------------------------------


async def test_if_match_on_another_subjects_private_row_is_404(test_client: AsyncClient) -> None:
    priv = hdrs(visibility="user")
    el = await ok(await element(test_client, None, _headers=priv))
    el2 = await ok(await element(test_client, None, "business_role", _headers=priv))
    rel = await ok(await relationship(test_client, el, "assignment", el2, headers=priv))
    v = await ok(
        await test_client.post(
            "/architecture/views", json={"title": "PV", "viewpoint": "layered"}, headers=priv
        )
    )
    bob = hdrs(BOB, if_match=999)
    cases = [
        (
            "PUT",
            f"/architecture/elements/{el['id']}",
            {"title": "x", "element_type": "business_actor"},
        ),
        ("DELETE", f"/architecture/elements/{el['id']}", None),
        ("PUT", f"/architecture/views/{v['id']}", {"title": "x", "viewpoint": "layered"}),
        ("DELETE", f"/architecture/views/{v['id']}", None),
        ("PUT", f"/architecture/relationships/{rel['id']}", {"name": "x"}),
        ("DELETE", f"/architecture/relationships/{rel['id']}", None),
        (
            "POST",
            f"/architecture/elements/{el['id']}/bindings",
            {"binding_kind": "coding_component", "container": "c", "component_id": "k"},
        ),
    ]
    for method, url, body in cases:
        resp = await test_client.request(method, url, json=body, headers=bob)
        assert resp.status_code == 404, (method, url, resp.status_code, resp.text)
        assert "ETag" not in resp.headers
    # A new edge whose source is Bob-invisible: refused, never 412 with the head.
    other = await ok(await element(test_client, None, _headers=hdrs(BOB)))
    resp = await test_client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": "architecture_elements",
            "source_id": el["id"],
            "target_entity_type": "architecture_elements",
            "target_id": other["id"],
            "relationship_type": "association",
        },
        headers=bob,
    )
    assert resp.status_code != 412 and "ETag" not in resp.headers, resp.text
    # The owner still gets the 412 with her head.
    own = await test_client.put(
        f"/architecture/elements/{el['id']}",
        json={"title": "x", "element_type": "business_actor"},
        headers=hdrs(visibility="user", if_match=999),
    )
    assert own.status_code == 412


# --- 8. force delete cascades through cross-domain edges --------------------------------


async def test_model_delete_removes_edges_and_links_on_deleted_cross_domain_edges(
    test_client: AsyncClient, test_session_factory
) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    el = await ok(await element(test_client, mid))
    req = await _requirement(test_session_factory)
    req2 = await _requirement(test_session_factory)
    async with test_session_factory() as s:

        def edge(src_type: str, src: str, tgt_type: str, tgt: str) -> ArchitectureRelationship:
            return ArchitectureRelationship(
                slug=f"x-{uuid.uuid4().hex[:8]}",
                tenant_id=TENANT,
                tenant_name="t",
                subject_id=ALICE,
                visibility="tenant",
                source_entity_type=src_type,
                source_id=uuid.UUID(src),
                target_entity_type=tgt_type,
                target_id=uuid.UUID(tgt),
                relationship_type="association",
            )

        cross = edge("requirements", req, "architecture_elements", el["id"])
        s.add(cross)
        await s.flush()
        on_cross = edge("requirements", req2, "architecture_relationships", str(cross.id))
        s.add(on_cross)
        await s.commit()
        cross_id, on_cross_id = cross.id, on_cross.id
        assert cross.model_id is None and on_cross.model_id is None
    trace = await ok(
        await link(test_client, ("relationship", str(cross_id)), ("requirement", req2)), 201
    )
    await seal(test_client, mid)
    await ok(
        await test_client.delete(
            f"/modeling/models/{mid}", params={"force": "true"}, headers=hdrs()
        )
    )
    async with test_session_factory() as s:
        left = (
            await s.execute(
                select(ArchitectureRelationship.id).where(
                    ArchitectureRelationship.id.in_([cross_id, on_cross_id])
                )
            )
        ).all()
        links = (
            await s.execute(
                select(ModelingTraceLink.id).where(ModelingTraceLink.id == uuid.UUID(trace["id"]))
            )
        ).all()
    assert left == [] and links == []


# --- 10. PATCH ignores approval fields ---------------------------------------------------


async def test_patch_ignores_approval_fields_on_architecture_rows(
    test_client: AsyncClient, test_session_factory
) -> None:
    el = await ok(await element(test_client, None))
    async with test_session_factory() as s:
        out = await architecture_element_service.patch(
            s,
            uuid.UUID(el["id"]),
            {
                "title": "Patched",
                "approved_by": "mallory",
                "approved_at": "2026-01-01T00:00:00Z",
                "published_at": "2026-01-01T00:00:00Z",
            },
            ArchitectureElementIn,
            TENANT,
            ALICE,
        )
        assert out.title == "Patched"
        assert (out.approved_by, out.approved_at, out.published_at) == (None, None, None)
        only = await architecture_element_service.patch(
            s, uuid.UUID(el["id"]), {"approved_by": "mallory"}, ArchitectureElementIn, TENANT, ALICE
        )
        assert only.approved_by is None and only.title == "Patched"
    async with test_session_factory() as s:
        row = await s.get(ArchitectureElement, uuid.UUID(el["id"]))
        assert row is not None and row.approved_by is None


# --- 11. exchange colour out of range ---------------------------------------------------


@pytest.mark.parametrize("r,g,b", [(300, 0, 0), (0, -1, 0), (0, 0, 256)])
async def test_out_of_range_colour_is_a_value_invalid_loss(r: int, g: int, b: int) -> None:
    ns = "http://www.opengroup.org/xsd/archimate/3.0/"
    xsi = "http://www.w3.org/2001/XMLSchema-instance"
    data = (
        f'<model xmlns="{ns}" xmlns:xsi="{xsi}" identifier="m"><name>M</name>'
        '<elements><element identifier="a" xsi:type="BusinessActor"><name>A</name></element>'
        "</elements><views><diagrams>"
        '<view identifier="v" xsi:type="Diagram"><name>V</name>'
        '<node identifier="na" elementRef="a" xsi:type="Element" x="0" y="0" w="10" h="10">'
        f'<style><fillColor r="{r}" g="{g}" b="{b}" a="50"/>'
        '<lineColor r="1" g="2" b="3" a="101"/></style></node>'
        "</view></diagrams></views></model>"
    ).encode()
    result = import_exchange(data)
    invalid = [x for x in result.losses if x.code == "VALUE-INVALID"]
    assert len(invalid) == 2, result.losses
    assert "0-255" in invalid[0].message and "0-100" in invalid[1].message
    style = result.snapshot.views[0].objects[0].style
    assert style == {"line_color": "#010203"}


# --- 12. a private model is published by its owner alone ---------------------------------


async def test_private_model_owner_reviews_and_publishes_alone(
    test_client: AsyncClient,
) -> None:
    m = await new_model(test_client, visibility="user")
    mid = m["id"]
    await ok(await element(test_client, mid, _headers=hdrs(visibility="user")))
    rev = await seal(test_client, mid)
    await ok(await run(test_client, mid, {"revision_id": rev["revision_id"]}), 201)
    approved = await ok(await review(test_client, mid, rev["revision_id"], "approve"), 201)
    resp = await test_client.post(
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/publish", headers=hdrs()
    )
    pub = await ok(resp, 201)
    assert pub["review_id"] == approved["id"]
