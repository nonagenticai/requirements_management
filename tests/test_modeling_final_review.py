"""Final pre-merge review fixes of the modeling branch (2026-10-27), over HTTP.

Each test fails on the code before its fix:

2. CPU work (canonicalise / hash / export / validate / occurrence check) runs off the loop;
3. a UML one-shot persist IntegrityError is not blamed on ``/ops/0``;
4. ArchiMate and UML render routes share one behaviour;
5. staging loss codes are one declared, enforced set;
7. view / diagram objects and folder trees keep their stored order (z-order);
10. an Idempotency-Key replay is answered before the size cap.

(1: ``test_exchange_schema_thread_safety.py``; 6: ``test_migration_chain_shape.py``;
8: ``test_openapi_error_models.py``; 9: ``test_migration_downgrade_with_data.py``.)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient

from services import model_exchange as xsvc
from services import modeling_models as models_svc
from services import view_occurrences
from tests.test_modeling_lifecycle import hdrs, ok, populated
from tests.test_modeling_lifecycle_part2 import run, seal
from tools.architecture import archimate_rules

# --- 2. CPU work never runs on the event loop -----------------------------------------


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _spy(calls: list[tuple[str, bool]], name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, _on_loop()))
        return fn(*args, **kwargs)

    return wrapper


@pytest.fixture
def cpu_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    calls: list[tuple[str, bool]] = []
    for mod, attr in (
        (models_svc, "canonicalize"),
        (models_svc, "content_hash"),
        (xsvc, "export_manifest"),
        (archimate_rules, "validate"),
        (view_occurrences, "introduced_errors"),
    ):
        monkeypatch.setattr(mod, attr, _spy(calls, attr, getattr(mod, attr)))
    return calls


async def test_cpu_work_runs_off_the_event_loop(
    test_client: AsyncClient, cpu_calls: list[tuple[str, bool]]
) -> None:
    p = await populated(test_client)  # occurrence creates run the occurrence check
    mid = p["model"]["id"]
    await ok(await run(test_client, mid), 201)  # draft run: canonicalise, hash, validate
    rev = await seal(test_client, mid)  # seal: canonicalise + hash under FOR UPDATE
    for url in (
        f"/modeling/models/{mid}/exchange",
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/exchange",
    ):
        resp = await test_client.get(url, headers=hdrs())
        assert resp.status_code == 200, resp.text
    names = {n for n, _ in cpu_calls}
    assert {
        "canonicalize",
        "content_hash",
        "export_manifest",
        "validate",
        "introduced_errors",
    } <= names, names
    on_loop = sorted({n for n, loop in cpu_calls if loop})
    assert on_loop == [], f"ran on the event loop: {on_loop}"


# --- 3. a UML persist constraint is not blamed on /ops/0 ------------------------------


async def test_uml_apply_constraint_blames_no_op(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    from services import uml_store
    from tests.test_uml_storage import new_element, uml_model
    from tests.test_uml_storage import propose as uml_propose

    m = await uml_model(test_client)
    mid = m["id"]
    pkg = await new_element(test_client, mid, "package", "P")
    ops = [
        {
            "op": "create",
            "kind": "uml_element",
            "id": f"c{i}",
            "attributes": {"kind": "class", "name": f"C{i}", "owner_id": pkg["id"]},
        }
        for i in range(2)
    ]
    head = (await ok(await test_client.get(f"/modeling/models/{mid}", headers=hdrs())))["head"]
    cs = await ok(await uml_propose(test_client, mid, ops, head), 201)
    assert cs["status"] == "proposed", cs

    class _Diag:
        constraint_name = "uq_probe"

    class _Orig(Exception):
        diag = _Diag()

    async def boom(*_a: Any, **_k: Any) -> None:
        raise IntegrityError("INSERT", {}, _Orig("duplicate"))

    monkeypatch.setattr(uml_store, "commit_draft", boom)
    resp = await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    assert resp.status_code == 422, resp.text
    findings = resp.json()["error"]["findings"]
    assert [f["rule_id"] for f in findings] == ["CS-OP-003"]
    assert findings[0]["path"] is None
    assert "uq_probe" in findings[0]["message"] and "one write" in findings[0]["message"]
    still = await ok(await test_client.get(f"/modeling/change-sets/{cs['id']}", headers=hdrs()))
    assert still["status"] == "proposed"


# --- 4. one render behaviour for ArchiMate views and UML diagrams ---------------------


async def test_render_routes_share_one_behaviour(test_client: AsyncClient) -> None:
    from tests.test_uml_storage import post, uml_model

    a = await populated(test_client)
    amid, vid = a["model"]["id"], a["view"]["id"]
    arev = (await seal(test_client, amid))["revision_id"]
    u = await uml_model(test_client)
    umid = u["id"]
    did = (
        await ok(await post(test_client, "/uml/diagrams", {"model_id": umid, "kind": "class"}), 201)
    )["id"]
    urev = (await seal(test_client, umid))["revision_id"]

    for path in (f"/architecture/views/{vid}/render", f"/uml/diagrams/{did}/render"):
        plain = await test_client.get(path, headers=hdrs())
        assert plain.status_code == 200, plain.text
        assert plain.headers["cache-control"] == "private, no-cache", path
        cached = await test_client.get(
            path, headers={**hdrs(), "If-None-Match": plain.headers["etag"]}
        )
        assert cached.status_code == 304 and cached.headers["cache-control"] == "private, no-cache"
        rep = await test_client.get(
            path, params={"report": "true", "format": "png"}, headers=hdrs()
        )
        assert rep.status_code == 200, (path, rep.text)
        assert rep.headers["content-type"].startswith("application/json")
        assert rep.headers["cache-control"] == "private, no-cache"
        assert set(rep.json()) == {"svg", "diagnostics", "renderer_version"}
        assert rep.json()["svg"] == plain.text

    # A revision of the other language: 404 on both families.
    wrong = (
        f"/modeling/models/{umid}/revisions/{urev}/views/{vid}/render",
        f"/modeling/models/{amid}/revisions/{arev}/diagrams/{did}/render",
    )
    for path in wrong:
        resp = await test_client.get(path, headers=hdrs())
        assert resp.status_code == 404, (path, resp.status_code, resp.text)


# --- 5. staging loss codes: one declared, closed set ----------------------------------


def test_staging_loss_codes_are_one_closed_set() -> None:
    import ast
    import inspect

    from tools.architecture.exchange import LOSS_CODES

    tree = ast.parse(inspect.getsource(xsvc))
    literals = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("STAGE-")
    }
    assert literals == set(xsvc.STAGING_LOSS_CODES)
    assert not set(xsvc.STAGING_LOSS_CODES) & set(LOSS_CODES)
    for code in xsvc.STAGING_LOSS_CODES:
        assert f"``{code}``" in (xsvc.__doc__ or ""), code
    diff = xsvc._Diff.__new__(xsvc._Diff)  # noqa: SLF001
    diff.losses = []
    with pytest.raises(ValueError, match="STAGE-UNDECLARED"):
        diff.loss("STAGE-UNDECLARED", None, "x")
    diff.loss("STAGE-MODEL-HEADER", None, "x")
    assert [x.code for x in diff.losses] == ["STAGE-MODEL-HEADER"]


# --- 7. stored order (z-order, folder order) is model content -------------------------


def _first_seen(svg: str, ids: list[str]) -> list[str]:
    import re

    seen: list[str] = []
    for m in re.finditer(r'data-object-id="([^"]+)"', svg):
        if m.group(1) in ids and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


async def _notes(client: AsyncClient, vid: str, texts: list[str]) -> list[str]:
    out = []
    for t in texts:
        o = await ok(
            await client.post(
                f"/architecture/views/{vid}/objects",
                json={"kind": "note", "text": t, "bounds": {"x": 0, "y": 0, "w": 100, "h": 40}},
                headers=hdrs(),
            ),
            201,
        )
        out.append(o["id"])
    return out


async def _manifest(client: AsyncClient, mid: str) -> tuple[dict[str, Any], str]:
    rev = await seal(client, mid)
    body = await ok(
        await client.get(f"/modeling/models/{mid}/revisions/{rev['revision_id']}", headers=hdrs())
    )
    return body["manifest"], body["content_hash"]


def _xml_note_order(xml: bytes, texts: set[str]) -> list[str]:
    from lxml import etree

    out = []
    for el in etree.fromstring(xml).iter():
        if isinstance(el.tag, str) and el.tag.endswith("}label") and el.text in texts:
            out.append(el.text)
    return out


async def test_view_object_z_order_round_trips(test_client: AsyncClient) -> None:
    from tests.test_modeling_exchange_routes import apply, import_into, import_new
    from tests.test_modeling_lifecycle import head_of, new_model, view

    m = await new_model(test_client)
    mid = m["id"]
    v = await ok(await view(test_client, mid))
    vid = v["id"]
    a, b, c = await _notes(test_client, vid, ["za", "zb", "zc"])
    # Ids ascend with creation (uuid7), so id order == creation order here; a move breaks it.
    _, hash_before = await _manifest(test_client, mid)
    head = await head_of(test_client, mid)
    moved = await test_client.put(
        f"/architecture/views/{vid}/objects/{c}",
        json={
            "kind": "note",
            "text": "zc",
            "bounds": {"x": 0, "y": 0, "w": 100, "h": 40},
            "position": 0,
        },
        headers=hdrs(),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["position"] == 0
    assert await head_of(test_client, mid) == head + 1
    listed = await ok(await test_client.get(f"/architecture/views/{vid}/objects", headers=hdrs()))
    assert [o["id"] for o in listed] == [c, a, b]
    manifest, hash_after = await _manifest(test_client, mid)
    assert hash_after != hash_before
    (mv,) = [x for x in manifest["views"] if x["id"] == vid]
    assert [o["id"] for o in mv["objects"]] == [c, a, b]

    svg = (await test_client.get(f"/architecture/views/{vid}/render", headers=hdrs())).text
    assert _first_seen(svg, [a, b, c]) == [c, a, b]  # drawn bottom to top

    xml = (await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())).content
    texts = {"za", "zb", "zc"}
    assert _xml_note_order(xml, texts) == ["zc", "za", "zb"]
    # Own export re-imported: nothing to do (order included).
    again = await ok(await import_into(test_client, mid, xml), 201)
    assert again["change_set"]["ops"] == []

    # Into a new model: the same order, through seal and export.
    out = await import_new(test_client, xml)
    await apply(test_client, out["change_set"]["id"])
    m2 = out["model_id"]
    man2, _ = await _manifest(test_client, m2)
    (v2,) = man2["views"]
    texts_in_order = [o["text"] for o in v2["objects"]]
    assert texts_in_order == ["zc", "za", "zb"]
    xml2 = (await test_client.get(f"/modeling/models/{m2}/exchange", headers=hdrs())).content
    assert _xml_note_order(xml2, texts) == ["zc", "za", "zb"]

    # A document with another sibling order stages the moves; applying them reorders.
    reordered = xml.replace(b">zc<", b">TMP<").replace(b">zb<", b">zc<").replace(b">TMP<", b">zb<")
    staged = await ok(await import_into(test_client, mid, reordered), 201)
    assert staged["change_set"]["status"] == "proposed", staged["change_set"]
    await apply(test_client, staged["change_set"]["id"])
    listed = await ok(await test_client.get(f"/architecture/views/{vid}/objects", headers=hdrs()))
    # Same objects in the same order; only their texts were swapped.
    assert [(o["id"], o["text"]) for o in listed] == [(c, "zb"), (a, "za"), (b, "zc")]


async def test_import_stages_z_order_moves(test_client: AsyncClient) -> None:
    from tests.test_modeling_exchange_routes import apply, import_into
    from tests.test_modeling_lifecycle import new_model, view

    m = await new_model(test_client)
    mid = m["id"]
    v = await ok(await view(test_client, mid))
    a, b, c = await _notes(test_client, v["id"], ["ya", "yb", "yc"])
    xml = (await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())).content
    from lxml import etree

    root = etree.fromstring(xml)
    nodes = [el for el in root.iter() if isinstance(el.tag, str) and el.tag.endswith("}node")]
    parent = nodes[0].getparent()
    for n in nodes:
        parent.remove(n)
    for n in reversed(nodes):  # yc, yb, ya
        parent.append(n)
    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    staged = await ok(await import_into(test_client, mid, data), 201)
    ops = staged["change_set"]["ops"]
    assert ops and all(set(op["attributes"]) == {"position"} for op in ops), ops
    await apply(test_client, staged["change_set"]["id"])
    listed = await ok(
        await test_client.get(f"/architecture/views/{v['id']}/objects", headers=hdrs())
    )
    assert [o["id"] for o in listed] == [c, b, a]


async def test_folder_order_is_kept_and_is_content(test_client: AsyncClient) -> None:
    from tests.test_modeling_lifecycle import head_of, new_model

    m = await new_model(test_client)
    mid = m["id"]

    def tree(names: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "id": f"f-{n}",
                "name": n,
                "documentation": None,
                "items": [],
                "children": [
                    {
                        "id": f"f-{n}-{k}",
                        "name": k,
                        "documentation": None,
                        "items": [],
                        "children": [],
                    }
                    for k in ("z", "a")
                ],
            }
            for n in names
        ]

    async def put(names: list[str]) -> None:
        resp = await test_client.put(
            f"/modeling/models/{mid}/organizations",
            json={"organizations": tree(names)},
            headers=hdrs(),
        )
        assert resp.status_code == 200, resp.text

    await put(["zeta", "alpha"])
    got = await ok(await test_client.get(f"/modeling/models/{mid}/organizations", headers=hdrs()))
    assert [f["name"] for f in got["organizations"]] == ["zeta", "alpha"]
    assert [k["name"] for k in got["organizations"][0]["children"]] == ["z", "a"]
    manifest, h1 = await _manifest(test_client, mid)
    assert [f["name"] for f in manifest["organizations"]] == ["zeta", "alpha"]
    assert [k["name"] for k in manifest["organizations"][0]["children"]] == ["z", "a"]
    head = await head_of(test_client, mid)
    await put(["alpha", "zeta"])
    assert await head_of(test_client, mid) == head + 1
    _, h2 = await _manifest(test_client, mid)
    assert h2 != h1


async def test_uml_diagram_object_z_order(test_client: AsyncClient) -> None:
    from tests.test_modeling_lifecycle import head_of
    from tests.test_uml_storage import new_element, post, uml_model

    m = await uml_model(test_client)
    mid = m["id"]
    pkg = await new_element(test_client, mid, "package", "P")
    classes = [
        await new_element(test_client, mid, "class", n, owner_id=pkg["id"]) for n in ("A", "B", "C")
    ]
    did = (
        await ok(await post(test_client, "/uml/diagrams", {"model_id": mid, "kind": "class"}), 201)
    )["id"]
    objs = [
        (
            await ok(
                await post(test_client, f"/uml/diagrams/{did}/objects", {"element_ref": c["id"]}),
                201,
            )
        )["id"]
        for c in classes
    ]
    a, b, c = objs
    _, hash_before = await _manifest(test_client, mid)
    head = await head_of(test_client, mid)
    moved = await test_client.put(
        f"/uml/diagrams/{did}/objects/{c}",
        json={"element_ref": classes[2]["id"], "position": 0},
        headers=hdrs(),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["position"] == 0
    assert await head_of(test_client, mid) == head + 1
    listed = await ok(await test_client.get(f"/uml/diagrams/{did}/objects", headers=hdrs()))
    assert [o["id"] for o in listed] == [c, a, b]
    assert [o["position"] for o in listed] == [0, 1, 2]
    manifest, hash_after = await _manifest(test_client, mid)
    assert hash_after != hash_before
    (d,) = manifest["diagrams"]
    assert [o["id"] for o in d["objects"]] == [c, a, b]
    svg = (await test_client.get(f"/uml/diagrams/{did}/render", headers=hdrs())).text
    assert _first_seen(svg, objs) == [c, a, b]
    # A create with a position inserts there.
    d_el = await new_element(test_client, mid, "class", "D", owner_id=pkg["id"])
    ins = await ok(
        await post(
            test_client, f"/uml/diagrams/{did}/objects", {"element_ref": d_el["id"], "position": 1}
        ),
        201,
    )
    listed = await ok(await test_client.get(f"/uml/diagrams/{did}/objects", headers=hdrs()))
    assert [o["id"] for o in listed] == [c, ins["id"], a, b]
    # A change set moves it too (in memory, then persisted).
    from tests.test_uml_storage import propose as uml_propose

    cs = await ok(
        await uml_propose(
            test_client,
            mid,
            [{"op": "update", "kind": "diagram_object", "id": b, "attributes": {"position": 0}}],
            await head_of(test_client, mid),
        ),
        201,
    )
    assert cs["status"] == "proposed", cs
    await ok(await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs()))
    listed = await ok(await test_client.get(f"/uml/diagrams/{did}/objects", headers=hdrs()))
    assert [o["id"] for o in listed] == [b, c, ins["id"], a]


# --- 10. an Idempotency-Key replay is answered before the size cap --------------------


async def test_import_replay_is_answered_before_the_size_cap(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from settings import get_settings
    from tests.test_modeling_exchange_routes import import_into, xh
    from tests.test_modeling_lifecycle import element, new_model

    src = await new_model(test_client)
    await ok(await element(test_client, src["id"]))
    xml = (await test_client.get(f"/modeling/models/{src['id']}/exchange", headers=hdrs())).content
    target = (await new_model(test_client))["id"]
    first = await test_client.post(
        f"/modeling/models/{target}/exchange-imports", content=xml, headers=xh(key="replay-k1")
    )
    assert first.status_code == 201, first.text
    # The head grows; the cap now refuses the same import...
    for _ in range(2):
        await ok(await element(test_client, target))
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 2)
    fresh = await import_into(test_client, target, xml)
    assert fresh.status_code == 422 and fresh.json()["error"]["code"] == "input_too_large"
    # ...but the keyed retry replays the original response.
    again = await test_client.post(
        f"/modeling/models/{target}/exchange-imports", content=xml, headers=xh(key="replay-k1")
    )
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    # New-model import: same, with the document itself over the cap.
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 10)
    new1 = await test_client.post(
        "/modeling/models/exchange-imports", content=xml, headers=xh(key="replay-k2")
    )
    assert new1.status_code == 201, new1.text
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 0)
    new2 = await test_client.post(
        "/modeling/models/exchange-imports", content=xml, headers=xh(key="replay-k2")
    )
    assert new2.status_code == 201 and new2.json() == new1.json()
    # Another body under the same key is still a conflict.
    other = await test_client.post(
        "/modeling/models/exchange-imports",
        content=xml.replace(b"<model ", b"<model  "),
        headers=xh(key="replay-k2"),
    )
    assert other.status_code == 409, other.text


async def test_nested_import_stages_no_position(test_client: AsyncClient) -> None:
    from tests.test_modeling_exchange_routes import apply, import_into, import_new
    from tests.test_modeling_lifecycle import new_model, view

    m = await new_model(test_client)
    mid = m["id"]
    v = await ok(await view(test_client, mid))
    vid = v["id"]
    box = {"x": 0, "y": 0, "w": 300, "h": 200}
    group = await ok(
        await test_client.post(
            f"/architecture/views/{vid}/objects",
            json={"kind": "group", "name": "G", "bounds": box},
            headers=hdrs(),
        ),
        201,
    )
    for text in ("n1", "n2"):
        await ok(
            await test_client.post(
                f"/architecture/views/{vid}/objects",
                json={
                    "kind": "note",
                    "text": text,
                    "parent_object_id": group["id"],
                    "bounds": {"x": 10, "y": 10, "w": 50, "h": 20},
                },
                headers=hdrs(),
            ),
            201,
        )
    await _notes(test_client, vid, ["top-after"])
    xml = (await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())).content
    again = await ok(await import_into(test_client, mid, xml), 201)
    assert again["change_set"]["ops"] == []
    fresh = await import_new(test_client, xml)
    ops = fresh["change_set"]["ops"]
    assert [op for op in ops if "position" in (op.get("attributes") or {})] == []
    await apply(test_client, fresh["change_set"]["id"])
    xml2 = (
        await test_client.get(f"/modeling/models/{fresh['model_id']}/exchange", headers=hdrs())
    ).content
    texts = {"n1", "n2", "top-after"}
    assert _xml_note_order(xml2, texts) == _xml_note_order(xml, texts) == ["n1", "n2", "top-after"]
    back = await ok(await import_into(test_client, fresh["model_id"], xml2), 201)
    assert back["change_set"]["ops"] == []


async def test_out_of_range_position_is_invalid_at_propose(test_client: AsyncClient) -> None:
    from tests.test_modeling_lifecycle import head_of, new_model, view
    from tests.test_modeling_lifecycle_part2 import propose

    m = await new_model(test_client)
    mid = m["id"]
    v = await ok(await view(test_client, mid))
    (oid,) = await _notes(test_client, v["id"], ["p"])
    for bad in (100001, -1):
        cs = await ok(
            await propose(
                test_client,
                mid,
                [
                    {
                        "op": "update",
                        "kind": "view_object",
                        "id": oid,
                        "attributes": {"position": bad},
                    }
                ],
                await head_of(test_client, mid),
            ),
            201,
        )
        assert cs["status"] == "invalid", (bad, cs)
    ok_cs = await ok(
        await propose(
            test_client,
            mid,
            [
                {
                    "op": "update",
                    "kind": "view_object",
                    "id": oid,
                    "attributes": {"position": 100000},
                }
            ],
            await head_of(test_client, mid),
        ),
        201,
    )
    assert ok_cs["status"] == "proposed", ok_cs
    await ok(await test_client.post(f"/modeling/change-sets/{ok_cs['id']}/apply", headers=hdrs()))
