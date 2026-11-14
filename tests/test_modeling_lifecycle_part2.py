"""Modeling lifecycle part 2 over HTTP, against real Postgres.

Persisted validation runs and findings (spec §7.3), trace links (§7.4), change sets
(§9.5) and review/publication (plan §7.3): tenant and private-subject isolation, run
reproducibility on a revision hash, clean/incomplete semantics, findings filters and
order, trace-link resolution refusals, change-set apply/stale/reject/atomicity, the
publication gate and actors taken from the principal.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from models.architecture import ArchitectureElement, ArchitectureRelationship
from models.requirements import Requirement
from services import change_sets as cs_svc
from settings import get_settings
from tests.tenants import T
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
)

pytestmark = pytest.mark.asyncio


async def run(client: AsyncClient, model_id: str, body: dict | None = None, headers=None):
    return await client.post(
        f"/modeling/models/{model_id}/validation-runs", json=body, headers=headers or hdrs()
    )


async def seal(client: AsyncClient, model_id: str, headers=None) -> dict[str, Any]:
    return await ok(
        await client.post(f"/modeling/models/{model_id}/revisions", headers=headers or hdrs()), 201
    )


# --- validation runs ------------------------------------------------------------


async def test_draft_run_is_complete_clean_and_hashes_like_a_seal(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    r = await ok(await run(test_client, mid), 201)
    assert r["target"] == "draft_head" and r["revision_id"] is None
    assert r["status"] == "complete" and r["clean"] is True and r["counts"]["error"] == 0
    assert r["requested_by"] == ALICE
    assert r["head"] == await head_of(test_client, mid)
    assert r["examined"] == r["expected"] and r["examined"]["element"] == 2
    sealed = await seal(test_client, mid)
    assert sealed["content_hash"] == r["content_hash"]
    model = await ok(await test_client.get(f"/modeling/models/{mid}", headers=hdrs()))
    assert model["latest_validation_run"]["run_id"] == r["run_id"]
    assert model["latest_validation_run"]["clean"] is True
    got = await ok(
        await test_client.get(f"/modeling/validation-runs/{r['run_id']}", headers=hdrs())
    )
    assert got == r


async def test_revision_run_is_reproducible_after_draft_edits(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    rev = await seal(test_client, mid)
    first = await ok(await run(test_client, mid, {"revision_id": rev["revision_id"]}), 201)
    f1 = await ok(
        await test_client.get(
            f"/modeling/validation-runs/{first['run_id']}/findings", headers=hdrs()
        )
    )
    # Change the draft a lot.
    await ok(await test_client.delete(f"/architecture/elements/{w['role']['id']}", headers=hdrs()))
    await ok(await element(test_client, mid, "business_actor", title="Dup"))
    await ok(await element(test_client, mid, "business_actor", title="Dup"))
    second = await ok(await run(test_client, mid, {"revision_id": rev["revision_id"]}), 201)
    f2 = await ok(
        await test_client.get(
            f"/modeling/validation-runs/{second['run_id']}/findings", headers=hdrs()
        )
    )
    assert second["content_hash"] == first["content_hash"] == rev["content_hash"]
    assert second["target"] == "revision" and second["head"] == rev["head"]
    keep = ("status", "clean", "counts", "examined", "enabled_rules", "ruleset_id")
    assert {k: second[k] for k in keep} == {k: first[k] for k in keep}
    assert f2 == f1
    draft = await ok(await run(test_client, mid), 201)
    assert draft["content_hash"] != rev["content_hash"]
    assert draft["counts"]["warning"] > first["counts"]["warning"]  # AM-DUP-001, AM-UNU-001


async def test_disabling_a_required_rule_is_never_clean(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    off = await ok(await run(test_client, mid, {"disable": ["AM-REF-001"]}), 201)
    assert off["status"] == "incomplete" and off["clean"] is False
    assert "AM-REF-001" not in off["enabled_rules"]
    only = await ok(await run(test_client, mid, {"rules": ["AM-REL-001"]}), 201)
    assert only["status"] == "incomplete" and only["clean"] is False
    optional = await ok(await run(test_client, mid, {"disable": ["AM-DUP-001"]}), 201)
    assert optional["status"] == "complete" and optional["clean"] is True
    listed = await test_client.get(
        f"/modeling/models/{mid}/validation-runs", params={"clean": "false"}, headers=hdrs()
    )
    assert {r["run_id"] for r in await ok(listed)} == {off["run_id"], only["run_id"]}
    assert listed.headers["X-Total-Count"] == "2"
    by_target = await ok(
        await test_client.get(
            f"/modeling/models/{mid}/validation-runs", params={"target": "revision"}, headers=hdrs()
        )
    )
    assert by_target == []


async def test_run_refusals(test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    bad = await run(test_client, mid, {"rules": ["AM-NOPE-999"]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_rule"
    bad = await run(test_client, mid, {"ruleset_id": "uml-2.5.1-p1"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_ruleset"
    rev = await seal(test_client, mid)
    # Above the sync bound the draft head is refused; a sealed revision is refused only
    # above the background bound (below it: 202, tests/test_modeling_background_validation.py).
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 2)
    monkeypatch.setattr(get_settings(), "model_async_max_concepts", 2)
    for body in (None, {"revision_id": rev["revision_id"]}):
        resp = await run(test_client, mid, body)
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"
    missing = await run(test_client, mid, {"revision_id": str(uuid.uuid4())})
    assert missing.status_code == 404


async def test_findings_are_ordered_and_filtered(
    test_client: AsyncClient, test_session_factory
) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    data = await ok(await element(test_client, mid, "data_object", title="D"))
    proc = await ok(await element(test_client, mid, "business_process", title="P"))
    # A stored edge that predates the matrix gate (2,548 such edges existed in prod):
    # written straight to the table, which only the validator — not the gate — reports.
    async with test_session_factory() as s:
        s.add(
            ArchitectureRelationship(
                slug=f"legacy-{uuid.uuid4().hex[:6]}",
                tenant_id=TENANT,
                subject_id=ALICE,
                visibility="tenant",
                source_entity_type="architecture_elements",
                source_id=uuid.UUID(data["id"]),
                target_entity_type="architecture_elements",
                target_id=uuid.UUID(proc["id"]),
                relationship_type="flow",
            )
        )
        await s.commit()
    r = await ok(await run(test_client, mid), 201)
    assert r["counts"]["error"] >= 1 and r["clean"] is False and r["status"] == "complete"
    url = f"/modeling/validation-runs/{r['run_id']}/findings"
    resp = await test_client.get(url, headers=hdrs())
    rows = await ok(resp)
    assert resp.headers["X-Total-Count"] == str(len(rows))
    rank = {"error": 0, "warning": 1, "advice": 2}
    keys = [
        (rank[f["severity"]], f["rule_id"], f["targets"][0]["id"] if f["targets"] else "")
        for f in rows
    ]
    assert keys == sorted(keys)
    assert rows[0]["severity"] == "error" and rows[0]["rule_id"] == "AM-REL-001"
    warnings = await ok(await test_client.get(url, params={"severity": "warning"}, headers=hdrs()))
    assert warnings and {f["severity"] for f in warnings} == {"warning"}
    unused = await ok(await test_client.get(url, params={"rule_id": "AM-UNU-001"}, headers=hdrs()))
    assert {f["targets"][0]["id"] for f in unused} == {data["id"], proc["id"]}
    about_d = await ok(await test_client.get(url, params={"target_id": data["id"]}, headers=hdrs()))
    assert about_d and all(data["id"] in {t["id"] for t in f["targets"]} for f in about_d)
    assert len(about_d) < len(rows)
    page = await ok(await test_client.get(url, params={"limit": 1, "offset": 1}, headers=hdrs()))
    assert page == rows[1:2]


async def test_runs_are_isolated_by_tenant_and_private_subject(test_client: AsyncClient) -> None:
    private = await new_model(test_client, visibility="user")
    r = await ok(await run(test_client, private["id"]), 201)
    for headers in (hdrs(BOB), hdrs(tenant=OTHER_TENANT)):
        assert (
            await test_client.get(f"/modeling/validation-runs/{r['run_id']}", headers=headers)
        ).status_code == 404
        assert (
            await test_client.get(
                f"/modeling/validation-runs/{r['run_id']}/findings", headers=headers
            )
        ).status_code == 404
        assert (await run(test_client, private["id"], headers=headers)).status_code == 404
        assert (
            await test_client.get(
                f"/modeling/models/{private['id']}/validation-runs", headers=headers
            )
        ).status_code == 404


async def test_uml_model_runs_the_uml_ruleset(test_client: AsyncClient) -> None:
    m = await new_model(test_client, language="uml", language_version="2.5.1-p1")
    r = await ok(await run(test_client, m["id"]), 201)
    assert r["ruleset_id"].startswith("uml-") and r["status"] == "complete" and r["clean"]


# --- trace links ----------------------------------------------------------------


async def _requirement(
    factory, tenant: str = TENANT, *, visibility: str = "tenant", subject: str = ALICE
) -> str:
    async with factory() as s:
        req = Requirement(
            slug=f"req-{uuid.uuid4().hex[:8]}",
            title="R",
            description="r",
            tenant_id=tenant,
            subject_id=subject,
            visibility=visibility,
            links={},
            acceptance=[],
            effort=1.0,
        )
        s.add(req)
        await s.commit()
        return str(req.id)


async def test_trace_links_resolve_both_ends(
    test_client: AsyncClient, test_session_factory
) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    req = await _requirement(test_session_factory)
    head = await head_of(test_client, mid)
    resp = await test_client.post(
        "/modeling/trace-links",
        json={
            "source": {"kind": "element", "id": w["actor"]["id"]},
            "target": {"kind": "requirement", "id": req},
            "link_type": "satisfies",
            "note": "n",
        },
        headers=hdrs(),
    )
    link = await ok(resp, 201)
    assert (
        link["model_id"] == mid and link["created_by"] == ALICE and link["visibility"] == "tenant"
    )
    assert etag(resp) == head + 1 == await head_of(test_client, mid)
    code = await ok(
        await test_client.post(
            "/modeling/trace-links",
            json={
                "source": {"kind": "requirement", "id": req},
                "target_code": {"repo": "org/repo", "commit": "abc123", "symbol": "pkg.mod:fn"},
                "link_type": "implements",
            },
            headers=hdrs(),
        ),
        201,
    )
    assert code["model_id"] is None and code["target_code"]["commit"] == "abc123"

    def body(target: dict) -> dict:
        return {
            "source": {"kind": "element", "id": w["actor"]["id"]},
            "target": target,
            "link_type": "refines",
        }

    bob_private = await ok(await element(test_client, None, _headers=hdrs(BOB, visibility="user")))
    other = await ok(await element(test_client, None, _headers=hdrs(tenant=OTHER_TENANT)))
    for target in (
        {"kind": "element", "id": str(uuid.uuid4())},
        {"kind": "element", "id": bob_private["id"]},
        {"kind": "element", "id": other["id"]},
        {"kind": "requirement", "id": w["actor"]["id"]},  # wrong kind for the id
        {"kind": "uml_element", "id": str(uuid.uuid4())},
    ):
        findings = integrity(
            await test_client.post("/modeling/trace-links", json=body(target), headers=hdrs())
        )
        assert findings[0]["rule_id"] == "TRC-REF-001" and findings[0]["path"] == "/target"
    both = await test_client.post(
        "/modeling/trace-links",
        json={
            **body({"kind": "requirement", "id": req}),
            "target_code": {"repo": "r", "commit": "c", "symbol": "s"},
        },
        headers=hdrs(),
    )
    assert both.status_code == 422
    # created_by is the principal; the body cannot carry it.
    forged = await test_client.post(
        "/modeling/trace-links",
        json={**body({"kind": "requirement", "id": req}), "created_by": "mallory"},
        headers=hdrs(),
    )
    assert forged.status_code == 422

    listed = await test_client.get(
        "/modeling/trace-links", params={"source_id": w["actor"]["id"]}, headers=hdrs()
    )
    assert [x["id"] for x in await ok(listed)] == [link["id"]]
    assert await ok(
        await test_client.get(
            "/modeling/trace-links", params={"link_type": "implements"}, headers=hdrs()
        )
    ) == [code]
    assert (
        await ok(
            await test_client.get(
                "/modeling/trace-links", params={"target_id": req}, headers=hdrs()
            )
        )
    )[0]["id"] == link["id"]
    assert (
        await ok(await test_client.get("/modeling/trace-links", headers=hdrs(tenant=OTHER_TENANT)))
        == []
    )

    # The link is part of the model's manifest.
    rev = await seal(test_client, mid)
    manifest = (
        await ok(
            await test_client.get(
                f"/modeling/models/{mid}/revisions/{rev['revision_id']}", headers=hdrs()
            )
        )
    )["manifest"]
    assert [t["id"] for t in manifest["trace_links"]] == [link["id"]]

    # Deleting an end deletes the link; the requirement-sourced link survives.
    await ok(await test_client.delete(f"/architecture/elements/{w['actor']['id']}", headers=hdrs()))
    assert (
        await test_client.get(f"/modeling/trace-links/{link['id']}", headers=hdrs())
    ).status_code == 404
    assert (await ok(await test_client.get(f"/modeling/trace-links/{code['id']}", headers=hdrs())))[
        "id"
    ] == code["id"]
    deleted = await test_client.delete(f"/modeling/trace-links/{code['id']}", headers=hdrs())
    assert deleted.status_code == 200 and "ETag" not in deleted.headers


async def test_a_link_to_a_private_row_is_private(test_client: AsyncClient) -> None:
    mine = await ok(await element(test_client, None, _headers=hdrs(visibility="user")))
    shared = await ok(await element(test_client, None))
    link = await ok(
        await test_client.post(
            "/modeling/trace-links",
            json={
                "source": {"kind": "element", "id": shared["id"]},
                "target": {"kind": "element", "id": mine["id"]},
                "link_type": "depends_on",
            },
            headers=hdrs(),
        ),
        201,
    )
    # Narrower than the shared model of its source: in no model (and no manifest).
    assert link["visibility"] == "user" and link["model_id"] is None
    assert (
        await test_client.get(f"/modeling/trace-links/{link['id']}", headers=hdrs(BOB))
    ).status_code == 404
    assert await ok(await test_client.get("/modeling/trace-links", headers=hdrs(BOB))) == []
    assert (
        await test_client.delete(f"/modeling/trace-links/{link['id']}", headers=hdrs(BOB))
    ).status_code == 404


# --- change sets ----------------------------------------------------------------


def _create_ops() -> list[dict[str, Any]]:
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
        {
            "op": "create",
            "kind": "view",
            "id": "v",
            "attributes": {"title": "V", "viewpoint": "organization"},
        },
        {
            "op": "create",
            "kind": "view_object",
            "id": "oa",
            "attributes": {
                "view_id": "v",
                "concept_ref": "a",
                "bounds": {"x": 1, "y": 2, "w": 3, "h": 4},
            },
        },
        {
            "op": "create",
            "kind": "view_object",
            "id": "or",
            "attributes": {"view_id": "v", "concept_ref": "r"},
        },
        {
            "op": "create",
            "kind": "view_connection",
            "id": "c",
            "attributes": {
                "view_id": "v",
                "relationship_ref": "ar",
                "source_object_id": "oa",
                "target_object_id": "or",
            },
        },
    ]


async def propose(client: AsyncClient, mid: str, ops: list[dict], base_head: int, headers=None):
    return await client.post(
        f"/modeling/models/{mid}/change-sets",
        json={
            "ops": ops,
            "rationale": "because",
            "evaluator_ref": "suggester@1",
            "base_head": base_head,
        },
        headers=headers or hdrs(),
    )


async def test_change_set_propose_and_apply(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    cs = await ok(await propose(test_client, mid, _create_ops(), 0), 201)
    assert (
        cs["status"] == "proposed" and cs["blocking_findings"] == [] and cs["created_by"] == ALICE
    )
    assert await head_of(test_client, mid) == 0  # nothing applied by proposing
    got = await ok(await test_client.get(f"/modeling/change-sets/{cs['id']}", headers=hdrs()))
    assert got == cs

    applied_resp = await test_client.post(
        f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs(BOB)
    )
    applied = await ok(applied_resp)
    assert applied["status"] == "applied" and applied["applied_by"] == BOB
    head = await head_of(test_client, mid)
    assert applied["applied_head"] == head == etag(applied_resp) == 1  # one transaction, one bump
    ids = applied["id_map"]
    assert set(ids) == {"a", "r", "ar", "v", "oa", "or", "c"}
    actor = await ok(await test_client.get(f"/architecture/elements/{ids['a']}", headers=hdrs()))
    assert actor["model_id"] == mid and actor["title"] == "Actor"
    rel = await ok(
        await test_client.get(f"/architecture/relationships/{ids['ar']}", headers=hdrs())
    )
    assert (rel["source_id"], rel["target_id"], rel["model_id"]) == (ids["a"], ids["r"], mid)
    conns = await ok(
        await test_client.get(f"/architecture/views/{ids['v']}/connections", headers=hdrs())
    )
    assert [c["id"] for c in conns] == [ids["c"]]
    again = await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    assert again.status_code == 409 and again.json()["error"]["code"] == "change_set_not_proposed"

    # Update + delete ops against the applied rows.
    ops = [
        {"op": "update", "kind": "element", "id": ids["a"], "attributes": {"title": "Actor 2"}},
        {"op": "delete", "kind": "view_connection", "id": ids["c"]},
        {
            "op": "update",
            "kind": "view_object",
            "id": ids["oa"],
            "attributes": {"bounds": {"x": 9, "y": 9, "w": 9, "h": 9}},
        },
        {"op": "delete", "kind": "relationship", "id": ids["ar"]},
    ]
    cs2 = await ok(await propose(test_client, mid, ops, head), 201)
    assert cs2["status"] == "proposed", cs2["blocking_findings"]
    await ok(await test_client.post(f"/modeling/change-sets/{cs2['id']}/apply", headers=hdrs()))
    assert (await ok(await test_client.get(f"/architecture/elements/{ids['a']}", headers=hdrs())))[
        "title"
    ] == "Actor 2"
    assert (
        await test_client.get(f"/architecture/relationships/{ids['ar']}", headers=hdrs())
    ).status_code == 404
    objs = {
        o["id"]: o
        for o in await ok(
            await test_client.get(f"/architecture/views/{ids['v']}/objects", headers=hdrs())
        )
    }
    assert objs[ids["oa"]]["bounds"] == {"x": 9, "y": 9, "w": 9, "h": 9}
    listed = await test_client.get(
        f"/modeling/models/{mid}/change-sets", params={"status": "applied"}, headers=hdrs()
    )
    assert {c["id"] for c in await ok(listed)} == {cs["id"], cs2["id"]}


async def test_invalid_change_sets_are_stored_with_findings(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    illegal = [
        {
            "op": "create",
            "kind": "element",
            "id": "d",
            "attributes": {"title": "D", "element_type": "data_object"},
        },
        {
            "op": "create",
            "kind": "element",
            "id": "p",
            "attributes": {"title": "P", "element_type": "business_process"},
        },
        {
            "op": "create",
            "kind": "relationship",
            "attributes": {"relationship_type": "flow", "source_id": "d", "target_id": "p"},
        },
    ]
    cs = await ok(await propose(test_client, mid, illegal, 0), 201)
    assert cs["status"] == "invalid"
    assert {f["rule_id"] for f in cs["blocking_findings"]} == {"AM-REL-001"}
    assert "AM-REL-001" in {f["rule_id"] for f in cs["findings"]}
    bad_ops = [
        {"op": "update", "kind": "element", "id": str(uuid.uuid4()), "attributes": {"title": "x"}},
        {
            "op": "create",
            "kind": "element",
            "attributes": {"title": "x", "element_type": "business_widget"},
        },
        {
            "op": "create",
            "kind": "element",
            "attributes": {"title": "x", "element_type": "node", "colour": "red"},
        },
        {
            "op": "create",
            "kind": "element",
            "attributes": {"title": "x", "element_type": "business_actor", "layer": "technology"},
        },
    ]
    cs = await ok(await propose(test_client, mid, bad_ops, 0), 201)
    assert cs["status"] == "invalid"
    by_path = {f["path"].split("/attributes")[0]: f["rule_id"] for f in cs["blocking_findings"]}
    assert by_path["/ops/0"] == "CS-OP-001" and by_path["/ops/1"] == "CS-OP-001"
    assert by_path["/ops/2"] == "CS-OP-001" and by_path["/ops/3"] == "AM-TYP-001"
    refused = await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    assert (
        refused.status_code == 409 and refused.json()["error"]["code"] == "change_set_not_proposed"
    )
    stale = await propose(test_client, mid, _create_ops(), 5)
    assert stale.status_code == 412 and stale.json()["error"]["head"] == 0
    # A UML model takes change sets of UML kinds only: ArchiMate ops are CS-OP-001 there
    # (tests/test_uml_storage.py covers UML change sets).
    uml = await new_model(test_client, language="uml", language_version="2.5.1-p1")
    other = await ok(await propose(test_client, uml["id"], _create_ops(), 0), 201)
    assert other["status"] == "invalid"
    assert {f["rule_id"] for f in other["blocking_findings"]} == {"CS-OP-001"}
    assert all("not a UML change kind" in f["message"] for f in other["blocking_findings"])


async def test_apply_on_a_moved_head_is_412_and_writes_nothing(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    cs = await ok(await propose(test_client, mid, _create_ops(), 0), 201)
    await ok(await element(test_client, mid))  # head 0 -> 1
    resp = await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    assert resp.status_code == 412 and resp.json()["error"]["code"] == "stale_head"
    assert resp.json()["error"]["head"] == 1
    assert await head_of(test_client, mid) == 1
    still = await ok(await test_client.get(f"/modeling/change-sets/{cs['id']}", headers=hdrs()))
    assert still["status"] == "proposed" and still["applied_by"] is None
    els = await ok(await test_client.get("/architecture/elements", headers=hdrs()))
    assert {e["title"] for e in els} & {"Actor", "Role"} == set()


async def test_apply_is_atomic(test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fault injection: the 6th op fails; ops 1-5 (elements, edge, view, object) roll back."""
    m = await new_model(test_client)
    mid = m["id"]
    cs = await ok(await propose(test_client, mid, _create_ops(), 0), 201)
    calls = {"n": 0}
    real = cs_svc.occ.create_object

    async def flaky(*args: Any, **kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 2:
            raise HTTPException(status_code=503, detail="injected fault")
        return await real(*args, **kwargs)

    monkeypatch.setattr(cs_svc.occ, "create_object", flaky)
    resp = await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    assert resp.status_code == 503 and calls["n"] == 2
    assert await head_of(test_client, mid) == 0
    assert await ok(await test_client.get("/architecture/elements", headers=hdrs())) == []
    assert await ok(await test_client.get("/architecture/views", headers=hdrs())) == []
    still = await ok(await test_client.get(f"/modeling/change-sets/{cs['id']}", headers=hdrs()))
    assert still["status"] == "proposed"
    monkeypatch.setattr(cs_svc.occ, "create_object", real)
    done = await ok(
        await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    )
    assert done["status"] == "applied"


async def test_reject_and_isolation(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    cs = await ok(await propose(test_client, mid, _create_ops(), 0), 201)
    assert (
        await test_client.post(f"/modeling/change-sets/{cs['id']}/reject", json={}, headers=hdrs())
    ).status_code == 422
    forged = await test_client.post(
        f"/modeling/change-sets/{cs['id']}/reject",
        json={"reason": "no", "rejected_by": "mallory"},
        headers=hdrs(),
    )
    assert forged.status_code == 422
    rejected = await ok(
        await test_client.post(
            f"/modeling/change-sets/{cs['id']}/reject",
            json={"reason": "not now"},
            headers=hdrs(BOB),
        )
    )
    assert (
        rejected["status"] == "rejected"
        and rejected["rejected_by"] == BOB
        and rejected["reject_reason"] == "not now"
    )
    assert (
        await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    ).status_code == 409
    assert (
        await test_client.post(
            f"/modeling/change-sets/{cs['id']}/reject", json={"reason": "x"}, headers=hdrs()
        )
    ).status_code == 409

    private = await new_model(test_client, visibility="user")
    pcs = await ok(await propose(test_client, private["id"], _create_ops(), 0), 201)
    for headers in (hdrs(BOB), hdrs(tenant=OTHER_TENANT)):
        assert (
            await test_client.get(f"/modeling/change-sets/{pcs['id']}", headers=headers)
        ).status_code == 404
        assert (
            await test_client.post(f"/modeling/change-sets/{pcs['id']}/apply", headers=headers)
        ).status_code == 404
        assert (
            await propose(test_client, private["id"], _create_ops(), 0, headers=headers)
        ).status_code == 404
    # A private model's applied rows are private to its owner.
    applied = await ok(
        await test_client.post(f"/modeling/change-sets/{pcs['id']}/apply", headers=hdrs())
    )
    actor = applied["id_map"]["a"]
    assert (
        await test_client.get(f"/architecture/elements/{actor}", headers=hdrs(BOB))
    ).status_code == 404


# --- review and publication -----------------------------------------------------


async def review(
    client: AsyncClient, mid: str, rid: str, decision: str, reason: str | None = None, headers=None
):
    return await client.post(
        f"/modeling/models/{mid}/revisions/{rid}/reviews",
        json={"decision": decision, **({"reason": reason} if reason else {})},
        headers=headers or hdrs(),
    )


async def publish(client: AsyncClient, mid: str, rid: str, headers=None):
    return await client.post(
        f"/modeling/models/{mid}/revisions/{rid}/publish", headers=headers or hdrs()
    )


def code(resp) -> str:
    return resp.json()["error"]["code"]


async def test_publish_gate(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    rev = await seal(test_client, mid)
    rid = rev["revision_id"]

    resp = await publish(test_client, mid, rid)
    assert resp.status_code == 409 and code(resp) == "revision_not_approved"
    assert (await review(test_client, mid, rid, "reject")).status_code == 422  # reason required
    rejected = await ok(
        await review(test_client, mid, rid, "reject", "needs work", headers=hdrs(BOB)), 201
    )
    assert rejected["actor"] == BOB
    forged = await test_client.post(
        f"/modeling/models/{mid}/revisions/{rid}/reviews",
        json={"decision": "approve", "actor": "mallory"},
        headers=hdrs(),
    )
    assert forged.status_code == 422
    resp = await publish(test_client, mid, rid)
    assert code(resp) == "revision_not_approved"
    approved = await ok(await review(test_client, mid, rid, "approve", headers=hdrs(BOB)), 201)
    reviews = await ok(
        await test_client.get(f"/modeling/models/{mid}/revisions/{rid}/reviews", headers=hdrs())
    )
    assert [r["decision"] for r in reviews] == ["approve", "reject"]

    resp = await publish(test_client, mid, rid)
    assert resp.status_code == 409 and code(resp) == "revision_not_validated"
    # A run with a required rule disabled never qualifies; nor does a run on other content.
    await ok(await run(test_client, mid, {"revision_id": rid, "disable": ["AM-VIEW-002"]}), 201)
    await ok(await element(test_client, mid))
    await ok(await run(test_client, mid), 201)  # draft now differs from the revision
    assert code(await publish(test_client, mid, rid)) == "revision_not_validated"

    good = await ok(await run(test_client, mid, {"revision_id": rid}), 201)
    pub = await ok(await publish(test_client, mid, rid, headers=hdrs(BOB)), 201)
    assert pub["published_by"] == BOB and pub["supersedes_revision_id"] is None
    assert pub["review_id"] == approved["id"] and pub["validation_run_id"] == good["run_id"]
    model = await ok(await test_client.get(f"/modeling/models/{mid}", headers=hdrs()))
    assert model["published_revision_id"] == rid
    assert code(await publish(test_client, mid, rid)) == "revision_already_published"

    # A newer revision: a draft-head run on identical content qualifies too; supersession kept.
    rev2 = await seal(test_client, mid)
    await ok(await review(test_client, mid, rev2["revision_id"], "approve", headers=hdrs(BOB)), 201)
    draft_run = await ok(await run(test_client, mid), 201)
    assert draft_run["content_hash"] == rev2["content_hash"] and draft_run["clean"]
    pub2 = await ok(await publish(test_client, mid, rev2["revision_id"]), 201)
    assert pub2["supersedes_revision_id"] == rid
    history = await ok(
        await test_client.get(f"/modeling/models/{mid}/publications", headers=hdrs())
    )
    assert [p["revision_id"] for p in history] == [rev2["revision_id"], rid]
    # The model's PATCH cannot set publication.
    resp = await test_client.patch(
        f"/modeling/models/{mid}", json={"published_revision_id": rid}, headers=hdrs()
    )
    assert resp.status_code == 422


async def test_publish_needs_a_clean_run(test_client: AsyncClient, test_session_factory) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    d = await ok(await element(test_client, mid, "data_object"))
    p = await ok(await element(test_client, mid, "business_process"))
    async with test_session_factory() as s:
        s.add(
            ArchitectureRelationship(
                slug=f"legacy-{uuid.uuid4().hex[:6]}",
                tenant_id=TENANT,
                subject_id=ALICE,
                visibility="tenant",
                source_entity_type="architecture_elements",
                source_id=uuid.UUID(d["id"]),
                target_entity_type="architecture_elements",
                target_id=uuid.UUID(p["id"]),
                relationship_type="flow",
                model_id=uuid.UUID(mid),
            )
        )
        await s.commit()
    rev = await seal(test_client, mid)
    await ok(await review(test_client, mid, rev["revision_id"], "approve", headers=hdrs(BOB)), 201)
    dirty = await ok(await run(test_client, mid, {"revision_id": rev["revision_id"]}), 201)
    assert dirty["status"] == "complete" and dirty["clean"] is False
    assert code(await publish(test_client, mid, rev["revision_id"])) == "revision_not_validated"


async def test_review_and_publish_isolation(test_client: AsyncClient) -> None:
    private = await new_model(test_client, visibility="user")
    rev = await seal(test_client, private["id"])
    for headers in (hdrs(BOB), hdrs(tenant=OTHER_TENANT)):
        assert (
            await review(test_client, private["id"], rev["revision_id"], "approve", headers=headers)
        ).status_code == 404
        assert (
            await publish(test_client, private["id"], rev["revision_id"], headers=headers)
        ).status_code == 404
        assert (
            await test_client.get(f"/modeling/models/{private['id']}/publications", headers=headers)
        ).status_code == 404
    other = await new_model(test_client)
    assert (
        await review(test_client, other["id"], rev["revision_id"], "approve")
    ).status_code == 404


async def test_legacy_writes_cannot_set_approval_fields(test_client: AsyncClient) -> None:
    body = {
        "title": "E",
        "element_type": "business_actor",
        "approved_by": "mallory",
        "approved_at": "2026-01-01T00:00:00Z",
        "published_at": "2026-01-01T00:00:00Z",
    }
    created = await ok(await test_client.post("/architecture/elements", json=body, headers=hdrs()))
    assert (created["approved_by"], created["approved_at"], created["published_at"]) == (
        None,
        None,
        None,
    )
    put = await ok(
        await test_client.put(f"/architecture/elements/{created['id']}", json=body, headers=hdrs())
    )
    assert (put["approved_by"], put["published_at"]) == (None, None)
    view = await ok(
        await test_client.post(
            "/architecture/views",
            json={"title": "V", "viewpoint": "layered", "approved_by": "mallory"},
            headers=hdrs(),
        )
    )
    assert view["approved_by"] is None


async def test_force_delete_removes_part2_state(
    test_client: AsyncClient, test_session_factory
) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    req = await _requirement(test_session_factory)
    rev = await seal(test_client, mid)
    await ok(await run(test_client, mid, {"revision_id": rev["revision_id"]}), 201)
    await ok(await review(test_client, mid, rev["revision_id"], "approve", headers=hdrs(BOB)), 201)
    await ok(await publish(test_client, mid, rev["revision_id"]), 201)
    cs = await ok(
        await propose(test_client, mid, _create_ops(), await head_of(test_client, mid)), 201
    )
    into = await ok(
        await test_client.post(
            "/modeling/trace-links",
            json={
                "source": {"kind": "requirement", "id": req},
                "target": {"kind": "element", "id": w["role"]["id"]},
                "link_type": "traces_to",
            },
            headers=hdrs(),
        ),
        201,
    )
    await ok(
        await test_client.delete(
            f"/modeling/models/{mid}", params={"force": "true"}, headers=hdrs()
        )
    )
    assert (
        await test_client.get(f"/modeling/change-sets/{cs['id']}", headers=hdrs())
    ).status_code == 404
    assert (
        await test_client.get(f"/modeling/trace-links/{into['id']}", headers=hdrs())
    ).status_code == 404


async def test_element_row_insert_without_model_is_assigned(test_session_factory) -> None:
    """Control for the direct-insert helpers above: an ORM insert gets a model."""
    async with test_session_factory() as s:
        e = ArchitectureElement(
            slug=f"ctl-{uuid.uuid4().hex[:6]}",
            title="ctl",
            tenant_id=T("modeling-lifecycle-ctl"),
            layer="business",
            element_type="business_actor",
        )
        s.add(e)
        await s.commit()
        assert e.model_id is not None


async def test_one_subject_seals_approves_and_publishes(test_client: AsyncClient) -> None:
    """A one-user tenant runs the whole lifecycle: the sealer approves and publishes."""
    w = await populated(test_client)
    mid = w["model"]["id"]
    rev = await seal(test_client, mid)  # sealed_by ALICE
    rid = rev["revision_id"]
    rejected = await ok(await review(test_client, mid, rid, "reject", "not ready"), 201)
    assert rejected["actor"] == ALICE
    approved = await ok(await review(test_client, mid, rid, "approve"), 201)
    assert approved["actor"] == ALICE
    good = await ok(await run(test_client, mid, {"revision_id": rid}), 201)
    pub = await ok(await publish(test_client, mid, rid), 201)
    assert pub["published_by"] == ALICE
    assert pub["review_id"] == approved["id"] and pub["validation_run_id"] == good["run_id"]
    model = await ok(await test_client.get(f"/modeling/models/{mid}", headers=hdrs()))
    assert model["published_revision_id"] == rid


async def test_change_sets_carry_notes_groups_and_lines(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    ops = _create_ops() + [
        {
            "op": "create",
            "kind": "view_object",
            "id": "g",
            "attributes": {"view_id": "v", "kind": "group", "name": "G"},
        },
        {
            "op": "create",
            "kind": "view_object",
            "id": "n",
            "attributes": {"view_id": "v", "kind": "note", "text": "hi", "parent_object_id": "g"},
        },
        {
            "op": "create",
            "kind": "view_connection",
            "id": "l",
            "attributes": {
                "view_id": "v",
                "kind": "line",
                "source_object_id": "n",
                "target_object_id": "oa",
                "label": "x",
            },
        },
    ]
    cs = await ok(await propose(test_client, mid, ops, 0), 201)
    assert cs["status"] == "proposed", cs["blocking_findings"]
    applied = await ok(
        await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    )
    ids = applied["id_map"]
    objs = {
        o["id"]: o
        for o in await ok(
            await test_client.get(f"/architecture/views/{ids['v']}/objects", headers=hdrs())
        )
    }
    assert objs[ids["n"]]["kind"] == "note" and objs[ids["n"]]["parent_object_id"] == ids["g"]
    conns = {
        c["id"]: c
        for c in await ok(
            await test_client.get(f"/architecture/views/{ids['v']}/connections", headers=hdrs())
        )
    }
    assert conns[ids["l"]]["kind"] == "line" and conns[ids["l"]]["label"] == "x"
    # A relationship connection onto a note is refused in memory (AM-VIEW-002) ...
    bad = [
        {
            "op": "create",
            "kind": "view_connection",
            "attributes": {
                "view_id": ids["v"],
                "relationship_ref": ids["ar"],
                "source_object_id": ids["oa"],
                "target_object_id": ids["n"],
            },
        },
        # ... and so is a note carrying a concept.
        {
            "op": "create",
            "kind": "view_object",
            "attributes": {"view_id": ids["v"], "kind": "note", "concept_ref": ids["a"]},
        },
    ]
    invalid = await ok(await propose(test_client, mid, bad, await head_of(test_client, mid)), 201)
    assert invalid["status"] == "invalid"
    assert {f["rule_id"] for f in invalid["blocking_findings"]} == {"AM-VIEW-002", "CS-OP-001"}
    # Deleting a relationship in a change set cascades in memory like the database does.
    chain = [
        {
            "op": "create",
            "kind": "element",
            "id": "x",
            "attributes": {"title": "X", "element_type": "business_actor"},
        },
        {
            "op": "create",
            "kind": "relationship",
            "id": "on",
            "attributes": {
                "relationship_type": "association",
                "source_id": "x",
                "target_id": ids["ar"],
            },
        },
        {"op": "delete", "kind": "relationship", "id": ids["ar"]},
    ]
    cs3 = await ok(await propose(test_client, mid, chain, await head_of(test_client, mid)), 201)
    assert cs3["status"] == "proposed", cs3["blocking_findings"]
    applied3 = await ok(
        await test_client.post(f"/modeling/change-sets/{cs3['id']}/apply", headers=hdrs())
    )
    assert (
        await test_client.get(
            f"/architecture/relationships/{applied3['id_map']['on']}", headers=hdrs()
        )
    ).status_code == 404


async def test_an_op_the_gate_would_refuse_is_invalid_even_if_undone_later(
    test_client: AsyncClient,
) -> None:
    """Apply runs ops one by one through the write gates: each op must pass on its own."""
    m = await new_model(test_client)
    cs = await ok(await propose(test_client, m["id"], _create_ops(), 0), 201)
    ids = (
        await ok(await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs()))
    )["id_map"]
    ops = [
        # a -> (a assigned-to r): the edge ends on a relationship that already has a (AM-REL-002)
        {
            "op": "create",
            "kind": "relationship",
            "id": "bad",
            "attributes": {
                "relationship_type": "association",
                "source_id": ids["a"],
                "target_id": ids["ar"],
            },
        },
        {"op": "delete", "kind": "relationship", "id": "bad"},
    ]
    invalid = await ok(
        await propose(test_client, m["id"], ops, await head_of(test_client, m["id"])), 201
    )
    assert invalid["status"] == "invalid"
    assert {f["rule_id"] for f in invalid["blocking_findings"]} == {"AM-REL-002"}


async def test_change_set_documentation_only_on_notes_and_groups(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    ops = _create_ops() + [
        {
            "op": "create",
            "kind": "view_object",
            "id": "n",
            "attributes": {"view_id": "v", "kind": "note", "documentation": "d"},
        },
        {"op": "update", "kind": "view_object", "id": "oa", "attributes": {"documentation": "no"}},
    ]
    cs = await ok(await propose(test_client, m["id"], ops, 0), 201)
    assert cs["status"] == "invalid"
    assert [f["path"] for f in cs["blocking_findings"]] == ["/ops/8"]
    cs = await ok(await propose(test_client, m["id"], ops[:-1], 0), 201)
    assert cs["status"] == "proposed", cs["blocking_findings"]
    ids = (
        await ok(await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs()))
    )["id_map"]
    objs = {
        o["id"]: o
        for o in await ok(
            await test_client.get(f"/architecture/views/{ids['v']}/objects", headers=hdrs())
        )
    }
    assert objs[ids["n"]]["documentation"] == "d"
