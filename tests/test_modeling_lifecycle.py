"""Modeling lifecycle part 1 over HTTP, against real Postgres (target spec §7.1–7.2, §8.1).

Models (CRUD, Idempotency-Key, tenant / private-subject isolation), the draft head
(increment on every write kind, ``If-Match`` → ``412 stale_head``, ``ETag``), sealed
revisions (immutable manifest + hash across later edits, ``input_too_large``, delete
guard), view occurrences (write-time integrity, cascade on delete) and the
write-vs-seal lock (two real sessions).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from models.architecture import ArchitectureElement
from models.modeling import ModelingModel
from services.modeling_models import canonical_json
from settings import get_settings
from tests.tenants import T

pytestmark = pytest.mark.asyncio

TENANT = T("modeling-lifecycle")
OTHER_TENANT = T("modeling-lifecycle-other")
ALICE, BOB = "alice-modeling", "bob-modeling"


def hdrs(
    subject: str = ALICE,
    *,
    tenant: str = TENANT,
    visibility: str | None = None,
    if_match: int | None = None,
    key: str | None = None,
) -> dict[str, str]:
    h = {"X-Tenant-Id": tenant, "X-Subject-Id": subject}
    if visibility:
        h["X-Visibility"] = visibility
    if if_match is not None:
        h["If-Match"] = str(if_match)
    if key:
        h["Idempotency-Key"] = key
    return h


def etag(resp) -> int:
    return int(resp.headers["ETag"].strip('"'))


async def new_model(client: AsyncClient, *, title: str = "M", **extra: Any) -> dict[str, Any]:
    headers = extra.pop("_headers", None) or hdrs()
    body = {"language": "archimate", "language_version": "3.2", "title": title, **extra}
    resp = await client.post("/modeling/models", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def head_of(client: AsyncClient, model_id: str, headers: dict | None = None) -> int:
    resp = await client.get(f"/modeling/models/{model_id}", headers=headers or hdrs())
    assert resp.status_code == 200, resp.text
    assert etag(resp) == resp.json()["head"]
    return resp.json()["head"]


async def element(
    client: AsyncClient, model_id: str | None, element_type: str = "business_actor", **extra: Any
):
    headers = extra.pop("_headers", None) or hdrs()
    body = {"title": f"{element_type}-{uuid.uuid4().hex[:6]}", "element_type": element_type}
    if model_id is not None:
        body["model_id"] = model_id
    return await client.post("/architecture/elements", json={**body, **extra}, headers=headers)


async def relationship(client: AsyncClient, source: dict, rel: str, target: dict, **kw: Any):
    return await client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": "architecture_elements",
            "source_id": source["id"],
            "target_entity_type": "architecture_elements",
            "target_id": target["id"],
            "relationship_type": rel,
        },
        headers=kw.get("headers") or hdrs(),
    )


async def view(client: AsyncClient, model_id: str | None, **extra: Any):
    body = {"title": f"view-{uuid.uuid4().hex[:6]}", "viewpoint": "layered"}
    if model_id is not None:
        body["model_id"] = model_id
    return await client.post("/architecture/views", json={**body, **extra}, headers=hdrs())


async def ok(resp, status: int = 200) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    return resp.json()


def integrity(resp) -> list[dict]:
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "model_integrity", err
    assert err["findings"], err
    return err["findings"]


async def populated(client: AsyncClient) -> dict[str, Any]:
    """A model with actor -assignment-> role, a view showing both and the edge."""
    m = await new_model(client)
    actor = await ok(await element(client, m["id"], "business_actor"))
    role = await ok(await element(client, m["id"], "business_role"))
    rel = await ok(await relationship(client, actor, "assignment", role))
    v = await ok(await view(client, m["id"]))
    o1 = await ok(
        await client.post(
            f"/architecture/views/{v['id']}/objects",
            json={"concept_ref": actor["id"], "bounds": {"x": 10, "y": 20, "w": 120, "h": 55}},
            headers=hdrs(),
        ),
        201,
    )
    o2 = await ok(
        await client.post(
            f"/architecture/views/{v['id']}/objects",
            json={"concept_ref": role["id"], "style": {"fill": "#ffffb5"}},
            headers=hdrs(),
        ),
        201,
    )
    c = await ok(
        await client.post(
            f"/architecture/views/{v['id']}/connections",
            json={
                "relationship_ref": rel["id"],
                "source_object_id": o1["id"],
                "target_object_id": o2["id"],
                "bendpoints": [{"x": 50, "y": 60}, {"x": 70, "y": 80}],
            },
            headers=hdrs(),
        ),
        201,
    )
    return {
        "model": m,
        "actor": actor,
        "role": role,
        "rel": rel,
        "view": v,
        "o1": o1,
        "o2": o2,
        "c": c,
    }


# --- models: CRUD, idempotency, isolation ------------------------------------


async def test_model_crud_round_trip(test_client: AsyncClient) -> None:
    m = await new_model(test_client, title="Enterprise", description="d", project_id="p-1")
    assert m["head"] == 0 and m["language"] == "archimate" and not m["is_default"]
    got = await ok(await test_client.get(f"/modeling/models/{m['id']}", headers=hdrs()))
    assert got["latest_revision_id"] is None and got["latest_validation_run"] is None

    await new_model(test_client, title="Other", language="uml", language_version="2.5.1-p1")
    resp = await test_client.get(
        "/modeling/models", params={"language": "archimate"}, headers=hdrs()
    )
    assert [r["id"] for r in await ok(resp)] == [m["id"]]
    assert resp.headers["X-Total-Count"] == "1"
    assert (
        len(
            await ok(
                await test_client.get("/modeling/models", params={"q": "ENTER"}, headers=hdrs())
            )
        )
        == 1
    )
    assert (
        len(
            await ok(
                await test_client.get(
                    "/modeling/models", params={"project_id": "p-x"}, headers=hdrs()
                )
            )
        )
        == 0
    )

    patched = await ok(
        await test_client.patch(
            f"/modeling/models/{m['id']}", json={"title": "Renamed"}, headers=hdrs()
        )
    )
    assert patched["title"] == "Renamed" and patched["description"] == "d"
    # language / language_version are immutable: not even accepted.
    resp = await test_client.patch(
        f"/modeling/models/{m['id']}", json={"language": "uml"}, headers=hdrs()
    )
    assert resp.status_code == 422, resp.text

    assert (
        await test_client.delete(f"/modeling/models/{m['id']}", headers=hdrs())
    ).status_code == 200
    assert (await test_client.get(f"/modeling/models/{m['id']}", headers=hdrs())).status_code == 404


async def test_create_model_idempotency_key(test_client: AsyncClient) -> None:
    key = f"k-{uuid.uuid4().hex}"
    body = {"language": "archimate", "language_version": "3.2", "title": "Idem"}
    first = await test_client.post("/modeling/models", json=body, headers=hdrs(key=key))
    again = await test_client.post("/modeling/models", json=body, headers=hdrs(key=key))
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    other = await test_client.post(
        "/modeling/models", json={**body, "title": "X"}, headers=hdrs(key=key)
    )
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"
    # Scoped per subject: Bob's same key is a new create, never Alice's row.
    bob = await test_client.post("/modeling/models", json=body, headers=hdrs(BOB, key=key))
    assert bob.status_code == 201 and bob.json()["id"] != first.json()["id"]
    rows = await ok(await test_client.get("/modeling/models", params={"q": "Idem"}, headers=hdrs()))
    assert len(rows) == 2


async def test_tenant_and_private_subject_isolation(test_client: AsyncClient) -> None:
    private = await new_model(test_client, title="Mine", visibility="user")
    shared = await new_model(test_client, title="Ours")
    for path in (
        f"/modeling/models/{private['id']}",
        f"/modeling/models/{private['id']}/revisions",
    ):
        assert (await test_client.get(path, headers=hdrs(BOB))).status_code == 404
    assert (
        await test_client.patch(
            f"/modeling/models/{private['id']}", json={"title": "x"}, headers=hdrs(BOB)
        )
    ).status_code == 404
    assert (
        await test_client.post(f"/modeling/models/{private['id']}/revisions", headers=hdrs(BOB))
    ).status_code == 404
    assert (
        await test_client.delete(f"/modeling/models/{private['id']}", headers=hdrs(BOB))
    ).status_code == 404
    bob_list = await ok(await test_client.get("/modeling/models", headers=hdrs(BOB)))
    assert {r["id"] for r in bob_list} == {shared["id"]}
    # Another tenant sees neither, and cannot write into Alice's model.
    other = hdrs(tenant=OTHER_TENANT)
    assert await ok(await test_client.get("/modeling/models", headers=other)) == []
    assert (
        await test_client.get(f"/modeling/models/{shared['id']}", headers=other)
    ).status_code == 404
    integrity(await element(test_client, shared["id"], _headers=other))
    # Bob cannot put a row into Alice's private model.
    integrity(await element(test_client, private["id"], _headers=hdrs(BOB, visibility="user")))


async def test_row_visibility_must_match_the_model(test_client: AsyncClient) -> None:
    shared = await new_model(test_client)
    findings = integrity(await element(test_client, shared["id"], _headers=hdrs(visibility="user")))
    assert findings[0]["rule_id"] == "AM-REF-001" and findings[0]["path"] == "/model_id"
    uml = await new_model(test_client, language="uml", language_version="2.5.1-p1")
    integrity(await element(test_client, uml["id"]))
    integrity(await element(test_client, str(uuid.uuid4())))


async def test_write_without_model_joins_the_scope_default(test_client: AsyncClient) -> None:
    a = await ok(await element(test_client, None, project_id="proj-d"))
    b = await ok(await element(test_client, None, project_id="proj-d"))
    c = await ok(await element(test_client, None, project_id="proj-e"))
    p = await ok(
        await element(test_client, None, project_id="proj-d", _headers=hdrs(visibility="user"))
    )
    assert a["model_id"] == b["model_id"]
    assert len({a["model_id"], c["model_id"], p["model_id"]}) == 3
    rel = await ok(await relationship(test_client, a, "association", b))
    assert rel["model_id"] == a["model_id"]
    default = await ok(await test_client.get(f"/modeling/models/{a['model_id']}", headers=hdrs()))
    assert default["is_default"] and default["project_id"] == "proj-d" and default["head"] >= 3
    resp = await test_client.patch(
        f"/modeling/models/{a['model_id']}", json={"visibility": "user"}, headers=hdrs()
    )
    assert (
        resp.status_code == 422 and resp.json()["error"]["code"] == "default_model_visibility_fixed"
    )


# --- draft head ----------------------------------------------------------------


async def test_head_increments_on_every_write_kind(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    seen = [0]

    async def bumped(resp, status: int = 200) -> dict:
        body = await ok(resp, status)
        head = await head_of(test_client, mid)
        assert etag(resp) == head > seen[-1], (resp.request.method, resp.request.url, seen, head)
        seen.append(head)
        return body

    actor = await bumped(await element(test_client, mid, "business_actor"))
    role = await bumped(await element(test_client, mid, "business_role"))
    rel = await bumped(await relationship(test_client, actor, "assignment", role))
    v = await bumped(await view(test_client, mid))
    vid = v["id"]
    o1 = await bumped(
        await test_client.post(
            f"/architecture/views/{vid}/objects", json={"concept_ref": actor["id"]}, headers=hdrs()
        ),
        201,
    )
    o2 = await bumped(
        await test_client.post(
            f"/architecture/views/{vid}/objects", json={"concept_ref": role["id"]}, headers=hdrs()
        ),
        201,
    )
    conn_body = {
        "relationship_ref": rel["id"],
        "source_object_id": o1["id"],
        "target_object_id": o2["id"],
    }
    c = await bumped(
        await test_client.post(
            f"/architecture/views/{vid}/connections", json=conn_body, headers=hdrs()
        ),
        201,
    )
    await bumped(
        await test_client.put(
            f"/architecture/views/{vid}/objects/{o1['id']}",
            json={"concept_ref": actor["id"], "bounds": {"x": 1, "y": 2, "w": 3, "h": 4}},
            headers=hdrs(),
        )
    )
    await bumped(
        await test_client.put(
            f"/architecture/views/{vid}/connections/{c['id']}",
            json={**conn_body, "bendpoints": [{"x": 5, "y": 5}]},
            headers=hdrs(),
        )
    )
    await bumped(
        await test_client.put(
            f"/architecture/elements/{actor['id']}",
            json={"title": "Renamed actor", "element_type": "business_actor"},
            headers=hdrs(),
        )
    )
    await bumped(
        await test_client.put(
            f"/architecture/relationships/{rel['id']}", json={"name": "plays"}, headers=hdrs()
        )
    )
    await bumped(
        await test_client.put(
            f"/architecture/views/{vid}",
            json={"title": "Renamed view", "viewpoint": "layered"},
            headers=hdrs(),
        )
    )
    await bumped(
        await test_client.delete(f"/architecture/views/{vid}/connections/{c['id']}", headers=hdrs())
    )
    await bumped(
        await test_client.delete(f"/architecture/views/{vid}/objects/{o2['id']}", headers=hdrs())
    )
    await bumped(
        await test_client.delete(f"/architecture/relationships/{rel['id']}", headers=hdrs())
    )
    await bumped(await test_client.delete(f"/architecture/views/{vid}", headers=hdrs()))
    await bumped(await test_client.delete(f"/architecture/elements/{role['id']}", headers=hdrs()))
    # A read does not move the head.
    await test_client.get(f"/architecture/elements/{actor['id']}", headers=hdrs())
    assert await head_of(test_client, mid) == seen[-1]


async def test_if_match_stale_is_412_with_current_head(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    first = await element(test_client, m["id"], _headers=hdrs(if_match=0))
    assert first.status_code == 200, first.text
    head = etag(first)
    assert head == 1

    stale = await element(test_client, m["id"], _headers=hdrs(if_match=0))
    assert stale.status_code == 412, stale.text
    err = stale.json()["error"]
    assert err["code"] == "stale_head" and err["head"] == 1
    assert etag(stale) == 1
    assert await head_of(test_client, m["id"]) == 1  # nothing written

    eid = first.json()["id"]
    put = {"title": "x", "element_type": "business_actor"}
    resp = await test_client.put(
        f"/architecture/elements/{eid}", json=put, headers=hdrs(if_match=0)
    )
    assert resp.status_code == 412
    resp = await test_client.put(
        f"/architecture/elements/{eid}", json=put, headers=hdrs(if_match=head)
    )
    assert resp.status_code == 200 and etag(resp) == 2
    resp = await test_client.delete(f"/architecture/elements/{eid}", headers=hdrs(if_match=1))
    assert resp.status_code == 412
    seal = await test_client.post(
        f"/modeling/models/{m['id']}/revisions", json={"head": 1}, headers=hdrs()
    )
    assert seal.status_code == 412 and seal.json()["error"]["head"] == 2
    seal = await test_client.post(f"/modeling/models/{m['id']}/revisions", headers=hdrs(if_match=2))
    assert seal.status_code == 201, seal.text
    bad = await element(test_client, m["id"], _headers={**hdrs(), "If-Match": "abc"})
    assert bad.status_code == 422


async def test_if_match_on_a_default_model_write(test_client: AsyncClient) -> None:
    a = await ok(await element(test_client, None, project_id="proj-im"))
    head = await head_of(test_client, a["model_id"])
    stale = await element(test_client, None, project_id="proj-im", _headers=hdrs(if_match=head - 1))
    assert stale.status_code == 412
    fresh = await element(test_client, None, project_id="proj-im", _headers=hdrs(if_match=head))
    assert fresh.status_code == 200 and etag(fresh) == head + 1


# --- revisions -------------------------------------------------------------------


async def test_revision_is_immutable_across_later_edits_and_deletes(
    test_client: AsyncClient,
) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    sealed = await ok(
        await test_client.post(
            f"/modeling/models/{mid}/revisions", json={"label": "v1"}, headers=hdrs()
        ),
        201,
    )
    assert sealed["seq"] == 1 and sealed["sealed_by"] == ALICE and sealed["label"] == "v1"
    assert sealed["head"] == await head_of(test_client, mid)
    rid = sealed["revision_id"]
    before = await ok(
        await test_client.get(f"/modeling/models/{mid}/revisions/{rid}", headers=hdrs())
    )
    manifest = before["manifest"]
    assert hashlib.sha256(canonical_json(manifest)).hexdigest() == sealed["content_hash"]
    assert {c["id"] for c in manifest["concepts"]} == {w["actor"]["id"], w["role"]["id"]}
    (mv,) = manifest["views"]
    by_id = {o["id"]: o for o in mv["objects"]}
    assert by_id[w["o1"]["id"]]["bounds"] == {"x": 10, "y": 20, "w": 120, "h": 55}
    assert by_id[w["o2"]["id"]]["style"] == {"fill": "#ffffb5"}
    assert mv["connections"][0]["bendpoints"] == [{"x": 50, "y": 60}, {"x": 70, "y": 80}]
    assert manifest["relationships"][0]["source"] == w["actor"]["id"]

    # Edit and delete the draft.
    await ok(
        await test_client.put(
            f"/architecture/elements/{w['actor']['id']}",
            json={"title": "Changed", "element_type": "business_actor"},
            headers=hdrs(),
        )
    )
    await ok(
        await test_client.delete(f"/architecture/relationships/{w['rel']['id']}", headers=hdrs())
    )
    await ok(await test_client.delete(f"/architecture/elements/{w['role']['id']}", headers=hdrs()))

    after = await ok(
        await test_client.get(f"/modeling/models/{mid}/revisions/{rid}", headers=hdrs())
    )
    assert after == before
    assert hashlib.sha256(canonical_json(after["manifest"])).hexdigest() == sealed["content_hash"]

    second = await ok(
        await test_client.post(f"/modeling/models/{mid}/revisions", headers=hdrs()), 201
    )
    assert second["seq"] == 2 and second["content_hash"] != sealed["content_hash"]
    listing = await test_client.get(f"/modeling/models/{mid}/revisions", headers=hdrs())
    assert [r["seq"] for r in await ok(listing)] == [2, 1]
    assert listing.headers["X-Total-Count"] == "2"
    got = await ok(await test_client.get(f"/modeling/models/{mid}", headers=hdrs()))
    assert got["latest_revision_id"] == second["revision_id"]

    # Sealing the same draft twice gives the same hash (canonical, content-only).
    third = await ok(
        await test_client.post(f"/modeling/models/{mid}/revisions", headers=hdrs()), 201
    )
    assert third["content_hash"] == second["content_hash"]
    missing = await test_client.get(
        f"/modeling/models/{mid}/revisions/{uuid.uuid4()}", headers=hdrs()
    )
    assert missing.status_code == 404


async def test_delete_model_with_revisions_needs_force(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    await ok(await test_client.post(f"/modeling/models/{mid}/revisions", headers=hdrs()), 201)
    refused = await test_client.delete(f"/modeling/models/{mid}", headers=hdrs())
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "model_has_revisions"
    await ok(
        await test_client.delete(
            f"/modeling/models/{mid}", params={"force": "true"}, headers=hdrs()
        )
    )
    for path in (
        f"/architecture/elements/{w['actor']['id']}",
        f"/architecture/relationships/{w['rel']['id']}",
        f"/architecture/views/{w['view']['id']}",
        f"/modeling/models/{mid}",
    ):
        assert (await test_client.get(path, headers=hdrs())).status_code == 404, path


async def test_seal_refuses_models_over_the_async_limit(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sealing is a snapshot copy, bounded by MODEL_ASYNC_MAX_CONCEPTS (not the sync bound)."""
    w = await populated(test_client)
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 1)
    monkeypatch.setattr(get_settings(), "model_async_max_concepts", 2)
    resp = await test_client.post(f"/modeling/models/{w['model']['id']}/revisions", headers=hdrs())
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"
    assert "MODEL_ASYNC_MAX_CONCEPTS=2" in resp.json()["error"]["message"]
    monkeypatch.setattr(get_settings(), "model_async_max_concepts", 3)
    resp = await test_client.post(f"/modeling/models/{w['model']['id']}/revisions", headers=hdrs())
    assert resp.status_code == 201, resp.text


async def test_uml_model_seals_an_empty_uml_manifest(test_client: AsyncClient) -> None:
    m = await new_model(test_client, language="uml", language_version="2.5.1-p1")
    sealed = await ok(
        await test_client.post(f"/modeling/models/{m['id']}/revisions", headers=hdrs()), 201
    )
    got = await ok(
        await test_client.get(
            f"/modeling/models/{m['id']}/revisions/{sealed['revision_id']}", headers=hdrs()
        )
    )
    assert got["manifest"]["elements"] == [] and got["manifest"]["diagrams"] == []


# --- view occurrences ------------------------------------------------------------


async def test_occurrence_integrity_refusals(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    vid = w["view"]["id"]
    elsewhere = await new_model(test_client)
    foreign = await ok(await element(test_client, elsewhere["id"]))
    findings = integrity(
        await test_client.post(
            f"/architecture/views/{vid}/objects",
            json={"concept_ref": foreign["id"]},
            headers=hdrs(),
        )
    )
    assert findings[0]["rule_id"] == "AM-REF-001"
    integrity(
        await test_client.post(
            f"/architecture/views/{vid}/objects",
            json={"concept_ref": str(uuid.uuid4())},
            headers=hdrs(),
        )
    )

    # AM-VIEW-002: a connection must join occurrences of the relationship's ends, in order.
    reversed_body = {
        "relationship_ref": w["rel"]["id"],
        "source_object_id": w["o2"]["id"],
        "target_object_id": w["o1"]["id"],
    }
    findings = integrity(
        await test_client.post(
            f"/architecture/views/{vid}/connections", json=reversed_body, headers=hdrs()
        )
    )
    assert findings[0]["rule_id"] == "AM-VIEW-002"
    dangling = {**reversed_body, "source_object_id": str(uuid.uuid4())}
    assert (
        integrity(
            await test_client.post(
                f"/architecture/views/{vid}/connections", json=dangling, headers=hdrs()
            )
        )[0]["rule_id"]
        == "AM-REF-001"
    )

    # AM-VIEW-003: visual parent in another view; nesting cycle.
    other_view = await ok(await view(test_client, w["model"]["id"]))
    other_obj = await ok(
        await test_client.post(
            f"/architecture/views/{other_view['id']}/objects",
            json={"concept_ref": w["actor"]["id"]},
            headers=hdrs(),
        ),
        201,
    )
    findings = integrity(
        await test_client.post(
            f"/architecture/views/{vid}/objects",
            json={"concept_ref": w["role"]["id"], "parent_object_id": other_obj["id"]},
            headers=hdrs(),
        )
    )
    assert findings[0]["rule_id"] == "AM-VIEW-003"
    await ok(
        await test_client.put(
            f"/architecture/views/{vid}/objects/{w['o2']['id']}",
            json={"concept_ref": w["role"]["id"], "parent_object_id": w["o1"]["id"]},
            headers=hdrs(),
        )
    )
    findings = integrity(
        await test_client.put(
            f"/architecture/views/{vid}/objects/{w['o1']['id']}",
            json={"concept_ref": w["actor"]["id"], "parent_object_id": w["o2"]["id"]},
            headers=hdrs(),
        )
    )
    assert findings[0]["rule_id"] == "AM-VIEW-003"
    # Nothing refused was written.
    objs = await ok(await test_client.get(f"/architecture/views/{vid}/objects", headers=hdrs()))
    assert len(objs) == 2
    assert {o["id"]: o["parent_object_id"] for o in objs}[w["o1"]["id"]] is None
    conns = await ok(
        await test_client.get(f"/architecture/views/{vid}/connections", headers=hdrs())
    )
    assert [c["id"] for c in conns] == [w["c"]["id"]]
    # Occurrence routes of an invisible view are 404, not a leak.
    assert (
        await test_client.get(
            f"/architecture/views/{vid}/objects", headers=hdrs(tenant=OTHER_TENANT)
        )
    ).status_code == 404


async def test_deleting_concepts_cleans_up_their_occurrences(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    vid = w["view"]["id"]
    # A connection that ends on a connection: removed when its end goes.
    role2 = await ok(await element(test_client, w["model"]["id"], "business_role"))
    on_rel = await ok(
        await test_client.post(
            "/architecture/relationships",
            json={
                "source_entity_type": "architecture_elements",
                "source_id": role2["id"],
                "target_entity_type": "architecture_relationships",
                "target_id": w["rel"]["id"],
                "relationship_type": "association",
            },
            headers=hdrs(),
        )
    )
    o3 = await ok(
        await test_client.post(
            f"/architecture/views/{vid}/objects", json={"concept_ref": role2["id"]}, headers=hdrs()
        ),
        201,
    )
    await ok(
        await test_client.post(
            f"/architecture/views/{vid}/connections",
            json={
                "relationship_ref": on_rel["id"],
                "source_object_id": o3["id"],
                "target_object_id": w["c"]["id"],
            },
            headers=hdrs(),
        ),
        201,
    )
    assert (
        len(
            await ok(
                await test_client.get(f"/architecture/views/{vid}/connections", headers=hdrs())
            )
        )
        == 2
    )

    await ok(await test_client.delete(f"/architecture/elements/{w['actor']['id']}", headers=hdrs()))
    objs = await ok(await test_client.get(f"/architecture/views/{vid}/objects", headers=hdrs()))
    assert {o["id"] for o in objs} == {w["o2"]["id"], o3["id"]}
    assert (
        await ok(await test_client.get(f"/architecture/views/{vid}/connections", headers=hdrs()))
        == []
    )

    await ok(await test_client.delete(f"/architecture/views/{vid}", headers=hdrs()))
    m = w["model"]["id"]
    sealed = await ok(
        await test_client.post(f"/modeling/models/{m}/revisions", headers=hdrs()), 201
    )
    got = await ok(
        await test_client.get(
            f"/modeling/models/{m}/revisions/{sealed['revision_id']}", headers=hdrs()
        )
    )
    assert got["manifest"]["views"] == []


async def test_delete_object_cascades_nested_objects_and_connections(
    test_client: AsyncClient,
) -> None:
    w = await populated(test_client)
    vid = w["view"]["id"]
    await ok(
        await test_client.put(
            f"/architecture/views/{vid}/objects/{w['o2']['id']}",
            json={"concept_ref": w["role"]["id"], "parent_object_id": w["o1"]["id"]},
            headers=hdrs(),
        )
    )
    await ok(
        await test_client.delete(
            f"/architecture/views/{vid}/objects/{w['o1']['id']}", headers=hdrs()
        )
    )
    assert (
        await ok(await test_client.get(f"/architecture/views/{vid}/objects", headers=hdrs())) == []
    )
    assert (
        await ok(await test_client.get(f"/architecture/views/{vid}/connections", headers=hdrs()))
        == []
    )
    missing = await test_client.delete(
        f"/architecture/views/{vid}/objects/{w['o1']['id']}", headers=hdrs()
    )
    assert missing.status_code == 404


# --- write vs seal: the model lock ----------------------------------------------


async def test_seal_waits_for_an_in_flight_write(
    test_client: AsyncClient, test_session_factory
) -> None:
    """A flushed, uncommitted write holds the model row lock; a seal blocks until it commits."""
    m = await new_model(test_client)
    mid = uuid.UUID(m["id"])
    async with test_session_factory() as writer:
        writer.add(
            ArchitectureElement(
                slug=f"locked-{uuid.uuid4().hex[:6]}",
                title="Written under lock",
                tenant_id=TENANT,
                subject_id=ALICE,
                visibility="tenant",
                layer="business",
                element_type="business_actor",
                model_id=mid,
            )
        )
        await writer.flush()  # UPDATE modeling_models … head = head + 1 → row lock held
        seal = asyncio.create_task(
            test_client.post(f"/modeling/models/{m['id']}/revisions", headers=hdrs())
        )
        await asyncio.sleep(1.0)
        assert not seal.done(), "the seal read the model while a write held its lock"
        await writer.commit()
    resp = await asyncio.wait_for(seal, timeout=30)
    assert resp.status_code == 201, resp.text
    assert resp.json()["head"] == 1
    got = await ok(
        await test_client.get(
            f"/modeling/models/{m['id']}/revisions/{resp.json()['revision_id']}", headers=hdrs()
        )
    )
    assert [c["name"] for c in got["manifest"]["concepts"]] == ["Written under lock"]


async def test_write_waits_for_an_in_flight_seal(
    test_client: AsyncClient, test_session_factory
) -> None:
    """The other order: a seal holding the lock makes a concurrent write wait for it."""
    m = await new_model(test_client)
    mid = uuid.UUID(m["id"])
    async with test_session_factory() as sealer:
        locked = (
            await sealer.execute(
                select(ModelingModel).where(ModelingModel.id == mid).with_for_update()
            )
        ).scalar_one()
        assert locked.head == 0
        write = asyncio.create_task(element(test_client, m["id"]))
        await asyncio.sleep(1.0)
        assert not write.done(), "a write changed the model while a seal held its lock"
        await sealer.commit()
    resp = await asyncio.wait_for(write, timeout=30)
    assert resp.status_code == 200, resp.text
    assert etag(resp) == 1


async def test_canonical_json_orders_ids_and_normalises_numbers() -> None:
    a = {"b": [{"id": "2", "v": 1.0}, {"id": "1", "v": 2.5}], "a": "é"}
    b = {"a": "é", "b": [{"v": 2.5, "id": "1"}, {"v": 1, "id": "2"}]}
    assert canonical_json(a) == canonical_json(b)
    assert (
        canonical_json(a)
        == json.dumps(
            {"a": "é", "b": [{"id": "1", "v": 2.5}, {"id": "2", "v": 1}]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )


# --- Core / raw-SQL write paths, cross-model edges, model-less edges ----------


async def test_binding_attach_and_detach_bump_head_and_honour_if_match(
    test_client: AsyncClient,
) -> None:
    m = await new_model(test_client)
    el = await ok(await element(test_client, m["id"]))
    head = await head_of(test_client, m["id"])
    body = {"binding_kind": "coding_container", "container": "svc-a"}
    stale = await test_client.post(
        f"/architecture/elements/{el['id']}/bindings", json=body, headers=hdrs(if_match=head - 1)
    )
    assert stale.status_code == 412 and stale.json()["error"]["head"] == head
    attached = await test_client.post(
        f"/architecture/elements/{el['id']}/bindings", json=body, headers=hdrs(if_match=head)
    )
    assert attached.status_code == 201, attached.text
    assert etag(attached) == head + 1 == await head_of(test_client, m["id"])
    bid = attached.json()["id"]
    stale = await test_client.delete(
        f"/architecture/elements/{el['id']}/bindings/{bid}", headers=hdrs(if_match=head)
    )
    assert stale.status_code == 412
    detached = await test_client.delete(
        f"/architecture/elements/{el['id']}/bindings/{bid}", headers=hdrs(if_match=head + 1)
    )
    assert detached.status_code == 200, detached.text
    assert etag(detached) == head + 2 == await head_of(test_client, m["id"])


async def test_code_binding_written_by_raw_insert_bumps_head(test_client: AsyncClient) -> None:
    """An element PUT that changes nothing but writes a code binding (Core INSERT/UPDATE)."""
    m = await new_model(test_client)
    el = await ok(await element(test_client, m["id"], "application_component"))
    head = await head_of(test_client, m["id"])
    put = {
        "title": el["title"],
        "element_type": "application_component",
        "properties": {"code_binding": {"container": "c1", "component_id": "comp-1"}},
    }
    resp = await test_client.put(f"/architecture/elements/{el['id']}", json=put, headers=hdrs())
    assert resp.status_code == 200, resp.text
    assert etag(resp) == await head_of(test_client, m["id"]) > head
    sealed = await ok(
        await test_client.post(f"/modeling/models/{m['id']}/revisions", headers=hdrs()), 201
    )
    got = await ok(
        await test_client.get(
            f"/modeling/models/{m['id']}/revisions/{sealed['revision_id']}", headers=hdrs()
        )
    )
    assert [(b["container"], b["component_id"]) for b in got["manifest"]["bindings"]] == [
        ("c1", "comp-1")
    ]


async def test_core_relationship_cleanup_bumps_head(
    test_client: AsyncClient, test_session_factory
) -> None:
    """``EntityService._cleanup_relationships`` deletes edges with a Core DELETE."""
    from services import architecture_element_service

    w = await populated(test_client)
    mid = w["model"]["id"]
    head = await head_of(test_client, mid)
    async with test_session_factory() as s:
        deleted = await architecture_element_service._cleanup_relationships(  # noqa: SLF001
            s, uuid.UUID(w["actor"]["id"]), TENANT
        )
        await s.commit()
    assert deleted == 1
    assert await head_of(test_client, mid) == head + 1


async def test_core_bulk_delete_bumps_head(test_client: AsyncClient, test_session_factory) -> None:
    from services import architecture_element_service

    w = await populated(test_client)
    mid = w["model"]["id"]
    head = await head_of(test_client, mid)
    async with test_session_factory() as s:
        assert await architecture_element_service.bulk_delete(s, TENANT, ALICE) == 2
    assert await head_of(test_client, mid) == head + 1
    assert (
        await test_client.get(f"/architecture/elements/{w['actor']['id']}", headers=hdrs())
    ).status_code == 404


async def test_relationship_across_models_is_refused(test_client: AsyncClient) -> None:
    a = await new_model(test_client)
    b = await new_model(test_client)
    a1 = await ok(await element(test_client, a["id"], "business_actor"))
    a2 = await ok(await element(test_client, a["id"], "business_role"))
    b1 = await ok(await element(test_client, b["id"], "business_role"))
    findings = integrity(await relationship(test_client, a1, "assignment", b1))
    assert findings[0]["rule_id"] == "AM-REF-003"
    assert findings[0]["evidence"] == {"source_model_id": a["id"], "target_model_id": b["id"]}
    rel = await ok(await relationship(test_client, a1, "assignment", a2))
    assert rel["model_id"] == a["id"]
    # Re-pointing the target into another model: refused, nothing changes.
    findings = integrity(
        await test_client.put(
            f"/architecture/relationships/{rel['id']}", json={"target_id": b1["id"]}, headers=hdrs()
        )
    )
    assert findings[0]["rule_id"] == "AM-REF-003"
    # Re-pointing BOTH ends into model b re-derives the edge's model, and bumps both heads.
    b2 = await ok(await element(test_client, b["id"], "business_actor"))
    heads = (await head_of(test_client, a["id"]), await head_of(test_client, b["id"]))
    moved = await test_client.put(
        f"/architecture/relationships/{rel['id']}",
        json={"source_id": b2["id"], "target_id": b1["id"]},
        headers=hdrs(),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["model_id"] == b["id"]
    assert await head_of(test_client, a["id"]) == heads[0] + 1
    assert await head_of(test_client, b["id"]) == heads[1] + 1
    # The registry lists the rule.
    rs = await ok(await test_client.get("/modeling/rulesets", headers=hdrs()))
    archimate = next(r for r in rs if r["language"] == "archimate")
    detail = await ok(
        await test_client.get(f"/modeling/rulesets/{archimate['ruleset_id']}", headers=hdrs())
    )
    assert "AM-REF-003" in {r["rule_id"] for r in detail["rules"]}


async def test_if_match_on_a_model_less_edge_is_400(test_client: AsyncClient) -> None:
    body = {
        "source_entity_type": "requirements",
        "source_id": str(uuid.uuid4()),
        "target_entity_type": "requirements",
        "target_id": str(uuid.uuid4()),
        "relationship_type": "traceability",
    }
    refused = await test_client.post(
        "/architecture/relationships", json=body, headers=hdrs(if_match=0)
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "if_match_not_applicable"
    created = await test_client.post("/architecture/relationships", json=body, headers=hdrs())
    assert created.status_code == 200 and "ETag" not in created.headers
    assert created.json()["model_id"] is None
    rid = created.json()["id"]
    for resp in (
        await test_client.put(
            f"/architecture/relationships/{rid}", json={"name": "x"}, headers=hdrs(if_match=0)
        ),
        await test_client.delete(f"/architecture/relationships/{rid}", headers=hdrs(if_match=0)),
    ):
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "if_match_not_applicable"
    updated = await test_client.put(
        f"/architecture/relationships/{rid}", json={"name": "x"}, headers=hdrs()
    )
    assert updated.status_code == 200 and "ETag" not in updated.headers
    deleted = await test_client.delete(f"/architecture/relationships/{rid}", headers=hdrs())
    assert deleted.status_code == 200 and "ETag" not in deleted.headers


async def test_explicit_model_refusals_carry_a_stable_reason(test_client: AsyncClient) -> None:
    shared = await new_model(test_client)
    uml = await new_model(test_client, language="uml", language_version="2.5.1-p1")
    cases = {
        "model_visibility_mismatch": await element(
            test_client, shared["id"], _headers=hdrs(visibility="user")
        ),
        "model_language_mismatch": await element(test_client, uml["id"]),
        "model_not_writable": await element(test_client, str(uuid.uuid4())),
    }
    for reason, resp in cases.items():
        (finding,) = integrity(resp)
        assert finding["rule_id"] == "AM-REF-001" and finding["evidence"]["reason"] == reason


# --- relationship cascade, notes / groups / lines --------------------------------


async def test_deleting_a_relationship_cascades_to_edges_ending_on_it(
    test_client: AsyncClient,
) -> None:
    """Archi semantics: r2 ends on r1, r3 ends on r2; deleting r1 deletes r2 and r3."""
    m = await new_model(test_client)
    mid = m["id"]
    a = await ok(await element(test_client, mid, "business_actor"))
    b = await ok(await element(test_client, mid, "business_role"))
    c = await ok(await element(test_client, mid, "business_actor"))
    d = await ok(await element(test_client, mid, "business_actor"))
    r1 = await ok(await relationship(test_client, a, "assignment", b))

    async def assoc(src: dict, tgt_rel: dict) -> dict:
        return await ok(
            await test_client.post(
                "/architecture/relationships",
                json={
                    "source_entity_type": "architecture_elements",
                    "source_id": src["id"],
                    "target_entity_type": "architecture_relationships",
                    "target_id": tgt_rel["id"],
                    "relationship_type": "association",
                },
                headers=hdrs(),
            )
        )

    r2 = await assoc(c, r1)
    r3 = await assoc(d, r2)
    unrelated = await ok(await relationship(test_client, c, "association", d))
    v = await ok(await view(test_client, mid))
    vid = v["id"]
    objs = {}
    for el in (a, b, c, d):
        objs[el["id"]] = await ok(
            await test_client.post(
                f"/architecture/views/{vid}/objects", json={"concept_ref": el["id"]}, headers=hdrs()
            ),
            201,
        )
    c1 = await ok(
        await test_client.post(
            f"/architecture/views/{vid}/connections",
            json={
                "relationship_ref": r1["id"],
                "source_object_id": objs[a["id"]]["id"],
                "target_object_id": objs[b["id"]]["id"],
            },
            headers=hdrs(),
        ),
        201,
    )
    c2 = await ok(
        await test_client.post(
            f"/architecture/views/{vid}/connections",
            json={
                "relationship_ref": r2["id"],
                "source_object_id": objs[c["id"]]["id"],
                "target_object_id": c1["id"],
            },
            headers=hdrs(),
        ),
        201,
    )
    await ok(
        await test_client.post(
            f"/architecture/views/{vid}/connections",
            json={
                "relationship_ref": r3["id"],
                "source_object_id": objs[d["id"]]["id"],
                "target_object_id": c2["id"],
            },
            headers=hdrs(),
        ),
        201,
    )
    head = await head_of(test_client, mid)
    resp = await test_client.delete(f"/architecture/relationships/{r1['id']}", headers=hdrs())
    assert resp.status_code == 200, resp.text
    assert etag(resp) == head + 1 == await head_of(test_client, mid)
    for r in (r1, r2, r3):
        assert (
            await test_client.get(f"/architecture/relationships/{r['id']}", headers=hdrs())
        ).status_code == 404, r
    assert (
        await test_client.get(f"/architecture/relationships/{unrelated['id']}", headers=hdrs())
    ).status_code == 200
    assert (
        await ok(await test_client.get(f"/architecture/views/{vid}/connections", headers=hdrs()))
        == []
    )
    # Deleting an ELEMENT cascades the same way through its edges.
    r4 = await ok(await relationship(test_client, a, "assignment", b))
    r5 = await assoc(c, r4)
    await ok(await test_client.delete(f"/architecture/elements/{a['id']}", headers=hdrs()))
    assert (
        await test_client.get(f"/architecture/relationships/{r5['id']}", headers=hdrs())
    ).status_code == 404


async def test_notes_groups_and_lines(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    vid = w["view"]["id"]
    url = f"/architecture/views/{vid}"
    group = await ok(
        await test_client.post(
            f"{url}/objects",
            json={"kind": "group", "name": "Front office", "border_type": "tabbed"},
            headers=hdrs(),
        ),
        201,
    )
    note = await ok(
        await test_client.post(
            f"{url}/objects",
            json={"kind": "note", "text": "Remember\nthis", "border_type": "dogear"},
            headers=hdrs(),
        ),
        201,
    )
    assert (group["kind"], group["concept_ref"], group["name"]) == ("group", None, "Front office")
    assert note["text"] == "Remember\nthis"
    # Nesting an element into a group needs no relationship.
    nested = await ok(
        await test_client.put(
            f"{url}/objects/{w['o1']['id']}",
            json={
                "concept_ref": w["actor"]["id"],
                "parent_object_id": group["id"],
                "label": "${name}",
            },
            headers=hdrs(),
        )
    )
    assert nested["parent_object_id"] == group["id"] and nested["label"] == "${name}"
    # A line may join any two objects (note -> group) or an object and a connection.
    line = await ok(
        await test_client.post(
            f"{url}/connections",
            json={
                "kind": "line",
                "source_object_id": note["id"],
                "target_object_id": group["id"],
                "label": "see",
            },
            headers=hdrs(),
        ),
        201,
    )
    assert (line["kind"], line["relationship_ref"], line["label"]) == ("line", None, "see")
    to_conn = await test_client.post(
        f"{url}/connections",
        json={"kind": "line", "source_object_id": note["id"], "target_object_id": w["c"]["id"]},
        headers=hdrs(),
    )
    assert to_conn.status_code == 201, to_conn.text
    # ... but it may not dangle.
    findings = integrity(
        await test_client.post(
            f"{url}/connections",
            json={
                "kind": "line",
                "source_object_id": note["id"],
                "target_object_id": str(uuid.uuid4()),
            },
            headers=hdrs(),
        )
    )
    assert findings[0]["rule_id"] == "AM-REF-001"
    # A relationship connection cannot attach to a note, group or line.
    for end in (note["id"], group["id"], line["id"]):
        findings = integrity(
            await test_client.post(
                f"{url}/connections",
                json={
                    "relationship_ref": w["rel"]["id"],
                    "source_object_id": w["o1"]["id"],
                    "target_object_id": end,
                },
                headers=hdrs(),
            )
        )
        assert findings[0]["rule_id"] == "AM-VIEW-002", findings
    # Shape: concept iff element, relationship iff relationship.
    for bad in (
        {"kind": "note", "concept_ref": w["actor"]["id"]},
        {"kind": "element"},
    ):
        assert (
            await test_client.post(f"{url}/objects", json=bad, headers=hdrs())
        ).status_code == 422
    bad_line = {
        "kind": "line",
        "relationship_ref": w["rel"]["id"],
        "source_object_id": note["id"],
        "target_object_id": group["id"],
    }
    assert (
        await test_client.post(f"{url}/connections", json=bad_line, headers=hdrs())
    ).status_code == 422

    # The revision manifest carries them.
    mid = w["model"]["id"]
    sealed = await ok(
        await test_client.post(f"/modeling/models/{mid}/revisions", headers=hdrs()), 201
    )
    manifest = (
        await ok(
            await test_client.get(
                f"/modeling/models/{mid}/revisions/{sealed['revision_id']}", headers=hdrs()
            )
        )
    )["manifest"]
    (mv,) = manifest["views"]
    by_id = {o["id"]: o for o in mv["objects"]}
    assert (by_id[group["id"]]["kind"], by_id[group["id"]]["name"]) == ("group", "Front office")
    assert by_id[note["id"]]["text"] == "Remember\nthis" and by_id[note["id"]]["concept_id"] is None
    assert by_id[w["o1"]["id"]]["label"] == "${name}"
    lines = {c["id"]: c for c in mv["connections"]}
    assert lines[line["id"]]["kind"] == "line" and lines[line["id"]]["relationship_id"] is None
    # Deleting the group deletes what is nested in it (FK cascade, as in Archi) and the
    # line attached to it.
    await ok(await test_client.delete(f"{url}/objects/{group['id']}", headers=hdrs()))
    remaining = {o["id"] for o in await ok(await test_client.get(f"{url}/objects", headers=hdrs()))}
    assert (
        group["id"] not in remaining and w["o1"]["id"] not in remaining and note["id"] in remaining
    )
    conns = {c["id"] for c in await ok(await test_client.get(f"{url}/connections", headers=hdrs()))}
    assert line["id"] not in conns


async def test_documentation_on_notes_and_groups_only(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    url = f"/architecture/views/{w['view']['id']}/objects"
    note = await ok(
        await test_client.post(
            url, json={"kind": "note", "text": "t", "documentation": "why"}, headers=hdrs()
        ),
        201,
    )
    assert note["documentation"] == "why"
    group = await ok(
        await test_client.post(
            url, json={"kind": "group", "name": "G", "documentation": "scope"}, headers=hdrs()
        ),
        201,
    )
    refused = await test_client.post(
        url, json={"concept_ref": w["actor"]["id"], "documentation": "no"}, headers=hdrs()
    )
    assert refused.status_code == 422
    refused = await test_client.put(
        f"{url}/{w['o1']['id']}",
        json={"concept_ref": w["actor"]["id"], "documentation": "no"},
        headers=hdrs(),
    )
    assert refused.status_code == 422
    mid = w["model"]["id"]
    sealed = await ok(
        await test_client.post(f"/modeling/models/{mid}/revisions", headers=hdrs()), 201
    )
    manifest = (
        await ok(
            await test_client.get(
                f"/modeling/models/{mid}/revisions/{sealed['revision_id']}", headers=hdrs()
            )
        )
    )["manifest"]
    docs = {o["id"]: o["documentation"] for o in manifest["views"][0]["objects"]}
    assert (
        docs[note["id"]] == "why" and docs[group["id"]] == "scope" and docs[w["o1"]["id"]] is None
    )
    # A run over that manifest still works (the validator input takes the shared fields).
    run = await ok(
        await test_client.post(f"/modeling/models/{mid}/validation-runs", headers=hdrs()), 201
    )
    assert run["status"] == "complete"
