"""Exchange export/import and view rendering over HTTP, against real Postgres.

Export (revision manifest and draft head) is byte-for-byte the pure exporter's output;
import stages a change set (never writes the model), applies through the normal route,
and export → import is a fixed point; re-imports are idempotent; a domain-invalid file
stays ``invalid`` and cannot be applied; oversize / DOCTYPE / wrong media type are
refused; tenant and private-subject isolation hold on every route. Rendering: content
types, security headers, strong ETag + 304, boundary sanitization of hostile stored
text, revision renders immune to later draft edits, and the size refusal.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from models.architecture import ArchitectureElement
from services import model_exchange as xsvc
from settings import get_settings
from tests.test_modeling_lifecycle import (
    ALICE,
    BOB,
    OTHER_TENANT,
    TENANT,
    element,
    hdrs,
    head_of,
    new_model,
    ok,
    populated,
    view,
)
from tools.architecture.exchange import export_exchange, import_exchange
from tools.rendering import ARCHIMATE_RENDERER_VERSION, sanitize_svg

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures" / "exchange"
NATIVE = (FIXTURES / "archi510_native_export.xml").read_bytes()
MODEL_VIEW = (FIXTURES / "opengroup_Model_View.xml").read_bytes()
PROBE = (FIXTURES / "archi510_probe_reexport.xml").read_bytes()
XML = {"Content-Type": "application/xml"}


def xh(**kw: Any) -> dict[str, str]:
    return {**hdrs(**kw), **XML}


async def import_new(client: AsyncClient, data: bytes, **kw: Any) -> dict[str, Any]:
    params = kw.pop("params", None)
    return await ok(
        await client.post(
            "/modeling/models/exchange-imports", content=data, headers=xh(**kw), params=params
        ),
        201,
    )


async def import_into(client: AsyncClient, model_id: str, data: bytes, **kw: Any):
    return await client.post(
        f"/modeling/models/{model_id}/exchange-imports", content=data, headers=xh(**kw)
    )


async def apply(client: AsyncClient, cs_id: str, **kw: Any) -> dict[str, Any]:
    return await ok(await client.post(f"/modeling/change-sets/{cs_id}/apply", headers=hdrs(**kw)))


async def seal(client: AsyncClient, model_id: str) -> dict[str, Any]:
    return await ok(
        await client.post(f"/modeling/models/{model_id}/revisions", headers=hdrs()), 201
    )


def loss_codes(out: dict[str, Any]) -> set[str]:
    return {loss["code"] for loss in out["losses"]}


# --- export ----------------------------------------------------------------------


async def test_revision_export_is_the_pure_export_of_its_manifest(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid = w["model"]["id"]
    rev = await seal(test_client, mid)
    detail = await ok(
        await test_client.get(
            f"/modeling/models/{mid}/revisions/{rev['revision_id']}", headers=hdrs()
        )
    )
    manifest = detail["manifest"]
    from services.modeling_models import archimate_snapshot

    expected = export_exchange(
        archimate_snapshot(manifest),
        model_id=manifest["model"]["id"],
        name=manifest["model"]["title"],
        documentation=manifest["model"]["description"],
    )
    resp = await test_client.get(
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/exchange", headers=hdrs()
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == expected
    assert resp.headers["content-type"].startswith("application/xml")
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="model-{mid}-rev-{rev["seq"]}.xml"'
    )
    assert resp.headers["x-content-type-options"] == "nosniff"
    report = await ok(
        await test_client.get(
            f"/modeling/models/{mid}/revisions/{rev['revision_id']}/exchange",
            params={"report": "true"},
            headers=hdrs(),
        )
    )
    assert report["xml"].encode() == expected
    assert int(resp.headers["x-exchange-loss-count"]) == len(report["losses"])
    assert all(set(loss) == {"code", "id", "xpath", "message"} for loss in report["losses"])

    # Immutable: later draft edits change the draft export, never the revision's.
    await ok(await element(test_client, mid, "business_actor", title="Later"))
    again = await test_client.get(
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/exchange", headers=hdrs()
    )
    assert again.content == expected
    draft = await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())
    assert draft.status_code == 200 and b"Later" in draft.content
    assert draft.headers["etag"] == f'"{await head_of(test_client, mid)}"'
    assert b"Later" not in expected


async def test_export_refusals(test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    uml = await new_model(test_client, language="uml", language_version="2.5.1-p1")
    resp = await test_client.get(f"/modeling/models/{uml['id']}/exchange", headers=hdrs())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == xsvc.UNSUPPORTED_LANGUAGE
    rev = await seal(test_client, uml["id"])
    resp = await test_client.get(
        f"/modeling/models/{uml['id']}/revisions/{rev['revision_id']}/exchange", headers=hdrs()
    )
    assert resp.status_code == 409
    resp = await import_into(test_client, uml["id"], NATIVE)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == xsvc.UNSUPPORTED_LANGUAGE

    w = await populated(test_client)
    mid = w["model"]["id"]
    rev = await seal(test_client, mid)
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 2)
    for path in (
        f"/modeling/models/{mid}/exchange",
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/exchange",
    ):
        resp = await test_client.get(path, headers=hdrs())
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"


# --- import ----------------------------------------------------------------------


async def test_import_stages_applies_and_round_trips_to_a_fixed_point(
    test_client: AsyncClient,
) -> None:
    out = await import_new(test_client, MODEL_VIEW, params={"filename": "model_view.xml"})
    mid = out["model_id"]
    cs = out["change_set"]
    assert out["model_created"] is True and out["schema_valid"] is True
    assert out["source_sha256"] == hashlib.sha256(MODEL_VIEW).hexdigest()
    assert out["source_bytes"] == len(MODEL_VIEW) and out["source_filename"] == "model_view.xml"
    assert cs["status"] == "proposed" and cs["evaluator_ref"] == "exchange-import"
    assert cs["base_head"] == 0 and cs["ops"]
    assert {op["op"] for op in cs["ops"]} == {"create"}
    meta = cs["findings"][0]
    assert meta["rule_id"] == xsvc.META_RULE
    assert meta["evidence"]["source_sha256"] == out["source_sha256"]
    assert meta["evidence"]["schema_valid"] is True

    # Imported ids are the create ops' local references, verbatim.
    parsed = import_exchange(MODEL_VIEW)
    concept_ids = {c.id for c in parsed.snapshot.concepts}
    assert concept_ids <= {op["id"] for op in cs["ops"]}

    # Staging wrote nothing to the model.
    model = await ok(await test_client.get(f"/modeling/models/{mid}", headers=hdrs()))
    assert model["head"] == 0 and model["title"] == parsed.model_name

    applied = await apply(test_client, cs["id"])
    assert applied["status"] == "applied" and set(applied["id_map"]) >= concept_ids
    rev = await seal(test_client, mid)
    exported = await test_client.get(
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/exchange", headers=hdrs()
    )
    assert exported.status_code == 200

    # Export -> import onto the same head stages NOTHING: a fixed point.
    again = await ok(await import_into(test_client, mid, exported.content), 201)
    assert again["change_set"]["ops"] == []
    assert again["change_set"]["status"] == "proposed"
    assert again["model_created"] is False

    # Re-importing the ORIGINAL file is also a no-op: its ids resolve through the
    # applied import's id_map.
    original = await ok(await import_into(test_client, mid, MODEL_VIEW), 201)
    assert original["change_set"]["ops"] == []
    assert set(original["id_resolution"]) >= concept_ids

    # The draft export after applying the empty set is byte-identical to the revision's.
    await apply(test_client, again["change_set"]["id"])
    draft = await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())
    assert draft.content == exported.content


async def test_reimport_against_the_same_head_is_idempotent(test_client: AsyncClient) -> None:
    out = await import_new(test_client, NATIVE)
    mid = out["model_id"]
    await apply(test_client, out["change_set"]["id"])
    edited = (
        await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())
    ).content.replace(b">Actor<", b">Actor renamed<")
    first = await ok(await import_into(test_client, mid, edited), 201)
    second = await ok(await import_into(test_client, mid, edited), 201)
    assert first["change_set"]["ops"] == second["change_set"]["ops"]
    assert first["change_set"]["id"] != second["change_set"]["id"]
    ops = first["change_set"]["ops"]
    assert len(ops) == 1 and ops[0]["op"] == "update" and ops[0]["kind"] == "element"
    assert ops[0]["attributes"] == {"title": "Actor renamed"}
    await apply(test_client, first["change_set"]["id"])
    # The second set was proposed on the old head: stale.
    stale = await test_client.post(
        f"/modeling/change-sets/{second['change_set']['id']}/apply", headers=hdrs()
    )
    assert stale.status_code == 412


async def test_staging_losses_are_reported_and_stored(test_client: AsyncClient) -> None:
    out = await import_new(test_client, NATIVE)
    codes = loss_codes(out)
    # Native Archi file: a nameless junction. Its folder tree and its view without a
    # viewpoint are stored as they are, so they are NOT losses.
    assert "STAGE-NAME-DEFAULTED" in codes
    assert not codes & {"STAGE-VIEWPOINT-DEFAULTED", "STAGE-ORGANIZATIONS"}
    stored = await ok(
        await test_client.get(f"/modeling/change-sets/{out['change_set']['id']}", headers=hdrs())
    )
    stored_codes = {
        f["evidence"]["code"] for f in stored["findings"] if f["rule_id"] == xsvc.LOSS_RULE
    }
    assert stored_codes == codes
    assert all(
        f["severity"] == "warning" for f in stored["findings"] if f["rule_id"] == xsvc.LOSS_RULE
    )
    assert stored["findings"][0]["evidence"]["loss_count"] == len(out["losses"])


async def test_domain_invalid_import_stays_invalid_and_cannot_be_applied(
    test_client: AsyncClient,
) -> None:
    out = await import_new(test_client, PROBE)
    cs = out["change_set"]
    assert cs["status"] == "invalid"
    assert any(f["rule_id"] == "AM-REL-001" for f in cs["blocking_findings"])
    resp = await test_client.post(f"/modeling/change-sets/{cs['id']}/apply", headers=hdrs())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "change_set_not_proposed"
    model = await ok(await test_client.get(f"/modeling/models/{out['model_id']}", headers=hdrs()))
    assert model["head"] == 0


async def test_import_refusals(test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    m = await new_model(test_client)
    resp = await test_client.post(
        f"/modeling/models/{m['id']}/exchange-imports",
        content=NATIVE,
        headers={**hdrs(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 415 and resp.json()["error"]["code"] == "unsupported_media_type"

    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<model xmlns="http://www.opengroup.org/xsd/archimate/3.0/">&x;</model>'
    )
    for path in (
        f"/modeling/models/{m['id']}/exchange-imports",
        "/modeling/models/exchange-imports",
    ):
        resp = await test_client.post(path, content=xxe, headers=xh())
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == xsvc.EXCHANGE_INVALID
        assert "root:" not in resp.text  # no entity was resolved
    resp = await import_into(test_client, m["id"], b"<not-closed")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == xsvc.EXCHANGE_INVALID

    monkeypatch.setattr(get_settings(), "exchange_import_max_bytes", 100)
    for path in (
        f"/modeling/models/{m['id']}/exchange-imports",
        "/modeling/models/exchange-imports",
    ):
        resp = await test_client.post(path, content=NATIVE, headers=xh())
        assert resp.status_code == 413 and resp.json()["error"]["code"] == xsvc.EXCHANGE_TOO_LARGE

    monkeypatch.setattr(get_settings(), "exchange_import_max_bytes", 25 * 1024 * 1024)
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 2)
    resp = await import_into(test_client, m["id"], NATIVE)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"
    # Nothing was staged by any refusal.
    listed = await ok(
        await test_client.get(f"/modeling/models/{m['id']}/change-sets", headers=hdrs())
    )
    assert listed == []


async def test_import_idempotency_key(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    first = await ok(await import_into(test_client, m["id"], NATIVE, key="imp-1"), 201)
    replay = await ok(await import_into(test_client, m["id"], NATIVE, key="imp-1"), 201)
    assert replay == first
    conflict = await import_into(test_client, m["id"], MODEL_VIEW, key="imp-1")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    listed = await ok(
        await test_client.get(f"/modeling/models/{m['id']}/change-sets", headers=hdrs())
    )
    assert [c["id"] for c in listed] == [first["change_set"]["id"]]

    new1 = await import_new(test_client, NATIVE, key="new-1")
    new2 = await import_new(test_client, NATIVE, key="new-1")
    assert new1 == new2
    models = await ok(
        await test_client.get("/modeling/models", params={"q": "Native"}, headers=hdrs())
    )
    assert [x["id"] for x in models] == [new1["model_id"]]


async def test_tenant_and_private_subject_isolation(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    mid, vid = w["model"]["id"], w["view"]["id"]
    rev = await seal(test_client, mid)
    rid = rev["revision_id"]
    reads = (
        f"/modeling/models/{mid}/exchange",
        f"/modeling/models/{mid}/revisions/{rid}/exchange",
        f"/architecture/views/{vid}/render",
        f"/modeling/models/{mid}/revisions/{rid}/views/{vid}/render",
    )
    for path in reads:
        assert (await test_client.get(path, headers=hdrs())).status_code == 200, path
        other = await test_client.get(path, headers=hdrs(tenant=OTHER_TENANT))
        assert other.status_code == 404, path
    other = await test_client.post(
        f"/modeling/models/{mid}/exchange-imports",
        content=NATIVE,
        headers={**hdrs(tenant=OTHER_TENANT), **XML},
    )
    assert other.status_code == 404

    # A private model of ALICE: invisible to BOB on every route; BOB's own private
    # import is invisible to ALICE.
    private = await new_model(test_client, visibility="user")
    pv = await ok(
        await test_client.post(
            "/architecture/views",
            json={"title": "private view", "viewpoint": "layered", "model_id": private["id"]},
            headers=hdrs(ALICE, visibility="user"),
        )
    )
    prev = await seal(test_client, private["id"])
    for path in (
        f"/modeling/models/{private['id']}/exchange",
        f"/modeling/models/{private['id']}/revisions/{prev['revision_id']}/exchange",
        f"/architecture/views/{pv['id']}/render",
        f"/modeling/models/{private['id']}/revisions/{prev['revision_id']}/views/{pv['id']}/render",
    ):
        assert (await test_client.get(path, headers=hdrs(ALICE))).status_code == 200, path
        assert (await test_client.get(path, headers=hdrs(BOB))).status_code == 404, path
    resp = await import_into(test_client, private["id"], NATIVE, subject=BOB)
    assert resp.status_code == 404
    bobs = await import_new(test_client, NATIVE, subject=BOB, visibility="user")
    assert (
        await test_client.get(f"/modeling/models/{bobs['model_id']}", headers=hdrs(ALICE))
    ).status_code == 404
    assert (
        await test_client.get(
            f"/modeling/change-sets/{bobs['change_set']['id']}", headers=hdrs(ALICE)
        )
    ).status_code == 404


# --- rendering ---------------------------------------------------------------------

_FORBIDDEN_TAGS = {"script", "foreignObject", "image", "use", "a", "style", "iframe", "img"}


def assert_svg_inert(svg: str) -> None:
    """No active element, event handler or script/external URL anywhere in the tree."""
    root = ET.fromstring(svg)
    for el in root.iter():
        tag = el.tag.split("}", 1)[-1]
        assert tag not in _FORBIDDEN_TAGS, tag
        for name, value in el.attrib.items():
            local = name.split("}", 1)[-1].lower()
            assert not local.startswith("on"), name
            assert "javascript:" not in value.lower(), (name, value)
            if local == "href":
                assert value.startswith("#"), value


async def test_render_svg_png_headers_and_conditional_get(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    vid = w["view"]["id"]
    resp = await test_client.get(f"/architecture/views/{vid}/render", headers=hdrs())
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert (
        resp.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    )
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-renderer-version"] == ARCHIMATE_RENDERER_VERSION
    tag = resp.headers["etag"]
    assert re.fullmatch(r'"[0-9a-f]{64}"', tag)
    svg = resp.text
    assert svg.startswith("<svg") and sanitize_svg(svg) == svg

    png = await test_client.get(
        f"/architecture/views/{vid}/render", params={"format": "png"}, headers=hdrs()
    )
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert png.headers["etag"] != tag

    cached = await test_client.get(
        f"/architecture/views/{vid}/render", headers={**hdrs(), "If-None-Match": tag}
    )
    assert cached.status_code == 304 and cached.content == b"" and cached.headers["etag"] == tag
    weak = await test_client.get(
        f"/architecture/views/{vid}/render", headers={**hdrs(), "If-None-Match": f'"x", W/{tag}'}
    )
    assert weak.status_code == 304

    report_resp = await test_client.get(
        f"/architecture/views/{vid}/render", params={"report": "true"}, headers=hdrs()
    )
    report, report_etag = await ok(report_resp), report_resp.headers["etag"]
    assert report["svg"] == svg and isinstance(report["diagnostics"], list)
    assert report["renderer_version"] == ARCHIMATE_RENDERER_VERSION
    # report=true is the JSON report whatever the format says (one behaviour with UML).
    png_report = await test_client.get(
        f"/architecture/views/{vid}/render",
        params={"report": "true", "format": "png"},
        headers=hdrs(),
    )
    assert png_report.status_code == 200 and png_report.json()["svg"] == svg
    assert png_report.headers["etag"] == report_etag

    # A draft edit changes the head, so the old ETag no longer matches.
    await test_client.put(
        f"/architecture/elements/{w['actor']['id']}",
        json={"title": "Renamed actor", "element_type": "business_actor"},
        headers=hdrs(),
    )
    fresh = await test_client.get(
        f"/architecture/views/{vid}/render", headers={**hdrs(), "If-None-Match": tag}
    )
    assert fresh.status_code == 200 and "Renamed actor" in fresh.text


async def test_render_sanitizes_hostile_stored_text(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    hostile = '<script>alert(1)</script><img src=x onerror="alert(2)">'
    actor = await ok(await element(test_client, m["id"], "business_actor", title=hostile[:300]))
    v = await ok(await view(test_client, m["id"], title=hostile[:300]))
    await ok(
        await test_client.post(
            f"/architecture/views/{v['id']}/objects",
            json={"concept_ref": actor["id"], "label": hostile},
            headers=hdrs(),
        ),
        201,
    )
    await ok(
        await test_client.post(
            f"/architecture/views/{v['id']}/objects",
            json={
                "kind": "note",
                "text": hostile,
                "style": {"fill_color": "url(javascript:alert(3))", "font_name": "x;}<script>"},
            },
            headers=hdrs(),
        ),
        201,
    )
    resp = await test_client.get(f"/architecture/views/{v['id']}/render", headers=hdrs())
    assert resp.status_code == 200, resp.text
    assert_svg_inert(resp.text)
    assert "&lt;script&gt;" in resp.text  # shown as text, escaped
    assert sanitize_svg(resp.text) == resp.text


async def test_revision_render_is_immutable_and_size_refused(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await populated(test_client)
    mid, vid = w["model"]["id"], w["view"]["id"]
    rev = await seal(test_client, mid)
    path = f"/modeling/models/{mid}/revisions/{rev['revision_id']}/views/{vid}/render"
    before = await test_client.get(path, headers=hdrs())
    assert before.status_code == 200
    draft_before = await test_client.get(f"/architecture/views/{vid}/render", headers=hdrs())
    assert draft_before.text == before.text  # same content as the draft it sealed

    await test_client.put(
        f"/architecture/elements/{w['actor']['id']}",
        json={"title": "Postseal", "element_type": "business_actor"},
        headers=hdrs(),
    )
    after = await test_client.get(path, headers=hdrs())
    assert after.content == before.content and after.headers["etag"] == before.headers["etag"]
    draft = await test_client.get(f"/architecture/views/{vid}/render", headers=hdrs())
    assert "Postseal" in draft.text and "Postseal" not in after.text
    assert (
        await test_client.get(path, headers={**hdrs(), "If-None-Match": before.headers["etag"]})
    ).status_code == 304

    missing = await test_client.get(
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/views/{w['actor']['id']}/render",
        headers=hdrs(),
    )
    assert missing.status_code == 404

    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 2)
    for p in (path, f"/architecture/views/{vid}/render"):
        resp = await test_client.get(p, headers=hdrs())
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"


async def test_staging_is_pure_and_deterministic() -> None:
    parsed = import_exchange(NATIVE)
    empty: dict[str, Any] = {"concepts": [], "relationships": [], "views": []}
    a = xsvc.stage(parsed, empty)
    b = xsvc.stage(import_exchange(NATIVE), empty)
    assert [op.model_dump() for op in a.ops] == [op.model_dump() for op in b.ops]
    kinds = [op.kind for op in a.ops]
    order = [
        "model_exchange",  # first: views may name its custom viewpoints
        "element",
        "relationship",
        "view",
        "view_object",
        "view_connection",
        "organization",
    ]
    assert kinds == sorted(kinds, key=order.index)
    assert json.dumps([f.model_dump(mode="json") for f in a.findings]) == json.dumps(
        [f.model_dump(mode="json") for f in b.findings]
    )


@pytest.mark.parametrize(
    "fixture", ["archi510_native_export.xml", "archi_bendpoint.test.xml", "archisurance.xml"]
)
async def test_real_archi_files_round_trip_to_a_fixed_point(
    test_client: AsyncClient, fixture: str
) -> None:
    data = (FIXTURES / fixture).read_bytes()
    out = await import_new(test_client, data)
    assert out["change_set"]["status"] == "proposed", out["change_set"]["blocking_findings"]
    mid = out["model_id"]
    await apply(test_client, out["change_set"]["id"])
    exported = (await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())).content
    again = await ok(await import_into(test_client, mid, exported), 201)
    assert again["change_set"]["ops"] == []
    original = await ok(await import_into(test_client, mid, data), 201)
    assert original["change_set"]["ops"] == []


async def test_raw_props_reads_only_the_models_own_tenants_rows(
    test_client: AsyncClient, test_session_factory
) -> None:
    """``_raw_props`` feeds import staging: a foreign row carrying the id is not content.

    ``model_id`` alone would hand the importer a foreign tenant's stored properties, which
    staging could then apply back into the model.
    """
    m = await new_model(test_client)
    mid = m["id"]
    own = await ok(await element(test_client, mid, properties={"owner": "own"}))
    async with test_session_factory() as s:
        foreign = ArchitectureElement(
            slug=f"probe-{uuid.uuid4().hex[:12]}",
            title="foreign row",
            layer="business",
            element_type="business_actor",
            tenant_id=OTHER_TENANT,
            tenant_name="exchange-other",
            subject_id=BOB,
            visibility="tenant",
            properties={"owner": "foreign"},
            model_id=uuid.UUID(mid),
        )
        s.add(foreign)
        await s.commit()
        foreign_id = str(foreign.id)

    async with test_session_factory() as s:
        props = await xsvc._raw_props(s, uuid.UUID(mid), TENANT)
    assert props[str(own["id"])] == {"owner": "own"}  # non-vacuous
    assert foreign_id not in props


async def test_no_connection_is_checked_out_while_rendering(
    test_client: AsyncClient, test_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import model_render as rsvc

    w = await populated(test_client)
    rev = await seal(test_client, w["model"]["id"])
    seen: list[int] = []
    real = rsvc.render_view

    def spy(manifest: dict[str, Any], view_id: str) -> Any:
        seen.append(test_engine.sync_engine.pool.checkedout())
        return real(manifest, view_id)

    monkeypatch.setattr(rsvc, "render_view", spy)
    # The engine may already hold connections for fixtures of this worker; rendering
    # must not add one (the request's own session is released before the render).
    baseline = test_engine.sync_engine.pool.checkedout()
    for path in (
        f"/architecture/views/{w['view']['id']}/render",
        f"/modeling/models/{w['model']['id']}/revisions/{rev['revision_id']}"
        f"/views/{w['view']['id']}/render?format=png",
    ):
        assert (await test_client.get(path, headers=hdrs())).status_code == 200
    assert seen == [baseline, baseline]


async def test_model_properties_viewpoints_and_view_refs_survive_the_database(
    test_client: AsyncClient,
) -> None:
    from tests.test_archimate_exchange_archi import FIDELITY_DOC

    out = await import_new(test_client, FIDELITY_DOC)
    cs = out["change_set"]
    assert cs["status"] == "proposed", cs["blocking_findings"]
    assert cs["ops"][0]["kind"] == "model_exchange"
    assert not loss_codes(out) & {"MODEL-PROPERTIES", "MODEL-METADATA", "VIEWPOINT-DEFINITION"}
    assert not loss_codes(out) & {"VIEWPOINT-REF", "VIEW-REF"}
    mid = out["model_id"]
    applied = await apply(test_client, cs["id"])
    main, detail = applied["id_map"]["v-main"], applied["id_map"]["v-detail"]

    stored = await test_client.get(f"/architecture/views/{main}/objects", headers=hdrs())
    assert [o["view_ref"] for o in stored.json()] == [detail]

    exported = await test_client.get(f"/modeling/models/{mid}/exchange", headers=hdrs())
    assert exported.status_code == 200
    root = ET.fromstring(exported.content)
    ns = {"a": "http://www.opengroup.org/xsd/archimate/3.0/"}
    assert root.findtext("a:properties/a:property/a:value", namespaces=ns) == "EA team"
    assert root.findtext("a:metadata/a:schema", namespaces=ns) == "Dublin Core"
    (vp,) = root.findall("a:views/a:viewpoints/a:viewpoint", ns)
    # Row ids (UUIDs, not NCNames) are exported with the ``id-`` prefix.
    view_el = next(
        v
        for v in root.findall("a:views/a:diagrams/a:view", ns)
        if v.get("identifier") == f"id-{main}"
    )
    assert view_el.get("viewpointRef") == vp.get("identifier") == "vp-1"
    view_ref = view_el.find("a:node/a:viewRef", ns)
    assert view_ref is not None and view_ref.get("ref") == f"id-{detail}"

    # Semantics survive the database: export -> import, and the original file, are no-ops.
    assert import_exchange(exported.content).losses == []
    again = await ok(await import_into(test_client, mid, exported.content), 201)
    assert again["change_set"]["ops"] == []
    original = await ok(await import_into(test_client, mid, FIDELITY_DOC), 201)
    assert original["change_set"]["ops"] == []

    # A sealed revision carries the same content.
    rev = await seal(test_client, mid)
    rev_export = await test_client.get(
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/exchange", headers=hdrs()
    )
    assert rev_export.content == exported.content
