"""UML SVG renderer (``tools.rendering.uml_svg``) — class, component and sequence
diagrams, asserted structurally on the parsed SVG (``data-*`` attributes)."""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from schemas.uml import UmlSnapshot
from tools.rendering import RenderTargetNotFoundError, render_uml_diagram, sanitize_svg

NS = "{http://www.w3.org/2000/svg}"


# ----------------------------------------------------------------------------- helpers


def by(root: ET.Element, **data: str) -> list[ET.Element]:
    return [
        e
        for e in root.iter()
        if all(e.get("data-" + k.replace("_", "-")) == v for k, v in data.items())
    ]


def one(root: ET.Element, **data: str) -> ET.Element:
    found = by(root, **data)
    assert len(found) == 1, f"expected one {data}, found {len(found)}"
    return found[0]


def marker_kind(root: ET.Element, ref: str | None) -> str | None:
    if ref is None:
        return None
    mid = re.fullmatch(r"url\(#([^)]+)\)", ref).group(1)  # type: ignore[union-attr]
    return next(e for e in root.iter(NS + "marker") if e.get("id") == mid).get("data-marker")


def first_path(g: ET.Element) -> ET.Element:
    p = g.find(NS + "path")
    assert p is not None
    return p


def texts(e: ET.Element) -> list[str]:
    return ["".join(t.itertext()) for t in e.iter(NS + "text")]


def el(i: str, kind: str, name: str = "", **attrs: Any) -> dict[str, Any]:
    return {"id": i, "kind": kind, "name": name or i, **attrs}


def render(data: dict[str, Any], diagram: str):
    res = render_uml_diagram(UmlSnapshot.model_validate(data), diagram)
    return res, ET.fromstring(res.svg)


def B(x: int, y: int, w: int = -1, h: int = -1) -> dict[str, int]:
    return {"x": x, "y": y, "w": w, "h": h}


CLASS_MODEL: dict[str, Any] = {
    "elements": [
        el(
            "Shape",
            "class",
            attributes={
                "is_abstract": True,
                "properties": [
                    {"id": "p-id", "name": "id", "type_ref": "Int", "visibility": "private"},
                    {
                        "id": "p-count",
                        "name": "count",
                        "type_ref": "Int",
                        "visibility": "public",
                        "is_static": True,
                        "default": "0",
                    },
                    {
                        "id": "p-tags",
                        "name": "tags",
                        "multiplicity": "0..*",
                        "visibility": "protected",
                    },
                ],
                "operations": [
                    {
                        "id": "o-area",
                        "name": "area",
                        "visibility": "public",
                        "is_abstract": True,
                        "return_type_ref": "Int",
                    },
                    {
                        "id": "o-move",
                        "name": "move",
                        "visibility": "package",
                        "is_static": True,
                        "parameters": [
                            {"id": "a", "name": "dx", "type_ref": "Int"},
                            {"id": "b", "name": "res", "direction": "out", "type_ref": "Int"},
                            {"id": "c", "name": "", "direction": "return", "type_ref": "Int"},
                        ],
                    },
                ],
            },
        ),
        el("Circle", "class"),
        el("Int", "primitive_type", "Integer"),
        el(
            "IDraw",
            "interface",
            "Drawable",
            attributes={"operations": [{"id": "o-draw", "name": "draw"}]},
        ),
        el("Color", "enumeration", attributes={"literals": ["RED", "GREEN"]}),
        el("Point", "data_type"),
        el("Canvas", "class"),
        el("Pkg", "package", "model"),
        el("User", "actor"),
    ],
    "relationships": [
        {"id": "gen", "kind": "generalization", "source": "Circle", "target": "Shape"},
        {"id": "ir", "kind": "interface_realization", "source": "Shape", "target": "IDraw"},
        {"id": "rl", "kind": "realization", "source": "Circle", "target": "IDraw"},
        {
            "id": "dep",
            "kind": "dependency",
            "source": "Canvas",
            "target": "Color",
            "name": "paints",
        },
        {"id": "use", "kind": "usage", "source": "Canvas", "target": "IDraw"},
        {
            "id": "comp",
            "kind": "association",
            "name": "holds",
            "source": "Canvas",
            "target": "Shape",
            "ends": [
                # UML 2.5.1: the part-typed member end carries aggregation=composite;
                # the diamond is drawn at the opposite (whole) end, Canvas.
                {"element_ref": "Canvas", "multiplicity": "1"},
                {
                    "element_ref": "Shape",
                    "role": "shapes",
                    "multiplicity": "0..*",
                    "navigable": True,
                    "aggregation": "composite",
                },
            ],
        },
        {
            "id": "shared",
            "kind": "association",
            "source": "Canvas",
            "target": "Point",
            "ends": [
                {"element_ref": "Canvas"},
                {"element_ref": "Point", "aggregation": "shared"},
            ],
        },
        {
            # Canvas is the whole AND navigable: diamond plus arrowhead on one terminal.
            "id": "both",
            "kind": "association",
            "source": "Canvas",
            "target": "Color",
            "ends": [
                {"element_ref": "Canvas", "navigable": True},
                {"element_ref": "Color", "aggregation": "composite"},
            ],
        },
    ],
    "diagrams": [
        {
            "id": "cd",
            "kind": "class",
            "objects": [
                {"id": f"d-{x}", "element_ref": x}
                for x in (
                    "Shape",
                    "Circle",
                    "IDraw",
                    "Color",
                    "Point",
                    "Canvas",
                    "Int",
                    "Pkg",
                    "User",
                )
            ],
            "connections": [
                {
                    "id": "c-gen",
                    "relationship_ref": "gen",
                    "source_object_id": "d-Circle",
                    "target_object_id": "d-Shape",
                },
                {
                    "id": "c-ir",
                    "relationship_ref": "ir",
                    "source_object_id": "d-Shape",
                    "target_object_id": "d-IDraw",
                },
                {
                    "id": "c-rl",
                    "relationship_ref": "rl",
                    "source_object_id": "d-Circle",
                    "target_object_id": "d-IDraw",
                },
                {
                    "id": "c-dep",
                    "relationship_ref": "dep",
                    "source_object_id": "d-Canvas",
                    "target_object_id": "d-Color",
                },
                {
                    "id": "c-use",
                    "relationship_ref": "use",
                    "source_object_id": "d-Canvas",
                    "target_object_id": "d-IDraw",
                },
                {
                    "id": "c-comp",
                    "relationship_ref": "comp",
                    "source_object_id": "d-Canvas",
                    "target_object_id": "d-Shape",
                },
                # Drawn in the reverse direction: the diamond must stay at Canvas.
                {
                    "id": "c-both",
                    "relationship_ref": "both",
                    "source_object_id": "d-Canvas",
                    "target_object_id": "d-Color",
                },
                {
                    "id": "c-shared",
                    "relationship_ref": "shared",
                    "source_object_id": "d-Point",
                    "target_object_id": "d-Canvas",
                },
            ],
        }
    ],
}


# ----------------------------------------------------------------------------- class diagrams


def test_class_compartments_visibility_keywords_abstract_static() -> None:
    res, root = render(CLASS_MODEL, "cd")
    assert [d.rule_id for d in res.diagnostics] == ["RENDER-UML-LAYOUT-001"]
    shape = one(root, object_id="d-Shape")
    assert shape.get("data-abstract") == "true"
    name = one(shape, compartment="name")
    title = next(t for t in name.iter(NS + "text"))
    assert "".join(title.itertext()) == "Shape"
    assert title.get("font-style") == "italic" and title.get("font-weight") == "bold"
    attrs = one(shape, compartment="attributes")
    assert texts(attrs) == ["-id : Integer", "+count : Integer = 0", "#tags[0..*]"]
    assert one(attrs, feature_id="p-count").get("text-decoration") == "underline"
    assert one(attrs, feature_id="p-id").get("data-visibility") == "private"
    ops = one(shape, compartment="operations")
    assert texts(ops) == ["+area() : Integer", "~move(dx : Integer, out res : Integer) : Integer"]
    assert one(ops, feature_id="o-area").get("font-style") == "italic"
    assert one(ops, feature_id="o-move").get("text-decoration") == "underline"

    assert texts(one(one(root, object_id="d-IDraw"), compartment="name")) == [
        "«interface»",
        "Drawable",
    ]
    color = one(root, object_id="d-Color")
    assert texts(one(color, compartment="name"))[0] == "«enumeration»"
    assert texts(one(color, compartment="literals")) == ["RED", "GREEN"]
    assert not by(color, compartment="attributes")
    assert texts(one(one(root, object_id="d-Point"), compartment="name"))[0] == "«dataType»"
    assert texts(one(one(root, object_id="d-Int"), compartment="name")) == [
        "«primitive»",
        "Integer",
    ]
    circle = one(root, object_id="d-Circle")
    assert by(circle, compartment="attributes") and by(circle, compartment="operations")
    assert one(root, object_id="d-Pkg").get("data-element-kind") == "package"
    assert by(one(root, object_id="d-Pkg"), part="tab")
    assert by(one(root, object_id="d-User"), part="figure")


@pytest.mark.parametrize(
    ("conn", "kind", "dashed", "start", "end", "label"),
    [
        ("c-gen", "generalization", False, None, "triangle-hollow", None),
        ("c-ir", "interface_realization", True, None, "triangle-hollow", None),
        ("c-rl", "realization", True, None, "triangle-hollow", None),
        ("c-dep", "dependency", True, None, "arrow-open", "paints"),
        ("c-use", "usage", True, None, "arrow-open", "«use»"),
        ("c-comp", "association", False, "diamond-filled", "arrow-open", "holds"),
        ("c-shared", "association", False, None, "diamond-hollow", None),
    ],
)
def test_relationship_notation(
    conn: str, kind: str, dashed: bool, start: str | None, end: str | None, label: str | None
) -> None:
    _, root = render(CLASS_MODEL, "cd")
    g = one(root, connection_id=conn)
    assert g.get("data-relationship-type") == kind
    p = first_path(g)
    assert bool(p.get("stroke-dasharray")) is dashed
    assert marker_kind(root, p.get("marker-start")) == start
    assert marker_kind(root, p.get("marker-end")) == end
    labels = ["".join(t.itertext()) for t in by(g, kind="label")]
    assert labels == ([label] if label else [])


def test_association_ends_carry_roles_and_multiplicities() -> None:
    _, root = render(CLASS_MODEL, "cd")
    g = one(root, connection_id="c-comp")
    assert g.get("data-source-end") == "composite" and g.get("data-target-end") == "navigable"
    assert not by(g, kind="navigability")
    roles = {(t.get("data-end"), "".join(t.itertext())) for t in by(g, kind="role")}
    mults = {(t.get("data-end"), "".join(t.itertext())) for t in by(g, kind="multiplicity")}
    assert roles == {("target", "shapes")}
    assert mults == {("source", "1"), ("target", "0..*")}
    rev = one(root, connection_id="c-shared")
    assert rev.get("data-target-end") == "shared" and rev.get("data-source-end") == "none"


def test_association_end_with_diamond_and_navigability_draws_both() -> None:
    _, root = render(CLASS_MODEL, "cd")
    g = one(root, connection_id="c-both")
    assert g.get("data-source-end") == "composite navigable"
    assert g.get("data-target-end") == "none"
    path = first_path(g)
    assert marker_kind(root, path.get("marker-start")) == "diamond-filled"
    assert path.get("marker-end") is None
    arrow = one(g, kind="navigability")
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", arrow.get("d", ""))]
    tip = (nums[2], nums[3])
    start = tuple(float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", path.get("d", ""))[:2])
    # the arrowhead tip sits just beyond the 14px diamond, pointing at Canvas
    assert abs(((tip[0] - start[0]) ** 2 + (tip[1] - start[1]) ** 2) ** 0.5 - 15) < 0.05


# ----------------------------------------------------------------------------- component diagrams

COMPONENT_MODEL: dict[str, Any] = {
    "elements": [
        el("Pkg", "package"),
        el(
            "App",
            "component",
            "Shop",
            owner_id="Pkg",
            attributes={"provided": ["IOrder"], "required": ["IPay"]},
        ),
        el("Pay", "component", "Payments", owner_id="Pkg", attributes={"provided": ["IPay"]}),
        el("IOrder", "interface", "Orders", owner_id="Pkg"),
        el("IPay", "interface", "Payment", owner_id="Pkg"),
        el("pOut", "port", "out", owner_id="App", attributes={"required": ["IPay"]}),
        el("pIn", "port", "in", owner_id="Pay", attributes={"provided": ["IPay"]}),
    ],
    "relationships": [
        {
            "id": "asm",
            "kind": "connector",
            "connector_kind": "assembly",
            "source": "pOut",
            "target": "pIn",
        },
        {
            "id": "dlg",
            "kind": "connector",
            "connector_kind": "delegation",
            "source": "pIn",
            "target": "pIn",
        },
        {"id": "real", "kind": "interface_realization", "source": "App", "target": "IOrder"},
    ],
    "diagrams": [
        {
            "id": "cmp",
            "kind": "component",
            "objects": [
                {"id": "k-app", "element_ref": "App", "bounds": B(0, 0)},
                {"id": "k-pay", "element_ref": "Pay", "bounds": B(400, 0)},
                {"id": "k-io", "element_ref": "IOrder", "bounds": B(0, 250)},
                {"id": "k-out", "element_ref": "pOut"},
                {"id": "k-in", "element_ref": "pIn", "bounds": B(393, 30, 14, 14)},
            ],
            "connections": [
                {
                    "id": "x-asm",
                    "relationship_ref": "asm",
                    "source_object_id": "k-out",
                    "target_object_id": "k-in",
                },
                {
                    "id": "x-real",
                    "relationship_ref": "real",
                    "source_object_id": "k-app",
                    "target_object_id": "k-io",
                },
                {
                    "id": "x-dlg",
                    "relationship_ref": "dlg",
                    "source_object_id": "k-in",
                    "target_object_id": "k-in",
                },
            ],
        }
    ],
}


def test_component_icon_ports_and_lollipops() -> None:
    res, root = render(COMPONENT_MODEL, "cmp")
    assert [d.rule_id for d in res.diagnostics] == ["RENDER-UML-LAYOUT-001"]
    app = one(root, object_id="k-app")
    assert by(app, kind="icon", glyph="component")
    assert texts(one(app, compartment="name")) == ["«component»", "Shop"]
    provided = one(app, kind="provided")
    assert provided.get("data-interface") == "IOrder" and provided.find(NS + "circle") is not None
    assert "Orders" in texts(provided)
    required = one(app, kind="required")
    assert required.get("data-interface") == "IPay"
    assert required.find(NS + "circle") is None and len(required.findall(NS + "path")) == 2
    # Required sockets on the left, provided lollipops on the right of a component.
    ax = float(app.get("data-x", "0"))
    aw = float(app.get("data-w", "0"))
    assert float(provided.find(NS + "circle").get("cx", "0")) > ax + aw  # type: ignore[union-attr]
    # Port without bounds sits on its owner's right border.
    out = one(root, object_id="k-out")
    port_rect = one(out, part="port")
    assert float(port_rect.get("x", "0")) == ax + aw - 7
    assert float(port_rect.get("width", "0")) == 14
    assert by(out, kind="required", interface="IPay")
    # Ports are painted after (on top of) their owners.
    order = [g.get("data-object-id") for g in by(root, kind="element")]
    assert order.index("k-out") > order.index("k-app")


def test_connectors_assembly_and_delegation() -> None:
    _, root = render(COMPONENT_MODEL, "cmp")
    asm = one(root, connection_id="x-asm")
    assert asm.get("data-connector-kind") == "assembly" and by(asm, kind="ball-socket")
    assert first_path(asm).get("marker-end") is None
    dlg = one(root, connection_id="x-dlg")
    assert marker_kind(root, first_path(dlg).get("marker-end")) == "arrow-open"
    assert "«delegate»" in "".join(dlg.itertext())


# ----------------------------------------------------------------------------- sequence diagrams


def _seq_model(connections_reversed: bool = True) -> dict[str, Any]:
    order = ["L1", "L2", "L2", "L3", "L2", "L2", "L1", "L2", "L2", "L1", "L1", "L2", "L2", "L3"]
    msgs = [
        ("m1", "synch_call", "pay", "o0", "o1"),
        ("m2", "create", "", "o2", "o3"),
        ("m3", "asynch_signal", "log", "o4", "o5"),
        ("m4", "reply", "ok", "o7", "o6"),
        ("m5", "reply", "fail", "o8", "o9"),
        ("m6", "asynch_call", "done", "o10", "o11"),
        ("m7", "delete", "", "o12", "o13"),
    ]
    conn_ids = [m[0] for m in msgs]
    if connections_reversed:
        conn_ids.reverse()
    return {
        "elements": [el("Canvas", "class"), el("Comp", "component", "Renderer")],
        "interactions": [
            {
                "id": "I1",
                "name": "Checkout",
                "lifelines": [
                    {"id": "L1", "name": "ui", "represents_ref": "Canvas"},
                    {"id": "L2", "name": "svc", "represents_ref": "Comp"},
                    {"id": "L3", "name": "tmp"},
                ],
                "occurrences": [{"id": f"o{i}", "lifeline": ll} for i, ll in enumerate(order)],
                "messages": [
                    {"id": i, "kind": k, "name": n, "send_occurrence": s, "receive_occurrence": r}
                    | ({"arguments": ["amount"]} if i == "m1" else {})
                    for i, k, n, s, r in msgs
                ],
                "executions": [{"id": "e1", "lifeline": "L2", "start": "o1", "finish": "o7"}],
                "fragments": [
                    {
                        "id": "f-alt",
                        "operator": "alt",
                        "covered": ["L1", "L2"],
                        "operands": [
                            {"guard": "funds", "occurrences": ["o7", "o6"]},
                            {"guard": "else", "occurrences": ["o8", "o9"]},
                        ],
                    },
                    {
                        "id": "f-loop",
                        "operator": "loop",
                        "covered": ["L2"],
                        "operands": [{"guard": "retry", "occurrences": ["o4", "o5"]}],
                    },
                ],
            }
        ],
        "diagrams": [
            {
                "id": "sq",
                "kind": "sequence",
                "objects": [
                    {"id": "s1", "lifeline_id": "L1"},
                    {"id": "s2", "lifeline_id": "L2"},
                    {"id": "s3", "lifeline_id": "L3"},
                ],
                "connections": [
                    {
                        "id": f"sc-{m}",
                        "message_id": m,
                        "source_object_id": "s1",
                        "target_object_id": "s2",
                    }
                    for m in conn_ids
                ],
            }
        ],
    }


def test_sequence_messages_follow_stored_occurrence_order_not_connection_order() -> None:
    res, root = render(_seq_model(connections_reversed=True), "sq")
    assert res.diagnostics == []
    msgs = by(root, kind="message")
    assert [m.get("data-message-id") for m in msgs] == ["m1", "m2", "m3", "m4", "m5", "m6", "m7"]
    ys = [float(m.get("data-y", "0")) for m in msgs]
    assert ys == sorted(ys) and len(set(ys)) == len(ys)
    assert [m.get("data-seq") for m in msgs] == [str(i) for i in range(7)]
    _, forward = render(_seq_model(connections_reversed=False), "sq")
    assert [m.get("data-y") for m in by(forward, kind="message")] == [m.get("data-y") for m in msgs]


@pytest.mark.parametrize(
    ("message", "dashed", "marker"),
    [
        ("m1", False, "arrow-filled"),  # synch
        ("m2", True, "arrow-open"),  # create
        ("m3", False, "arrow-open"),  # asynch signal
        ("m4", True, "arrow-open"),  # reply
        ("m6", False, "arrow-open"),  # asynch call
        ("m7", False, "arrow-filled"),  # delete
    ],
)
def test_message_arrow_styles(message: str, dashed: bool, marker: str) -> None:
    _, root = render(_seq_model(), "sq")
    p = first_path(one(root, message_id=message))
    assert bool(p.get("stroke-dasharray")) is dashed
    assert marker_kind(root, p.get("marker-end")) == marker


def test_lifelines_create_delete_and_executions() -> None:
    _, root = render(_seq_model(), "sq")
    heads = {g.get("data-lifeline-id"): g for g in by(root, kind="lifeline")}
    assert set(heads) == {"L1", "L2", "L3"}
    assert "ui : Canvas" in texts(heads["L1"])
    assert one(heads["L1"], part="line").get("stroke-dasharray")
    create_y = float(one(root, message_id="m2").get("data-y", "0"))
    head = one(heads["L3"], part="head")
    assert float(head.get("y", "0")) + float(head.get("height", "0")) / 2 == create_y
    assert float(one(heads["L1"], part="head").get("y", "1")) == 0
    # create arrow ends at the created head's border, not its lifeline
    end_x = float(
        re.findall(r"-?\d+(?:\.\d+)?", first_path(one(root, message_id="m2")).get("d", ""))[-2]
    )
    assert end_x == float(head.get("x", "0"))
    # delete: lifeline stops at the ✕
    delete_y = float(one(root, message_id="m7").get("data-y", "0"))
    assert by(heads["L3"], part="destruction")
    line_d = one(heads["L3"], part="line").get("d", "")
    assert line_d.endswith(f"V{delete_y:g}")
    ex = one(root, kind="execution")
    y0, h = float(ex.get("y", "0")), float(ex.get("height", "0"))
    m1y = float(one(root, message_id="m1").get("data-y", "0"))
    m4y = float(one(root, message_id="m4").get("data-y", "0"))
    assert (y0, y0 + h) == (m1y, m4y)


def test_combined_fragments_are_framed_with_operator_guards_and_separators() -> None:
    _, root = render(_seq_model(), "sq")
    alt = one(root, fragment_id="f-alt")
    assert alt.get("data-operator") == "alt"
    assert one(alt, part="frame") is not None and one(alt, part="operator-tab") is not None
    assert "".join(one(alt, kind="operator").itertext()) == "alt"
    assert ["".join(g.itertext()) for g in by(alt, kind="guard")] == ["[funds]", "[else]"]
    seps = by(alt, part="separator")
    assert len(seps) == 1 and seps[0].get("stroke-dasharray")
    top = float(alt.get("data-y", "0"))
    bottom = top + float(alt.get("data-h", "0"))
    sep_y = float(re.findall(r"-?\d+(?:\.\d+)?", seps[0].get("d", ""))[1])
    m4 = float(one(root, message_id="m4").get("data-y", "0"))
    m5 = float(one(root, message_id="m5").get("data-y", "0"))
    assert top < m4 < sep_y < m5 < bottom
    loop = one(root, fragment_id="f-loop")
    m3 = float(one(root, message_id="m3").get("data-y", "0"))
    assert (
        float(loop.get("data-y", "0"))
        < m3
        < float(loop.get("data-y", "0")) + float(loop.get("data-h", "0"))
    )
    assert not by(loop, part="separator")
    assert by(root, kind="interaction-frame")


def test_sequence_without_message_connections_draws_all_messages_with_advice() -> None:
    model = _seq_model()
    model["diagrams"][0]["connections"] = []
    res, root = render(model, "sq")
    assert [d.rule_id for d in res.diagnostics] == ["RENDER-UML-SEQ-003"]
    assert len(by(root, kind="message")) == 7


# ----------------------------------------------------------------------------- unresolved + safety


def test_unresolved_references_degrade_to_placeholders_and_diagnostics() -> None:
    model = {
        "elements": [el("A", "class"), el("W", "widget")],
        "relationships": [{"id": "x", "kind": "teleport", "source": "A", "target": "A"}],
        "interactions": [],
        "diagrams": [
            {
                "id": "cd",
                "kind": "class",
                "objects": [
                    {"id": "oa", "element_ref": "A", "bounds": B(0, 0)},
                    {"id": "ghost", "element_ref": "nope", "bounds": B(200, 0)},
                    {"id": "weird", "element_ref": "W", "bounds": B(400, 0, 100, 40)},
                    {"id": "bare", "bounds": B(600, 0, 100, 40)},
                ],
                "connections": [
                    {
                        "id": "c1",
                        "relationship_ref": "missing",
                        "source_object_id": "oa",
                        "target_object_id": "ghost",
                    },
                    {
                        "id": "c2",
                        "relationship_ref": "x",
                        "source_object_id": "oa",
                        "target_object_id": "oa",
                    },
                    {
                        "id": "c3",
                        "relationship_ref": "x",
                        "source_object_id": "oa",
                        "target_object_id": "gone",
                    },
                ],
            },
            {
                "id": "sq",
                "kind": "sequence",
                "objects": [{"id": "s", "lifeline_id": "nope"}],
                "connections": [],
            },
        ],
    }
    res, root = render(model, "cd")
    ids = {d.rule_id for d in res.diagnostics}
    assert {
        "RENDER-UML-OBJ-001",
        "RENDER-UML-OBJ-002",
        "RENDER-UML-CON-001",
        "RENDER-UML-CON-002",
        "RENDER-UML-CON-003",
    } <= ids
    assert {g.get("data-object-id") for g in by(root, kind="unresolved")} == {"ghost", "bare"}
    assert {g.get("data-connection-id") for g in by(root, kind="unresolved-connection")} == {
        "c1",
        "c2",
    }
    res, root = render(model, "sq")
    assert {d.rule_id for d in res.diagnostics} == {"RENDER-UML-SEQ-004", "RENDER-UML-OBJ-001"}
    assert by(root, kind="unresolved")
    with pytest.raises(RenderTargetNotFoundError):
        render(model, "missing")


def test_hostile_text_is_escaped_and_output_is_sanitized() -> None:
    hostile = "</text><script>alert(1)</script>"
    model = {
        "elements": [
            el(
                "A",
                "class",
                hostile,
                attributes={
                    "properties": [
                        {"id": "p", "name": "javascript:alert(1)", "default": "<img onerror=x>"}
                    ]
                },
            )
        ],
        "diagrams": [
            {
                "id": "cd",
                "kind": "class",
                "objects": [
                    {
                        "id": "o",
                        "element_ref": "A",
                        "style": {"fill_color": "url(javascript:x)", "onload": "x"},
                    }
                ],
                "connections": [],
            }
        ],
    }
    res, root = render(model, "cd")
    assert "<script" not in res.svg and "<img" not in res.svg
    assert hostile in "".join(root.itertext())
    assert not any(k.startswith("on") for e in root.iter() for k in e.attrib)
    assert not any("javascript" in v for e in root.iter() for v in e.attrib.values())
    assert {"RENDER-UML-STYLE-001", "RENDER-UML-STYLE-003"} <= {d.rule_id for d in res.diagnostics}
    assert sanitize_svg(res.svg) == res.svg


@pytest.mark.parametrize(
    ("model", "diagram"), [(CLASS_MODEL, "cd"), (COMPONENT_MODEL, "cmp"), (None, "sq")]
)
def test_uml_rendering_is_deterministic(model: dict[str, Any] | None, diagram: str) -> None:
    data = model or _seq_model()
    a = render_uml_diagram(UmlSnapshot.model_validate(data), diagram)
    b = render_uml_diagram(UmlSnapshot.model_validate(data), diagram)
    assert a.svg == b.svg
    assert a.renderer_version == "uml-svg/1.0.0"


def test_class_auto_layout_is_layered_without_overlaps() -> None:
    res, root = render(CLASS_MODEL, "cd")
    rects = {
        g.get("data-object-id"): tuple(
            float(g.get(k, "0")) for k in ("data-x", "data-y", "data-w", "data-h")
        )
        for g in by(root, kind="element")
    }
    ids = sorted(rects)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            ra, rb = rects[a], rects[b]
            overlap = (
                ra[0] < rb[0] + rb[2]
                and rb[0] < ra[0] + ra[2]
                and ra[1] < rb[1] + rb[3]
                and rb[1] < ra[1] + ra[3]
            )
            assert not overlap, (a, b)
    # general above specific, realized interface above its realizer, owner above part
    assert rects["d-Shape"][1] < rects["d-Circle"][1]
    assert rects["d-IDraw"][1] < rects["d-Shape"][1]
    assert rects["d-Canvas"][1] < rects["d-Color"][1]
    assert render_uml_diagram(UmlSnapshot.model_validate(CLASS_MODEL), "cd").svg == res.svg


LONG_WORD = "Supercalifragilisticexpialidocious-12345"


def _line_extent(t: ET.Element) -> tuple[float, float]:
    """Horizontal extent of a (possibly multi-line) <text>."""
    from tools.rendering.svg import text_width

    fs = float(t.get("font-size", "0"))
    spans = t.findall(NS + "tspan")
    lines = [sp.text or "" for sp in spans] if spans else [t.text or ""]
    w = max(text_width(line, fs, bold=t.get("font-weight") == "bold") for line in lines)
    x = float(t.get("x", "0"))
    anchor = t.get("text-anchor", "start")
    left = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
    return left, left + w


def test_a_40_character_word_never_overflows_a_120px_class_box() -> None:
    model = {
        "elements": [
            el(
                "C",
                "class",
                LONG_WORD,
                attributes={"properties": [{"id": "p", "name": LONG_WORD, "visibility": "public"}]},
            )
        ],
        "diagrams": [
            {
                "id": "cd",
                "kind": "class",
                "objects": [{"id": "o", "element_ref": "C", "bounds": B(0, 0, 120, -1)}],
            }
        ],
    }
    res, root = render(model, "cd")
    assert res.diagnostics == []
    g = one(root, object_id="o")
    assert float(g.get("data-w", "0")) == 120
    h = float(g.get("data-h", "0"))
    for t in g.iter(NS + "text"):
        left, right = _line_extent(t)
        assert 0 <= left and right <= 120, "".join(t.itertext())
        assert float(t.get("y", "0")) <= h
    attr = one(g, feature_id="p")
    assert attr.get("data-shrunk") == "true"
    assert "".join(sp.text or "" for sp in attr.findall(NS + "tspan")) == "+" + LONG_WORD
    name = next(t for t in one(g, compartment="name").iter(NS + "text"))
    assert "".join(sp.text or "" for sp in name.findall(NS + "tspan")) == LONG_WORD


def test_uml_text_that_cannot_fit_is_ellipsized_or_dropped_with_a_diagnostic() -> None:
    model = {
        "elements": [
            el(
                "C",
                "class",
                LONG_WORD * 4,
                attributes={"properties": [{"id": f"p{i}", "name": f"attr{i}"} for i in range(6)]},
            )
        ],
        "diagrams": [
            {
                "id": "cd",
                "kind": "class",
                "objects": [{"id": "o", "element_ref": "C", "bounds": B(0, 0, 120, 60)}],
            }
        ],
    }
    res, root = render(model, "cd")
    assert {d.rule_id for d in res.diagnostics} == {"RENDER-UML-TEXT-001"}
    g = one(root, object_id="o")
    name = next(t for t in one(g, compartment="name").iter(NS + "text"))
    assert name.get("data-truncated") == "true" and "".join(name.itertext()).endswith("…")
    for t in g.iter(NS + "text"):
        left, right = _line_extent(t)
        assert 0 <= left and right <= 120
        assert float(t.get("y", "0")) <= 60, "no line is drawn below the box"
