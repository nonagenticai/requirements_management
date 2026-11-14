"""Folder trees (``organizations``) and optional viewpoints over HTTP, real Postgres.

The tree is model content (manifest, head bump, change-set op), round-trips through the
exchange format with its folders (archisurance fixed point), sheds items of deleted rows,
refuses items outside the model, and is isolated per tenant and private subject. A view
without a viewpoint is stored as NULL and imported/exported without loss.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from schemas.modeling import ArchimateOrganization
from tests.test_modeling_exchange_routes import apply, import_into, import_new, seal
from tests.test_modeling_lifecycle import (
    ALICE,
    BOB,
    OTHER_TENANT,
    element,
    etag,
    hdrs,
    head_of,
    new_model,
    ok,
    populated,
)
from tools.architecture.exchange import export_exchange, import_exchange

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures" / "exchange"
ARCHISURANCE = (FIXTURES / "archisurance.xml").read_bytes()
NATIVE = (FIXTURES / "archi510_native_export.xml").read_bytes()


def folders(items: list[str], **kw: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": "f-root",
            "name": "Business",
            "documentation": None,
            "items": items,
            "children": [
                {"id": "f-sub", "name": "Sub", "documentation": "d", "items": [], "children": []}
            ],
            **kw,
        }
    ]


async def put_tree(client: AsyncClient, mid: str, tree: Any, **kw: Any):
    return await client.put(
        f"/modeling/models/{mid}/organizations", json={"organizations": tree}, headers=hdrs(**kw)
    )


def all_items(tree: list[dict[str, Any]] | None) -> list[str]:
    out: list[str] = []
    for f in tree or []:
        out += f["items"] + all_items(f["children"])
    return out


async def test_put_get_tree_is_content_and_bumps_head(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    empty = await test_client.get(f"/modeling/models/{mid}/organizations", headers=hdrs())
    assert (await ok(empty))["organizations"] is None
    head = etag(empty)
    tree = folders([w["actor"]["id"], w["rel"]["id"], w["view"]["id"]])
    put = await put_tree(test_client, mid, tree, if_match=head)
    body = await ok(put)
    assert body["head"] == head + 1 == etag(put) == await head_of(test_client, mid)
    assert body["organizations"] == tree
    got = await ok(await test_client.get(f"/modeling/models/{mid}/organizations", headers=hdrs()))
    assert got["organizations"] == tree

    stale = await put_tree(test_client, mid, None, if_match=head)
    assert stale.status_code == 412 and stale.json()["error"]["code"] == "stale_head"

    rev = await seal(test_client, mid)
    manifest = (
        await ok(
            await test_client.get(
                f"/modeling/models/{mid}/revisions/{rev['revision_id']}", headers=hdrs()
            )
        )
    )["manifest"]
    assert all_items(manifest["organizations"]) == all_items(tree)
    xml = (
        await test_client.get(
            f"/modeling/models/{mid}/revisions/{rev['revision_id']}/exchange", headers=hdrs()
        )
    ).content
    parsed = import_exchange(xml)
    assert parsed.snapshot.organizations is not None
    assert [f.name for f in parsed.snapshot.organizations] == ["Business"]

    # Changing only the tree changes the content hash.
    await ok(await put_tree(test_client, mid, None))
    rev2 = await seal(test_client, mid)
    assert rev2["content_hash"] != rev["content_hash"]


async def test_put_refuses_items_outside_the_model(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    other = await populated(test_client)
    resp = await put_tree(test_client, w["model"]["id"], folders([other["actor"]["id"]]))
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "model_integrity" and err["findings"][0]["rule_id"] == "AM-REF-001"
    bad = await test_client.put(
        f"/modeling/models/{w['model']['id']}/organizations",
        json={"organizations": [{"items": "not-a-list"}]},
        headers=hdrs(),
    )
    assert bad.status_code == 422
    uml = await new_model(test_client, language="uml", language_version="2.5.1-p1")
    resp = await put_tree(test_client, uml["id"], None)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "organizations_unsupported_language"


async def test_deleting_a_concept_removes_it_from_folders(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    await ok(
        await put_tree(
            test_client, mid, folders([w["actor"]["id"], w["role"]["id"], w["rel"]["id"]])
        )
    )
    head = await head_of(test_client, mid)
    # Deleting the actor cascades to the relationship (its end).
    await ok(await test_client.delete(f"/architecture/elements/{w['actor']['id']}", headers=hdrs()))
    assert await head_of(test_client, mid) == head + 1
    got = await ok(await test_client.get(f"/modeling/models/{mid}/organizations", headers=hdrs()))
    assert all_items(got["organizations"]) == [w["role"]["id"]]
    assert [c["name"] for c in got["organizations"][0]["children"]] == ["Sub"]  # folders stay
    rev = await seal(test_client, mid)
    manifest = (
        await ok(
            await test_client.get(
                f"/modeling/models/{mid}/revisions/{rev['revision_id']}", headers=hdrs()
            )
        )
    )["manifest"]
    assert all_items(manifest["organizations"]) == [w["role"]["id"]]
    draft = await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())
    assert w["actor"]["id"].encode() not in draft.content


async def test_change_set_organization_op(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    mid = m["id"]
    ops = [
        {
            "op": "create",
            "kind": "element",
            "id": "new-actor",
            "attributes": {"title": "A", "element_type": "business_actor"},
        },
        {
            "op": "update",
            "kind": "organization",
            "attributes": {"organizations": folders(["new-actor"])},
        },
    ]
    cs = await ok(
        await test_client.post(
            f"/modeling/models/{mid}/change-sets",
            json={"ops": ops, "rationale": "folders", "base_head": 0},
            headers=hdrs(),
        ),
        201,
    )
    assert cs["status"] == "proposed", cs["blocking_findings"]
    applied = await apply(test_client, cs["id"])
    real = applied["id_map"]["new-actor"]
    got = await ok(await test_client.get(f"/modeling/models/{mid}/organizations", headers=hdrs()))
    assert all_items(got["organizations"]) == [real]
    assert got["head"] == applied["applied_head"]

    for bad in (
        {
            "op": "update",
            "kind": "organization",
            "attributes": {"organizations": folders(["nope"])},
        },
        {"op": "create", "kind": "organization", "attributes": {"organizations": None}},
        {"op": "update", "kind": "organization", "attributes": {}},
        {"op": "update", "kind": "organization", "attributes": {"organizations": [{"items": 1}]}},
    ):
        resp = await ok(
            await test_client.post(
                f"/modeling/models/{mid}/change-sets",
                json={"ops": [bad], "rationale": "bad", "base_head": got["head"]},
                headers=hdrs(),
            ),
            201,
        )
        assert resp["status"] == "invalid", bad
        assert resp["blocking_findings"][0]["rule_id"] == "CS-OP-001"


async def test_archisurance_with_folders_round_trips_to_a_fixed_point(
    test_client: AsyncClient,
) -> None:
    source = archisurance_with_folders()
    parsed = import_exchange(source)
    assert parsed.snapshot.organizations, "the document must carry a custom folder tree"
    out = await import_new(test_client, source)
    assert out["change_set"]["status"] == "proposed"
    assert "STAGE-ORGANIZATIONS" not in {loss["code"] for loss in out["losses"]}
    assert out["change_set"]["ops"][-1]["kind"] == "organization"
    mid = out["model_id"]
    applied = await apply(test_client, out["change_set"]["id"])
    tree = (
        await ok(await test_client.get(f"/modeling/models/{mid}/organizations", headers=hdrs()))
    )["organizations"]
    expected = [
        applied["id_map"].get(i, i) for f in parsed.snapshot.organizations for i in _items(f)
    ]
    assert sorted(all_items(tree)) == sorted(expected)

    exported = (await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())).content
    reparsed = import_exchange(exported)
    assert _names(reparsed.snapshot.organizations) == _names(parsed.snapshot.organizations)
    again = await ok(await import_into(test_client, mid, exported), 201)
    assert again["change_set"]["ops"] == []
    original = await ok(await import_into(test_client, mid, source), 201)
    assert original["change_set"]["ops"] == []
    # The pinned fixture itself (no folders) leaves the stored tree alone: no ops either.
    plain = await ok(await import_into(test_client, mid, ARCHISURANCE), 201)
    assert plain["change_set"]["ops"] == []


def archisurance_with_folders() -> bytes:
    """Archisurance (pinned fixture, which has no folders) with a custom nested tree."""
    base = import_exchange(ARCHISURANCE)
    snap = base.snapshot
    business = [c.id for c in snap.concepts if c.type.startswith("business_")]
    apps = [c.id for c in snap.concepts if c.type.startswith("application_")]
    tree = [
        ArchimateOrganization(
            id="f-business",
            name="Business",
            documentation="who and how",
            items=business[: len(business) // 2],
            children=[
                ArchimateOrganization(
                    id="f-business-rest", name="More", items=business[len(business) // 2 :]
                )
            ],
        ),
        ArchimateOrganization(id="f-apps", name="Applications", items=apps),
        ArchimateOrganization(
            id="f-rest",
            name="Everything else",
            items=[r.id for r in snap.relationships] + [v.id for v in snap.views or []],
        ),
    ]
    return export_exchange(
        snap.model_copy(update={"organizations": tree}),
        model_id=base.model_id or "archisurance",
        name=base.model_name,
        documentation=base.model_documentation,
    )


def _items(folder: Any) -> list[str]:
    return list(folder.items) + [i for c in folder.children for i in _items(c)]


def _names(folders_: Any) -> list[Any]:
    return sorted((f.name or "", f.documentation or "", _names(f.children)) for f in folders_ or [])


async def test_nullable_viewpoint_round_trips(test_client: AsyncClient) -> None:
    parsed = import_exchange(NATIVE)
    assert any(v.viewpoint is None for v in parsed.snapshot.views or [])
    out = await import_new(test_client, NATIVE)
    assert "STAGE-VIEWPOINT-DEFAULTED" not in {loss["code"] for loss in out["losses"]}
    applied = await apply(test_client, out["change_set"]["id"])
    no_vp = next(v for v in parsed.snapshot.views or [] if v.viewpoint is None)
    vid = applied["id_map"][no_vp.id]
    view = await ok(await test_client.get(f"/architecture/views/{vid}", headers=hdrs()))
    assert view["viewpoint"] is None
    exported = (
        await test_client.get(f"/modeling/models/{out['model_id']}/exchange", headers=hdrs())
    ).content
    back = import_exchange(exported)
    assert next(v for v in back.snapshot.views or [] if v.name == no_vp.name).viewpoint is None
    rendered = await test_client.get(f"/architecture/views/{vid}/render", headers=hdrs())
    assert rendered.status_code == 200

    # The plain API: a view may be created without a viewpoint.
    created = await ok(
        await test_client.post(
            "/architecture/views",
            json={"title": "free", "model_id": out["model_id"]},
            headers=hdrs(),
        )
    )
    assert created["viewpoint"] is None
    run = await ok(
        await test_client.post(
            f"/modeling/models/{out['model_id']}/validation-runs", headers=hdrs()
        ),
        201,
    )
    assert not [r for r in run["enabled_rules"] if r is None]


async def test_organizations_isolation(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    path = f"/modeling/models/{mid}/organizations"
    assert (await test_client.get(path, headers=hdrs(tenant=OTHER_TENANT))).status_code == 404
    assert (await put_tree(test_client, mid, None, tenant=OTHER_TENANT)).status_code == 404
    private = await new_model(test_client, visibility="user")
    actor = await ok(
        await element(
            test_client, private["id"], "business_actor", _headers=hdrs(ALICE, visibility="user")
        )
    )
    await ok(await put_tree(test_client, private["id"], folders([actor["id"]]), subject=ALICE))
    ppath = f"/modeling/models/{private['id']}/organizations"
    assert (await test_client.get(ppath, headers=hdrs(BOB))).status_code == 404
    assert (await put_tree(test_client, private["id"], None, subject=BOB)).status_code == 404
    assert all_items(
        (await ok(await test_client.get(ppath, headers=hdrs(ALICE))))["organizations"]
    ) == [actor["id"]]
    # BOB cannot smuggle ALICE's private element into his own model's folders.
    bobs = await new_model(test_client, _headers=hdrs(BOB))
    resp = await put_tree(test_client, bobs["id"], folders([actor["id"]]), subject=BOB)
    assert resp.status_code == 422
