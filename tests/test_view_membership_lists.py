"""Legacy view membership lists (``element_ids`` / ``relationship_ids``), real Postgres.

Every id a view write introduces must be a visible row of the view's own model
(``422 model_integrity`` AM-REF-001 otherwise), and a listed relationship's ends must be
listed too (AM-VIEW-002, as for a relationship connection). Deleting an element or a
relationship — single, cascade, batch — removes it from every view's lists in the same
transaction, and a revision manifest only ever carries the model's own rows.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from models.architecture import ArchitectureView
from tests.test_modeling_lifecycle import (
    OTHER_TENANT,
    element,
    hdrs,
    new_model,
    ok,
    relationship,
    view,
)
from tools.architecture.exchange import NS

pytestmark = pytest.mark.asyncio


def findings(resp) -> dict[str, list[dict[str, Any]]]:
    """``rule_id`` -> findings of a ``422 model_integrity`` refusal."""
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "model_integrity", err
    out: dict[str, list[dict[str, Any]]] = {}
    for f in err["findings"]:
        out.setdefault(f["rule_id"], []).append(f)
    return out


async def graph(client: AsyncClient) -> dict[str, Any]:
    """A model with actor -assignment-> role and a view listing all three."""
    m = await new_model(client)
    actor = await ok(await element(client, m["id"], "business_actor"))
    role = await ok(await element(client, m["id"], "business_role"))
    rel = await ok(await relationship(client, actor, "assignment", role))
    v = await ok(
        await view(
            client,
            m["id"],
            element_ids=[actor["id"], role["id"]],
            relationship_ids=[rel["id"]],
        )
    )
    return {"model": m, "actor": actor, "role": role, "rel": rel, "view": v}


async def get_view(client: AsyncClient, vid: str) -> dict[str, Any]:
    return await ok(await client.get(f"/architecture/views/{vid}", headers=hdrs()))


async def test_create_accepts_the_models_own_visible_rows(test_client: AsyncClient) -> None:
    g = await graph(test_client)
    v = await get_view(test_client, g["view"]["id"])
    assert v["element_ids"] == [g["actor"]["id"], g["role"]["id"]]
    assert v["relationship_ids"] == [g["rel"]["id"]]


async def test_create_refuses_unresolvable_ids_naming_each(test_client: AsyncClient) -> None:
    g = await graph(test_client)
    other_model = await new_model(test_client, title="other")
    elsewhere = await ok(await element(test_client, other_model["id"]))
    foreign_model = await new_model(test_client, _headers=hdrs(tenant=OTHER_TENANT))
    foreign = await ok(
        await element(test_client, foreign_model["id"], _headers=hdrs(tenant=OTHER_TENANT))
    )
    missing = str(uuid.uuid4())
    resp = await view(
        test_client,
        g["model"]["id"],
        element_ids=[g["actor"]["id"], elsewhere["id"], foreign["id"], missing],
        relationship_ids=[missing],
    )
    by_rule = findings(resp)
    assert set(by_rule) == {"AM-REF-001"}
    evidence = {k: v for f in by_rule["AM-REF-001"] for k, v in f["evidence"].items()}
    assert evidence == {
        "element_ids": sorted([elsewhere["id"], foreign["id"], missing]),
        "relationship_ids": [missing],
    }
    # Another tenant's row is refused exactly like a missing one.
    assert {t["id"] for f in by_rule["AM-REF-001"] for t in f["targets"]} >= {foreign["id"]}


async def test_a_relationship_needs_its_ends_in_the_view(test_client: AsyncClient) -> None:
    g = await graph(test_client)
    resp = await view(
        test_client,
        g["model"]["id"],
        element_ids=[g["actor"]["id"]],
        relationship_ids=[g["rel"]["id"]],
    )
    by_rule = findings(resp)
    assert set(by_rule) == {"AM-VIEW-002"}
    assert by_rule["AM-VIEW-002"][0]["evidence"] == {"relationship_ids": [g["rel"]["id"]]}


async def test_update_refuses_new_bad_ids_but_not_ones_it_already_had(
    test_client: AsyncClient, test_session_factory
) -> None:
    g = await graph(test_client)
    vid = g["view"]["id"]
    body = {
        "title": "renamed",
        "viewpoint": "layered",
        "element_ids": [g["actor"]["id"], g["role"]["id"], str(uuid.uuid4())],
        "relationship_ids": [g["rel"]["id"]],
    }
    resp = await test_client.put(f"/architecture/views/{vid}", json=body, headers=hdrs())
    assert set(findings(resp)) == {"AM-REF-001"}

    # A stale id stored before the check existed does not freeze the view.
    stale = str(uuid.uuid4())
    async with test_session_factory() as s:
        await s.execute(
            update(ArchitectureView)
            .where(ArchitectureView.id == uuid.UUID(vid))
            .values(element_ids=[g["actor"]["id"], g["role"]["id"], stale])
        )
        await s.commit()
    body["element_ids"] = [g["actor"]["id"], g["role"]["id"], stale]
    resp = await test_client.put(f"/architecture/views/{vid}", json=body, headers=hdrs())
    assert resp.status_code == 200, resp.text

    # ... and a revision manifest never carries it.
    rev = await ok(
        await test_client.post(f"/modeling/models/{g['model']['id']}/revisions", headers=hdrs()),
        201,
    )
    detail = await ok(
        await test_client.get(
            f"/modeling/models/{g['model']['id']}/revisions/{rev['revision_id']}",
            headers=hdrs(),
        )
    )
    (mv,) = [v for v in detail["manifest"]["attributes"]["views"] if v["id"] == vid]
    assert mv["element_ids"] == [g["actor"]["id"], g["role"]["id"]]
    assert mv["relationship_ids"] == [g["rel"]["id"]]


async def test_deleting_an_element_prunes_it_and_its_cascaded_edges(
    test_client: AsyncClient,
) -> None:
    g = await graph(test_client)
    resp = await test_client.delete(f"/architecture/elements/{g['role']['id']}", headers=hdrs())
    assert resp.status_code == 200, resp.text
    v = await get_view(test_client, g["view"]["id"])
    assert v["element_ids"] == [g["actor"]["id"]]
    assert v["relationship_ids"] == []


async def test_deleting_a_relationship_prunes_it_and_its_dependents(
    test_client: AsyncClient,
) -> None:
    g = await graph(test_client)
    note = await ok(await element(test_client, g["model"]["id"], "business_object"))
    # An association ending on the assignment: it goes with it (relationship_cascade).
    dep = await ok(
        await test_client.post(
            "/architecture/relationships",
            json={
                "source_entity_type": "architecture_elements",
                "source_id": note["id"],
                "target_entity_type": "architecture_relationships",
                "target_id": g["rel"]["id"],
                "relationship_type": "association",
            },
            headers=hdrs(),
        )
    )
    body = {
        "title": "v",
        "viewpoint": "layered",
        "element_ids": [g["actor"]["id"], g["role"]["id"], note["id"]],
        "relationship_ids": [g["rel"]["id"], dep["id"]],
    }
    resp = await test_client.put(
        f"/architecture/views/{g['view']['id']}", json=body, headers=hdrs()
    )
    assert resp.status_code == 200, resp.text
    resp = await test_client.delete(f"/architecture/relationships/{g['rel']['id']}", headers=hdrs())
    assert resp.status_code == 200, resp.text
    v = await get_view(test_client, g["view"]["id"])
    assert v["relationship_ids"] == []
    assert v["element_ids"] == [g["actor"]["id"], g["role"]["id"], note["id"]]


async def test_batch_delete_prunes_every_view(test_client: AsyncClient) -> None:
    g = await graph(test_client)
    second = await ok(await view(test_client, g["model"]["id"], element_ids=[g["actor"]["id"]]))
    resp = await test_client.post(
        "/architecture/elements/batch-delete",
        json={"ids": [g["actor"]["id"]]},
        headers=hdrs(),
    )
    assert resp.status_code == 200, resp.text
    assert (await get_view(test_client, second["id"]))["element_ids"] == []
    first = await get_view(test_client, g["view"]["id"])
    assert first["element_ids"] == [g["role"]["id"]]
    assert first["relationship_ids"] == []


async def test_view_object_view_ref_must_be_a_view_of_the_same_model(
    test_client: AsyncClient,
) -> None:
    g = await graph(test_client)
    detail = await ok(await view(test_client, g["model"]["id"]))
    elsewhere = await ok(await view(test_client, (await new_model(test_client))["id"]))
    url = f"/architecture/views/{g['view']['id']}/objects"
    body = {"concept_ref": g["actor"]["id"], "bounds": {"x": 0, "y": 0, "w": 120, "h": 55}}
    refused = await test_client.post(
        url, json={**body, "view_ref": elsewhere["id"]}, headers=hdrs()
    )
    assert findings(refused)["AM-REF-001"][0]["evidence"] == {"view_ref": elsewhere["id"]}
    made = await ok(
        await test_client.post(url, json={**body, "view_ref": detail["id"]}, headers=hdrs()), 201
    )
    assert made["view_ref"] == detail["id"]
    # Deleting the referenced view clears the reference (FK ON DELETE SET NULL).
    resp = await test_client.delete(f"/architecture/views/{detail['id']}", headers=hdrs())
    assert resp.status_code == 200, resp.text
    objs = await test_client.get(url, headers=hdrs())
    assert [o["view_ref"] for o in objs.json()] == [None]


async def test_a_model_exchange_op_must_be_exportable(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    resp = await test_client.post(
        f"/modeling/models/{m['id']}/change-sets",
        json={
            "ops": [
                {
                    "op": "update",
                    "kind": "model_exchange",
                    # Well-formed and in the right namespace, but not schema-valid.
                    "attributes": {
                        "exchange": {"metadata": f'<metadata xmlns="{NS}"><bogus/></metadata>'}
                    },
                }
            ],
            "rationale": "r",
            "evaluator_ref": "t",
            "base_head": 0,
        },
        headers=hdrs(),
    )
    cs = await ok(resp, 201)
    assert cs["status"] == "invalid"
    (f,) = cs["blocking_findings"]
    assert f["rule_id"] == "CS-OP-001" and "not exportable" in f["message"]


async def _fidelity_model(client: AsyncClient) -> dict[str, Any]:
    """FIDELITY_DOC applied: view ``main`` names viewpoint ``vp-1`` and holds one node whose
    ``view_ref`` is view ``detail``."""
    from tests.test_archimate_exchange_archi import FIDELITY_DOC
    from tests.test_modeling_exchange_routes import apply, import_new

    out = await import_new(client, FIDELITY_DOC)
    assert out["change_set"]["status"] == "proposed", out["change_set"]["blocking_findings"]
    applied = await apply(client, out["change_set"]["id"])
    mid = out["model_id"]
    model = await ok(await client.get(f"/modeling/models/{mid}", headers=hdrs()))
    return {
        "model_id": mid,
        "head": model["head"],
        "main": applied["id_map"]["v-main"],
        "detail": applied["id_map"]["v-detail"],
    }


async def _change_set(client: AsyncClient, f: dict[str, Any], ops: list[dict]) -> dict:
    resp = await client.post(
        f"/modeling/models/{f['model_id']}/change-sets",
        json={"ops": ops, "rationale": "r", "evaluator_ref": "t", "base_head": f["head"]},
        headers=hdrs(),
    )
    return await ok(resp, 201)


async def test_removing_a_viewpoint_a_view_still_names_is_refused(
    test_client: AsyncClient,
) -> None:
    f = await _fidelity_model(test_client)
    drop_viewpoints = {"op": "update", "kind": "model_exchange", "attributes": {"exchange": {}}}
    cs = await _change_set(test_client, f, [drop_viewpoints])
    assert cs["status"] == "invalid"
    (finding,) = [b for b in cs["blocking_findings"] if b["rule_id"] == "AM-REF-001"]
    assert finding["path"] == "/ops/0"
    assert finding["evidence"] == {"views": [{"view_id": f["main"], "viewpoint_ref": "vp-1"}]}

    # The same removal together with the view dropping its reference is fine (order of the
    # ops does not matter: an import replaces the exchange content first).
    unref = {"op": "update", "kind": "view", "id": f["main"], "attributes": {"viewpoint_ref": None}}
    cs = await _change_set(test_client, f, [drop_viewpoints, unref])
    assert cs["status"] == "proposed", cs["blocking_findings"]
    from tests.test_modeling_exchange_routes import apply

    await apply(test_client, cs["id"])
    exported = await test_client.get(f"/modeling/models/{f['model_id']}/exchange", headers=hdrs())
    assert exported.status_code == 200
    assert b"viewpointRef" not in exported.content


async def test_deleting_a_view_clears_view_refs_in_the_evaluation_as_the_fk_does(
    test_client: AsyncClient,
) -> None:
    f = await _fidelity_model(test_client)
    url = f"/architecture/views/{f['main']}/objects"
    (node,) = [o for o in (await ok(await test_client.get(url, headers=hdrs()))) if o["view_ref"]]
    assert node["view_ref"] == f["detail"]
    cs = await _change_set(
        test_client,
        f,
        [
            {"op": "delete", "kind": "view", "id": f["detail"]},
            # Applied one by one, this update sees the node with view_ref NULL (FK SET NULL);
            # the evaluation must see the same state, not a dangling reference.
            {
                "op": "update",
                "kind": "view_object",
                "id": node["id"],
                "attributes": {"bounds": {**node["bounds"], "x": node["bounds"]["x"] + 10}},
            },
        ],
    )
    assert cs["status"] == "proposed", cs["blocking_findings"]
    from tests.test_modeling_exchange_routes import apply

    await apply(test_client, cs["id"])
    after = await ok(await test_client.get(url, headers=hdrs()))
    assert [o["view_ref"] for o in after if o["id"] == node["id"]] == [None]
