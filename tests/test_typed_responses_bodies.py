"""The response_models added for fr's typed client must not change a single body.

Each route below used to return a plain dict straight to the client. A response_model
filters and re-serializes that dict, so a field missing from the model would be DROPPED
silently and a mistyped one would turn a 200 into a 500. The handlers now also RETURN the
model instance, which must serialize to the same bytes. These tests pin the exact body
(full equality, not a subset) of every one of the 24 routes.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from httpx import AsyncClient, Response

from schemas.uml import UmlDiagramIn, UmlElementIn, UmlInteractionIn, UmlRelationshipIn
from tests.tenants import T
from tests.test_conformance import _PROJECT as _SEED_PROJECT
from tests.test_conformance import _seed_bridge
from tests.test_modeling_lifecycle import hdrs, new_model, ok, populated
from tests.test_openapi_typed_responses import FR_CONSUMED_TYPED
from tests.test_pm_crud import RISK_PAYLOAD
from tests.test_uml_storage import load_example, uml_model
from tools.architecture.conformance import DEFAULT_LAYER_ORDER, analyze_conformance
from tools.uml.profile import get_profile
from tools.uml.rules import ruleset_info

# Every operation a test below pins, so a 25th typed route cannot go unpinned silently.
PINNED: frozenset[str] = frozenset(
    {
        "analyze_architecture_conformance",
        "delete_architecture_element",
        "delete_architecture_relationship",
        "delete_architecture_standard",
        "delete_architecture_view",
        "delete_architecture_view_connection",
        "delete_architecture_view_object",
        "delete_decision",
        "delete_model",
        "delete_principle",
        "delete_programme",
        "delete_requirement",
        "delete_risk",
        "delete_toc_edge",
        "delete_toc_node",
        "delete_toc_tree",
        "delete_trace_link",
        "delete_uml_diagram",
        "delete_uml_diagram_connection",
        "delete_uml_diagram_object",
        "delete_uml_element",
        "delete_uml_interaction",
        "delete_uml_relationship",
        "get_uml_profile",
    }
)


def test_every_typed_route_has_a_pinned_body() -> None:
    assert PINNED == FR_CONSUMED_TYPED


def assert_body(resp: Response, expected: Any, msg: str = "") -> None:
    """Exact body, AND the exact bytes the route sent before it had a response_model
    (a plain return goes through ``jsonable_encoder`` + ``JSONResponse``): same keys,
    same key order, same encoding of UUIDs and floats."""
    assert resp.status_code == 200, resp.text
    assert resp.json() == expected, msg
    assert resp.content == JSONResponse(jsonable_encoder(expected)).body, msg


async def _delete_ok(client: AsyncClient, path: str, expected: Any, **kw: Any) -> None:
    assert_body(await client.delete(path, **kw), expected, path)


async def test_delete_governed_entity_bodies(
    authenticated_client: AsyncClient, sample_requirement, sample_decision, sample_principle
):
    """delete_requirement / delete_decision / delete_principle."""
    for path, row in (
        ("/requirements", sample_requirement),
        ("/decisions", sample_decision),
        ("/principles", sample_principle),
    ):
        await _delete_ok(
            authenticated_client, f"{path}/{row.id}", {"status": "deleted", "id": str(row.id)}
        )


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/pm/risks", RISK_PAYLOAD),
        ("/architecture/standards", None),
    ],
)
async def test_delete_created_entity_body(
    authenticated_client: AsyncClient, path: str, payload: dict[str, Any] | None
):
    """delete_risk / delete_architecture_standard."""
    payload = payload or {
        "title": "Typed standard",
        "project_id": "typed-project",
        "standard_type": "technology_standard",
    }
    created = await authenticated_client.post(path, json=payload)
    assert created.status_code in (200, 201), created.text
    eid = created.json()["id"]
    await _delete_ok(authenticated_client, f"{path}/{eid}", {"status": "deleted", "id": eid})


async def test_delete_programme_body(authenticated_client: AsyncClient):
    created = await authenticated_client.post("/pm/programmes", json={"title": "Typed P"})
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    resp = await authenticated_client.delete(f"/pm/programmes/{pid}")
    assert_body(resp, {"status": "deleted", "id": pid})


async def test_delete_toc_tree_and_node_bodies(authenticated_client: AsyncClient):
    tree = await authenticated_client.post("/toc", json={"title": "T", "tree_type": "io_map"})
    assert tree.status_code == 200, tree.text
    tid = tree.json()["id"]
    node_ids = []
    for kind, title in (("goal", "G"), ("csf", "C"), ("necessary_condition", "N")):
        node = await authenticated_client.post(
            f"/toc/{tid}/nodes",
            json={"title": title, "statement": f"{title} s", "node_kind": kind},
        )
        assert node.status_code == 200, node.text
        node_ids.append(node.json()["id"])

    edge = await authenticated_client.post(
        f"/toc/{tid}/edges",
        json={
            "source_id": node_ids[1],
            "target_id": node_ids[2],
            "relationship_type": "toc_necessity",
        },
    )
    assert edge.status_code == 200, edge.text
    eid = edge.json()["id"]
    resp = await authenticated_client.delete(f"/toc/{tid}/edges/{eid}")
    assert_body(resp, {"status": "deleted", "id": eid})

    resp = await authenticated_client.delete(f"/toc/{tid}/nodes/{node_ids[0]}")
    assert_body(resp, {"status": "deleted", "id": node_ids[0]})

    resp = await authenticated_client.delete(f"/toc/{tid}")
    assert_body(resp, {"status": "deleted", "id": tid, "nodes_deleted": 2})


async def test_delete_uml_element_body(test_client: AsyncClient):
    model = await new_model(
        test_client, language="uml", language_version="2.5.1-p1", _headers=hdrs()
    )
    el = await ok(
        await test_client.post(
            "/uml/elements",
            json={"model_id": model["id"], "kind": "class", "name": "C"},
            headers=hdrs(),
        ),
        201,
    )
    resp = await test_client.delete(f"/uml/elements/{el['id']}", headers=hdrs())
    assert_body(resp, {"status": "deleted", "id": el["id"], "cascaded": {}})


async def test_delete_archimate_and_modeling_bodies(test_client: AsyncClient):
    """delete_architecture_view_connection / _view_object / _view / _relationship /
    _element, delete_trace_link and delete_model, leaf first so each is a plain ack."""
    w = await populated(test_client)
    vid = w["view"]["id"]
    link = await ok(
        await test_client.post(
            "/modeling/trace-links",
            json={
                "source": {"kind": "element", "id": w["actor"]["id"]},
                "target": {"kind": "element", "id": w["role"]["id"]},
                "link_type": "refines",
            },
            headers=hdrs(),
        ),
        201,
    )
    for path, rid in (
        (f"/modeling/trace-links/{link['id']}", link["id"]),
        (f"/architecture/views/{vid}/connections/{w['c']['id']}", w["c"]["id"]),
        (f"/architecture/views/{vid}/objects/{w['o1']['id']}", w["o1"]["id"]),
        (f"/architecture/views/{vid}", vid),
        (f"/architecture/relationships/{w['rel']['id']}", w["rel"]["id"]),
        (f"/architecture/elements/{w['actor']['id']}", w["actor"]["id"]),
        (f"/modeling/models/{w['model']['id']}", w["model"]["id"]),
    ):
        await _delete_ok(test_client, path, {"status": "deleted", "id": rid}, headers=hdrs())


async def test_delete_uml_occurrence_and_concept_bodies(test_client: AsyncClient):
    """delete_uml_diagram_connection / _diagram_object / _diagram / _relationship /
    _interaction / _element on the clean example model, with their exact ``cascaded``."""
    mid = (await uml_model(test_client))["id"]
    ids = await load_example(test_client, mid)

    async def delete(path: str, rid: str, cascaded: dict[str, list[str]]) -> None:
        expected = {"status": "deleted", "id": ids[rid], "cascaded": cascaded}
        await _delete_ok(test_client, path, expected, headers=hdrs())

    await delete(f"/uml/diagrams/{ids['d_comp']}/connections/{ids['c_conn']}", "c_conn", {})
    await delete(
        f"/uml/diagrams/{ids['d_class']}/objects/{ids['ob_svc']}",
        "ob_svc",
        {"diagram_connection": sorted([ids["c_assoc"], ids["c_gen"]])},
    )
    await delete(
        f"/uml/diagrams/{ids['d_class2']}", "d_class2", {"diagram_object": [ids["ob2_svc"]]}
    )
    await delete(f"/uml/relationships/{ids['dep1']}", "dep1", {})
    await delete(
        f"/uml/interactions/{ids['int1']}",
        "int1",
        {
            "diagram_object": sorted([ids["ol_user"], ids["ol_svc"]]),
            "diagram_connection": [ids["c_m1"]],
        },
    )
    await delete(f"/uml/elements/{ids['actor_user']}", "actor_user", {})


async def test_uml_metamodel_body_is_unchanged(test_client: AsyncClient):
    """Rebuild the body exactly as the handler did before it had a response_model."""
    body = asdict(get_profile())
    for key, value in list(body.items()):
        if isinstance(value, frozenset):
            body[key] = sorted(value)
        elif isinstance(value, dict):
            body[key] = {k: sorted(v) if isinstance(v, frozenset) else v for k, v in value.items()}
    body["ruleset"] = ruleset_info().model_dump()
    body["schemas"] = {
        "element": UmlElementIn.model_json_schema(),
        "relationship": UmlRelationshipIn.model_json_schema(),
        "interaction": UmlInteractionIn.model_json_schema(),
        "diagram": UmlDiagramIn.model_json_schema(),
    }
    resp = await test_client.get("/uml/metamodel", headers=hdrs())
    assert_body(resp, json.loads(json.dumps(body)))


async def test_conformance_body_passes_free_form_code_units_through(
    test_client: AsyncClient,
):
    """Caller-supplied code units are free-form dicts: a non-string ``name`` or
    ``container`` must come back verbatim, not 500 on the response model."""
    units = [
        {"id": "a", "name": None, "layer": "data", "container": None, "dependencies": ["b"]},
        {"id": "b", "name": 7, "layer": "presentation", "container": None, "dependencies": []},
    ]
    resp = await test_client.post(
        "/architecture/tools/conformance",
        json={"project_id": "typed-conformance-empty", "code_units": units},
        headers={"X-Tenant-Id": T("user-123")},
    )
    expected = analyze_conformance([], [], units, DEFAULT_LAYER_ORDER)
    assert_body(resp, json.loads(json.dumps(expected)))
    assert resp.json()["illegal_dependencies"]["illegal_count"] == 1


async def test_conformance_body_over_a_populated_model(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """A bound, realizing model drives the element-summary and binding paths (the empty
    case above never builds an element summary). The model's rows are server-generated,
    so the expected body is the exact dict ``analyze_conformance`` built inside the
    route, captured on the way out: the response must be the bytes a plain return of
    THAT dict sent before the response_model (no key dropped, reordered or re-typed)."""
    import routers.architecture_tools as tools_router

    produced: list[dict[str, Any]] = []

    def _capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
        out = analyze_conformance(*args, **kwargs)
        produced.append(out)
        return out

    monkeypatch.setattr(tools_router, "analyze_conformance", _capture)
    app = await _seed_bridge(authenticated_client)
    resp = await authenticated_client.post(
        "/architecture/tools/conformance",
        json={
            "project_id": _SEED_PROJECT,
            "code_units": [
                {
                    "id": "svc",
                    "name": "svc",
                    "layer": "application",
                    "container": "demo",
                    "dependencies": [],
                },
                {
                    "id": "orphan",
                    "name": "orphan",
                    "layer": "data",
                    "container": "demo",
                    "dependencies": ["svc"],
                },
            ],
        },
    )
    assert len(produced) == 1
    assert_body(resp, json.loads(json.dumps(jsonable_encoder(produced[0]))))
    coverage = resp.json()["realization_coverage"]
    assert coverage["realized_count"] == 1
    assert [e["id"] for e in coverage["realized"]] == [app["id"]]
    assert [u["id"] for u in coverage["orphan_artifacts"] + coverage["unbound_code_units"]] == [
        "orphan"
    ]
    assert coverage["dead_bindings"] == []
