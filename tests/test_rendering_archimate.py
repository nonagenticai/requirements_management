"""ArchiMate SVG renderer (``tools.rendering.archimate_svg``) — structure, not pixels.

Every assertion parses the SVG with an XML parser and inspects elements by
their ``data-*`` attributes and geometry, so a test fails when the notation
changes, not when a coordinate is nudged. Covers plan §10: icons, arrowheads,
access direction, association direction, junctions, nesting, relationship
endpoints (incl. connection-to-connection), unresolved references,
determinism and sanitization.
"""

from __future__ import annotations

import math
import re
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from schemas.modeling import ArchimateSnapshot
from tools.architecture.registry import get_registry
from tools.rendering import (
    RenderOptions,
    RenderTargetNotFoundError,
    UnsafeSvgError,
    render_archimate_view,
    sanitize_svg,
)
from tools.rendering.archi import FIGURES, TYPE_FIGURE
from tools.rendering.archimate_svg import RELATIONSHIP_STYLE
from tools.rendering.fonts import FONT_FAMILY, FONT_FILES
from tools.rendering.layout import _seg_hits as route_seg_hits
from tools.rendering.layout import assign_ranks, layered_layout, route
from tools.rendering.png import render_png
from tools.rendering.svg import LINE_HEIGHT, Rect, text_width

NS = "{http://www.w3.org/2000/svg}"
REGISTRY = get_registry()


# ----------------------------------------------------------------------------- helpers


def snap(
    concepts: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    relationships: list[dict[str, Any]] | None = None,
    connections: list[dict[str, Any]] | None = None,
) -> ArchimateSnapshot:
    return ArchimateSnapshot.model_validate(
        {
            "concepts": concepts,
            "relationships": relationships or [],
            "views": [
                {"id": "v", "name": "V", "objects": objects, "connections": connections or []}
            ],
        }
    )


def B(x: int, y: int, w: int = 120, h: int = 55) -> dict[str, int]:
    return {"x": x, "y": y, "w": w, "h": h}


def render(s: ArchimateSnapshot, **kw: Any):
    res = render_archimate_view(s, "v", **kw)
    return res, ET.fromstring(res.svg)


def by(root: ET.Element, **data: str) -> list[ET.Element]:
    """All elements whose data-* attributes match (``kind="element"`` → data-kind)."""
    out = []
    for e in root.iter():
        if all(e.get("data-" + k.replace("_", "-")) == v for k, v in data.items()):
            out.append(e)
    return out


def one(root: ET.Element, **data: str) -> ET.Element:
    found = by(root, **data)
    assert len(found) == 1, f"expected exactly one {data}, found {len(found)}"
    return found[0]


def rule_ids(res) -> list[str]:
    return [d.rule_id for d in res.diagnostics]


def marker_kind(root: ET.Element, ref: str | None) -> str | None:
    if ref is None:
        return None
    m = re.fullmatch(r"url\(#([^)]+)\)", ref)
    assert m, ref
    marker = next(e for e in root.iter(NS + "marker") if e.get("id") == m.group(1))
    return marker.get("data-marker")


def line_of(conn_group: ET.Element) -> ET.Element:
    paths = conn_group.findall(NS + "path")
    assert paths, "connection has no path"
    return paths[0]


def path_points(d: str) -> list[tuple[float, float]]:
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
    return list(zip(nums[0::2], nums[1::2], strict=True))


def two_box_view(rel: dict[str, Any]) -> ArchimateSnapshot:
    return snap(
        [
            {"id": "a", "type": "business_process", "name": "A"},
            {"id": "b", "type": "business_object", "name": "B"},
        ],
        [
            {"id": "oa", "concept_id": "a", "bounds": B(0, 0)},
            {"id": "ob", "concept_id": "b", "bounds": B(300, 0)},
        ],
        [{"id": "r", "source": "a", "target": "b"} | rel],
        [{"id": "x", "relationship_id": "r", "source_object_id": "oa", "target_object_id": "ob"}],
    )


# ----------------------------------------------------------------------------- concepts


def figure_nodes(g: ET.Element) -> list[ET.Element]:
    return list(one(g, part="figure"))


def fills(g: ET.Element) -> list[str]:
    return [e.get("fill", "") for e in figure_nodes(g) if e.get("fill") not in (None, "none")]


@pytest.mark.parametrize("notation", ["box", "icon"])
@pytest.mark.parametrize("concept_type", sorted(REGISTRY.concepts))
def test_every_registry_concept_type_renders_its_archi_figure(
    concept_type: str, notation: str
) -> None:
    extra = {"junction_type": "or"} if concept_type == "junction" else {}
    s = snap(
        [{"id": "c", "type": concept_type, "name": "Thing"} | extra],
        [
            {
                "id": "o",
                "concept_id": "c",
                "bounds": B(10, 10, -1, -1),
                "style": {"notation": notation},
            }
        ],
    )
    res, root = render(s)
    assert res.diagnostics == []
    g = one(root, kind="element")
    figure = TYPE_FIGURE[concept_type]
    spec = FIGURES[figure]
    assert g.get("data-concept-type") == concept_type
    assert g.get("data-layer") == REGISTRY.concepts[concept_type].layer
    assert g.get("data-figure") == figure
    assert figure_nodes(g), "the Archi figure draws something"
    drawn_as = notation if spec.alt is not None else "box"
    assert g.get("data-notation") == drawn_as
    has_icon = drawn_as == "box" and spec.icon is not None and spec.icon_origin is not None
    assert bool(by(g, kind="icon")) is has_icon
    if concept_type != "junction":
        assert "Thing" in "".join(g.itertext())


def test_every_registry_type_maps_to_a_ported_figure_with_both_notations() -> None:
    assert set(TYPE_FIGURE) == set(REGISTRY.concepts)
    assert set(TYPE_FIGURE.values()) <= set(FIGURES)
    no_alt = {t for t, f in TYPE_FIGURE.items() if FIGURES[f].alt is None}
    assert no_alt == {"junction"}  # Archi has no alternative junction figure


def test_box_figures_follow_archi() -> None:
    concepts = [
        {"id": "p", "type": "business_process"},
        {"id": "a", "type": "business_actor"},
        {"id": "g", "type": "goal"},
    ]
    objs = [
        {"id": f"o{c['id']}", "concept_id": c["id"], "bounds": B(i * 200, 0)}
        for i, c in enumerate(concepts)
    ]
    _, root = render(snap(concepts, objs))
    process = figure_nodes(one(root, object_id="op"))
    assert process[0].tag == NS + "rect" and process[0].get("rx") == "10"  # rounded 20x20 arc
    actor = figure_nodes(one(root, object_id="oa"))
    assert actor[0].tag == NS + "rect" and actor[0].get("rx") is None
    goal = figure_nodes(one(root, object_id="og"))
    pts = path_points(goal[0].get("d", ""))
    assert len(pts) == 8, "motivation elements have cut corners"


def test_default_fill_colours_match_archi() -> None:
    concepts = [
        {"id": "b", "type": "business_actor"},
        {"id": "ap", "type": "application_component"},
        {"id": "t", "type": "system_software"},
        {"id": "m", "type": "goal"},
        {"id": "s", "type": "capability"},
        {"id": "l", "type": "location"},
        {"id": "pl", "type": "plateau"},
        {"id": "gr", "type": "grouping"},
        {"id": "j", "type": "junction"},
    ]
    objs = [
        {"id": f"o{c['id']}", "concept_id": c["id"], "bounds": B(i * 150, 0)}
        for i, c in enumerate(concepts)
    ]
    _, root = render(snap(concepts, objs))
    expected = {
        "ob": "#ffffb5",
        "oap": "#b5ffff",
        "ot": "#c9e7b7",
        "om": "#ccccff",
        "os": "#f5deaa",
        "ol": "#fbb875",
        "opl": "#e0ffe0",
        "ogr": "#d2d7d7",
        "oj": "#000000",
    }
    for oid, colour in expected.items():
        assert fills(one(root, object_id=oid))[0] == colour, oid


def test_icon_notation_draws_the_alternative_figure() -> None:
    s = snap(
        [
            {"id": "a", "type": "business_actor", "name": "Actor"},
            {"id": "d", "type": "data_object", "name": "Order"},
            {"id": "n", "type": "node", "name": "Server"},
        ],
        [
            {"id": "box", "concept_id": "a", "bounds": B(0, 0)},
            {
                "id": "icon",
                "concept_id": "a",
                "bounds": B(200, 0, 60, 80),
                "style": {"notation": "icon"},
            },
            {"id": "obj", "concept_id": "d", "bounds": B(300, 0), "style": {"notation": "icon"}},
            {"id": "node", "concept_id": "n", "bounds": B(450, 0), "style": {"notation": "icon"}},
        ],
    )
    res, root = render(s)
    assert res.diagnostics == []
    box = one(root, object_id="box")
    assert by(box, kind="icon") and len(figure_nodes(box)) == 2  # rectangle fill + outline
    icon = one(root, object_id="icon")
    assert icon.get("data-notation") == "icon" and not by(icon, kind="icon")
    assert all(e.tag == NS + "path" for e in figure_nodes(icon))  # head, body, arms, legs
    obj = figure_nodes(one(root, object_id="obj"))
    assert [e.tag.removeprefix(NS) for e in obj] == ["rect", "rect", "path"]  # banded object
    node = figure_nodes(one(root, object_id="node"))
    assert any(e.get("fill") == "#b4cfa4" for e in node), "3D box side is Archi's darker fill"


def test_junction_and_is_filled_or_is_hollow() -> None:
    s = snap(
        [
            {"id": "j1", "type": "junction", "junction_type": "and"},
            {"id": "j2", "type": "junction", "junction_type": "or"},
            {"id": "j3", "type": "junction"},
        ],
        [
            {"id": "oj1", "concept_id": "j1", "bounds": B(0, 0, 15, 15)},
            {"id": "oj2", "concept_id": "j2", "bounds": B(50, 0, 15, 15)},
            {"id": "oj3", "concept_id": "j3", "bounds": B(100, 0, -1, -1)},
        ],
    )
    _, root = render(s)
    assert one(root, object_id="oj1").get("data-junction") == "and"
    assert one(root, object_id="oj2").get("data-junction") == "or"
    and_fig = figure_nodes(one(root, object_id="oj1"))
    or_fig = figure_nodes(one(root, object_id="oj2"))
    assert [(e.get("fill"), e.get("rx")) for e in and_fig] == [("#000000", "7.5")]
    assert [e.get("fill") for e in or_fig] == ["none"]
    assert or_fig[0].get("stroke") == "#000000"
    assert one(root, object_id="oj3").get("data-junction") == "and", "Archi's default is AND"
    assert one(root, object_id="oj3").get("data-w") == "15"


def test_grouping_box_is_a_dashed_rectangle_and_icon_notation_is_tabbed() -> None:
    s = snap(
        [{"id": "g", "type": "grouping", "name": "Sales"}],
        [
            {"id": "og", "concept_id": "g", "bounds": B(0, 0, 300, 150)},
            {
                "id": "ot",
                "concept_id": "g",
                "bounds": B(400, 0, 300, 150),
                "style": {"notation": "icon"},
            },
        ],
    )
    _, root = render(s)
    box = one(root, object_id="og")
    assert any(e.get("stroke-dasharray") for e in figure_nodes(box))
    assert by(box, kind="icon")
    tabbed = figure_nodes(one(root, object_id="ot"))
    assert (
        sum(1 for e in tabbed if e.tag == NS + "rect" and e.get("fill") not in (None, "none")) == 2
    )
    label = one(one(root, object_id="ot"), kind="label")
    assert "".join(label.itertext()) == "Sales" and label.get("text-anchor") == "start"


# ----------------------------------------------------------------------------- relationships

EXPECTED = {
    # type: (dashed?, source marker, target marker)
    "composition": (False, "diamond-filled", None),
    "aggregation": (False, "diamond-hollow", None),
    "assignment": (False, "dot", "arrow-filled"),
    "realization": (True, None, "triangle-hollow"),
    "serving": (False, None, "arrow-open"),
    "access": (True, None, "access-arrow"),  # default access_type is write
    "influence": (True, None, "arrow-open"),
    "triggering": (False, None, "arrow-filled"),
    "flow": (True, None, "arrow-filled"),
    "specialization": (False, None, "triangle-hollow"),
    "association": (False, None, None),
}


def test_expected_table_covers_all_eleven_relationship_types() -> None:
    assert set(EXPECTED) == set(REGISTRY.relationship_types) == set(RELATIONSHIP_STYLE)
    assert len(EXPECTED) == 11


@pytest.mark.parametrize("rel_type", sorted(EXPECTED))
def test_relationship_line_style_and_markers(rel_type: str) -> None:
    res, root = render(two_box_view({"type": rel_type}))
    assert res.diagnostics == []
    g = one(root, kind="connection")
    assert g.get("data-relationship-type") == rel_type
    path = line_of(g)
    dashed, start, end = EXPECTED[rel_type]
    assert bool(path.get("stroke-dasharray")) is dashed
    assert marker_kind(root, path.get("marker-start")) == start
    assert marker_kind(root, path.get("marker-end")) == end


def test_dash_patterns_distinguish_realization_access_influence_flow() -> None:
    patterns = {}
    for t in ("realization", "access", "influence", "flow"):
        _, root = render(two_box_view({"type": t}))
        patterns[t] = line_of(one(root, kind="connection")).get("stroke-dasharray")
    assert len(set(patterns.values())) == 4, patterns


@pytest.mark.parametrize(
    ("access_type", "start", "end"),
    [
        ("write", None, "access-arrow"),  # arrow at the accessed object (target)
        ("read", "access-arrow", None),  # arrow back at the accessor (source)
        ("readwrite", "access-arrow", "access-arrow"),
        ("access", None, None),
        (None, None, "access-arrow"),  # unspecified = write (Archi default)
    ],
)
def test_access_direction(access_type: str | None, start: str | None, end: str | None) -> None:
    _, root = render(two_box_view({"type": "access", "access_type": access_type}))
    g = one(root, kind="connection")
    path = line_of(g)
    assert marker_kind(root, path.get("marker-start")) == start
    assert marker_kind(root, path.get("marker-end")) == end
    assert g.get("data-access-type") == (access_type or "write")


def test_association_direction() -> None:
    _, undirected = render(two_box_view({"type": "association"}))
    _, directed = render(two_box_view({"type": "association", "directed": True}))
    u = line_of(one(undirected, kind="connection"))
    d = line_of(one(directed, kind="connection"))
    assert u.get("marker-end") is None and u.get("marker-start") is None
    assert marker_kind(directed, d.get("marker-end")) == "half-arrow"
    assert d.get("marker-start") is None
    assert one(directed, kind="connection").get("data-directed") == "true"


def test_influence_strength_is_labelled() -> None:
    _, root = render(two_box_view({"type": "influence", "strength": "++", "name": "pushes"}))
    g = one(root, kind="connection")
    assert g.get("data-strength") == "++"
    assert "pushes ++" in "".join(g.itertext())


def test_marker_colour_follows_line_colour() -> None:
    s = two_box_view({"type": "triggering"})
    s.views[0].connections[0].style = {"line_color": "#3366cc"}  # type: ignore[index]
    _, root = render(s)
    path = line_of(one(root, kind="connection"))
    assert path.get("stroke") == "#3366cc"
    ref = path.get("marker-end", "")
    marker = next(e for e in root.iter(NS + "marker") if f"#{e.get('id')})" in ref)
    assert marker.find(NS + "path").get("fill") == "#3366cc"  # type: ignore[union-attr]


def test_endpoints_are_clipped_to_box_borders_and_bendpoints_honoured() -> None:
    data = two_box_view({"type": "serving"}).model_dump(mode="json")
    data["views"][0]["connections"][0]["bendpoints"] = [{"x": 60, "y": 150}, {"x": 360, "y": 150}]
    s = ArchimateSnapshot.model_validate(data)
    _, root = render(s)
    pts = path_points(line_of(one(root, kind="connection")).get("d", ""))
    assert pts[1:-1] == [(60, 150), (360, 150)]
    # source box (0,0,120,55): the line leaves through the bottom edge toward (60,150)
    assert pts[0] == (60, 55)
    # target box (300,0,120,55): arrives through its bottom edge from (360,150)
    assert pts[-1] == (360, 55)


def test_endpoint_on_junction_is_clipped_to_the_circle() -> None:
    s = snap(
        [{"id": "p", "type": "business_process"}, {"id": "j", "type": "junction"}],
        [
            {"id": "op", "concept_id": "p", "bounds": B(0, 0)},
            {"id": "oj", "concept_id": "j", "bounds": B(300, 20, 15, 15)},
        ],
        [{"id": "r", "type": "triggering", "source": "p", "target": "j"}],
        [{"id": "x", "relationship_id": "r", "source_object_id": "op", "target_object_id": "oj"}],
    )
    _, root = render(s)
    end = path_points(line_of(one(root, kind="connection")).get("d", ""))[-1]
    assert math.isclose(math.dist(end, (307.5, 27.5)), 7.5, abs_tol=0.02)


def test_connection_to_connection_attaches_at_the_target_connection_midpoint() -> None:
    s = snap(
        [
            {"id": "a", "type": "business_actor"},
            {"id": "b", "type": "business_role"},
            {"id": "c", "type": "business_object"},
        ],
        [
            {"id": "oa", "concept_id": "a", "bounds": B(0, 0)},
            {"id": "ob", "concept_id": "b", "bounds": B(400, 0)},
            {"id": "oc", "concept_id": "c", "bounds": B(200, 200)},
        ],
        [
            {"id": "r1", "type": "assignment", "source": "a", "target": "b"},
            {"id": "r2", "type": "association", "source": "c", "target": "r1"},
        ],
        [
            # Declared BEFORE the connection it targets: order must not matter.
            {
                "id": "x2",
                "relationship_id": "r2",
                "source_object_id": "oc",
                "target_object_id": "x1",
            },
            {
                "id": "x1",
                "relationship_id": "r1",
                "source_object_id": "oa",
                "target_object_id": "ob",
            },
        ],
    )
    res, root = render(s)
    assert res.diagnostics == []
    x1 = path_points(line_of(one(root, connection_id="x1")).get("d", ""))
    x2g = one(root, connection_id="x2")
    x2 = path_points(line_of(x2g).get("d", ""))
    assert x1 == [(120, 27.5), (400, 27.5)]
    assert x2[-1] == (260, 27.5), "attaches at the midpoint of x1"
    assert x2g.get("data-target-endpoint") == "connection"
    assert x2g.get("data-source-endpoint") == "object"


def test_self_relationship_gets_a_loop() -> None:
    s = snap(
        [{"id": "a", "type": "business_process"}],
        [{"id": "oa", "concept_id": "a", "bounds": B(0, 0)}],
        [{"id": "r", "type": "triggering", "source": "a", "target": "a"}],
        [{"id": "x", "relationship_id": "r", "source_object_id": "oa", "target_object_id": "oa"}],
    )
    _, root = render(s)
    assert len(path_points(line_of(one(root, kind="connection")).get("d", ""))) == 5


# ----------------------------------------------------------------------------- nesting


def test_nesting_child_absolute_is_parent_plus_relative_and_parents_draw_first() -> None:
    s = snap(
        [
            {"id": "g", "type": "grouping", "name": "G"},
            {"id": "n", "type": "node", "name": "N"},
            {"id": "s", "type": "system_software", "name": "S"},
        ],
        [
            {"id": "os", "concept_id": "s", "parent_object_id": "on", "bounds": B(5, 25, 80, 40)},
            {
                "id": "on",
                "concept_id": "n",
                "parent_object_id": "og",
                "bounds": B(10, 30, 200, 100),
            },
            {"id": "og", "concept_id": "g", "bounds": B(100, 50, 400, 300)},
        ],
    )
    res, root = render(s)
    assert res.diagnostics == []
    pos = {g.get("data-object-id"): g for g in by(root, kind="element")}
    assert (float(pos["og"].get("data-x")), float(pos["og"].get("data-y"))) == (100, 50)  # type: ignore[arg-type]
    assert (float(pos["on"].get("data-x")), float(pos["on"].get("data-y"))) == (110, 80)  # type: ignore[arg-type]
    assert (float(pos["os"].get("data-x")), float(pos["os"].get("data-y"))) == (115, 105)  # type: ignore[arg-type]
    order = [g.get("data-object-id") for g in by(root, kind="element")]
    assert order == ["og", "on", "os"], "parents are painted before (under) their children"


def test_repeated_occurrences_of_one_concept_each_get_a_shape() -> None:
    s = snap(
        [{"id": "a", "type": "application_component", "name": "Shop"}],
        [{"id": f"o{i}", "concept_id": "a", "bounds": B(i * 150, 0)} for i in range(3)],
    )
    _, root = render(s)
    shapes = by(root, kind="element")
    assert [g.get("data-object-id") for g in shapes] == ["o0", "o1", "o2"]
    assert {g.get("data-concept-id") for g in shapes} == {"a"}


def test_missing_bounds_get_a_synthesized_layout_and_a_diagnostic() -> None:
    s = snap(
        [
            {"id": "a", "type": "business_actor"},
            {"id": "b", "type": "application_component"},
            {"id": "c", "type": "node"},
            {"id": "d", "type": "business_role"},
            {"id": "p", "type": "node"},
            {"id": "q", "type": "system_software"},
        ],
        [
            {"id": "oa", "concept_id": "a"},
            {"id": "ob", "concept_id": "b"},
            {"id": "oc", "concept_id": "c"},
            {"id": "od", "concept_id": "d", "bounds": B(0, 0)},
            {"id": "op", "concept_id": "p"},
            {"id": "oq", "concept_id": "q", "parent_object_id": "op"},
        ],
    )
    res, root = render(s)
    layout = [d for d in res.diagnostics if d.rule_id == "RENDER-AM-LAYOUT-001"]
    assert len(layout) == 1
    assert sorted(t.id for t in layout[0].targets) == ["oa", "ob", "oc", "op", "oq"]
    rects = {
        g.get("data-object-id"): tuple(
            float(g.get(k)) for k in ("data-x", "data-y", "data-w", "data-h")
        )  # type: ignore[arg-type]
        for g in by(root, kind="element")
    }
    top = [rects[k] for k in ("oa", "ob", "oc", "od", "op")]
    for i, r1 in enumerate(top):
        for r2 in top[i + 1 :]:
            overlap = (
                r1[0] < r2[0] + r2[2]
                and r2[0] < r1[0] + r1[2]
                and r1[1] < r2[1] + r2[3]
                and r2[1] < r1[1] + r1[3]
            )
            assert not overlap, (r1, r2)
    # business row above application row above technology row
    assert rects["oa"][1] < rects["ob"][1] < rects["oc"][1]
    # the synthesized container grew to hold its synthesized child
    px, py, pw, ph = rects["op"]
    cx, cy, cw, ch = rects["oq"]
    assert px <= cx and py <= cy and cx + cw <= px + pw and cy + ch <= py + ph


# ----------------------------------------------------------------------------- unresolved


def test_unresolved_references_become_placeholders_and_diagnostics_not_crashes() -> None:
    s = snap(
        [{"id": "a", "type": "business_actor"}, {"id": "w", "type": "warp_drive", "name": "W"}],
        [
            {"id": "oa", "concept_id": "a", "bounds": B(0, 0)},
            {"id": "ghost", "concept_id": "nope", "bounds": B(200, 0)},
            {"id": "weird", "concept_id": "w", "bounds": B(400, 0)},
            {"id": "orphan", "concept_id": "a", "parent_object_id": "missing", "bounds": B(0, 200)},
        ],
        [{"id": "rt", "type": "teleport", "source": "a", "target": "a"}],
        [
            {
                "id": "x-missing-rel",
                "relationship_id": "zzz",
                "source_object_id": "oa",
                "target_object_id": "ghost",
            },
            {
                "id": "x-unknown-type",
                "relationship_id": "rt",
                "source_object_id": "oa",
                "target_object_id": "weird",
            },
            {
                "id": "x-missing-end",
                "relationship_id": "zzz",
                "source_object_id": "oa",
                "target_object_id": "nowhere",
            },
            {
                "id": "x-cyc-1",
                "relationship_id": "zzz",
                "source_object_id": "oa",
                "target_object_id": "x-cyc-2",
            },
            {
                "id": "x-cyc-2",
                "relationship_id": "zzz",
                "source_object_id": "oa",
                "target_object_id": "x-cyc-1",
            },
        ],
    )
    res, root = render(s)
    ids = rule_ids(res)
    for rule in (
        "RENDER-AM-OBJ-001",
        "RENDER-AM-OBJ-002",
        "RENDER-AM-NEST-001",
        "RENDER-AM-CON-001",
        "RENDER-AM-CON-002",
        "RENDER-AM-CON-003",
        "RENDER-AM-CON-004",
    ):
        assert rule in ids, (rule, ids)
    assert {g.get("data-object-id") for g in by(root, kind="unresolved")} == {"ghost", "weird"}
    assert {g.get("data-connection-id") for g in by(root, kind="unresolved-connection")} == {
        "x-missing-rel",
        "x-unknown-type",
    }
    assert not by(root, connection_id="x-missing-end")
    assert one(root, object_id="orphan").get("data-kind") == "element"


def test_unknown_view_raises_not_found() -> None:
    with pytest.raises(RenderTargetNotFoundError):
        render_archimate_view(ArchimateSnapshot(), "v")
    with pytest.raises(RenderTargetNotFoundError):
        render_archimate_view(snap([], []), "other")


def test_empty_view_renders_a_valid_document() -> None:
    res, root = render(snap([], []))
    assert root.tag == NS + "svg"
    assert res.diagnostics == []


# ----------------------------------------------------------------------------- style vocabulary


def test_style_vocabulary_is_applied() -> None:
    s = snap(
        [{"id": "a", "type": "business_actor", "name": "Styled"}],
        [
            {
                "id": "oa",
                "concept_id": "a",
                "bounds": B(0, 0),
                "style": {
                    "fill_color": "#112233",
                    "fill_alpha": 50,
                    "line_color": "#445566",
                    "line_width": 3,
                    "font_color": "#778899",
                    "font_size": 12,
                    "font_style": ["bold", "italic", "underline"],
                    "font_name": "Courier New",
                    "text_alignment": "left",
                },
            }
        ],
    )
    res, root = render(s)
    # One pinned font: a different font_name is reported, never silently honoured.
    assert rule_ids(res) == ["RENDER-AM-STYLE-004"]
    assert res.diagnostics[0].evidence == {"font_name": "Courier New"}
    g = one(root, object_id="oa")
    assert g.get("fill-opacity") == "0.5"
    fill, outline = figure_nodes(g)
    assert fill.get("fill") == "#112233"
    assert outline.get("stroke") == "#445566" and outline.get("stroke-width") == "3"
    label = one(g, kind="label")
    assert label.get("fill") == "#778899"
    assert label.get("font-size") == "16"  # 12pt → 16px
    assert label.get("font-weight") == "bold" and label.get("font-style") == "italic"
    assert label.get("text-decoration") == "underline"
    assert label.get("font-family") is None  # inherits the document's DejaVu Sans
    assert label.get("text-anchor") == "start"
    assert label.get("fill-opacity") == "1", "label does not inherit the fill alpha"


def test_unknown_style_keys_and_bad_values_are_ignored_with_diagnostics() -> None:
    s = snap(
        [{"id": "a", "type": "business_actor"}],
        [
            {
                "id": "oa",
                "concept_id": "a",
                "bounds": B(0, 0),
                "style": {
                    "fillColor": "#ff0000",
                    "line_color": "red",
                    "notation": "sketch",
                    "font_style": ["blink"],
                },
            }
        ],
    )
    res, root = render(s)
    ids = rule_ids(res)
    assert ids.count("RENDER-AM-STYLE-001") == 3  # line_color, notation, font_style
    assert ids.count("RENDER-AM-STYLE-003") == 1  # fillColor is not in the vocabulary
    unknown = next(d for d in res.diagnostics if d.rule_id == "RENDER-AM-STYLE-003")
    assert unknown.evidence == {"keys": ["fillColor"]}
    g = one(root, object_id="oa")
    assert fills(g) == ["#ffffb5"]
    assert g.get("data-notation") == "box"


# ----------------------------------------------------------------------------- safety + determinism

HOSTILE = "</text><script>alert(1)</script><text>"


def _all_attr_values(root: ET.Element) -> list[str]:
    return [v for e in root.iter() for v in e.attrib.values()]


def test_hostile_names_ids_and_style_values_cannot_inject() -> None:
    s = snap(
        [
            {"id": "a", "type": "business_actor", "name": HOSTILE},
            {"id": 'b"><script>x</script>', "type": "business_role", "name": "&amp; <b>"},
        ],
        [
            {
                "id": "oa",
                "concept_id": "a",
                "bounds": B(0, 0),
                "style": {
                    "fill_color": "url(javascript:alert(1))",
                    "line_color": '#000" onload="x',
                },
            },
            {"id": "ob' onclick='x", "concept_id": 'b"><script>x</script>', "bounds": B(200, 0)},
        ],
        [
            {
                "id": "r",
                "type": "serving",
                "source": "a",
                "target": 'b"><script>x</script>',
                "name": "javascript:alert(1)",
            }
        ],
        [
            {
                "id": "x",
                "relationship_id": "r",
                "source_object_id": "oa",
                "target_object_id": "ob' onclick='x",
                "style": {"line_color": "expression(alert(1))"},
            }
        ],
    )
    res, root = render(s)
    tags = {e.tag.removeprefix(NS) for e in root.iter()}
    assert "script" not in tags and "foreignObject" not in tags
    for e in root.iter():
        assert not any(k.startswith("on") for k in e.attrib), e.attrib
    values = _all_attr_values(root)
    assert not any("javascript" in v.lower() or "expression(" in v for v in values)
    # The hostile name survives as TEXT, escaped, not as markup.
    assert HOSTILE in "".join(root.itertext())
    assert "&amp; <b>" in "".join(root.itertext())
    assert "RENDER-AM-STYLE-001" in rule_ids(res)
    assert "<script" not in res.svg


def test_sanitize_svg_strips_everything_outside_the_allowlist() -> None:
    dirty = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" onload="x()">'
        "<script>alert(1)</script>"
        "<style>@import url(http://evil)</style>"
        '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>'
        '<image href="http://evil/x.png"/>'
        '<a href="javascript:alert(1)"><rect width="1" height="1"/></a>'
        '<use xlink:href="http://evil#x"/>'
        '<rect width="5" height="5" fill="url(http://evil#g)" stroke="url(#ok)" style="fill:red" onclick="x"/>'
        '<path d="M0 0" marker-end="url(#m-1)" href="#local"/>'
        '<text font-family="Comic Sans" x="1">hi<tspan>there</tspan></text>'
        "</svg>"
    )
    out = sanitize_svg(dirty)
    root = ET.fromstring(out)
    tags = [e.tag.removeprefix(NS) for e in root.iter()]
    assert tags == ["svg", "rect", "path", "text", "tspan"]
    rect = root.find(NS + "rect")
    assert rect is not None
    assert "fill" not in rect.attrib and "style" not in rect.attrib and "onclick" not in rect.attrib
    assert rect.get("stroke") == "url(#ok)"
    path = root.find(NS + "path")
    assert (
        path is not None and path.get("marker-end") == "url(#m-1)" and path.get("href") == "#local"
    )
    text = root.find(NS + "text")
    assert text is not None and "font-family" not in text.attrib
    assert "onload" not in root.attrib
    assert sanitize_svg(out) == out, "sanitizing is idempotent"


@pytest.mark.parametrize(
    "bad",
    [
        '<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
        "<svg><rect/></svg>",  # no SVG namespace
        '<html xmlns="http://www.w3.org/2000/svg"/>',
        "not xml",
    ],
)
def test_sanitize_svg_refuses_unsafe_or_foreign_documents(bad: str) -> None:
    with pytest.raises(UnsafeSvgError):
        sanitize_svg(bad)


def test_output_is_already_sanitized_and_has_no_external_references() -> None:
    res, root = render(two_box_view({"type": "realization"}))
    assert sanitize_svg(res.svg) == res.svg
    for v in _all_attr_values(root):
        for m in re.findall(r"url\(([^)]*)\)", v):
            assert m.startswith("#")
    assert root.get("font-family") == "DejaVu Sans, sans-serif"


def _big_snapshot() -> ArchimateSnapshot:
    types = sorted(REGISTRY.concepts)
    concepts = [{"id": f"c{i}", "type": t, "name": f"{t} ✓ ü"} for i, t in enumerate(types)]
    objs = [{"id": f"o{i}", "concept_id": f"c{i}"} for i in range(len(types))]
    rels = [
        {"id": f"r{i}", "type": t, "source": "c0", "target": f"c{i + 1}"}
        for i, t in enumerate(sorted(EXPECTED))
    ]
    conns = [
        {
            "id": f"x{i}",
            "relationship_id": f"r{i}",
            "source_object_id": "o0",
            "target_object_id": f"o{i + 1}",
        }
        for i in range(len(rels))
    ]
    return snap(concepts, objs, rels, conns)


def test_rendering_is_deterministic() -> None:
    s = _big_snapshot()
    a = render_archimate_view(s, "v")
    b = render_archimate_view(ArchimateSnapshot.model_validate(s.model_dump(mode="json")), "v")
    assert a.svg == b.svg
    assert [d.model_dump() for d in a.diagnostics] == [d.model_dump() for d in b.diagnostics]
    assert a.renderer_version == "archimate-svg/1.0.0"
    root = ET.fromstring(a.svg)
    assert root.get("data-renderer") == a.renderer_version


def test_options_change_output_predictably() -> None:
    s = two_box_view({"type": "serving"})
    plain = ET.fromstring(render_archimate_view(s, "v").svg)
    roomy = ET.fromstring(
        render_archimate_view(s, "v", options=RenderOptions(margin=50, background=None)).svg
    )
    assert float(roomy.get("width", "0")) == float(plain.get("width", "0")) + 60
    assert not by(roomy, kind="background") and by(plain, kind="background")


def test_png_rasterisation() -> None:
    png = render_png(render_archimate_view(_big_snapshot(), "v").svg)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000
    with pytest.raises(ValueError):
        render_png("<svg/>", zoom=0)


# ----------------------------------------------------------------------------- notes, groups, labels, lines


def test_notes_draw_archi_borders_with_wrapped_text() -> None:
    body = "A note that is long enough to wrap onto several lines of text"
    s = snap(
        [],
        [
            {"id": "n1", "kind": "note", "text": body, "bounds": B(0, 0, 150, 80)},
            {
                "id": "n2",
                "kind": "note",
                "text": "boxed",
                "border_type": "rectangle",
                "bounds": B(200, 0),
            },
            {
                "id": "n3",
                "kind": "note",
                "text": "bare",
                "border_type": "none",
                "bounds": B(400, 0),
            },
            {
                "id": "n4",
                "kind": "note",
                "text": "odd",
                "border_type": "cloud",
                "bounds": B(600, 0),
            },
        ],
    )
    res, root = render(s)
    assert rule_ids(res) == ["RENDER-AM-STYLE-001"]  # n4's unknown border_type
    dogear = one(root, object_id="n1")
    assert dogear.get("data-kind") == "note" and dogear.get("data-border") == "dogear"
    fill, outline = figure_nodes(dogear)
    assert fill.get("fill") == "#ffffff"
    assert path_points(outline.get("d", "")) == [(0, 0), (150, 0), (150, 67), (137, 80), (0, 80)]
    label = one(dogear, kind="label")
    assert len(label.findall(NS + "tspan")) >= 2, "note text wraps"
    assert " ".join(t.text or "" for t in label.findall(NS + "tspan")) == body
    assert label.get("text-anchor") == "start"
    assert [e.tag.removeprefix(NS) for e in figure_nodes(one(root, object_id="n2"))] == [
        "rect",
        "rect",
    ]
    assert all(e.get("stroke") in (None, "none") for e in figure_nodes(one(root, object_id="n3")))
    assert one(root, object_id="n4").get("data-border") == "dogear"


def test_visual_groups_are_tabbed_or_rectangular_and_hold_children() -> None:
    s = snap(
        [{"id": "a", "type": "business_actor", "name": "A"}],
        [
            {"id": "g1", "kind": "group", "name": "Customers", "bounds": B(0, 0, 300, 160)},
            {"id": "oa", "concept_id": "a", "parent_object_id": "g1", "bounds": B(20, 40)},
            {
                "id": "g2",
                "kind": "group",
                "name": "Plain",
                "border_type": "rectangle",
                "bounds": B(400, 0, 200, 100),
            },
            {"id": "g3", "kind": "group", "name": "Auto"},
            {"id": "ob", "concept_id": "a", "parent_object_id": "g3"},
        ],
    )
    res, root = render(s)
    assert [d.rule_id for d in res.diagnostics] == ["RENDER-AM-LAYOUT-001"]
    g1 = one(root, object_id="g1")
    assert g1.get("data-kind") == "group" and g1.get("data-border") == "tabbed"
    tab_fill = figure_nodes(g1)[0]
    assert tab_fill.get("fill") == "#bdc1c1"  # Archi's darker group colour for the tab
    assert float(tab_fill.get("height", "0")) == 18
    assert "".join(one(g1, kind="label").itertext()) == "Customers"
    child = one(root, object_id="oa")
    assert (child.get("data-x"), child.get("data-y")) == ("20", "40")
    assert one(root, object_id="g2").get("data-border") == "rectangle"
    # synthesized group contains its synthesized child
    g3, ob = one(root, object_id="g3"), one(root, object_id="ob")
    gx, gy, gw, gh = (float(g3.get(k, "0")) for k in ("data-x", "data-y", "data-w", "data-h"))
    cx, cy, cw, ch = (float(ob.get(k, "0")) for k in ("data-x", "data-y", "data-w", "data-h"))
    assert gx < cx and gy < cy and cx + cw <= gx + gw and cy + ch <= gy + gh


@pytest.mark.parametrize(
    ("expr", "shown"),
    [
        ("${name}", "Customer"),
        ("${type}", "Business Actor"),
        ("${documentation}", "Buys things"),
        ("${property:owner}", "Sales"),
        ("${property:missing}", ""),
        ("plain text", "plain text"),
        ("${name} (${type}) - ${property:owner}", "Customer (Business Actor) - Sales"),
    ],
)
def test_element_label_expressions(expr: str, shown: str) -> None:
    s = snap(
        [
            {
                "id": "a",
                "type": "business_actor",
                "name": "Customer",
                "documentation": "Buys things",
                "properties": [("owner", "Sales"), ("owner", "second")],
            }
        ],
        [{"id": "oa", "concept_id": "a", "label": expr, "bounds": B(0, 0, 300, 55)}],
    )
    res, root = render(s)
    assert res.diagnostics == []
    labels = by(one(root, object_id="oa"), kind="label")
    assert "".join("".join(t.itertext()) for t in labels) == shown


def test_unsupported_label_expression_is_shown_verbatim_with_advice() -> None:
    s = snap(
        [{"id": "a", "type": "business_actor", "name": "X"}],
        [
            {
                "id": "oa",
                "concept_id": "a",
                "label": "${viewpoint} ${name}",
                "bounds": B(0, 0, 300, 55),
            }
        ],
    )
    res, root = render(s)
    assert rule_ids(res) == ["RENDER-AM-LABEL-001"]
    assert "".join(one(root, kind="label").itertext()) == "${viewpoint} X"


def test_line_connections_join_any_objects_and_carry_labels() -> None:
    s = snap(
        [{"id": "a", "type": "business_actor"}, {"id": "b", "type": "business_role"}],
        [
            {"id": "oa", "concept_id": "a", "bounds": B(0, 0)},
            {"id": "ob", "concept_id": "b", "bounds": B(300, 0)},
            {"id": "n", "kind": "note", "text": "why", "bounds": B(150, 150)},
        ],
        [{"id": "r", "type": "assignment", "source": "a", "target": "b", "name": "plays"}],
        [
            {
                "id": "x",
                "relationship_id": "r",
                "source_object_id": "oa",
                "target_object_id": "ob",
                "label": "${name} [${type}]",
            },
            {
                "id": "l1",
                "kind": "line",
                "source_object_id": "n",
                "target_object_id": "oa",
                "label": "see",
            },
            {"id": "l2", "kind": "line", "source_object_id": "n", "target_object_id": "x"},
        ],
    )
    res, root = render(s)
    assert res.diagnostics == []
    l1 = one(root, connection_id="l1")
    assert l1.get("data-kind") == "line"
    path = line_of(l1)
    assert path.get("marker-start") is None and path.get("marker-end") is None
    assert path.get("stroke-dasharray") is None
    assert "".join(one(l1, kind="label").itertext()) == "see"
    l2 = one(root, connection_id="l2")
    assert path_points(line_of(l2).get("d", ""))[-1] == (210, 27.5)  # midpoint of x
    assert not by(l2, kind="label")
    rel_label = one(one(root, connection_id="x"), kind="label")
    assert "".join(rel_label.itertext()) == "plays [Assignment relationship]"


# ----------------------------------------------------------------------------- layout + routing


def _layered_model(n: int = 30) -> ArchimateSnapshot:
    types = [
        "business_actor",
        "business_process",
        "application_component",
        "application_service",
        "node",
        "goal",
        "capability",
    ]
    concepts = [
        {"id": f"c{i}", "type": types[i % len(types)], "name": f"Element {i}"} for i in range(n)
    ]
    concepts.append({"id": "grp", "type": "grouping", "name": "Group"})
    objs = [{"id": f"o{i}", "concept_id": f"c{i}"} for i in range(n)]
    objs.append({"id": "og", "concept_id": "grp"})
    objs += [{"id": f"k{i}", "concept_id": f"c{i}", "parent_object_id": "og"} for i in range(5)]
    rels = [
        {"id": f"r{i}", "type": "association", "source": f"c{i}", "target": f"c{(i * 7 + 3) % n}"}
        for i in range(n)
    ]
    conns = [
        {
            "id": f"x{i}",
            "relationship_id": f"r{i}",
            "source_object_id": f"o{i}",
            "target_object_id": f"o{(i * 7 + 3) % n}",
        }
        for i in range(n)
    ]
    return snap(concepts, objs, rels, conns)


def _rects(root: ET.Element) -> dict[str, tuple[float, float, float, float]]:
    return {
        g.get("data-object-id", ""): tuple(
            float(g.get(k, "0")) for k in ("data-x", "data-y", "data-w", "data-h")
        )  # type: ignore[misc]
        for g in by(root, kind="element")
    }


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def test_layered_layout_has_no_overlaps_rows_by_layer_and_children_inside_parents() -> None:
    res, root = render(_layered_model())
    rects = _rects(root)
    top = [k for k in rects if k.startswith("o")]
    for i, a in enumerate(top):
        for b in top[i + 1 :]:
            assert not _overlap(rects[a], rects[b]), (a, b)
    kids = [k for k in rects if k.startswith("k")]
    for i, a in enumerate(kids):
        gx, gy, gw, gh = rects["og"]
        x, y, w, h = rects[a]
        assert gx < x and gy < y and x + w <= gx + gw and y + h <= gy + gh
        for b in kids[i + 1 :]:
            assert not _overlap(rects[a], rects[b])
    layer_order = ["motivation", "strategy", "business", "application", "technology"]
    by_layer: dict[str, list[float]] = {}
    for g in by(root, kind="element"):
        if g.get("data-object-id", "").startswith("o") and g.get("data-layer") in layer_order:
            by_layer.setdefault(g.get("data-layer", ""), []).append(float(g.get("data-y", "0")))
    tops = [(min(by_layer[layer]), max(by_layer[layer])) for layer in layer_order]
    for (_, upper_bottom), (lower_top, _) in zip(tops, tops[1:], strict=False):
        assert upper_bottom < lower_top, "each ArchiMate layer is a band below the previous"
    assert render_archimate_view(_layered_model(), "v").svg == res.svg


def test_assign_ranks_and_barycenter_ordering_are_deterministic() -> None:
    ranks = assign_ranks(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "a"), ("a", "d")])
    assert ranks == {"a": 0, "b": 1, "c": 2, "d": 1}  # cycle broken at the back edge c→a
    nodes = [("top1", 50, 20), ("top2", 50, 20), ("x", 50, 20), ("y", 50, 20)]
    edges = [("top1", "y"), ("top2", "x")]
    pos = layered_layout(nodes, edges, ranks={"top1": 0, "top2": 0, "x": 1, "y": 1})
    assert pos["y"][0] < pos["x"][0], "barycenter puts y under top1, x under top2"
    assert pos == layered_layout(nodes, edges, ranks={"top1": 0, "top2": 0, "x": 1, "y": 1})


def test_auto_layout_routes_around_an_unrelated_box() -> None:
    s = snap(
        [
            {"id": "a", "type": "business_actor", "name": "A"},
            {"id": "b", "type": "business_role", "name": "B"},
            {"id": "c", "type": "business_object", "name": "C"},
        ],
        [
            {"id": "oa", "concept_id": "a"},
            {"id": "ob", "concept_id": "b"},
            {"id": "oc", "concept_id": "c"},
        ],
        [{"id": "r", "type": "association", "source": "a", "target": "c"}],
        [{"id": "x", "relationship_id": "r", "source_object_id": "oa", "target_object_id": "oc"}],
    )
    _, root = render(s)
    rects = _rects(root)
    assert rects["oa"][1] == rects["ob"][1] == rects["oc"][1], "one business row"
    pts = path_points(line_of(one(root, connection_id="x")).get("d", ""))
    assert len(pts) > 2, "a detour was taken"
    bx, by_, bw, bh = rects["ob"]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
        assert x1 == x2 or y1 == y2, "the detour is orthogonal"
        crosses = (
            min(x1, x2) < bx + bw
            and max(x1, x2) > bx
            and min(y1, y2) < by_ + bh
            and max(y1, y2) > by_
        )
        assert not crosses, "the line does not pass through B"


def test_stored_layout_is_never_rerouted() -> None:
    s = snap(
        [
            {"id": "a", "type": "business_actor"},
            {"id": "b", "type": "business_role"},
            {"id": "c", "type": "business_object"},
        ],
        [
            {"id": "oa", "concept_id": "a", "bounds": B(0, 0)},
            {"id": "ob", "concept_id": "b", "bounds": B(200, 0)},
            {"id": "oc", "concept_id": "c", "bounds": B(400, 0)},
        ],
        [{"id": "r", "type": "association", "source": "a", "target": "c"}],
        [{"id": "x", "relationship_id": "r", "source_object_id": "oa", "target_object_id": "oc"}],
    )
    _, root = render(s)
    assert path_points(line_of(one(root, connection_id="x")).get("d", "")) == [
        (120, 27.5),
        (400, 27.5),
    ]


def test_route_keeps_straight_lines_and_gives_up_without_a_simple_detour() -> None:
    a, c = Rect(0, 0, 50, 20), Rect(200, 0, 50, 20)
    assert route(a, a.center, c, c.center, [Rect(100, 100, 20, 20)]) == []
    walls = [Rect(100, -5000, 20, 10000)]  # no L/Z detour exists
    assert route(a, a.center, c, c.center, walls) == []


# ----------------------------------------------------------------------------- font + PNG determinism


def test_documents_name_the_bundled_font_and_measure_text_with_it() -> None:
    _, root = render(two_box_view({"type": "serving"}))
    assert root.get("font-family") == FONT_FAMILY == "DejaVu Sans, sans-serif"
    for f in FONT_FILES:
        assert f.is_file()
    assert (FONT_FILES[0].parent / "LICENSE").is_file()
    # advance widths come from the font, not a guess: 'W' is wider than 'i'
    assert text_width("W", 10) > 3 * text_width("i", 10)


def test_png_bytes_are_identical_across_renders() -> None:
    svg = render_archimate_view(_big_snapshot(), "v").svg
    assert render_png(svg) == render_png(svg)


# ----------------------------------------------------------------------------- text fitting

LONG_WORD = "Supercalifragilisticexpialidocious-12345"  # 40 characters


def _text_lines(t: ET.Element) -> list[str]:
    spans = t.findall(NS + "tspan")
    return [s.text or "" for s in spans] if spans else [t.text or ""]


def _text_box(t: ET.Element) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) of a <text> from its own attributes + font metrics."""
    fs = float(t.get("font-size", "0"))
    lines = _text_lines(t)
    width = max(text_width(s, fs, bold=t.get("font-weight") == "bold") for s in lines)
    x, y = float(t.get("x", "0")), float(t.get("y", "0"))
    anchor = t.get("text-anchor", "start")
    left = x - width / 2 if anchor == "middle" else (x - width if anchor == "end" else x)
    top = y - fs
    return left, top, left + width, top + fs * LINE_HEIGHT * len(lines)


@pytest.mark.parametrize("notation", ["box", "icon"])
def test_a_40_character_word_never_overflows_a_120px_box(notation: str) -> None:
    assert len(LONG_WORD) == 40
    s = snap(
        [{"id": "a", "type": "business_actor", "name": LONG_WORD}],
        [
            {
                "id": "oa",
                "concept_id": "a",
                "bounds": B(0, 0, 120, 55),
                "style": {"notation": notation},
            }
        ],
    )
    res, root = render(s)
    assert res.diagnostics == []
    label = one(one(root, object_id="oa"), kind="label")
    assert "".join(_text_lines(label)) == LONG_WORD, "broken by character, nothing lost"
    assert len(_text_lines(label)) >= 2
    left, top, right, bottom = _text_box(label)
    assert 0 <= left and right <= 120 and 0 <= top and bottom <= 55
    assert label.get("data-truncated") is None


def test_text_that_cannot_fit_is_ellipsized_with_a_diagnostic() -> None:
    s = snap(
        [{"id": "a", "type": "business_actor", "name": LONG_WORD * 7}],
        [{"id": "oa", "concept_id": "a", "bounds": B(0, 0, 120, 55)}],
    )
    res, root = render(s)
    assert rule_ids(res) == ["RENDER-AM-TEXT-001"]
    label = one(one(root, object_id="oa"), kind="label")
    assert label.get("data-truncated") == "true" and _text_lines(label)[-1].endswith("…")
    left, top, right, bottom = _text_box(label)
    assert 0 <= left and right <= 120 and 0 <= top and bottom <= 55


@pytest.mark.parametrize(
    "concept_type", sorted(t for t in REGISTRY.concepts if t not in ("junction", "grouping"))
)
def test_icon_notation_label_sits_below_the_figure(concept_type: str) -> None:
    s = snap(
        [{"id": "c", "type": concept_type, "name": "A reasonably long element name"}],
        [
            {
                "id": "o",
                "concept_id": "c",
                "bounds": B(10, 10, 120, 80),
                "style": {"notation": "icon"},
            }
        ],
    )
    res, root = render(s)
    assert res.diagnostics == []
    g = one(root, object_id="o")
    x0, y0, x1, y1 = (float(v) for v in one(g, part="figure").get("data-bbox", "").split())
    left, top, right, bottom = _text_box(one(g, kind="label"))
    assert top >= y1, "the label starts below the figure's bounding box"
    assert 10 <= left and right <= 130 and bottom <= 90, "and stays inside the object"


# ----------------------------------------------------------------------------- connection geometry


def _nested_model() -> ArchimateSnapshot:
    """The 'Sales' corner of the relationships sample: nesting, self loop, bends."""
    return snap(
        [
            {"id": "grp", "type": "grouping", "name": "Sales"},
            {"id": "actor", "type": "business_actor", "name": "Customer"},
            {"id": "role", "type": "business_role", "name": "Buyer"},
            {"id": "proc", "type": "business_process", "name": "Order"},
        ],
        [
            {"id": "o-grp", "concept_id": "grp", "bounds": B(20, 20, 330, 120)},
            {
                "id": "o-actor",
                "concept_id": "actor",
                "parent_object_id": "o-grp",
                "bounds": B(10, 40),
            },
            {
                "id": "o-role",
                "concept_id": "role",
                "parent_object_id": "o-grp",
                "bounds": B(190, 40),
            },
            {"id": "o-proc", "concept_id": "proc", "bounds": B(400, 60)},
        ],
        [
            {"id": "r-agg", "type": "aggregation", "source": "grp", "target": "actor"},
            {"id": "r-agg2", "type": "aggregation", "source": "grp", "target": "role"},
            {"id": "r-asg", "type": "assignment", "source": "actor", "target": "role"},
            {"id": "r-spc", "type": "specialization", "source": "role", "target": "role"},
            {"id": "r-in", "type": "composition", "source": "proc", "target": "grp"},
        ],
        [
            {
                "id": "x-agg",
                "relationship_id": "r-agg",
                "source_object_id": "o-grp",
                "target_object_id": "o-actor",
            },
            {
                "id": "x-agg2",
                "relationship_id": "r-agg2",
                "source_object_id": "o-role",
                "target_object_id": "o-grp",
            },
            {
                "id": "x-asg",
                "relationship_id": "r-asg",
                "source_object_id": "o-actor",
                "target_object_id": "o-role",
            },
            {
                "id": "x-spc",
                "relationship_id": "r-spc",
                "source_object_id": "o-role",
                "target_object_id": "o-role",
            },
            {
                "id": "x-in",
                "relationship_id": "r-in",
                "source_object_id": "o-proc",
                "target_object_id": "o-grp",
            },
            {
                "id": "x-bend",
                "relationship_id": "r-agg",
                "source_object_id": "o-grp",
                "target_object_id": "o-actor",
                "bendpoints": [{"x": 90, "y": 125}],
            },
        ],
    )


def _crosses_interior(a: tuple[float, float], b: tuple[float, float], r: tuple[float, ...]) -> bool:
    return route_seg_hits(a, b, Rect(*r))


def _on_border(p: tuple[float, float], r: tuple[float, ...]) -> bool:
    x, y, w, h = r
    inside = x - 0.01 <= p[0] <= x + w + 0.01 and y - 0.01 <= p[1] <= y + h + 0.01
    on_edge = min(abs(p[0] - x), abs(p[0] - x - w), abs(p[1] - y), abs(p[1] - y - h)) < 0.02
    return inside and on_edge


@pytest.mark.parametrize("model", ["nested", "big", "layered"])
def test_no_connection_is_drawn_through_its_own_end_boxes(model: str) -> None:
    s = {"nested": _nested_model, "big": _big_snapshot, "layered": _layered_model}[model]()
    _, root = render(s)
    rects = _rects(root)
    junctions = {
        g.get("data-object-id") for g in by(root, kind="element") if g.get("data-junction")
    }
    checked = 0
    for g in by(root, kind="connection"):
        pts = path_points(line_of(g).get("d", ""))
        ends = [(g.get("data-source", ""), pts[0]), (g.get("data-target", ""), pts[-1])]
        for (oid, endpoint), (other, _) in (ends, ends[::-1]):
            if oid not in rects:
                continue  # an end on another connection
            box = rects[oid]
            if oid not in junctions:
                assert _on_border(endpoint, box), (g.get("data-connection-id"), oid, endpoint, box)
            other_box = rects.get(other)
            nested_inside = other_box is not None and (
                box[0] <= other_box[0]
                and box[1] <= other_box[1]
                and other_box[0] + other_box[2] <= box[0] + box[2]
                and other_box[1] + other_box[3] <= box[1] + box[3]
            )
            if nested_inside or oid == other:
                continue  # the line necessarily runs inside its container / loops out
            for a, b in zip(pts, pts[1:], strict=False):
                assert not _crosses_interior(a, b, box), (g.get("data-connection-id"), oid, a, b)
            checked += 1
    assert checked > 3


def test_a_connection_to_a_nested_child_leaves_by_the_nearest_container_border() -> None:
    _, root = render(_nested_model())
    rects = _rects(root)
    actor = rects["o-actor"]  # absolute (30, 60, 120, 55) inside the group at (20, 20)
    assert actor[:2] == (30, 60)
    pts = path_points(line_of(one(root, connection_id="x-agg")).get("d", ""))
    # the group border nearest Customer is the bottom one (25px away): straight down to it
    assert pts == [(90, 140), (90, 115)]
    pts2 = path_points(line_of(one(root, connection_id="x-agg2")).get("d", ""))
    # child as SOURCE: from Buyer's bottom edge down to the group's nearest (bottom) border
    assert pts2 == [(270, 115), (270, 140)]
    bend = path_points(line_of(one(root, connection_id="x-bend")).get("d", ""))
    assert bend[0] == (90, 140) and bend[1] == (90, 125) and bend[-1] == (90, 115)


def test_self_loop_leaves_the_right_edge_and_re_enters_the_top_edge() -> None:
    _, root = render(_nested_model())
    x, y, w, h = _rects(root)["o-role"]
    pts = path_points(line_of(one(root, connection_id="x-spc")).get("d", ""))
    assert pts[0] == (x + w, y + h * 0.3) and pts[-1] == (x + w * 0.7, y)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
        assert x1 == x2 or y1 == y2
        assert not _crosses_interior((x1, y1), (x2, y2), (x, y, w, h))
