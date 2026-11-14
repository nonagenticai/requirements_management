"""Stored UML (target spec §8.2) on the shared modeling lifecycle (§7), over HTTP, real Postgres.

Plan Phase 5 acceptance: class / component / sequence examples are created, sealed,
validated (clean), rendered and traced to an ArchiMate application component without
repository files; unsupported constructs are explicit (``UML-UNSUP-001`` makes a run not
clean); one class shown on two diagrams keeps one identity. Plus every write-time
refusal, tenant and private-subject isolation on every route, ``If-Match`` / ``ETag``,
``Idempotency-Key``, UML change sets, revision immutability and delete cascades.

The example model is ``tests/test_uml_rules.py``'s clean ``_BASE`` snapshot, replayed
through the routes (its ids are local; the server assigns row ids).
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from services import uml_manifest
from services.uml_draft import map_refs
from settings import get_settings
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
)
from tests.test_uml_rules import base
from tools.uml import rules as uml_rules

pytestmark = pytest.mark.asyncio

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CSP = "default-src 'none'; style-src 'unsafe-inline'"


async def uml_model(client: AsyncClient, headers: dict | None = None, **extra: Any) -> dict:
    return await new_model(
        client,
        language="uml",
        language_version="2.5.1-p1",
        _headers=headers or hdrs(),
        **extra,
    )


async def post(client: AsyncClient, path: str, body: dict, headers: dict | None = None):
    return await client.post(path, json=body, headers=headers or hdrs())


async def put(client: AsyncClient, path: str, body: dict, headers: dict | None = None):
    return await client.put(path, json=body, headers=headers or hdrs())


async def new_element(
    client: AsyncClient, mid: str, kind: str, name: str = "", headers=None, **extra: Any
) -> dict:
    body = {"model_id": mid, "kind": kind, "name": name or kind, **extra}
    return await ok(await post(client, "/uml/elements", body, headers), 201)


def finding_rules(resp) -> set[str]:
    return {f["rule_id"] for f in integrity(resp)}


def _owners_first(elements: list[dict]) -> list[dict]:
    done: set[str] = set()
    out: list[dict] = []
    pending = list(elements)
    while pending:
        for e in list(pending):
            if e.get("owner_id") is None or e["owner_id"] in done:
                out.append(e)
                done.add(e["id"])
                pending.remove(e)
    return out


async def load_example(client: AsyncClient, mid: str, snap: dict | None = None) -> dict[str, str]:
    """Replay a snapshot through the routes. Returns local id -> server id."""
    snap = snap or base()
    ids: dict[str, str] = {}
    # 1. elements with their owners only (features may reference elements made later) …
    for e in _owners_first(snap["elements"]):
        out = await new_element(
            client,
            mid,
            e["kind"],
            e.get("name", ""),
            owner_id=ids.get(e["owner_id"]) if e.get("owner_id") else None,
        )
        ids[e["id"]] = out["id"]
    # 2. … then every element's attributes, references mapped.
    for e in snap["elements"]:
        if e.get("attributes"):
            body = map_refs(
                "uml_element",
                {
                    "kind": e["kind"],
                    "name": e.get("name", ""),
                    "owner_id": e.get("owner_id"),
                    "attributes": e["attributes"],
                },
                ids,
            )
            await ok(await put(client, f"/uml/elements/{ids[e['id']]}", body))
    for r in snap["relationships"]:
        body = map_refs("uml_relationship", {k: v for k, v in r.items() if k != "id"}, ids)
        out = await ok(await post(client, "/uml/relationships", body), 201)
        ids[r["id"]] = out["id"]
    for i in snap["interactions"]:
        body = map_refs("interaction", {k: v for k, v in i.items() if k != "id"}, ids)
        out = await ok(await post(client, "/uml/interactions", {**body, "model_id": mid}), 201)
        ids[i["id"]] = out["id"]
    for d in snap.get("diagrams") or []:
        out = await ok(
            await post(
                client,
                "/uml/diagrams",
                {"model_id": mid, "kind": d["kind"], "name": d.get("name") or d["id"]},
            ),
            201,
        )
        ids[d["id"]] = out["id"]
        for o in d.get("objects", []):
            body = map_refs("diagram_object", {k: v for k, v in o.items() if k != "id"}, ids)
            got = await ok(await post(client, f"/uml/diagrams/{out['id']}/objects", body), 201)
            ids[o["id"]] = got["id"]
        for c in d.get("connections", []):
            body = map_refs("diagram_connection", {k: v for k, v in c.items() if k != "id"}, ids)
            got = await ok(await post(client, f"/uml/diagrams/{out['id']}/connections", body), 201)
            ids[c["id"]] = got["id"]
    return ids


async def run(client: AsyncClient, mid: str, body: dict | None = None) -> dict:
    return await ok(
        await client.post(f"/modeling/models/{mid}/validation-runs", json=body, headers=hdrs()),
        201,
    )


async def seal(client: AsyncClient, mid: str) -> dict:
    return await ok(await client.post(f"/modeling/models/{mid}/revisions", headers=hdrs()), 201)


async def revision(client: AsyncClient, mid: str, rev: str) -> dict:
    return await ok(await client.get(f"/modeling/models/{mid}/revisions/{rev}", headers=hdrs()))


# ---------------------------------------------------------------------------
# Phase 5 acceptance
# ---------------------------------------------------------------------------


async def test_examples_are_created_sealed_validated_rendered_and_traced(
    test_client: AsyncClient,
) -> None:
    m = await uml_model(test_client)
    mid = m["id"]
    ids = await load_example(test_client, mid)

    draft = await run(test_client, mid)
    assert draft["status"] == "complete" and draft["clean"] is True, draft
    assert draft["counts"] == {"error": 0, "warning": 0, "advice": 0}
    assert draft["examined"]["uml_element"] == 14 and draft["examined"]["diagram"] == 4
    assert draft["examined"]["diagram_connection"] == 4

    rev = await seal(test_client, mid)
    assert rev["content_hash"] == draft["content_hash"]
    rev_run = await run(test_client, mid, {"revision_id": rev["revision_id"]})
    assert rev_run["clean"] is True and rev_run["content_hash"] == rev["content_hash"]

    manifest = (await revision(test_client, mid, rev["revision_id"]))["manifest"]
    assert {e["id"] for e in manifest["elements"]} == {ids[e["id"]] for e in base()["elements"]}
    inter = manifest["interactions"][0]
    # Order is model content: occurrences and parameters keep it through canonicalisation.
    assert [o["id"] for o in inter["occurrences"]] == [
        o["id"] for o in base()["interactions"][0]["occurrences"]
    ]
    lifelines = {ll["id"]: ll for ll in inter["lifelines"]}
    assert lifelines["ll_user"]["represents_ref"] == ids["actor_user"]
    by_id = {d["id"]: d for d in manifest["diagrams"]}
    assert len(by_id[ids["d_class"]]["objects"]) == 3
    assert manifest["attributes"]["diagrams"]

    # Rendered: every diagram, draft and revision, SVG; PNG and report for one.
    for local in ("d_class", "d_class2", "d_comp", "d_seq"):
        did = ids[local]
        resp = await test_client.get(f"/uml/diagrams/{did}/render", headers=hdrs())
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert resp.headers["content-security-policy"] == CSP
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-renderer-version"] == "uml-svg/1.0.0"
        assert resp.text.startswith("<svg") and "<script" not in resp.text
        sealed = await test_client.get(
            f"/modeling/models/{mid}/revisions/{rev['revision_id']}/diagrams/{did}/render",
            headers=hdrs(),
        )
        assert sealed.status_code == 200 and sealed.text == resp.text
    svc_svg = (await test_client.get(f"/uml/diagrams/{ids['d_class']}/render", headers=hdrs())).text
    assert "OrderService" in svc_svg and "Item" in svc_svg
    png = await test_client.get(
        f"/uml/diagrams/{ids['d_seq']}/render", params={"format": "png"}, headers=hdrs()
    )
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.content.startswith(PNG_MAGIC)
    report = await ok(
        await test_client.get(
            f"/uml/diagrams/{ids['d_comp']}/render", params={"report": "true"}, headers=hdrs()
        )
    )
    assert report["svg"].startswith("<svg") and isinstance(report["diagnostics"], list)
    assert report["renderer_version"] == "uml-svg/1.0.0"

    # Traced to an ArchiMate application component (both directions).
    am = await new_model(test_client)
    app = await ok(await element(test_client, am["id"], "application_component"))
    am_head = await head_of(test_client, am["id"])
    to_uml = await ok(
        await post(
            test_client,
            "/modeling/trace-links",
            {
                "source": {"kind": "element", "id": app["id"]},
                "target": {"kind": "uml_element", "id": ids["comp_api"]},
                "link_type": "realizes",
            },
        ),
        201,
    )
    assert to_uml["model_id"] == am["id"]
    assert await head_of(test_client, am["id"]) == am_head + 1
    uml_head = await head_of(test_client, mid)
    from_uml = await ok(
        await post(
            test_client,
            "/modeling/trace-links",
            {
                "source": {"kind": "diagram", "id": ids["d_comp"]},
                "target": {"kind": "element", "id": app["id"]},
                "link_type": "refines",
            },
        ),
        201,
    )
    assert from_uml["model_id"] == mid
    assert await head_of(test_client, mid) == uml_head + 1
    for kind, local in (("uml_relationship", "conn1"), ("interaction", "int1")):
        await ok(
            await post(
                test_client,
                "/modeling/trace-links",
                {
                    "source": {"kind": kind, "id": ids[local]},
                    "target": {"kind": "element", "id": app["id"]},
                    "link_type": "traces_to",
                },
            ),
            201,
        )
    rev2 = await seal(test_client, mid)
    links = (await revision(test_client, mid, rev2["revision_id"]))["manifest"]["trace_links"]
    assert {(link["source"]["kind"], link["target"]["id"]) for link in links} == {
        ("diagram", app["id"]),
        ("uml_relationship", app["id"]),
        ("interaction", app["id"]),
    }


async def test_one_class_on_two_diagrams_has_one_identity(test_client: AsyncClient) -> None:
    m = await uml_model(test_client)
    ids = await load_example(test_client, m["id"])
    svc_id = ids["cls_svc"]
    shown = []
    for local in ("d_class", "d_class2"):
        objs = await ok(
            await test_client.get(f"/uml/diagrams/{ids[local]}/objects", headers=hdrs())
        )
        shown += [o for o in objs if o["element_ref"] == svc_id]
    assert len(shown) == 2 and shown[0]["id"] != shown[1]["id"]
    listed = await ok(
        await test_client.get(
            "/uml/elements", params={"model_id": m["id"], "kind": "class"}, headers=hdrs()
        )
    )
    assert [e["id"] for e in listed].count(svc_id) == 1
    # One rename shows on both diagrams.
    cur = await ok(await test_client.get(f"/uml/elements/{svc_id}", headers=hdrs()))
    await ok(
        await put(
            test_client,
            f"/uml/elements/{svc_id}",
            {k: cur[k] for k in ("kind", "owner_id", "attributes")} | {"name": "Renamed"},
        )
    )
    for local in ("d_class", "d_class2"):
        svg = (await test_client.get(f"/uml/diagrams/{ids[local]}/render", headers=hdrs())).text
        assert "Renamed" in svg and "OrderService" not in svg


# ---------------------------------------------------------------------------
# Write-time integrity
# ---------------------------------------------------------------------------


async def test_every_write_time_refusal(test_client: AsyncClient) -> None:
    m = await uml_model(test_client)
    mid = m["id"]
    ids = await load_example(test_client, mid)
    other = await uml_model(test_client, title="other")
    foreign = await new_element(test_client, other["id"], "class", "Foreign")

    async def element_put(local: str, **change: Any):
        cur = await ok(await test_client.get(f"/uml/elements/{ids[local]}", headers=hdrs()))
        body = {k: cur[k] for k in ("kind", "name", "owner_id", "attributes")} | change
        return await put(test_client, f"/uml/elements/{ids[local]}", body)

    async def rel(body: dict):
        return await post(test_client, "/uml/relationships", body)

    cases = [
        # UML-REF-001: an unresolved type, an owner in another model, a junk id.
        (
            await post(
                test_client,
                "/uml/elements",
                {
                    "model_id": mid,
                    "kind": "class",
                    "attributes": {"properties": [{"id": "px", "type_ref": str(uuid.uuid4())}]},
                },
            ),
            "UML-REF-001",
        ),
        (
            await post(
                test_client,
                "/uml/elements",
                {"model_id": mid, "kind": "class", "owner_id": foreign["id"]},
            ),
            "UML-REF-001",
        ),
        (
            await rel(
                {"model_id": mid, "kind": "dependency", "source": ids["cls_svc"], "target": "x"}
            ),
            "UML-REF-001",
        ),
        # UML-REF-002: a feature id that is already an element's id.
        (
            await element_put(
                "cls_repo_impl", attributes={"operations": [{"id": ids["cls_svc"], "name": "d"}]}
            ),
            "UML-REF-002",
        ),
        # UML-OWN-001: one feature id owned by two elements.
        (
            await element_put(
                "cls_repo_impl", attributes={"operations": [{"id": "op_handle", "name": "dup"}]}
            ),
            "UML-OWN-001",
        ),
        # UML-OWN-001: a port with no owner; an ownership cycle; a class owned by a class.
        (
            await post(test_client, "/uml/elements", {"model_id": mid, "kind": "port"}),
            "UML-OWN-001",
        ),
        (await element_put("pkg", owner_id=ids["pkg"]), "UML-OWN-001"),
        (await element_put("cls_item", owner_id=ids["cls_svc"]), "UML-OWN-001"),
        # UML-GEN-001 / 002.
        (
            await rel(
                {"kind": "generalization", "source": ids["cls_base"], "target": ids["cls_svc"]}
            ),
            "UML-GEN-001",
        ),
        (
            await rel(
                {"kind": "generalization", "source": ids["cls_item"], "target": ids["iface_repo"]}
            ),
            "UML-GEN-002",
        ),
        # UML-MUL-001.
        (
            await element_put(
                "cls_item", attributes={"properties": [{"id": "pm", "multiplicity": "3..1"}]}
            ),
            "UML-MUL-001",
        ),
        # UML-RLZ-001.
        (
            await rel(
                {
                    "kind": "interface_realization",
                    "source": ids["cls_svc"],
                    "target": ids["cls_item"],
                }
            ),
            "UML-RLZ-001",
        ),
        # UML-CON-001: connector ends that are not ports.
        (
            await rel(
                {
                    "kind": "connector",
                    "connector_kind": "assembly",
                    "source": ids["comp_api"],
                    "target": ids["comp_store"],
                }
            ),
            "UML-CON-001",
        ),
        # Unsupported KIND of a row: refused (UML-UNSUP-001 on /kind).
        (
            await post(test_client, "/uml/elements", {"model_id": mid, "kind": "use_case"}),
            "UML-UNSUP-001",
        ),
        (
            await post(test_client, "/uml/diagrams", {"model_id": mid, "kind": "activity"}),
            "UML-UNSUP-001",
        ),
    ]
    for resp, rule in cases:
        assert rule in finding_rules(resp), (rule, resp.text)

    # Sequence rules on interaction PUT.
    inter = await ok(await test_client.get(f"/uml/interactions/{ids['int1']}", headers=hdrs()))
    doc = {
        k: inter[k]
        for k in (
            "name",
            "owner_id",
            "lifelines",
            "occurrences",
            "messages",
            "executions",
            "fragments",
        )
    }

    async def inter_put(mutate) -> Any:
        d = copy.deepcopy(doc)
        mutate(d)
        return await put(test_client, f"/uml/interactions/{ids['int1']}", d)

    def swap_send(d):  # m1 receive before send
        d["messages"][0]["send_occurrence"], d["messages"][0]["receive_occurrence"] = "o2", "o1"

    def orphan_reply(d):
        d["messages"] = [x for x in d["messages"] if x["id"] != "m2"]

    def unguarded(d):
        d["fragments"][0]["operands"][0]["guard"] = None

    for mutate, rule in (
        (swap_send, "UML-SEQ-001"),
        (orphan_reply, "UML-SEQ-002"),
        (unguarded, "UML-SEQ-004"),
    ):
        assert rule in finding_rules(await inter_put(mutate)), rule

    # Diagram rules.
    d_class, d_seq = ids["d_class"], ids["d_seq"]
    resp = await post(
        test_client, f"/uml/diagrams/{d_class}/objects", {"element_ref": foreign["id"]}
    )
    assert "UML-DIA-001" in finding_rules(resp)
    resp = await post(
        test_client,
        f"/uml/diagrams/{d_class}/connections",
        {
            "relationship_ref": ids["gen1"],
            "source_object_id": ids["ob_item"],
            "target_object_id": ids["ob_base"],
        },
    )
    assert "UML-DIA-002" in finding_rules(resp)
    # An occurrence that does not fit the diagram's kind is refused (UNSUP on the object).
    resp = await post(test_client, f"/uml/diagrams/{d_class}/objects", {"lifeline_id": "ll_user"})
    assert "UML-UNSUP-001" in finding_rules(resp)
    resp = await post(
        test_client, f"/uml/diagrams/{d_seq}/objects", {"element_ref": ids["cls_svc"]}
    )
    assert "UML-UNSUP-001" in finding_rules(resp)
    # A relationship re-pointed under its connection no longer matches what is drawn.
    resp = await put(
        test_client,
        f"/uml/relationships/{ids['gen1']}",
        {"kind": "generalization", "source": ids["cls_svc"], "target": ids["cls_item"]},
    )
    assert "UML-DIA-002" in finding_rules(resp)

    # Deleting a concept another one still references is refused, nothing is deleted.
    before = await head_of(test_client, mid)
    resp = await test_client.delete(f"/uml/elements/{ids['iface_repo']}", headers=hdrs())
    assert "UML-REF-001" in finding_rules(resp)
    assert await head_of(test_client, mid) == before
    assert (
        await test_client.get(f"/uml/elements/{ids['iface_repo']}", headers=hdrs())
    ).status_code == 200

    # Model membership refusals carry evidence.reason.
    am = await new_model(test_client)
    for body, reason in (
        ({"model_id": am["id"], "kind": "class"}, "model_language_mismatch"),
        ({"model_id": str(uuid.uuid4()), "kind": "class"}, "model_not_writable"),
        ({"model_id": mid, "kind": "class", "project_id": "elsewhere"}, "model_project_mismatch"),
    ):
        f = integrity(await post(test_client, "/uml/elements", body))[0]
        assert f["rule_id"] == "UML-REF-001" and f["evidence"]["reason"] == reason
    f = integrity(
        await post(
            test_client,
            "/uml/elements",
            {"model_id": mid, "kind": "class"},
            hdrs(visibility="user"),
        )
    )[0]
    assert f["evidence"]["reason"] == "model_visibility_mismatch"
    f = integrity(await post(test_client, "/uml/relationships", {"kind": "dependency"}))[0]
    assert f["evidence"]["reason"] == "no_model"
    # Nothing above was written.
    assert (await run(test_client, mid))["clean"] is True


async def test_input_too_large_on_write_and_render(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = await uml_model(test_client)
    a = await new_element(test_client, m["id"], "class", "A")
    d = await ok(
        await post(test_client, "/uml/diagrams", {"model_id": m["id"], "kind": "class"}), 201
    )
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 1)
    resp = await post(test_client, "/uml/elements", {"model_id": m["id"], "kind": "class"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"
    # Edits that do not grow an at-cap model stay possible.
    await ok(await put(test_client, f"/uml/elements/{a['id']}", {"kind": "class", "name": "A2"}))
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 0)
    resp = await test_client.get(f"/uml/diagrams/{d['id']}/render", headers=hdrs())
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"


async def test_unsupported_constructs_are_stored_and_make_the_run_not_clean(
    test_client: AsyncClient,
) -> None:
    m = await uml_model(test_client)
    mid = m["id"]
    ids = await load_example(test_client, mid)
    assert (await run(test_client, mid))["clean"] is True
    # literals on a class: outside the profile, stored as authored.
    weird = await ok(
        await post(
            test_client,
            "/uml/elements",
            {"model_id": mid, "kind": "class", "name": "W", "attributes": {"literals": ["a"]}},
        ),
        201,
    )
    r = await run(test_client, mid)
    assert r["status"] == "complete" and r["clean"] is False and r["counts"]["error"] == 1
    findings = await ok(
        await test_client.get(f"/modeling/validation-runs/{r['run_id']}/findings", headers=hdrs())
    )
    assert [(f["rule_id"], f["targets"][0]["id"]) for f in findings] == [
        ("UML-UNSUP-001", weird["id"])
    ]
    # A fragment operator outside the profile is stored too, and reported.
    inter = await ok(await test_client.get(f"/uml/interactions/{ids['int1']}", headers=hdrs()))
    doc = {
        k: inter[k]
        for k in (
            "name",
            "owner_id",
            "lifelines",
            "occurrences",
            "messages",
            "executions",
            "fragments",
        )
    }
    doc["fragments"][0]["operator"] = "critical"
    await ok(await put(test_client, f"/uml/interactions/{ids['int1']}", doc))
    r = await run(test_client, mid)
    assert r["clean"] is False and r["counts"]["error"] >= 2
    rev = await seal(test_client, mid)
    rr = await run(test_client, mid, {"revision_id": rev["revision_id"]})
    assert rr["clean"] is False


# ---------------------------------------------------------------------------
# Mutation check: each refusal is the named rule's, not an accident
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "rule"),
    [
        ("_check_mult", "UML-MUL-001"),
        ("_check_connector", "UML-CON-001"),
        ("_cycles", "UML-GEN-001"),
        ("_check_replies", "UML-SEQ-002"),
    ],
)
async def test_mutation_disabling_the_rule_lets_the_write_through(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, check: str, rule: str
) -> None:
    m = await uml_model(test_client)
    ids = await load_example(test_client, m["id"])

    async def offending():
        if rule == "UML-MUL-001":
            return await put(
                test_client,
                f"/uml/elements/{ids['cls_item']}",
                {
                    "kind": "class",
                    "attributes": {"properties": [{"id": "pm", "multiplicity": "3..1"}]},
                },
            )
        if rule == "UML-CON-001":
            return await post(
                test_client,
                "/uml/relationships",
                {
                    "kind": "connector",
                    "connector_kind": "assembly",
                    "source": ids["comp_api"],
                    "target": ids["comp_store"],
                },
            )
        if rule == "UML-GEN-001":
            return await post(
                test_client,
                "/uml/relationships",
                {"kind": "generalization", "source": ids["cls_base"], "target": ids["cls_svc"]},
            )
        inter = await ok(await test_client.get(f"/uml/interactions/{ids['int1']}", headers=hdrs()))
        doc = {
            k: inter[k]
            for k in (
                "name",
                "owner_id",
                "lifelines",
                "occurrences",
                "messages",
                "executions",
                "fragments",
            )
        }
        doc["messages"] = [x for x in doc["messages"] if x["id"] != "m2"]
        return await put(test_client, f"/uml/interactions/{ids['int1']}", doc)

    assert rule in finding_rules(await offending())
    monkeypatch.setattr(uml_rules, check, lambda *a, **k: [] if check == "_cycles" else None)
    resp = await offending()
    assert resp.status_code in (200, 201), resp.text


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


async def test_tenant_and_private_subject_isolation_on_every_route(
    test_client: AsyncClient,
) -> None:
    m = await uml_model(test_client)
    ids = await load_example(test_client, m["id"])
    alice_private = await uml_model(test_client, headers=hdrs(visibility="user"), title="mine")
    secret = await new_element(
        test_client, alice_private["id"], "class", "Secret", headers=hdrs(visibility="user")
    )
    sd = await ok(
        await post(
            test_client,
            "/uml/diagrams",
            {"model_id": alice_private["id"], "kind": "class"},
            hdrs(visibility="user"),
        ),
        201,
    )
    assert secret["visibility"] == "user" and sd["visibility"] == "user"

    concept_rows = {
        "elements": ids["cls_svc"],
        "relationships": ids["gen1"],
        "interactions": ids["int1"],
        "diagrams": ids["d_class"],
    }
    outsiders = (hdrs(tenant=OTHER_TENANT), hdrs(BOB, tenant=OTHER_TENANT))
    for h in outsiders:
        for path, rid in concept_rows.items():
            assert (await test_client.get(f"/uml/{path}/{rid}", headers=h)).status_code == 404
            assert (await test_client.delete(f"/uml/{path}/{rid}", headers=h)).status_code == 404
            listed = await test_client.get(f"/uml/{path}", headers=h)
            assert listed.status_code == 200 and listed.json() == []
            assert listed.headers["X-Total-Count"] == "0"
        for path, rid, body in (
            ("elements", ids["cls_svc"], {"kind": "class"}),
            ("relationships", ids["gen1"], {"kind": "dependency"}),
            ("interactions", ids["int1"], {}),
            ("diagrams", ids["d_class"], {"kind": "class"}),
        ):
            resp = await test_client.put(f"/uml/{path}/{rid}", json=body, headers=h)
            assert resp.status_code == 404, (path, resp.text)
        # Creating into the model, or ending on its rows, never reaches them.
        for path, body in (
            ("elements", {"model_id": m["id"], "kind": "class"}),
            ("interactions", {"model_id": m["id"]}),
            ("diagrams", {"model_id": m["id"], "kind": "class"}),
            ("relationships", {"model_id": m["id"], "kind": "dependency"}),
        ):
            f = integrity(await test_client.post(f"/uml/{path}", json=body, headers=h))[0]
            assert f["evidence"]["reason"] == "model_not_writable", path
        f = integrity(
            await test_client.post(
                "/uml/relationships",
                json={"kind": "dependency", "source": ids["cls_svc"], "target": ids["cls_item"]},
                headers=h,
            )
        )[0]
        assert f["rule_id"] == "UML-REF-001" and f["path"] == "/source"
        for path, body in (
            (f"objects/{ids['ob_svc']}", {"element_ref": ids["cls_svc"]}),
            (
                f"connections/{ids['c_gen']}",
                {
                    "relationship_ref": ids["gen1"],
                    "source_object_id": ids["ob_svc"],
                    "target_object_id": ids["ob_base"],
                },
            ),
        ):
            resp = await test_client.put(
                f"/uml/diagrams/{ids['d_class']}/{path}", json=body, headers=h
            )
            assert resp.status_code == 404
        resp = await test_client.post(
            f"/uml/diagrams/{ids['d_class']}/connections",
            json={
                "relationship_ref": ids["gen1"],
                "source_object_id": ids["ob_svc"],
                "target_object_id": ids["ob_base"],
            },
            headers=h,
        )
        assert resp.status_code == 404
        for sub in ("objects", "connections", "render"):
            resp = await test_client.get(f"/uml/diagrams/{ids['d_class']}/{sub}", headers=h)
            assert resp.status_code == 404
        resp = await test_client.post(
            f"/uml/diagrams/{ids['d_class']}/objects",
            json={"element_ref": ids["cls_item"]},
            headers=h,
        )
        assert resp.status_code == 404
        resp = await test_client.delete(
            f"/uml/diagrams/{ids['d_class']}/objects/{ids['ob_svc']}", headers=h
        )
        assert resp.status_code == 404
        resp = await test_client.delete(
            f"/uml/diagrams/{ids['d_class']}/connections/{ids['c_gen']}", headers=h
        )
        assert resp.status_code == 404

    # Bob (same tenant) sees the tenant model but never Alice's private one.
    bob = hdrs(BOB)
    assert (
        await test_client.get(f"/uml/elements/{ids['cls_svc']}", headers=bob)
    ).status_code == 200
    for path, rid in (("elements", secret["id"]), ("diagrams", sd["id"])):
        assert (await test_client.get(f"/uml/{path}/{rid}", headers=bob)).status_code == 404
        assert (await test_client.delete(f"/uml/{path}/{rid}", headers=bob)).status_code == 404
    listed = await ok(await test_client.get("/uml/elements", headers=bob))
    assert secret["id"] not in {e["id"] for e in listed}
    assert (
        await test_client.get(f"/uml/diagrams/{sd['id']}/render", headers=bob)
    ).status_code == 404
    assert (
        await test_client.get(f"/uml/diagrams/{sd['id']}/objects", headers=bob)
    ).status_code == 404
    # … cannot reference it, write into its model, or trace to it.
    resp = await post(
        test_client,
        "/uml/relationships",
        {"kind": "dependency", "source": secret["id"], "target": ids["cls_svc"]},
        bob,
    )
    assert integrity(resp)[0]["rule_id"] == "UML-REF-001"
    resp = await post(
        test_client,
        "/uml/elements",
        {"model_id": alice_private["id"], "kind": "class"},
        hdrs(BOB, visibility="user"),
    )
    assert integrity(resp)[0]["evidence"]["reason"] == "model_not_writable"
    resp = await post(
        test_client,
        "/modeling/trace-links",
        {
            "source": {"kind": "uml_element", "id": ids["cls_svc"]},
            "target": {"kind": "uml_element", "id": secret["id"]},
            "link_type": "depends_on",
        },
        bob,
    )
    assert integrity(resp)[0]["rule_id"] == "TRC-REF-001"
    # A private default UML model is per subject.
    mine = await ok(
        await post(test_client, "/uml/elements", {"kind": "class"}, hdrs(visibility="user")), 201
    )
    theirs = await ok(
        await post(test_client, "/uml/elements", {"kind": "class"}, hdrs(BOB, visibility="user")),
        201,
    )
    assert mine["model_id"] != theirs["model_id"]
    assert (await test_client.get(f"/uml/elements/{mine['id']}", headers=bob)).status_code == 404

    # A revision render is as private as its model.
    rev = await seal(test_client, m["id"])
    path = f"/modeling/models/{m['id']}/revisions/{rev['revision_id']}/diagrams/{ids['d_class']}/render"
    assert (await test_client.get(path, headers=hdrs(tenant=OTHER_TENANT))).status_code == 404
    assert (await test_client.get(path, headers=bob)).status_code == 200


async def test_model_visibility_moves_uml_rows_and_their_links(test_client: AsyncClient) -> None:
    """Shared → private follows the §7.1 rules for UML rows too, and re-derives the links."""
    m = await uml_model(test_client)
    mid = m["id"]
    bobs = await new_element(test_client, mid, "class", "B", headers=hdrs(BOB))
    to_private = {"visibility": "user"}
    refused = await test_client.patch(f"/modeling/models/{mid}", json=to_private, headers=hdrs())
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "visibility_change_foreign_rows"
    await ok(await test_client.delete(f"/uml/elements/{bobs['id']}", headers=hdrs(BOB)))
    el = await new_element(test_client, mid, "class", "C")
    am = await new_model(test_client)
    app = await ok(await element(test_client, am["id"], "application_component"))
    into = await ok(
        await post(
            test_client,
            "/modeling/trace-links",
            {
                "source": {"kind": "element", "id": app["id"]},
                "target": {"kind": "uml_element", "id": el["id"]},
                "link_type": "realizes",
            },
        ),
        201,
    )
    out = await ok(
        await post(
            test_client,
            "/modeling/trace-links",
            {
                "source": {"kind": "uml_element", "id": el["id"]},
                "target": {"kind": "element", "id": app["id"]},
                "link_type": "refines",
            },
        ),
        201,
    )
    assert into["model_id"] == am["id"] and out["model_id"] == mid
    am_head = await head_of(test_client, am["id"])
    forbidden = await test_client.patch(
        f"/modeling/models/{mid}", json=to_private, headers=hdrs(BOB)
    )
    assert forbidden.status_code == 403
    await ok(await test_client.patch(f"/modeling/models/{mid}", json=to_private, headers=hdrs()))
    assert (
        await test_client.get(f"/uml/elements/{el['id']}", headers=hdrs(BOB))
    ).status_code == 404
    moved = await ok(await test_client.get(f"/uml/elements/{el['id']}", headers=hdrs()))
    assert moved["visibility"] == "user"
    # Both links went private; the one sourced in the shared ArchiMate model left it.
    for link, model_id in ((into, None), (out, mid)):
        got = await ok(await test_client.get(f"/modeling/trace-links/{link['id']}", headers=hdrs()))
        assert got["visibility"] == "user" and got["model_id"] == model_id
        assert (
            await test_client.get(f"/modeling/trace-links/{link['id']}", headers=hdrs(BOB))
        ).status_code == 404
    assert await head_of(test_client, am["id"]) == am_head + 1
    rev = await seal(test_client, am["id"])
    assert (await revision(test_client, am["id"], rev["revision_id"]))["manifest"][
        "trace_links"
    ] == []


async def test_list_filters(test_client: AsyncClient) -> None:
    m1 = await uml_model(test_client, project_id="p1")
    m2 = await uml_model(test_client, title="two")
    pkg = await new_element(test_client, m1["id"], "package", "P")
    cls = await new_element(test_client, m1["id"], "class", "C", owner_id=pkg["id"])
    other = await new_element(test_client, m2["id"], "class", "D")

    async def names(params: dict) -> set[str]:
        rows = await ok(await test_client.get("/uml/elements", params=params, headers=hdrs()))
        return {r["name"] for r in rows}

    assert await names({"model_id": m1["id"]}) == {"P", "C"}
    assert await names({"kind": "class"}) == {"C", "D"}
    assert await names({"project_id": "p1"}) == {"P", "C"}
    assert await names({"owner_id": pkg["id"]}) == {"C"}
    assert cls["project_id"] == "p1" and other["project_id"] is None
    await ok(
        await post(
            test_client,
            "/uml/relationships",
            {"kind": "dependency", "source": cls["id"], "target": pkg["id"]},
        ),
        201,
    )
    rels = await ok(
        await test_client.get(
            "/uml/relationships", params={"kind": "dependency", "project_id": "p1"}, headers=hdrs()
        )
    )
    assert len(rels) == 1 and rels[0]["model_id"] == m1["id"]  # derived from the source end
    await ok(
        await post(test_client, "/uml/diagrams", {"model_id": m2["id"], "kind": "sequence"}), 201
    )
    seq = await ok(
        await test_client.get("/uml/diagrams", params={"kind": "sequence"}, headers=hdrs())
    )
    assert [d["model_id"] for d in seq] == [m2["id"]]
    i = await ok(
        await post(test_client, "/uml/interactions", {"model_id": m1["id"], "owner_id": pkg["id"]}),
        201,
    )
    got = await ok(
        await test_client.get("/uml/interactions", params={"owner_id": pkg["id"]}, headers=hdrs())
    )
    assert [x["id"] for x in got] == [i["id"]]


# ---------------------------------------------------------------------------
# Draft head, If-Match, ETag, Idempotency-Key
# ---------------------------------------------------------------------------


async def test_every_write_bumps_head_and_honours_if_match(test_client: AsyncClient) -> None:
    m = await uml_model(test_client)
    mid = m["id"]
    head = await head_of(test_client, mid)

    resp = await post(
        test_client, "/uml/elements", {"model_id": mid, "kind": "class"}, hdrs(if_match=head + 3)
    )
    assert resp.status_code == 412 and resp.json()["error"]["head"] == head
    resp = await post(
        test_client,
        "/uml/elements",
        {"model_id": mid, "kind": "class", "name": "A"},
        hdrs(if_match=head),
    )
    a = await ok(resp, 201)
    assert etag(resp) == head + 1 == await head_of(test_client, mid)

    steps = [
        ("put", f"/uml/elements/{a['id']}", {"kind": "class", "name": "A2"}),
        ("post", "/uml/diagrams", {"model_id": mid, "kind": "class"}),
    ]
    for method, path, body in steps:
        cur = await head_of(test_client, mid)
        stale = await getattr(test_client, method)(path, json=body, headers=hdrs(if_match=cur - 1))
        assert stale.status_code == 412, (path, stale.text)
        good = await getattr(test_client, method)(path, json=body, headers=hdrs(if_match=cur))
        assert good.status_code in (200, 201), good.text
        assert etag(good) == cur + 1
    diagram = (
        await ok(await test_client.get("/uml/diagrams", params={"model_id": mid}, headers=hdrs()))
    )[0]
    cur = await head_of(test_client, mid)
    obj = await test_client.post(
        f"/uml/diagrams/{diagram['id']}/objects",
        json={"element_ref": a["id"]},
        headers=hdrs(if_match=cur),
    )
    assert obj.status_code == 201 and etag(obj) == cur + 1
    stale = await test_client.delete(
        f"/uml/diagrams/{diagram['id']}/objects/{obj.json()['id']}", headers=hdrs(if_match=cur)
    )
    assert stale.status_code == 412
    gone = await test_client.delete(
        f"/uml/diagrams/{diagram['id']}/objects/{obj.json()['id']}", headers=hdrs(if_match=cur + 1)
    )
    assert gone.status_code == 200 and etag(gone) == cur + 2
    # A refused write leaves the head alone.
    before = await head_of(test_client, mid)
    integrity(await post(test_client, "/uml/elements", {"model_id": mid, "kind": "port"}))
    assert await head_of(test_client, mid) == before
    resp = await post(
        test_client,
        "/uml/elements",
        {"model_id": mid, "kind": "class"},
        {**hdrs(), "If-Match": "x"},
    )
    assert resp.status_code == 422


async def test_idempotent_create(test_client: AsyncClient) -> None:
    m = await uml_model(test_client)
    body = {"model_id": m["id"], "kind": "class", "name": "Once"}
    first = await post(test_client, "/uml/elements", body, hdrs(key="k-1"))
    assert first.status_code == 201
    head = await head_of(test_client, m["id"])
    again = await post(test_client, "/uml/elements", body, hdrs(key="k-1"))
    assert again.status_code == 201 and again.json() == first.json()
    assert await head_of(test_client, m["id"]) == head
    conflict = await post(test_client, "/uml/elements", {**body, "name": "Other"}, hdrs(key="k-1"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    listed = await ok(
        await test_client.get("/uml/elements", params={"model_id": m["id"]}, headers=hdrs())
    )
    assert len(listed) == 1
    # Same key on another route is another request; a refused create leaves no key behind.
    d = await post(
        test_client, "/uml/diagrams", {"model_id": m["id"], "kind": "nope"}, hdrs(key="k-1")
    )
    assert d.status_code == 422
    d = await post(
        test_client, "/uml/diagrams", {"model_id": m["id"], "kind": "class"}, hdrs(key="k-1")
    )
    assert d.status_code == 201


async def test_render_etag_304_and_change(test_client: AsyncClient) -> None:
    m = await uml_model(test_client)
    ids = await load_example(test_client, m["id"])
    path = f"/uml/diagrams/{ids['d_class']}/render"
    first = await test_client.get(path, headers=hdrs())
    tag = first.headers["etag"]
    assert tag.startswith('"') and not tag.startswith("W/")
    same = await test_client.get(path, headers={**hdrs(), "If-None-Match": tag})
    assert same.status_code == 304 and same.content == b"" and same.headers["etag"] == tag
    assert same.headers["content-security-policy"] == CSP
    png = await test_client.get(
        path, params={"format": "png"}, headers={**hdrs(), "If-None-Match": tag}
    )
    assert png.status_code == 200 and png.headers["etag"] != tag
    cur = await ok(await test_client.get(f"/uml/elements/{ids['cls_item']}", headers=hdrs()))
    await ok(
        await put(
            test_client,
            f"/uml/elements/{ids['cls_item']}",
            {k: cur[k] for k in ("kind", "owner_id", "attributes")} | {"name": "Thing"},
        )
    )
    changed = await test_client.get(path, headers={**hdrs(), "If-None-Match": tag})
    assert changed.status_code == 200 and "Thing" in changed.text
    missing = await test_client.get(f"/uml/diagrams/{uuid.uuid4()}/render", headers=hdrs())
    assert missing.status_code == 404
    bad = await test_client.get(path, params={"format": "gif"}, headers=hdrs())
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Change sets
# ---------------------------------------------------------------------------


async def propose(client: AsyncClient, mid: str, ops: list[dict], base_head: int):
    return await client.post(
        f"/modeling/models/{mid}/change-sets",
        json={"ops": ops, "rationale": "r", "base_head": base_head},
        headers=hdrs(),
    )


async def test_uml_change_sets_apply_reject_and_stale(test_client: AsyncClient) -> None:
    m = await uml_model(test_client)
    mid = m["id"]
    pkg = await new_element(test_client, mid, "package", "P")
    ops = [
        {
            "op": "create",
            "kind": "uml_element",
            "id": "c",
            "attributes": {"kind": "class", "name": "C", "owner_id": pkg["id"]},
        },
        {
            "op": "create",
            "kind": "uml_element",
            "id": "i",
            "attributes": {"kind": "interface", "name": "I", "owner_id": pkg["id"]},
        },
        {
            "op": "create",
            "kind": "uml_relationship",
            "id": "r",
            "attributes": {"kind": "interface_realization", "source": "c", "target": "i"},
        },
        {
            "op": "update",
            "kind": "uml_element",
            "id": "c",
            "attributes": {
                "attributes": {"properties": [{"id": "pi", "name": "dep", "type_ref": "i"}]}
            },
        },
        {
            "op": "create",
            "kind": "diagram",
            "id": "d",
            "attributes": {"kind": "class", "name": "D"},
        },
        {
            "op": "create",
            "kind": "diagram_object",
            "id": "oc",
            "attributes": {"diagram_id": "d", "element_ref": "c"},
        },
        {
            "op": "create",
            "kind": "diagram_object",
            "id": "oi",
            "attributes": {"diagram_id": "d", "element_ref": "i"},
        },
        {
            "op": "create",
            "kind": "diagram_connection",
            "id": "cr",
            "attributes": {
                "diagram_id": "d",
                "relationship_ref": "r",
                "source_object_id": "oc",
                "target_object_id": "oi",
            },
        },
    ]
    head = await head_of(test_client, mid)
    cs = await ok(await propose(test_client, mid, ops, head), 201)
    assert cs["status"] == "proposed", cs["blocking_findings"]
    assert await head_of(test_client, mid) == head  # nothing applied yet
    applied = await ok(
        await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    )
    assert applied["status"] == "applied" and applied["applied_head"] == head + 1
    idm = applied["id_map"]
    assert set(idm) == {"c", "i", "r", "d", "oc", "oi", "cr"}
    c = await ok(await test_client.get(f"/uml/elements/{idm['c']}", headers=hdrs()))
    assert c["attributes"]["properties"][0]["type_ref"] == idm["i"]
    conns = await ok(await test_client.get(f"/uml/diagrams/{idm['d']}/connections", headers=hdrs()))
    assert conns[0]["relationship_ref"] == idm["r"] and conns[0]["source_object_id"] == idm["oc"]
    assert (await run(test_client, mid))["clean"] is True

    # Delete / update ops on stored rows, and the head moved: 412 on apply.
    head = await head_of(test_client, mid)
    cs2 = await ok(
        await propose(
            test_client,
            mid,
            [
                {
                    "op": "update",
                    "kind": "diagram",
                    "id": idm["d"],
                    "attributes": {"name": "Renamed"},
                },
                {"op": "delete", "kind": "diagram_connection", "id": idm["cr"]},
            ],
            head,
        ),
        201,
    )
    assert cs2["status"] == "proposed", cs2["blocking_findings"]
    await new_element(test_client, mid, "class", "Interloper")
    stale = await test_client.post(f"/modeling/change-sets/{cs2['id']}/apply", headers=hdrs())
    assert stale.status_code == 412 and stale.json()["error"]["code"] == "stale_head"
    assert (
        len(
            await ok(await test_client.get(f"/uml/diagrams/{idm['d']}/connections", headers=hdrs()))
        )
        == 1
    )

    # Invalid: a rule the write gate enforces, an unknown id, a foreign kind.
    head = await head_of(test_client, mid)
    bad = await ok(
        await propose(
            test_client,
            mid,
            [
                {
                    "op": "create",
                    "kind": "uml_relationship",
                    "attributes": {
                        "kind": "generalization",
                        "source": idm["c"],
                        "target": idm["i"],
                    },
                },
                {"op": "delete", "kind": "uml_element", "id": str(uuid.uuid4())},
                {"op": "create", "kind": "element", "attributes": {"title": "x"}},
                {
                    "op": "update",
                    "kind": "diagram_object",
                    "id": idm["oc"],
                    "attributes": {"diagram_id": str(uuid.uuid4())},
                },
            ],
            head,
        ),
        201,
    )
    assert bad["status"] == "invalid"
    rules = [f["rule_id"] for f in bad["blocking_findings"]]
    assert "UML-GEN-002" in rules and rules.count("CS-OP-001") == 3
    refused = await test_client.post(f"/modeling/change-sets/{bad['id']}/apply", headers=hdrs())
    assert refused.status_code == 409


async def test_change_set_apply_is_atomic(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure at apply after earlier ops were written rolls every op back."""
    m = await uml_model(test_client)
    mid = m["id"]
    a = await new_element(test_client, mid, "class", "A")
    head = await head_of(test_client, mid)
    cs = await ok(
        await propose(
            test_client,
            mid,
            [
                {
                    "op": "create",
                    "kind": "uml_element",
                    "id": "n",
                    "attributes": {"kind": "class", "name": "New"},
                },
                {
                    "op": "update",
                    "kind": "uml_element",
                    "id": a["id"],
                    "attributes": {"name": "A2"},
                },
            ],
            head,
        ),
        201,
    )
    assert cs["status"] == "proposed"
    # A failure after every op's rows were written (the persist itself succeeded).
    real = uml_manifest.persist
    calls = {"n": 0}

    async def failing(*args: Any, **kw: Any) -> None:
        calls["n"] += 1
        await real(*args, **kw)
        raise RuntimeError("boom")

    monkeypatch.setattr(uml_manifest, "persist", failing)
    try:
        resp = await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
        assert resp.status_code == 500
    except RuntimeError:
        pass
    assert calls["n"] == 1
    monkeypatch.undo()
    assert await head_of(test_client, mid) == head
    names = {
        e["name"]
        for e in await ok(
            await test_client.get("/uml/elements", params={"model_id": mid}, headers=hdrs())
        )
    }
    assert names == {"A"}
    again = await ok(await test_client.get(f"/modeling/change-sets/{cs['id']}", headers=hdrs()))
    assert again["status"] == "proposed"


# ---------------------------------------------------------------------------
# Revisions are immutable; deletes cascade
# ---------------------------------------------------------------------------


async def test_revision_is_immutable_after_draft_edits_and_deletes(
    test_client: AsyncClient,
) -> None:
    m = await uml_model(test_client)
    mid = m["id"]
    ids = await load_example(test_client, mid)
    rev = await seal(test_client, mid)
    before = await revision(test_client, mid, rev["revision_id"])
    rpath = (
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/diagrams/{ids['d_class']}/render"
    )
    svg_before = (await test_client.get(rpath, headers=hdrs())).text

    cur = await ok(await test_client.get(f"/uml/elements/{ids['cls_svc']}", headers=hdrs()))
    await ok(
        await put(
            test_client,
            f"/uml/elements/{ids['cls_svc']}",
            {k: cur[k] for k in ("kind", "owner_id", "attributes")} | {"name": "Changed"},
        )
    )
    await ok(await test_client.delete(f"/uml/diagrams/{ids['d_comp']}", headers=hdrs()))
    await ok(await test_client.delete(f"/uml/relationships/{ids['assoc1']}", headers=hdrs()))
    await ok(await test_client.delete(f"/uml/interactions/{ids['int1']}", headers=hdrs()))

    after = await revision(test_client, mid, rev["revision_id"])
    assert after == before
    assert (await test_client.get(rpath, headers=hdrs())).text == svg_before
    assert "OrderService" in svg_before
    rerun = await run(test_client, mid, {"revision_id": rev["revision_id"]})
    assert rerun["content_hash"] == rev["content_hash"] and rerun["clean"] is True
    draft = await run(test_client, mid)
    assert draft["content_hash"] != rev["content_hash"]
    # The deleted diagram still renders from the revision, not from the draft.
    old = f"/modeling/models/{mid}/revisions/{rev['revision_id']}/diagrams/{ids['d_comp']}/render"
    assert (await test_client.get(old, headers=hdrs())).status_code == 200
    assert (
        await test_client.get(f"/uml/diagrams/{ids['d_comp']}/render", headers=hdrs())
    ).status_code == 404
    # An ArchiMate model's revision has no UML diagrams.
    am = await new_model(test_client)
    arev = await seal(test_client, am["id"])
    resp = await test_client.get(
        f"/modeling/models/{am['id']}/revisions/{arev['revision_id']}/diagrams/{ids['d_class']}/render",
        headers=hdrs(),
    )
    assert resp.status_code == 404


async def test_delete_cascades(test_client: AsyncClient) -> None:
    m = await uml_model(test_client)
    mid = m["id"]
    ids = await load_example(test_client, mid)
    am = await new_model(test_client)
    app = await ok(await element(test_client, am["id"], "application_component"))
    link = await ok(
        await post(
            test_client,
            "/modeling/trace-links",
            {
                "source": {"kind": "element", "id": app["id"]},
                "target": {"kind": "uml_element", "id": ids["port_api"]},
                "link_type": "realizes",
            },
        ),
        201,
    )
    am_head = await head_of(test_client, am["id"])

    # Deleting a component deletes its port (owned), the connector ending on the port, the
    # occurrences of both, and the trace link (whose source model is bumped too).
    resp = await ok(await test_client.delete(f"/uml/elements/{ids['comp_api']}", headers=hdrs()))
    c = resp["cascaded"]
    assert c["uml_element"] == [ids["port_api"]]
    assert set(c["uml_relationship"]) == {ids["conn1"], ids["use1"]}
    assert set(c["diagram_object"]) == {ids["ob_capi"], ids["ob_papi"]}
    assert c["diagram_connection"] == [ids["c_conn"]]
    for path in (
        f"/uml/elements/{ids['port_api']}",
        f"/uml/relationships/{ids['conn1']}",
        f"/modeling/trace-links/{link['id']}",
    ):
        assert (await test_client.get(path, headers=hdrs())).status_code == 404, path
    assert await head_of(test_client, am["id"]) == am_head + 1
    objs = await ok(await test_client.get(f"/uml/diagrams/{ids['d_comp']}/objects", headers=hdrs()))
    assert [o["id"] for o in objs] == [ids["ob_pstore"]]

    # Deleting an object deletes the connections attached to it.
    resp = await ok(
        await test_client.delete(
            f"/uml/diagrams/{ids['d_class']}/objects/{ids['ob_svc']}", headers=hdrs()
        )
    )
    assert set(resp["cascaded"]["diagram_connection"]) == {ids["c_assoc"], ids["c_gen"]}

    # A replaced interaction document drops occurrences of lifelines/messages it lost.
    inter = await ok(await test_client.get(f"/uml/interactions/{ids['int1']}", headers=hdrs()))
    doc = {
        k: inter[k]
        for k in (
            "name",
            "owner_id",
            "lifelines",
            "occurrences",
            "messages",
            "executions",
            "fragments",
        )
    }
    # m1 and the reply m4 that answers it go; c_m1 showed m1.
    doc["messages"] = [x for x in doc["messages"] if x["id"] not in ("m1", "m4")]
    await ok(await put(test_client, f"/uml/interactions/{ids['int1']}", doc))
    assert (
        await ok(await test_client.get(f"/uml/diagrams/{ids['d_seq']}/connections", headers=hdrs()))
        == []
    )

    # Deleting a diagram deletes its occurrences, never the concepts.
    resp = await ok(await test_client.delete(f"/uml/diagrams/{ids['d_class2']}", headers=hdrs()))
    assert resp["cascaded"] == {"diagram_object": [ids["ob2_svc"]]}
    assert (
        await test_client.get(f"/uml/elements/{ids['cls_svc']}", headers=hdrs())
    ).status_code == 200

    # Deleting the package deletes everything it owns (transitively: ports of components),
    # the interaction it owns, every relationship and every remaining occurrence.
    resp = await ok(await test_client.delete(f"/uml/elements/{ids['pkg']}", headers=hdrs()))
    assert len(resp["cascaded"]["uml_element"]) == 11
    assert resp["cascaded"]["interaction"] == [ids["int1"]]
    assert (
        await ok(await test_client.get("/uml/elements", params={"model_id": mid}, headers=hdrs()))
        == []
    )
    assert (
        await ok(
            await test_client.get("/uml/relationships", params={"model_id": mid}, headers=hdrs())
        )
        == []
    )
    assert (await run(test_client, mid))["clean"] is True

    # Force-deleting a model removes its UML rows and links INTO them from other models.
    m2 = await uml_model(test_client, title="gone")
    x = await new_element(test_client, m2["id"], "class", "X")
    link2 = await ok(
        await post(
            test_client,
            "/modeling/trace-links",
            {
                "source": {"kind": "element", "id": app["id"]},
                "target": {"kind": "uml_element", "id": x["id"]},
                "link_type": "realizes",
            },
        ),
        201,
    )
    await seal(test_client, m2["id"])
    await ok(
        await test_client.delete(
            f"/modeling/models/{m2['id']}", params={"force": "true"}, headers=hdrs()
        )
    )
    assert (await test_client.get(f"/uml/elements/{x['id']}", headers=hdrs())).status_code == 404
    assert (
        await test_client.get(f"/modeling/trace-links/{link2['id']}", headers=hdrs())
    ).status_code == 404


async def test_default_uml_model_per_scope(test_client: AsyncClient) -> None:
    """A write that names no model joins its scope's default UML model (created on first use)."""
    first = await ok(
        await post(test_client, "/uml/elements", {"kind": "class", "project_id": "px"}), 201
    )
    second = await ok(
        await post(test_client, "/uml/diagrams", {"kind": "class", "project_id": "px"}), 201
    )
    assert first["model_id"] == second["model_id"] and first["project_id"] == "px"
    model = await ok(await test_client.get(f"/modeling/models/{first['model_id']}", headers=hdrs()))
    assert model["language"] == "uml" and model["is_default"] is True
    other = await ok(await post(test_client, "/uml/elements", {"kind": "class"}), 201)
    assert other["model_id"] != first["model_id"] and other["project_id"] is None


# ---------------------------------------------------------------------------
# Review findings (2026-10-27) — each test fails on the code before its fix
# ---------------------------------------------------------------------------


async def test_etag_is_the_uml_models_head_when_a_write_bumps_another_model(
    test_client: AsyncClient,
) -> None:
    """(1) A UML delete that prunes a link sourced in an ArchiMate model bumps both heads;
    the ETag is the UML model's (the request's target), not the other's or none."""
    m = await uml_model(test_client)
    el = await new_element(test_client, m["id"], "class", "C")
    am = await new_model(test_client)
    app = await ok(await element(test_client, am["id"], "application_component"))
    await ok(
        await post(
            test_client,
            "/modeling/trace-links",
            {
                "source": {"kind": "element", "id": app["id"]},
                "target": {"kind": "uml_element", "id": el["id"]},
                "link_type": "realizes",
            },
        ),
        201,
    )
    am_head = await head_of(test_client, am["id"])
    resp = await test_client.delete(f"/uml/elements/{el['id']}", headers=hdrs())
    assert resp.status_code == 200, resp.text
    assert await head_of(test_client, am["id"]) == am_head + 1
    assert etag(resp) == await head_of(test_client, m["id"])


async def test_a_private_link_on_a_shared_uml_model_is_not_in_its_manifest(
    test_client: AsyncClient,
) -> None:
    """(2) A link narrower than the model belongs to no model and never reaches a revision."""
    m = await uml_model(test_client)
    a = await new_element(test_client, m["id"], "class", "A")
    b = await new_element(test_client, m["id"], "class", "B")
    link = await ok(
        await post(
            test_client,
            "/modeling/trace-links",
            {
                "source": {"kind": "uml_element", "id": a["id"]},
                "target": {"kind": "uml_element", "id": b["id"]},
                "link_type": "depends_on",
            },
            hdrs(visibility="user"),
        ),
        201,
    )
    assert link["visibility"] == "user" and link["model_id"] is None
    rev = await seal(test_client, m["id"])
    assert (await revision(test_client, m["id"], rev["revision_id"]))["manifest"][
        "trace_links"
    ] == []


async def test_validation_runs_off_the_event_loop_and_a_change_set_validates_once(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(3) The whole-model validator never runs on the event loop thread while the model
    lock is held, and applying a UML change set validates the model once, not per op."""
    import threading

    from services import uml_draft

    loop_thread = threading.get_ident()
    calls: list[int] = []
    real = uml_draft.validate_uml

    def recording(*args: Any, **kw: Any) -> Any:
        calls.append(threading.get_ident())
        return real(*args, **kw)

    monkeypatch.setattr(uml_draft, "validate_uml", recording)
    m = await uml_model(test_client)
    await new_element(test_client, m["id"], "class", "A")
    assert calls and loop_thread not in calls
    ops = [
        {"op": "create", "kind": "uml_element", "id": f"c{n}", "attributes": {"kind": "class"}}
        for n in range(4)
    ]
    cs = await ok(
        await propose(test_client, m["id"], ops, await head_of(test_client, m["id"])), 201
    )
    assert cs["status"] == "proposed"
    calls.clear()
    await ok(await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs()))
    assert len(calls) == 2, calls  # the draft before and after, once
    assert loop_thread not in calls
    listed = await ok(
        await test_client.get("/uml/elements", params={"model_id": m["id"]}, headers=hdrs())
    )
    assert len(listed) == 5


async def test_the_size_cap_counts_interactions_diagrams_and_occurrences(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(4) Every row and interaction part counts towards MODEL_SYNC_MAX_CONCEPTS."""
    m = await uml_model(test_client)
    a = await new_element(test_client, m["id"], "class", "A")
    d = await ok(
        await post(test_client, "/uml/diagrams", {"model_id": m["id"], "kind": "class"}), 201
    )
    await ok(
        await post(test_client, f"/uml/diagrams/{d['id']}/objects", {"element_ref": a["id"]}), 201
    )
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 10)
    doc = {
        "model_id": m["id"],
        "lifelines": [{"id": "l1"}],
        "occurrences": [{"id": f"o{n}", "lifeline": "l1"} for n in range(12)],
    }
    resp = await post(test_client, "/uml/interactions", doc)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 2)  # 3 rows: el, dia, obj
    resp = await test_client.get(f"/uml/diagrams/{d['id']}/render", headers=hdrs())
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"
    # Sealing is bounded by MODEL_ASYNC_MAX_CONCEPTS, with the same row count.
    monkeypatch.setattr(get_settings(), "model_async_max_concepts", 2)
    seal_resp = await test_client.post(f"/modeling/models/{m['id']}/revisions", headers=hdrs())
    assert seal_resp.status_code == 422
    monkeypatch.setattr(get_settings(), "model_async_max_concepts", 3)
    seal_resp = await test_client.post(f"/modeling/models/{m['id']}/revisions", headers=hdrs())
    assert seal_resp.status_code == 201, seal_resp.text


async def test_body_lists_are_bounded(test_client: AsyncClient) -> None:
    """(4) Every list a UML body carries has an upper bound."""
    m = await uml_model(test_client)
    too_many = {
        "model_id": m["id"],
        "lifelines": [{"id": f"l{n}"} for n in range(5001)],
    }
    assert (await post(test_client, "/uml/interactions", too_many)).status_code == 422
    params = [{"id": f"p{n}"} for n in range(501)]
    body = {
        "model_id": m["id"],
        "kind": "class",
        "attributes": {"operations": [{"id": "op", "parameters": params}]},
    }
    assert (await post(test_client, "/uml/elements", body)).status_code == 422
    d = await ok(
        await post(test_client, "/uml/diagrams", {"model_id": m["id"], "kind": "class"}), 201
    )
    a = await new_element(test_client, m["id"], "class", "A")
    o = await ok(
        await post(test_client, f"/uml/diagrams/{d['id']}/objects", {"element_ref": a["id"]}), 201
    )
    bends = [{"x": n, "y": n} for n in range(501)]
    resp = await post(
        test_client,
        f"/uml/diagrams/{d['id']}/connections",
        {
            "relationship_ref": "x",
            "source_object_id": o["id"],
            "target_object_id": o["id"],
            "bendpoints": bends,
        },
    )
    assert resp.status_code == 422 and "model_integrity" not in resp.text


async def test_editing_an_already_invalid_value_is_not_an_introduced_error(
    test_client: AsyncClient, test_session_factory
) -> None:
    """(5) The gate keys findings on (rule, targets, path): a row already invalid at a
    position (stored before a rule tightened) can be edited without the edit being
    refused for the error it already had; a NEW invalid position is still refused."""
    from models.uml import UmlElement
    from schemas.uml import UmlElementAttributes

    m = await uml_model(test_client)
    attrs = UmlElementAttributes.model_validate(
        {"properties": [{"id": "p", "multiplicity": "3..1"}]}
    ).model_dump(mode="json")
    async with test_session_factory() as s:
        row = UmlElement(
            tenant_id=TENANT,
            model_id=uuid.UUID(m["id"]),
            visibility="tenant",
            subject_id=ALICE,
            kind="class",
            name="Legacy",
            attributes=attrs,
        )
        s.add(row)
        await s.commit()
        rid = str(row.id)
    fixed_up = {
        "kind": "class",
        "name": "Legacy",
        "attributes": {"properties": [{"id": "p", "multiplicity": "5..2"}]},
    }
    await ok(await put(test_client, f"/uml/elements/{rid}", fixed_up))
    worse = {
        "kind": "class",
        "attributes": {
            "properties": [{"id": "p", "multiplicity": "5..2"}, {"id": "q", "multiplicity": "9..1"}]
        },
    }
    assert "UML-MUL-001" in finding_rules(await put(test_client, f"/uml/elements/{rid}", worse))


async def test_occurrence_creates_take_an_idempotency_key(test_client: AsyncClient) -> None:
    """(6) POST …/objects and …/connections replay on the same key and body."""
    m = await uml_model(test_client)
    a = await new_element(test_client, m["id"], "class", "A")
    b = await new_element(test_client, m["id"], "class", "B")
    r = await ok(
        await post(
            test_client,
            "/uml/relationships",
            {"kind": "dependency", "source": a["id"], "target": b["id"]},
        ),
        201,
    )
    d = await ok(
        await post(test_client, "/uml/diagrams", {"model_id": m["id"], "kind": "class"}), 201
    )
    path = f"/uml/diagrams/{d['id']}"
    first = await post(test_client, f"{path}/objects", {"element_ref": a["id"]}, hdrs(key="o"))
    head = await head_of(test_client, m["id"])
    again = await post(test_client, f"{path}/objects", {"element_ref": a["id"]}, hdrs(key="o"))
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    assert await head_of(test_client, m["id"]) == head
    other = await post(test_client, f"{path}/objects", {"element_ref": b["id"]}, hdrs(key="o"))
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"
    ob = await ok(await post(test_client, f"{path}/objects", {"element_ref": b["id"]}), 201)
    body = {
        "relationship_ref": r["id"],
        "source_object_id": first.json()["id"],
        "target_object_id": ob["id"],
    }
    c1 = await post(test_client, f"{path}/connections", body, hdrs(key="c"))
    c2 = await post(test_client, f"{path}/connections", body, hdrs(key="c"))
    assert c1.status_code == c2.status_code == 201 and c1.json()["id"] == c2.json()["id"]
    assert len(await ok(await test_client.get(f"{path}/objects", headers=hdrs()))) == 2
    assert len(await ok(await test_client.get(f"{path}/connections", headers=hdrs()))) == 1


async def test_the_database_keeps_references_inside_one_model(
    test_client: AsyncClient, test_session_factory
) -> None:
    """(7) Composite FKs: whatever path writes, a reference cannot cross models/diagrams."""
    from sqlalchemy.exc import IntegrityError

    from models.uml import UmlDiagramConnection, UmlDiagramObject, UmlRelationship

    m1, m2 = await uml_model(test_client), await uml_model(test_client, title="two")
    a1 = await new_element(test_client, m1["id"], "class", "A1")
    b1 = await new_element(test_client, m1["id"], "class", "B1")
    a2 = await new_element(test_client, m2["id"], "class", "A2")
    d1 = await ok(
        await post(test_client, "/uml/diagrams", {"model_id": m1["id"], "kind": "class"}), 201
    )
    d1b = await ok(
        await post(test_client, "/uml/diagrams", {"model_id": m1["id"], "kind": "class"}), 201
    )
    r1 = await ok(
        await post(
            test_client,
            "/uml/relationships",
            {"kind": "dependency", "source": a1["id"], "target": b1["id"]},
        ),
        201,
    )
    o1 = await ok(
        await post(test_client, f"/uml/diagrams/{d1['id']}/objects", {"element_ref": a1["id"]}), 201
    )
    ob = await ok(
        await post(test_client, f"/uml/diagrams/{d1b['id']}/objects", {"element_ref": b1["id"]}),
        201,
    )
    mid1 = uuid.UUID(m1["id"])

    def u(x: str) -> uuid.UUID:
        return uuid.UUID(x)

    bad_rows = [
        UmlDiagramObject(
            tenant_id=TENANT, model_id=mid1, diagram_id=u(d1["id"]), element_ref=u(a2["id"])
        ),
        UmlRelationship(
            tenant_id=TENANT,
            model_id=mid1,
            visibility="tenant",
            subject_id=ALICE,
            kind="dependency",
            source_ref=u(a1["id"]),
            target_ref=u(a2["id"]),
        ),
        UmlDiagramConnection(
            tenant_id=TENANT,
            model_id=mid1,
            diagram_id=u(d1["id"]),
            relationship_ref=u(r1["id"]),
            source_object_id=u(o1["id"]),
            target_object_id=u(ob["id"]),
        ),
    ]
    for row in bad_rows:
        async with test_session_factory() as s:
            s.add(row)
            with pytest.raises(IntegrityError):
                await s.commit()


async def test_a_tenant_only_caller_owns_its_rows_as_the_tenant(
    test_client: AsyncClient, test_session_factory
) -> None:
    """(8) No subject header: rows carry subject_id = tenant id, never ''."""
    from sqlalchemy import select

    from models.uml import UmlElement

    tenant_only = {"X-Tenant-Id": TENANT}
    resp = await post(test_client, "/uml/elements", {"kind": "class"}, tenant_only)
    el = await ok(resp, 201)
    async with test_session_factory() as s:
        subject = (
            await s.execute(
                select(UmlElement.subject_id).where(UmlElement.id == uuid.UUID(el["id"]))
            )
        ).scalar_one()
    assert subject == TENANT


async def test_concurrent_apply_and_reject_of_a_uml_change_set(test_client: AsyncClient) -> None:
    """(9) Two applies, or an apply and a reject, racing: exactly one wins, the other 409."""
    import asyncio

    m = await uml_model(test_client)
    ops = [{"op": "create", "kind": "uml_element", "id": "c", "attributes": {"kind": "class"}}]

    async def proposed() -> str:
        cs = await ok(
            await propose(test_client, m["id"], ops, await head_of(test_client, m["id"])), 201
        )
        return cs["id"]

    cid = await proposed()
    apply = f"/modeling/change-sets/{cid}/apply"
    r1, r2 = await asyncio.gather(
        test_client.post(apply, headers=hdrs()), test_client.post(apply, headers=hdrs())
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 409], (r1.text, r2.text)
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["error"]["code"] == "change_set_not_proposed"
    listed = await ok(
        await test_client.get("/uml/elements", params={"model_id": m["id"]}, headers=hdrs())
    )
    assert len(listed) == 1

    cid = await proposed()
    a, r = await asyncio.gather(
        test_client.post(f"/modeling/change-sets/{cid}/apply", headers=hdrs()),
        test_client.post(
            f"/modeling/change-sets/{cid}/reject", json={"reason": "no"}, headers=hdrs()
        ),
    )
    assert sorted([a.status_code, r.status_code]) == [200, 409], (a.text, r.text)
    final = await ok(await test_client.get(f"/modeling/change-sets/{cid}", headers=hdrs()))
    assert final["status"] == ("applied" if a.status_code == 200 else "rejected")
    listed = await ok(
        await test_client.get("/uml/elements", params={"model_id": m["id"]}, headers=hdrs())
    )
    assert len(listed) == (2 if a.status_code == 200 else 1)


async def test_render_etag_ignores_format_for_reports_and_drawing_neutral_edits(
    test_client: AsyncClient,
) -> None:
    """(10) A report's ETag does not depend on ``format``; a documentation-only diagram
    edit (the renderer never sees it) keeps the ETag, a rename changes it."""
    m = await uml_model(test_client)
    ids = await load_example(test_client, m["id"])
    path = f"/uml/diagrams/{ids['d_class']}/render"
    svg_report = await test_client.get(path, params={"report": "true"}, headers=hdrs())
    png_report = await test_client.get(
        path, params={"report": "true", "format": "png"}, headers=hdrs()
    )
    assert svg_report.headers["etag"] == png_report.headers["etag"]
    assert svg_report.json() == png_report.json()
    image = await test_client.get(path, headers=hdrs())
    tag = image.headers["etag"]
    assert tag != svg_report.headers["etag"]
    cur = await ok(await test_client.get(f"/uml/diagrams/{ids['d_class']}", headers=hdrs()))
    await ok(
        await put(
            test_client,
            f"/uml/diagrams/{ids['d_class']}",
            {"kind": cur["kind"], "name": cur["name"], "documentation": "now documented"},
        )
    )
    same = await test_client.get(path, headers={**hdrs(), "If-None-Match": tag})
    assert same.status_code == 304
    await ok(
        await put(
            test_client,
            f"/uml/diagrams/{ids['d_class']}",
            {"kind": cur["kind"], "name": "Renamed", "documentation": "now documented"},
        )
    )
    changed = await test_client.get(path, headers={**hdrs(), "If-None-Match": tag})
    assert changed.status_code == 200


async def test_a_foreign_tenant_row_never_enters_a_uml_seal(
    test_client: AsyncClient, test_session_factory
) -> None:
    """A foreign-tenant row carrying the model id (pre-gate data) is not model content."""
    from models.uml import UmlElement

    m = await uml_model(test_client)
    mid = m["id"]
    own = await new_element(test_client, mid, "class", "Own")
    async with test_session_factory() as s:
        foreign = UmlElement(
            tenant_id=OTHER_TENANT,
            model_id=uuid.UUID(mid),
            visibility="tenant",
            subject_id=BOB,
            kind="class",
            name="Foreign",
            attributes={},
        )
        s.add(foreign)
        await s.commit()
        foreign_id = str(foreign.id)

    rev = await seal(test_client, mid)
    detail = await revision(test_client, mid, rev["revision_id"])
    sealed = {e["id"] for e in detail["manifest"]["elements"]}
    assert own["id"] in sealed  # non-vacuous
    assert foreign_id not in sealed


async def test_deleting_a_model_removes_the_links_of_a_row_it_cascades(
    test_client: AsyncClient, test_session_factory
) -> None:
    """``delete_links_into_model`` collects ids by ``model_id`` alone, deliberately.

    A UML row's ``model_id`` FK cascades, so a foreign-tenant row carrying the id IS
    deleted by the database with the model; its trace links are not cascade-deleted
    (generic ``source_id`` / ``target_id``), so tenant-scoping the collection would leave
    them dangling. This pins that the links go with the row the cascade deletes.
    """
    from models.modeling import ModelingTraceLink
    from models.uml import UmlElement

    m = await uml_model(test_client)
    mid = m["id"]
    own = await new_element(test_client, mid, "class", "Own")
    async with test_session_factory() as s:
        foreign = UmlElement(
            tenant_id=OTHER_TENANT,
            model_id=uuid.UUID(mid),
            visibility="tenant",
            subject_id=BOB,
            kind="class",
            name="Foreign",
            attributes={},
        )
        s.add(foreign)
        await s.flush()
        s.add(
            ModelingTraceLink(
                tenant_id=OTHER_TENANT,
                subject_id=BOB,
                visibility="tenant",
                source_kind="uml_element",
                source_id=foreign.id,
                target_kind="element",
                target_id=uuid.UUID(own["id"]),
                link_type="traces_to",
                created_by=BOB,
            )
        )
        await s.commit()
        foreign_id = str(foreign.id)
    async with test_session_factory() as s:
        link_id = (
            await s.execute(
                text("SELECT id FROM modeling_trace_links WHERE source_id = :s"),
                {"s": foreign_id},
            )
        ).scalar_one()  # non-vacuous: the pre-gate link exists

    await ok(await test_client.delete(f"/modeling/models/{mid}", headers=hdrs()))
    async with test_session_factory() as s:
        rows = (
            await s.execute(
                text("SELECT count(*) FROM uml_elements WHERE model_id = :m"), {"m": mid}
            )
        ).scalar_one()
        links = (
            await s.execute(
                text("SELECT count(*) FROM modeling_trace_links WHERE id = :id"),
                {"id": str(link_id)},
            )
        ).scalar_one()
    assert rows == 0  # the FK cascade took the foreign row
    assert links == 0  # and its link did not dangle
