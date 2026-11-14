"""Open Group exchange format import/export (tools/architecture/exchange.py).

Pure module: no routes, no DB. Covers XSD validity of export for every registry
concept and relationship type, semantic + byte round trips, published
third-party files, hostile input, and loss reporting in both directions.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import pytest
from lxml import etree

from schemas.modeling import (
    ArchimateConceptIn,
    ArchimateOrganization,
    ArchimateRelationshipIn,
    ArchimateSnapshot,
    ArchimateViewConnectionIn,
    ArchimateViewIn,
    ArchimateViewObjectIn,
    Bounds,
    Point,
)
from tools.architecture.archimate_rules import validate
from tools.architecture.exchange import (
    NS,
    XSD_DIR,
    ExchangeError,
    export_exchange,
    export_exchange_report,
    import_exchange,
    schema_errors,
)
from tools.architecture.registry import get_registry

FIXTURES = Path(__file__).parent / "fixtures" / "exchange"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

FIXTURE_SHA256 = {
    "archisurance.xml": "7ffab113951fa8c08200d898b228dff5e4dcdf7e3b82b9e6e50ba8a4d12b9fc1",
    "archi_bendpoint.test.xml": "c2b1e74b44089c84ca054fc2c3dd9a9789e2e7fbc33990aae9654632c08bc95f",
    "opengroup_Model_View.xml": "cf4af23751519efd8994449d8d8a470bfdaa75cddefd5e969e1caf94e677cdf7",
    "opengroup_Basic_Model_Organization.xml": "3823cbeb9d76c1b3d4d8830870b0cfd1891f5d407c0adb567405e86ed7f05045",
    "opengroup_Basic_Model_Properties.xml": "fa3540878b8798f0dfe604a9ab19200007ea2ba5eef47f9c5c3d73d95669ef21",
    "opengroup_Basic_Model_Metadata.xml": "75c9dd683c2ce3852dae7f774853d9b0a54258d7179b7a9fbfdca126f59c2358",
}
XSD_SHA256 = {
    "archimate3_Model.xsd": "01686f6905f5f6b8bb1cff0373949c3be96a250c4e60314f6acb7f32db3d446f",
    "archimate3_View.xsd": "d708ce176403034b1229b892712cfd69660aefe17da4cc54acea1ac35e4a9854",
    "archimate3_Diagram.xsd": "6419080f4c4bc43b4a7b8acf870146a7bae6c3487a3ce08d3c521c028ea6056e",
    "dc.xsd": "30e25f92f27bf8832120285db14b85f9b33fc8f8e2f6758a37a6389055fa9997",
    "xml.xsd": "61960fb3131e38022caad5360e2f33a3382578ab3c80cd58bd74320ede61b20c",
}


def _concrete_types() -> list[str]:
    return [t for t in get_registry().concepts if t != "junction"]


def _pair_for(rel: str) -> tuple[str, str]:
    reg = get_registry()
    for s in _concrete_types():
        for t in _concrete_types():
            if reg.is_valid(s, rel, t):
                return s, t
    raise AssertionError(f"no concrete pair allows {rel}")


def full_snapshot() -> ArchimateSnapshot:
    """Every registry concept and relationship type, plus every special construct."""
    reg = get_registry()
    concepts = [
        ArchimateConceptIn(
            id=f"c-{t}",
            type=t,
            name=f"{t} One",
            documentation=f"doc of {t}\n  preserved  whitespace",
            properties=[("owner", "team-a"), ("cost", "10"), ("owner", "team-b")]
            if t == "business_actor"
            else None,
        )
        for t in _concrete_types()
    ]
    concepts.append(ArchimateConceptIn(id="j-and", type="junction", junction_type="and"))
    concepts.append(ArchimateConceptIn(id="j-or", type="junction", junction_type="or"))
    rels: list[ArchimateRelationshipIn] = []
    for rt in reg.relationship_types:
        s, t = _pair_for(rt)
        rels.append(
            ArchimateRelationshipIn(
                id=f"r-{rt}",
                type=rt,
                source=f"c-{s}",
                target=f"c-{t}",
                name=f"{rt} rel",
                access_type="write" if rt == "access" else None,
                strength="++" if rt == "influence" else None,
                directed=True if rt == "association" else None,
                documentation=f"{rt} documentation",
                properties=[("weight", "3")] if rt == "serving" else None,
            )
        )
    s, t = _pair_for("access")
    for mode in ("read", "access", "readwrite"):
        rels.append(
            ArchimateRelationshipIn(
                id=f"r-access-{mode}",
                type="access",
                source=f"c-{s}",
                target=f"c-{t}",
                access_type=mode,
            )
        )
    s, t = _pair_for("association")
    rels.append(
        ArchimateRelationshipIn(
            id="r-assoc-undirected",
            type="association",
            source=f"c-{s}",
            target=f"c-{t}",
            directed=False,
        )
    )
    s, t = _pair_for("influence")
    rels.append(
        ArchimateRelationshipIn(
            id="r-influence-5", type="influence", source=f"c-{s}", target=f"c-{t}", strength="5"
        )
    )
    # Junctions: process -triggering-> AND -triggering-> process; OR likewise.
    rels += [
        ArchimateRelationshipIn(
            id="r-j1", type="triggering", source="c-business_process", target="j-and"
        ),
        ArchimateRelationshipIn(
            id="r-j2", type="triggering", source="j-and", target="c-business_event"
        ),
        ArchimateRelationshipIn(
            id="r-j3", type="flow", source="c-business_function", target="j-or"
        ),
        ArchimateRelationshipIn(id="r-j4", type="flow", source="j-or", target="c-business_process"),
    ]
    # Relationship as an endpoint: association to a relationship, aggregation from grouping.
    rels.append(
        ArchimateRelationshipIn(
            id="r-to-rel", type="association", source="c-business_actor", target="r-serving"
        )
    )
    rels.append(
        ArchimateRelationshipIn(
            id="r-grp-rel", type="aggregation", source="c-grouping", target="r-serving"
        )
    )
    srv = next(r for r in rels if r.id == "r-serving")
    view = ArchimateViewIn(
        id="v-1",
        name="Everything",
        viewpoint="layered",
        documentation="the main view",
        properties=[("status", "draft")],
        objects=[
            ArchimateViewObjectIn(
                id="o-grp",
                concept_id="c-grouping",
                bounds=Bounds(x=10, y=20, w=400, h=300),
                style={
                    "fill_color": "#ffcc00",
                    "fill_alpha": 80,
                    "line_color": "#000000",
                    "line_width": 2,
                    "font_name": "Segoe UI",
                    "font_size": 9.5,
                    "font_style": ["bold", "italic"],
                    "font_color": "#112233",
                },
            ),
            ArchimateViewObjectIn(
                id="o-actor-nested",
                concept_id="c-business_actor",
                parent_object_id="o-grp",
                bounds=Bounds(x=30, y=40, w=120, h=55),
            ),
            ArchimateViewObjectIn(
                id="o-actor-again",
                concept_id="c-business_actor",
                bounds=Bounds(x=500, y=20, w=120, h=55),
            ),
            ArchimateViewObjectIn(
                id="o-src", concept_id=srv.source, bounds=Bounds(x=500, y=200, w=120, h=55)
            ),
            ArchimateViewObjectIn(
                id="o-tgt",
                concept_id=srv.target,
                bounds=Bounds(x=700, y=200, w=120, h=55),
                label="${name} (${type})",
            ),
            # Visual-only objects: a group holding a note and an element occurrence.
            ArchimateViewObjectIn(
                id="o-vgroup",
                kind="group",
                name="Visual group",
                border_type="rectangle",
                documentation="what the group means",
                bounds=Bounds(x=900, y=20, w=300, h=220),
                style={"fill_color": "#e1e1e1"},
            ),
            ArchimateViewObjectIn(
                id="o-note",
                kind="note",
                text="A note\nsecond line",
                border_type="dogear",
                documentation="note doc",
                parent_object_id="o-vgroup",
                bounds=Bounds(x=10, y=30, w=150, h=60),
            ),
            ArchimateViewObjectIn(
                id="o-role-in-group",
                concept_id="c-business_role",
                parent_object_id="o-vgroup",
                bounds=Bounds(x=10, y=120, w=120, h=55),
            ),
        ],
        connections=[
            ArchimateViewConnectionIn(
                id="cn-serving",
                relationship_id="r-serving",
                source_object_id="o-src",
                target_object_id="o-tgt",
                bendpoints=[Point(x=600, y=150), Point(x=650, y=150)],
                style={"line_color": "#ff0000", "line_width": 3},
                label="serves",
            ),
            # Plain line (no relationship) from the note to the group's element.
            ArchimateViewConnectionIn(
                id="cn-line",
                kind="line",
                source_object_id="o-note",
                target_object_id="o-role-in-group",
                label="see role",
                style={"line_color": "#0000ff"},
            ),
            # Connection to a connection (relationship-as-endpoint occurrence).
            ArchimateViewConnectionIn(
                id="cn-to-conn",
                relationship_id="r-to-rel",
                source_object_id="o-actor-again",
                target_object_id="cn-serving",
            ),
        ],
    )
    empty = ArchimateViewIn(id="v-2", name="Empty view")
    return ArchimateSnapshot(concepts=concepts, relationships=rels, views=[view, empty])


def _root(data: bytes) -> etree._Element:
    return etree.fromstring(data)


# ---------------------------------------------------------------------------
# Pinned data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,digest", sorted(XSD_SHA256.items()))
def test_vendored_xsd_is_pinned(name: str, digest: str) -> None:
    assert hashlib.sha256((XSD_DIR / name).read_bytes()).hexdigest() == digest


def test_vendored_xsds_never_reference_the_network() -> None:
    locations = [
        (xsd.name, el.get("schemaLocation"))
        for xsd in XSD_DIR.glob("*.xsd")
        for el in etree.parse(str(xsd)).iter("{http://www.w3.org/2001/XMLSchema}*")
        if el.get("schemaLocation") is not None
    ]
    assert locations, "no schemaLocation examined: the check would be vacuous"
    remote = [(name, loc) for name, loc in locations if "://" in str(loc)]
    assert not remote, f"vendored XSDs import over the network: {remote}"


@pytest.mark.parametrize("name,digest", sorted(FIXTURE_SHA256.items()))
def test_fixture_is_pinned(name: str, digest: str) -> None:
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_covers_every_registry_type_and_is_xsd_valid() -> None:
    snap = full_snapshot()
    res = export_exchange_report(snap, model_id="m-1", name="Full", documentation="all of it")
    assert schema_errors(res.data) == []
    assert res.losses == []
    root = _root(res.data)
    el_types = {e.get(f"{{{XSI}}}type") for e in root.iter(f"{{{NS}}}element")}
    reg = get_registry()
    expected = {c.archi_class for c in reg.concepts.values() if c.type != "junction"}
    assert el_types == expected | {"AndJunction", "OrJunction"}
    rel_types = {e.get(f"{{{XSI}}}type") for e in root.iter(f"{{{NS}}}relationship")}
    assert rel_types == {t.capitalize() for t in reg.relationship_types}
    assert len(reg.relationship_types) == 11 and len(expected) == 60


def test_export_attributes_and_structures() -> None:
    root = _root(export_exchange(full_snapshot(), model_id="m-1", name="Full"))
    rels = {e.get("identifier"): e for e in root.iter(f"{{{NS}}}relationship")}
    assert rels["r-access"].get("accessType") == "Write"
    assert rels["r-access-read"].get("accessType") == "Read"
    assert rels["r-access-readwrite"].get("accessType") == "ReadWrite"
    assert rels["r-access-access"].get("accessType") == "Access"
    assert rels["r-influence"].get("modifier") == "++"
    assert rels["r-association"].get("isDirected") == "true"
    assert rels["r-assoc-undirected"].get("isDirected") == "false"
    assert rels["r-to-rel"].get("target") == "r-serving"  # relationship as endpoint
    # Property definitions: one per distinct key, values in order, repeats kept.
    pdefs = {
        d.get("identifier"): d.findtext(f"{{{NS}}}name")
        for d in root.iter(f"{{{NS}}}propertyDefinition")
    }
    assert sorted(pdefs.values()) == ["archi:borderType", "cost", "owner", "status", "weight"]
    actor = next(
        e for e in root.iter(f"{{{NS}}}element") if e.get("identifier") == "c-business_actor"
    )
    vals = [
        (pdefs[p.get("propertyDefinitionRef")], p.findtext(f"{{{NS}}}value"))
        for p in actor.iter(f"{{{NS}}}property")
    ]
    assert vals == [("owner", "team-a"), ("cost", "10"), ("owner", "team-b")]
    # Organizations: Archi's default folders.
    labels = [i.findtext(f"{{{NS}}}label") for i in root.find(f"{{{NS}}}organizations")]
    assert labels == [
        "Strategy",
        "Business",
        "Application",
        "Technology & Physical",
        "Motivation",
        "Implementation & Migration",
        "Other",
        "Relations",
        "Views",
    ]
    # Nested node: absolute coordinates in the exchange file.
    nodes = {n.get("identifier"): n for n in root.iter(f"{{{NS}}}node")}
    nested = nodes["o-actor-nested"]
    assert nested.getparent() is nodes["o-grp"]
    assert (nested.get("x"), nested.get("y")) == ("40", "60")
    view = next(v for v in root.iter(f"{{{NS}}}view") if v.get("identifier") == "v-1")
    assert view.get("viewpoint") == "Layered"
    conns = {c.get("identifier"): c for c in root.iter(f"{{{NS}}}connection")}
    assert conns["cn-to-conn"].get("target") == "cn-serving"
    assert [(b.get("x"), b.get("y")) for b in conns["cn-serving"].iter(f"{{{NS}}}bendpoint")] == [
        ("600", "150"),
        ("650", "150"),
    ]
    style = nodes["o-grp"].find(f"{{{NS}}}style")
    assert style.get("lineWidth") == "2"
    fill = style.find(f"{{{NS}}}fillColor")
    assert (fill.get("r"), fill.get("g"), fill.get("b"), fill.get("a")) == ("255", "204", "0", "80")
    font = style.find(f"{{{NS}}}font")
    assert (font.get("name"), font.get("size"), font.get("style")) == (
        "Segoe UI",
        "9.5",
        "bold italic",
    )


def test_export_is_deterministic() -> None:
    a = export_exchange(full_snapshot(), model_id="m-1", name="Full")
    b = export_exchange(full_snapshot(), model_id="m-1", name="Full")
    assert a == b


def test_round_trip_is_semantically_equal_and_byte_stable() -> None:
    snap = full_snapshot()
    first = export_exchange(snap, model_id="m-1", name="Full", documentation="d")
    imported = import_exchange(first)
    assert imported.schema_valid
    assert imported.losses == []
    assert imported.model_id == "m-1"
    assert imported.model_name == "Full"
    assert imported.model_documentation == "d"
    assert imported.snapshot == snap
    second = export_exchange(imported.snapshot, model_id="m-1", name="Full", documentation="d")
    assert second == first


def test_export_reports_what_it_cannot_carry() -> None:
    snap = ArchimateSnapshot(
        concepts=[
            ArchimateConceptIn(id="1abc", type="business_actor", name="A", parent_id="p"),
            ArchimateConceptIn(id="p", type="business_role", name="P"),
            ArchimateConceptIn(id="weird", type="not_a_type", name="W"),
        ],
        relationships=[
            ArchimateRelationshipIn(id="r1", type="assignment", source="1abc", target="p"),
            ArchimateRelationshipIn(id="r2", type="association", source="weird", target="p"),
            ArchimateRelationshipIn(id="r3", type="traces_to", source="1abc", target="p"),
        ],
        views=[
            ArchimateViewIn(
                id="v",
                name="V",
                viewpoint="landscape",
                objects=[
                    ArchimateViewObjectIn(id="o1", concept_id="1abc", style={"notation": "icon"}),
                    ArchimateViewObjectIn(id="o2", concept_id="weird", bounds=Bounds(w=10, h=10)),
                    ArchimateViewObjectIn(
                        id="o3",
                        concept_id="p",
                        parent_object_id="o2",
                        bounds=Bounds(x=-5, y=1, w=10, h=10),
                    ),
                ],
            )
        ],
    )
    res = export_exchange_report(snap, model_id="m", name="N")
    assert schema_errors(res.data) == []
    codes = Counter(loss.code for loss in res.losses)
    assert codes["ID-REWRITTEN"] == 1 and res.id_map == {"1abc": "id-1abc"}
    assert codes["CONCEPT-PARENT"] == 1
    assert codes["EXPORT-TYPE-UNKNOWN"] == 1
    assert codes["EXPORT-REL-TYPE-UNKNOWN"] == 1
    assert codes["VIEWPOINT-UNKNOWN"] == 1
    assert codes["STYLE-KEY-UNSUPPORTED"] == 1
    assert codes["BOUNDS-MISSING"] == 1 and codes["BOUNDS-DEFAULT-SIZE"] == 1
    assert codes["BOUNDS-CLAMPED"] == 1
    assert codes["OBJECT-REPARENTED"] == 1
    # r2 (end not exported) and o2 (concept not exported) are reported, not silently gone.
    dangling = {loss.id for loss in res.losses if loss.code == "EXPORT-DANGLING"}
    assert dangling == {"r2", "o2"}
    root = _root(res.data)
    assert {e.get("identifier") for e in root.iter(f"{{{NS}}}element")} == {"id-1abc", "p"}


# ---------------------------------------------------------------------------
# Import of published third-party files
# ---------------------------------------------------------------------------


def test_import_archisurance_keeps_every_standard_construct() -> None:
    data = (FIXTURES / "archisurance.xml").read_bytes()
    res = import_exchange(data)
    assert res.schema_valid
    root = _root(data)
    n_el = len(list(root.iter(f"{{{NS}}}element")))
    n_rel = len(list(root.iter(f"{{{NS}}}relationship")))
    element_nodes = [n for n in root.iter(f"{{{NS}}}node") if n.get(f"{{{XSI}}}type") == "Element"]
    rel_conns = [
        c for c in root.iter(f"{{{NS}}}connection") if c.get(f"{{{XSI}}}type") == "Relationship"
    ]
    snap = res.snapshot
    assert (len(snap.concepts), len(snap.relationships)) == (n_el, n_rel) == (216, 254)
    assert len(snap.views or []) == 19
    objects = [o for v in snap.views or [] for o in v.objects]
    kinds = Counter(o.kind for o in objects)
    containers = sum(
        1 for n in root.iter(f"{{{NS}}}node") if n.get(f"{{{XSI}}}type") == "Container"
    )
    labels = sum(1 for n in root.iter(f"{{{NS}}}node") if n.get(f"{{{XSI}}}type") == "Label")
    # Every node is kept: element occurrences, visual groups and notes.
    assert kinds == {"element": len(element_nodes), "group": containers, "note": labels}
    assert (len(element_nodes), containers, labels) == (316, 15, 12)
    assert sum(len(v.connections) for v in snap.views or []) == len(rel_conns) == 216
    reg = get_registry()
    assert all(c.type in reg.concepts for c in snap.concepts)
    assert all(r.type in reg.relationship_types for r in snap.relationships)
    # Every construct is kept, the 12 diagram references (node viewRef) included.
    assert res.losses == []
    view_ids = {v.id for v in res.snapshot.views or []}
    refs = [o.view_ref for v in res.snapshot.views or [] for o in v.objects if o.view_ref]
    assert len(refs) == 12 and set(refs) <= view_ids
    note_text = {o.id: o.text for o in objects if o.kind == "note"}
    assert note_text["id-3657"] == "Application Structure View"
    group = next(o for o in objects if o.id == "id-4096")
    assert (group.name, group.parent_object_id) == ("External Application Services", None)
    # IDs preserved verbatim.
    assert {c.id for c in snap.concepts} == {
        e.get("identifier") for e in root.iter(f"{{{NS}}}element")
    }
    assert all(k == v for k, v in res.id_map.items())
    # Coordinates: nested occurrence relative to its parent occurrence.
    obj = {o.id: o for v in snap.views or [] for o in v.objects}
    nested = next(n for n in element_nodes if n.getparent().get(f"{{{XSI}}}type") == "Element")
    parent = nested.getparent()
    o = obj[nested.get("identifier")]
    assert o.parent_object_id == parent.get("identifier")
    assert o.bounds is not None
    assert o.bounds.x == int(nested.get("x")) - int(parent.get("x"))
    # Re-export is XSD-valid and stable.
    again = export_exchange(snap, model_id=res.model_id, name=res.model_name)
    assert schema_errors(again) == []
    assert import_exchange(again).snapshot == snap


def test_import_is_idempotent() -> None:
    data = (FIXTURES / "archisurance.xml").read_bytes()
    assert import_exchange(data).model_dump() == import_exchange(data).model_dump()


def test_open_group_examples_report_their_losses() -> None:
    org = import_exchange((FIXTURES / "opengroup_Basic_Model_Organization.xml").read_bytes())
    assert org.schema_valid
    assert Counter(loss.code for loss in org.losses) == {"MODEL-VERSION": 1}
    assert org.snapshot.organizations == [
        ArchimateOrganization(id="folder1", name="Business Layer", items=["ba1", "br1"]),
        ArchimateOrganization(id="folder2", name="Relations", items=["relation-1"]),
    ]
    props = import_exchange((FIXTURES / "opengroup_Basic_Model_Properties.xml").read_bytes())
    assert props.snapshot.concepts[0].properties == [("Cost", "4000")]
    assert Counter(loss.code for loss in props.losses) == {"MODEL-VERSION": 1, "PROPERTY-TYPE": 1}
    assert props.snapshot.exchange is not None
    assert props.snapshot.exchange.properties == [("User Property", "A new property value")]
    meta = import_exchange((FIXTURES / "opengroup_Basic_Model_Metadata.xml").read_bytes())
    assert Counter(loss.code for loss in meta.losses) == {"MODEL-VERSION": 1}
    assert meta.snapshot.exchange is not None and meta.snapshot.exchange.metadata is not None
    assert "Basic Model example with Metadata" in meta.snapshot.exchange.metadata
    assert not any(loss.code in ("UNHANDLED", "EXTENSION") for loss in meta.losses)
    view = import_exchange((FIXTURES / "opengroup_Model_View.xml").read_bytes())
    assert view.schema_valid and len(view.snapshot.views or []) == 1
    bend = import_exchange((FIXTURES / "archi_bendpoint.test.xml").read_bytes())
    assert Counter(loss.code for loss in bend.losses) == {"CONNECTION-ATTACHMENT": 4}


# ---------------------------------------------------------------------------
# Hostile input
# ---------------------------------------------------------------------------

_MIN_MODEL = (
    '<model xmlns="http://www.opengroup.org/xsd/archimate/3.0/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" identifier="m">'
    "<name>{body}</name></model>"
)


def test_xxe_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    data = (
        f'<?xml version="1.0"?><!DOCTYPE model [<!ENTITY xxe SYSTEM "file://{secret}">]>'
        + _MIN_MODEL.format(body="&xxe;")
    ).encode()
    with pytest.raises(ExchangeError, match="DOCTYPE"):
        import_exchange(data)


def test_billion_laughs_is_refused() -> None:
    ents = '<!ENTITY lol "lol">' + "".join(
        f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">'
        for i in range(1, 10)
    )
    data = (f"<!DOCTYPE model [{ents}]>" + _MIN_MODEL.format(body="&lol9;")).encode()
    with pytest.raises(ExchangeError):
        import_exchange(data)


def test_plain_doctype_is_refused() -> None:
    with pytest.raises(ExchangeError, match="DOCTYPE"):
        import_exchange(("<!DOCTYPE model>" + _MIN_MODEL.format(body="x")).encode())


def test_oversize_is_refused_before_parsing() -> None:
    data = _MIN_MODEL.format(body="x" * 2000).encode()
    with pytest.raises(ExchangeError, match="limit"):
        import_exchange(data, max_bytes=1000)


def test_malformed_and_foreign_roots_are_refused() -> None:
    with pytest.raises(ExchangeError, match="well-formed"):
        import_exchange(b"<model><unclosed></model>")
    with pytest.raises(ExchangeError, match="root element"):
        import_exchange(b'<model xmlns="urn:other"/>')


# ---------------------------------------------------------------------------
# Losses on import
# ---------------------------------------------------------------------------


def _model(elements: str, relationships: str = "", extra: str = "", ns_extra: str = "") -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<model xmlns="{NS}" xmlns:xsi="{XSI}" {ns_extra} identifier="m">'
        f'<name xml:lang="en">M</name>'
        f"<elements>{elements}</elements>"
        + (f"<relationships>{relationships}</relationships>" if relationships else "")
        + extra
        + "</model>"
    ).encode()


def test_unknown_xsi_type_is_retained_and_reported() -> None:
    data = _model(
        '<element identifier="e1" xsi:type="FancyThing"><name>F</name></element>'
        '<element identifier="e2" xsi:type="BusinessActor"><name>A</name></element>'
    )
    res = import_exchange(data)
    assert not res.schema_valid
    codes = Counter(loss.code for loss in res.losses)
    assert codes["XSD-INVALID"] >= 1 and codes["TYPE-UNKNOWN"] == 1
    kept = {c.id: c.type for c in res.snapshot.concepts}
    assert kept == {"e1": "FancyThing", "e2": "business_actor"}
    assert any(f.rule_id == "AM-TYP-001" and f.targets[0].id == "e1" for f in res.findings)
    with pytest.raises(ExchangeError, match="XSD"):
        import_exchange(data, strict=True)


def test_invalid_relationship_is_retained_with_a_finding() -> None:
    reg = get_registry()
    assert not reg.is_valid("goal", "assignment", "business_actor")
    data = _model(
        '<element identifier="g" xsi:type="Goal"><name>G</name></element>'
        '<element identifier="a" xsi:type="BusinessActor"><name>A</name></element>',
        '<relationship identifier="bad" xsi:type="Assignment" source="g" target="a"/>',
    )
    res = import_exchange(data)
    assert res.schema_valid and res.losses == []
    assert [r.id for r in res.snapshot.relationships] == ["bad"]
    assert any(f.rule_id == "AM-REL-001" and f.targets[0].id == "bad" for f in res.findings)


def test_vendor_extensions_are_reported_explicitly() -> None:
    data = _model(
        '<element identifier="e" xsi:type="BusinessActor" v:color="red">'
        "<name>A</name><v:extra>x</v:extra></element>",
        ns_extra='xmlns:v="urn:vendor"',
    )
    res = import_exchange(data)
    ext = [loss for loss in res.losses if loss.code == "EXTENSION"]
    assert len(ext) == 2
    assert all(loss.id == "e" or "urn:vendor" in loss.message for loss in ext)
    assert res.snapshot.concepts[0].name == "A"


def test_extra_languages_are_reported() -> None:
    data = _model(
        '<element identifier="e" xsi:type="BusinessActor">'
        '<name xml:lang="en">Clerk</name><name xml:lang="nl">Klerk</name></element>'
    )
    res = import_exchange(data)
    assert res.snapshot.concepts[0].name == "Clerk"
    assert [(loss.code, loss.id) for loss in res.losses] == [("LANG-EXTRA", "e")]


def test_overlong_identifier_is_remapped_and_references_follow() -> None:
    long_id = "id-" + "x" * 150
    data = _model(
        f'<element identifier="{long_id}" xsi:type="BusinessActor"><name>A</name></element>'
        '<element identifier="r" xsi:type="BusinessRole"><name>R</name></element>',
        f'<relationship identifier="rel" xsi:type="Assignment" source="{long_id}" target="r"/>',
    )
    res = import_exchange(data)
    new = res.id_map[long_id]
    assert new != long_id and len(new) <= 100
    assert res.snapshot.relationships[0].source == new
    assert [loss.code for loss in res.losses] == ["ID-REMAPPED"]
    assert res.findings == [] or all(f.rule_id != "AM-REF-001" for f in res.findings)


def test_unknown_standard_namespace_content_is_never_silently_dropped() -> None:
    data = _model(
        '<element identifier="e" xsi:type="BusinessActor" bogusAttr="1">'
        "<name>A</name><bogus>x</bogus></element>"
    )
    res = import_exchange(data)
    unhandled = [loss for loss in res.losses if loss.code == "UNHANDLED"]
    assert len(unhandled) == 2
    assert {loss.xpath for loss in unhandled} == {
        "/model/elements/element/bogus",
        "/model/elements/element",
    }


def test_default_folders_read_back_as_none_and_custom_folders_are_kept() -> None:
    elements = (
        '<element identifier="a" xsi:type="BusinessActor"><name>A</name></element>'
        '<element identifier="g" xsi:type="Goal"><name>G</name></element>'
    )
    default = _model(
        elements,
        extra='<organizations><item><label xml:lang="en">Business</label>'
        '<item identifierRef="a"/></item><item><label xml:lang="en">Motivation</label>'
        '<item identifierRef="g"/></item></organizations>',
    )
    res = import_exchange(default)
    assert res.losses == [] and res.snapshot.organizations is None
    custom = _model(
        elements,
        extra='<organizations><item identifier="f1"><label xml:lang="en">Business</label>'
        "<documentation>the business</documentation>"
        '<item><label>Actors</label><item identifierRef="a"/></item>'
        '<item identifierRef="g"/></item></organizations>',
    )
    res = import_exchange(custom)
    assert res.losses == []
    assert res.snapshot.organizations == [
        ArchimateOrganization(
            id="f1",
            name="Business",
            documentation="the business",
            items=["g"],
            children=[ArchimateOrganization(name="Actors", items=["a"])],
        )
    ]
    # And it is written back as it came in, sub-folders first.
    again = import_exchange(
        export_exchange(res.snapshot, model_id="m", name="M")
    ).snapshot.organizations
    assert again == res.snapshot.organizations


def test_organizations_export_and_report_what_they_cannot_carry() -> None:
    snap = full_snapshot().model_copy(
        update={
            "organizations": [
                ArchimateOrganization(
                    id="fold-1",
                    name="Everything",
                    documentation="top",
                    items=["c-business_actor", "r-serving", "v-1", "gone"],
                    children=[
                        ArchimateOrganization(name="Empty sub-folder"),
                        ArchimateOrganization(id="fold-2", items=["c-goal"]),
                    ],
                )
            ]
        }
    )
    res = export_exchange_report(snap, model_id="m", name="M")
    assert schema_errors(res.data) == []
    assert [(loss.code, loss.id) for loss in res.losses] == [("EXPORT-DANGLING", "gone")]
    back = import_exchange(res.data)
    assert back.losses == []
    expected = snap.organizations
    assert expected is not None
    expected[0].items.remove("gone")
    assert back.snapshot.organizations == expected
    assert back.snapshot.model_copy(update={"organizations": None}) == full_snapshot()


def test_validators_ignore_organizations() -> None:
    snap = full_snapshot()
    with_orgs = snap.model_copy(
        update={"organizations": [ArchimateOrganization(name="X", items=["nope"])]}
    )
    assert validate(with_orgs) == validate(snap)


def test_visual_objects_lines_labels_and_border_types_are_written_as_the_standard_says() -> None:
    root = _root(export_exchange(full_snapshot(), model_id="m-1", name="Full"))
    nodes = {n.get("identifier"): n for n in root.iter(f"{{{NS}}}node")}
    t = f"{{{XSI}}}type"
    assert nodes["o-vgroup"].get(t) == "Container"
    assert nodes["o-vgroup"].findtext(f"{{{NS}}}label") == "Visual group"
    assert nodes["o-note"].get(t) == "Label"
    assert nodes["o-note"].findtext(f"{{{NS}}}label") == "A note\nsecond line"
    assert nodes["o-note"].getparent() is nodes["o-vgroup"]
    assert nodes["o-note"].get("elementRef") is None
    assert nodes["o-tgt"].findtext(f"{{{NS}}}label") == "${name} (${type})"
    conns = {c.get("identifier"): c for c in root.iter(f"{{{NS}}}connection")}
    assert conns["cn-line"].get(t) == "Line" and conns["cn-line"].get("relationshipRef") is None
    assert conns["cn-line"].findtext(f"{{{NS}}}label") == "see role"
    assert conns["cn-serving"].findtext(f"{{{NS}}}label") == "serves"
    view = next(v for v in root.iter(f"{{{NS}}}view") if v.get("identifier") == "v-1")
    pdefs = {
        d.get("identifier"): d.findtext(f"{{{NS}}}name")
        for d in root.iter(f"{{{NS}}}propertyDefinition")
    }
    props_el = view.find(f"{{{NS}}}properties")
    assert props_el is not None
    props = [
        (pdefs[p.get("propertyDefinitionRef")], p.findtext(f"{{{NS}}}value")) for p in props_el
    ]
    assert props == [
        ("status", "draft"),
        ("archi:borderType", "o-vgroup=rectangle"),
        ("archi:borderType", "o-note=dogear"),
    ]


def test_note_cannot_contain_nodes_and_misplaced_text_is_reported() -> None:
    snap = ArchimateSnapshot(
        concepts=[ArchimateConceptIn(id="a", type="business_actor", name="A")],
        views=[
            ArchimateViewIn(
                id="v",
                name="V",
                properties=[("archi:borderType", "x=y")],
                objects=[
                    ArchimateViewObjectIn(
                        id="n", kind="note", text="t", bounds=Bounds(x=5, y=5, w=200, h=200)
                    ),
                    ArchimateViewObjectIn(
                        id="o",
                        concept_id="a",
                        parent_object_id="n",
                        text="not for elements",
                        bounds=Bounds(x=10, y=10, w=50, h=50),
                    ),
                ],
            )
        ],
    )
    res = export_exchange_report(snap, model_id="m", name="M")
    assert schema_errors(res.data) == []
    assert [(loss.code, loss.id) for loss in res.losses] == [
        ("ATTR-NOT-APPLICABLE", "o"),
        ("PROPERTY-KEY-RESERVED", "v"),
        ("OBJECT-REPARENTED", "o"),
    ]
    objs = {o.id: o for o in import_exchange(res.data).snapshot.views[0].objects}  # type: ignore[index]
    # Re-parented to the note's parent (the view) at the same absolute position.
    assert (objs["o"].parent_object_id, objs["o"].bounds) == (None, Bounds(x=15, y=15, w=50, h=50))


def test_border_type_property_that_names_no_node_is_reported() -> None:
    data = _model(
        '<element identifier="a" xsi:type="BusinessActor"><name>A</name></element>',
        extra='<propertyDefinitions><propertyDefinition identifier="pd" type="string">'
        "<name>archi:borderType</name></propertyDefinition></propertyDefinitions>"
        '<views><diagrams><view identifier="v" xsi:type="Diagram"><name>V</name>'
        '<properties><property propertyDefinitionRef="pd"><value>nobody=dogear</value></property>'
        '<property propertyDefinitionRef="pd"><value>na=tabbed</value></property>'
        "</properties>"
        '<node identifier="na" xsi:type="Container" x="0" y="0" w="9" h="9"/>'
        "</view></diagrams></views>",
    )
    res = import_exchange(data)
    assert [(loss.code, loss.id) for loss in res.losses] == [("VALUE-INVALID", "v")]
    view = (res.snapshot.views or [])[0]
    assert view.properties is None
    assert [(o.id, o.kind, o.border_type) for o in view.objects] == [("na", "group", "tabbed")]


def test_view_object_documentation_is_for_notes_and_groups_only() -> None:
    with pytest.raises(ValueError, match="documentation is not allowed on an element occurrence"):
        ArchimateViewObjectIn(id="o", concept_id="a", documentation="d")
    group = ArchimateViewObjectIn(id="g", kind="group", name="G", documentation="gd")
    note = ArchimateViewObjectIn(id="n", kind="note", text="t", documentation="nd")
    assert (group.documentation, note.documentation) == ("gd", "nd")


def test_note_and_group_documentation_round_trips_and_nothing_reads_it() -> None:
    from tools.rendering.archimate_svg import render_archimate_view

    snap = full_snapshot()
    root = _root(export_exchange(snap, model_id="m-1", name="Full"))
    nodes = {n.get("identifier"): n for n in root.iter(f"{{{NS}}}node")}
    assert nodes["o-vgroup"].findtext(f"{{{NS}}}documentation") == "what the group means"
    assert nodes["o-note"].findtext(f"{{{NS}}}documentation") == "note doc"
    back = import_exchange(export_exchange(snap, model_id="m-1", name="Full"))
    assert back.losses == []
    objs = {o.id: o for v in back.snapshot.views or [] for o in v.objects}
    assert (objs["o-vgroup"].documentation, objs["o-note"].documentation) == (
        "what the group means",
        "note doc",
    )
    # The validator and the renderer ignore it: same findings, same SVG without it.
    stripped = snap.model_copy(
        update={
            "views": [
                v.model_copy(
                    update={
                        "objects": [o.model_copy(update={"documentation": None}) for o in v.objects]
                    }
                )
                for v in snap.views or []
            ]
        }
    )
    assert validate(snap) == validate(stripped)
    assert render_archimate_view(snap, "v-1").svg == render_archimate_view(stripped, "v-1").svg
