"""PDF renders, whole-model PDF / SVG-zip exports, UML XMI and the ArchiMate-only
relationship list — over HTTP, against real Postgres, through the real routes.

* Every render route answers ``format=pdf`` with a vector PDF attachment.
* ``/modeling/models/{id}/export``: an ArchiMate model with 2 views is a 2-page PDF (one
  outline entry per view, named after it) and a zip of 2 SVGs byte-equal to the
  per-view render route's SVG; the sealed revision exports the same way.
* A UML model with 1 class diagram: 1-page PDF, 1-SVG zip, and XMI 2.5.1 that parses and
  carries every element, relationship and the diagram's membership; an ArchiMate model's
  XMI is ``409``. An empty model's export is ``404 no_diagrams``.
* ``GET /architecture/relationships`` lists ArchiMate edges only by default; a governance
  (requirement → requirement) edge is listed when its type is named.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from typing import Any

import pytest
from httpx import AsyncClient
from lxml import etree
from pypdf import PdfReader

from tests.test_modeling_lifecycle import element, hdrs, new_model, ok, populated, view
from tests.test_uml_rules import base
from tests.test_uml_storage import load_example, seal, uml_model
from tools.uml.xmi import UML_NS, XMI_NS, xid

pytestmark = pytest.mark.asyncio

XMI_ID = f"{{{XMI_NS}}}id"
XMI_TYPE = f"{{{XMI_NS}}}type"


def pdf_pages(resp) -> PdfReader:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    return PdfReader(io.BytesIO(resp.content))


def zip_svgs(resp) -> dict[str, str]:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return {name: zf.read(name).decode("utf-8") for name in zf.namelist()}


async def archimate_two_views(client: AsyncClient) -> dict[str, Any]:
    w = await populated(client)
    mid = w["model"]["id"]
    second = await ok(await view(client, mid, title="Second view"))
    await ok(
        await client.post(
            f"/architecture/views/{second['id']}/objects",
            json={"concept_ref": w["actor"]["id"]},
            headers=hdrs(),
        ),
        201,
    )
    return {**w, "views": [w["view"], second]}


async def test_render_routes_answer_pdf(test_client: AsyncClient) -> None:
    w = await populated(test_client)
    vid, mid = w["view"]["id"], w["model"]["id"]
    resp = await test_client.get(
        f"/architecture/views/{vid}/render", params={"format": "pdf"}, headers=hdrs()
    )
    assert len(pdf_pages(resp).pages) == 1
    assert resp.headers["content-disposition"] == f'attachment; filename="view-{vid}.pdf"'
    assert resp.headers["x-content-type-options"] == "nosniff"
    svg = await test_client.get(f"/architecture/views/{vid}/render", headers=hdrs())
    assert resp.headers["etag"] != svg.headers["etag"]  # the representation is in the ETag
    again = await test_client.get(
        f"/architecture/views/{vid}/render",
        params={"format": "pdf"},
        headers={**hdrs(), "If-None-Match": resp.headers["etag"]},
    )
    assert again.status_code == 304

    rev = await seal(test_client, mid)
    sealed = await test_client.get(
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/views/{vid}/render",
        params={"format": "pdf"},
        headers=hdrs(),
    )
    assert len(pdf_pages(sealed).pages) == 1

    m = await uml_model(test_client)
    ids = await load_example(test_client, m["id"])
    did = ids["d_class"]
    uml = await test_client.get(
        f"/uml/diagrams/{did}/render", params={"format": "pdf"}, headers=hdrs()
    )
    assert len(pdf_pages(uml).pages) == 1
    assert uml.headers["content-disposition"] == f'attachment; filename="diagram-{did}.pdf"'
    urev = await seal(test_client, m["id"])
    usealed = await test_client.get(
        f"/modeling/models/{m['id']}/revisions/{urev['revision_id']}/diagrams/{did}/render",
        params={"format": "pdf"},
        headers=hdrs(),
    )
    assert len(pdf_pages(usealed).pages) == 1


async def test_archimate_model_exports_one_page_and_one_svg_per_view(
    test_client: AsyncClient,
) -> None:
    w = await archimate_two_views(test_client)
    mid = w["model"]["id"]
    names = sorted(w["views"], key=lambda v: v["id"])  # manifest order is by id

    doc = pdf_pages(
        await test_client.get(
            f"/modeling/models/{mid}/export", params={"format": "pdf"}, headers=hdrs()
        )
    )
    assert len(doc.pages) == 2
    assert [o.title for o in doc.outline] == [v["title"] for v in names]
    assert doc.metadata is not None and doc.metadata.title == w["model"]["title"]
    for page, v in zip(doc.pages, names, strict=True):
        assert v["title"] in page.extract_text()

    svgs = zip_svgs(
        await test_client.get(
            f"/modeling/models/{mid}/export", params={"format": "svg"}, headers=hdrs()
        )
    )
    assert len(svgs) == 2 and all(n.endswith(".svg") for n in svgs)
    rendered = {
        (await test_client.get(f"/architecture/views/{v['id']}/render", headers=hdrs())).text
        for v in w["views"]
    }
    assert set(svgs.values()) == rendered

    rev = await seal(test_client, mid)
    sealed = pdf_pages(
        await test_client.get(
            f"/modeling/models/{mid}/revisions/{rev['revision_id']}/export", headers=hdrs()
        )
    )
    assert len(sealed.pages) == 2
    sealed_svgs = zip_svgs(
        await test_client.get(
            f"/modeling/models/{mid}/revisions/{rev['revision_id']}/export",
            params={"format": "svg"},
            headers=hdrs(),
        )
    )
    assert sealed_svgs == svgs

    xmi = await test_client.get(f"/modeling/models/{mid}/xmi", headers=hdrs())
    assert xmi.status_code == 409, xmi.text
    assert xmi.json()["error"]["code"] == "xmi_unsupported_language"
    assert "/exchange" in xmi.json()["error"]["message"]


async def test_empty_model_export_is_404_no_diagrams(test_client: AsyncClient) -> None:
    m = await new_model(test_client)
    await ok(await element(test_client, m["id"], "business_actor"))
    for fmt in ("pdf", "svg"):
        resp = await test_client.get(
            f"/modeling/models/{m['id']}/export", params={"format": fmt}, headers=hdrs()
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "no_diagrams"
    other = await test_client.get(f"/modeling/models/{uuid.uuid4()}/export", headers=hdrs())
    assert other.status_code == 404


def _one_class_diagram() -> dict[str, Any]:
    snap = base()
    snap["diagrams"] = [d for d in snap["diagrams"] if d["id"] == "d_class"]
    return snap


async def test_uml_model_exports_pdf_svg_zip_and_xmi(test_client: AsyncClient) -> None:
    m = await uml_model(test_client, title="Orders UML")
    mid = m["id"]
    snap = _one_class_diagram()
    ids = await load_example(test_client, mid, snap)

    doc = pdf_pages(await test_client.get(f"/modeling/models/{mid}/export", headers=hdrs()))
    assert len(doc.pages) == 1
    assert "OrderService" in doc.pages[0].extract_text()
    svgs = zip_svgs(
        await test_client.get(
            f"/modeling/models/{mid}/export", params={"format": "svg"}, headers=hdrs()
        )
    )
    assert list(svgs.values()) == [
        (await test_client.get(f"/uml/diagrams/{ids['d_class']}/render", headers=hdrs())).text
    ]

    resp = await test_client.get(f"/modeling/models/{mid}/xmi", headers=hdrs())
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/xml")
    assert resp.headers["content-disposition"].endswith('.xmi"')
    root = etree.fromstring(resp.content)
    assert root.tag == f"{{{XMI_NS}}}XMI"
    model = root.find(f"{{{UML_NS}}}Model")
    assert model is not None and model.get("name") == "Orders UML"
    by_id = {n.get(XMI_ID): n for n in root.iter() if n.get(XMI_ID)}
    # Every stored element and relationship is in the document, as its UML metaclass.
    types = {
        "package": "uml:Package",
        "class": "uml:Class",
        "interface": "uml:Interface",
        "data_type": "uml:DataType",
        "enumeration": "uml:Enumeration",
        "primitive_type": "uml:PrimitiveType",
        "component": "uml:Component",
        "port": "uml:Port",
        "actor": "uml:Actor",
    }
    for e in snap["elements"]:
        node = by_id[xid(ids[e["id"]])]
        assert node.get(XMI_TYPE) == types[e["kind"]] and node.get("name") == e["name"]
    rel_types = {
        "generalization": "uml:Generalization",
        "association": "uml:Association",
        "interface_realization": "uml:InterfaceRealization",
        "realization": "uml:Realization",
        "dependency": "uml:Dependency",
        "usage": "uml:Usage",
        "connector": "uml:Connector",
    }
    for r in snap["relationships"]:
        assert by_id[xid(ids[r["id"]])].get(XMI_TYPE) == rel_types[r["kind"]]
    gen = by_id[xid(ids["gen1"])]
    assert gen.getparent() is by_id[xid(ids["cls_svc"])]
    assert gen.get("general") == xid(ids["cls_base"])
    # A port is an attribute of its component; an interaction is in the document.
    assert by_id[xid(ids["port_api"])].getparent() is by_id[xid(ids["comp_api"])]
    assert by_id[xid(ids["int1"])].get(XMI_TYPE) == "uml:Interaction"
    # The class diagram's membership, as idrefs in the extension.
    ext = root.find(f"{{{XMI_NS}}}Extension")
    assert ext is not None and ext.get("extender") == "requirements-api"
    diagram = ext.find("diagram")
    assert diagram is not None and diagram.get("id") == xid(ids["d_class"])
    shown = {n.get("ref") for n in diagram.findall("element")}
    expected = {xid(ids[o["element_ref"]]) for o in snap["diagrams"][0]["objects"]}
    assert shown == expected and len(shown) == 3

    rev = await seal(test_client, mid)
    sealed = await test_client.get(
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}/xmi", headers=hdrs()
    )
    assert sealed.status_code == 200 and sealed.content == resp.content


async def test_relationship_list_is_archimate_only_unless_a_type_is_named(
    test_client: AsyncClient,
) -> None:
    w = await populated(test_client)
    dep = await ok(
        await test_client.post(
            "/architecture/relationships",
            json={
                "source_entity_type": "requirements",
                "source_id": str(uuid.uuid4()),
                "target_entity_type": "requirements",
                "target_id": str(uuid.uuid4()),
                "relationship_type": "traceability",
                "name": "traces to",
            },
            headers=hdrs(),
        ),
    )

    listed = await ok(
        await test_client.get("/architecture/relationships", params={"limit": 1000}, headers=hdrs())
    )
    listed_ids = {r["id"] for r in listed}
    assert w["rel"]["id"] in listed_ids
    assert dep["id"] not in listed_ids
    assert all(
        {r["source_entity_type"], r["target_entity_type"]}
        <= {"architecture_elements", "architecture_relationships"}
        for r in listed
    )

    named = await ok(
        await test_client.get(
            "/architecture/relationships",
            params={"source_entity_type": "requirements", "limit": 1000},
            headers=hdrs(),
        )
    )
    assert dep["id"] in {r["id"] for r in named}


async def test_xml_illegal_characters_in_stored_strings_do_not_break_exports(
    test_client: AsyncClient,
) -> None:
    """Writes accept control characters XML 1.0 cannot carry (``\\x0b``, ``\\x01``); every
    export drops them instead of 500ing — above all on a sealed revision, which cannot be
    edited to remove them."""
    # ArchiMate: a view title, an element name and its documentation.
    w = await populated(test_client)
    mid = w["model"]["id"]
    actor = await ok(
        await element(
            test_client, mid, "business_actor", title="bad\x01actor", description="doc\x0bhere"
        )
    )
    bad_view = await ok(await view(test_client, mid, title="bad\x0btitle"))
    await ok(
        await test_client.post(
            f"/architecture/views/{bad_view['id']}/objects",
            json={"concept_ref": actor["id"]},
            headers=hdrs(),
        ),
        201,
    )
    rev = await seal(test_client, mid)
    for base_path in (
        f"/modeling/models/{mid}",
        f"/modeling/models/{mid}/revisions/{rev['revision_id']}",
    ):
        doc = pdf_pages(await test_client.get(f"{base_path}/export", headers=hdrs()))
        assert len(doc.pages) == 2
        assert any("badtitle" in page.extract_text() for page in doc.pages)
        svgs = zip_svgs(
            await test_client.get(f"{base_path}/export", params={"format": "svg"}, headers=hdrs())
        )
        assert any("badactor" in s for s in svgs.values())
        exchange = await test_client.get(f"{base_path}/exchange", headers=hdrs())
        assert exchange.status_code == 200, exchange.text
        assert b"badactor" in exchange.content and b"\x01" not in exchange.content
        etree.fromstring(exchange.content)

    # UML: a model name, its documentation and an element name with \x01 / \x0b.
    m = await uml_model(test_client, title="bad\x01model", description="doc\x0bline")
    umid = m["id"]
    snap = _one_class_diagram()
    snap["elements"][0]["name"] = "Bad\x0bName"
    await load_example(test_client, umid, snap)
    urev = await seal(test_client, umid)
    for base_path in (
        f"/modeling/models/{umid}",
        f"/modeling/models/{umid}/revisions/{urev['revision_id']}",
    ):
        doc = pdf_pages(await test_client.get(f"{base_path}/export", headers=hdrs()))
        assert len(doc.pages) == 1
        zip_svgs(
            await test_client.get(f"{base_path}/export", params={"format": "svg"}, headers=hdrs())
        )
        xmi = await test_client.get(f"{base_path}/xmi", headers=hdrs())
        assert xmi.status_code == 200, xmi.text
        root = etree.fromstring(xmi.content)
        model = root.find(f"{{{UML_NS}}}Model")
        assert model is not None and model.get("name") == "badmodel"
        assert "BadName" in {n.get("name") for n in root.iter()}
        body = model.find("ownedComment/body")
        assert body is not None and body.text == "docline"
