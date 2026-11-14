"""Archi 5.10.0 as a compatibility oracle for tools/architecture/exchange.py.

Archi cannot run in CI, so its outputs are pinned fixtures (provenance, commands
and hashes in ``tests/fixtures/exchange/SOURCE.md``):

- ``archi510_full_reexport.xml``  -- Archi import + export of our export of
  :func:`full_snapshot` (every concept, relationship and view construct).
- ``archi510_probe_reexport.xml`` -- the same for :func:`probe_snapshot` (edge
  cases: whitespace, unicode, deep nesting, connection-to-connection, every
  standard viewpoint, every style key). Archi's file is XSD-invalid (upstream).
- ``archi510_native_export.xml``  -- Archi's export of ``archi510_native.archimate``,
  a hand-written Archi model using Archi-only features.

Every difference between what went in and what Archi gave back is listed below
with the Archi behaviour that causes it; the tests fail on any difference that
is not listed, and on any listed difference that stops happening.

The input hashes are pinned too: if this module's export of either builder
changes, the fixtures are stale and must be regenerated with Archi.

Also here: one triggering case per stable loss code, so every code is tested.
"""

from __future__ import annotations

import ast
import hashlib
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import tools.architecture.exchange as exchange_module
from schemas.modeling import (
    ArchimateConceptIn,
    ArchimateExchangeContent,
    ArchimateOrganization,
    ArchimateRelationshipIn,
    ArchimateSnapshot,
    ArchimateViewConnectionIn,
    ArchimateViewIn,
    ArchimateViewObjectIn,
    Bounds,
    Point,
)
from tests.test_archimate_exchange import full_snapshot
from tools.architecture.exchange import (
    EXCHANGE_VIEWPOINTS,
    LOSS_CODES,
    NS,
    ExchangeLoss,
    export_exchange,
    export_exchange_report,
    import_exchange,
    schema_errors,
)
from tools.architecture.registry import get_registry

FIXTURES = Path(__file__).parent / "fixtures" / "exchange"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

ARCHI_FIXTURE_SHA256 = {
    "archi510_full_reexport.xml": "3c6816972519fdcd9111710b27e0cca54571c03789215c2ae8530f26f1399051",
    "archi510_probe_reexport.xml": "2d3e280a887a59590b8ed35285f108c05dbf2e010757405f47a93ee6dbd19d75",
    "archi510_native.archimate": "27931f719894800a293c1d199f8ef773b8b802465ee8cacc8d2785abe4588c28",
    "archi510_native_export.xml": "a28a4980bdf8e8bc6fe1319a06077d99a1825d0389896f6dc3c798ba58098b7d",
}
# sha256 of the exports that were fed to Archi to produce the *_reexport fixtures.
FULL_INPUT_SHA256 = "fc6794b11c5220cda7059a314589c9fa6f763d62e5cc773326c2c88d4887443e"
PROBE_INPUT_SHA256 = "c38b7f7b9865091d57d35d5a59489cad63d123f84b51f08e3dc468c0db81cea2"

# Archi's effective defaults, materialised on every node/connection it exports.
# "Lucida Grande" is Archi's default font on macOS, where the fixtures were made.
ARCHI_FONT = {"font_name": "Lucida Grande", "font_size": 12, "font_color": "#000000"}
ARCHI_CONNECTION = {"line_color": "#000000", **ARCHI_FONT}


def _archi_node(fill: str) -> dict[str, Any]:
    return {
        "fill_color": fill,
        "fill_alpha": 100,
        "line_color": "#5c5c5c",
        "line_alpha": 100,
        **ARCHI_FONT,
    }


def full_input() -> bytes:
    return export_exchange(full_snapshot(), model_id="m-1", name="Full", documentation="all of it")


def probe_snapshot() -> ArchimateSnapshot:
    """Edge cases for the Archi round trip; exports with zero losses."""
    reg = get_registry()
    vp_ids = [vp.id for vp in reg.viewpoints.values() if vp.name in EXCHANGE_VIEWPOINTS]
    concepts = [
        ArchimateConceptIn(id="n-ws", type="business_actor", name="  two  spaces\tand tab  "),
        ArchimateConceptIn(id="n-nl", type="business_role", name="line one\nline two"),
        ArchimateConceptIn(
            id="n-uni",
            type="business_object",
            name="Ünïcödé → 😀 <&> \"q\" 'a'",
            documentation="crlf\r\nline\ttab  end  ",
        ),
        ArchimateConceptIn(id="n-empty", type="business_service", name=""),
        ArchimateConceptIn(
            id="n-prop",
            type="node",
            name="P",
            properties=[("k", ""), ("", "empty key"), ("k sp/é", "v\nmulti")],
        ),
        ArchimateConceptIn(
            id="n-jname",
            type="junction",
            junction_type="or",
            name="named junction",
            documentation="jdoc",
        ),
        ArchimateConceptIn(id="n-deep1", type="location", name="L1"),
        ArchimateConceptIn(id="n-deep2", type="location", name="L2"),
        ArchimateConceptIn(id="n-deep3", type="location", name="L3"),
    ]
    rels = [
        ArchimateRelationshipIn(
            id="r-inf", type="influence", source="n-deep1", target="n-deep2", strength="+/- custom"
        ),
        ArchimateRelationshipIn(id="r-agg", type="aggregation", source="n-deep1", target="n-deep2"),
        ArchimateRelationshipIn(
            id="r-rr", type="association", source="r-inf", target="r-agg", name="rel to rel"
        ),
        ArchimateRelationshipIn(id="r-j", type="association", source="n-ws", target="n-jname"),
    ]
    objects = [
        ArchimateViewObjectIn(
            id="o1",
            concept_id="n-deep1",
            bounds=Bounds(x=5, y=5, w=600, h=400),
            style={"font_style": ["underline"], "font_alpha": 50, "font_color": "#010203"},
        ),
        ArchimateViewObjectIn(
            id="o2",
            concept_id="n-deep2",
            parent_object_id="o1",
            bounds=Bounds(x=10, y=30, w=400, h=300),
            style={
                "font_style": ["plain"],
                "line_color": "#abcdef",
                "line_alpha": 30,
                "line_width": 1,
            },
        ),
        ArchimateViewObjectIn(
            id="o3",
            concept_id="n-deep3",
            parent_object_id="o2",
            bounds=Bounds(x=15, y=35, w=200, h=100),
            style={
                "font_style": ["bold", "italic", "underline"],
                "font_size": 14,
                "fill_color": "#000000",
                "fill_alpha": 0,
            },
        ),
        ArchimateViewObjectIn(
            id="o4",
            concept_id="n-ws",
            parent_object_id="o3",
            bounds=Bounds(x=1, y=2, w=50, h=40),
            style={"line_width": 3, "line_color": "#00ff00"},
        ),
        ArchimateViewObjectIn(
            id="o5", concept_id="n-jname", bounds=Bounds(x=700, y=10, w=15, h=15)
        ),
        ArchimateViewObjectIn(
            id="o6", concept_id="n-uni", bounds=Bounds(x=700, y=100, w=120, h=55)
        ),
        ArchimateViewObjectIn(
            id="o-note2",
            kind="note",
            text="probe note",
            border_type="none",
            bounds=Bounds(x=900, y=10, w=185, h=80),
        ),
        ArchimateViewObjectIn(
            id="o-grp2",
            kind="group",
            name="G2",
            border_type="tabbed",
            bounds=Bounds(x=900, y=200, w=200, h=150),
        ),
        ArchimateViewObjectIn(
            id="o7",
            concept_id="n-empty",
            parent_object_id="o-grp2",
            label="${type}",
            bounds=Bounds(x=20, y=40, w=120, h=55),
        ),
    ]
    connections = [
        ArchimateViewConnectionIn(
            id="c-agg",
            relationship_id="r-agg",
            source_object_id="o1",
            target_object_id="o2",
            bendpoints=[Point(x=1, y=1)],
            style={
                "line_color": "#123456",
                "line_alpha": 40,
                "font_style": ["underline"],
                "font_alpha": 70,
                "font_color": "#654321",
            },
        ),
        ArchimateViewConnectionIn(
            id="c-inf",
            relationship_id="r-inf",
            source_object_id="o1",
            target_object_id="o2",
            style={"line_width": 1},
        ),
        ArchimateViewConnectionIn(
            id="c-rr",
            relationship_id="r-rr",
            source_object_id="c-inf",
            target_object_id="c-agg",
            bendpoints=[Point(x=300, y=300), Point(x=310, y=320)],
        ),
        ArchimateViewConnectionIn(
            id="c-j",
            relationship_id="r-j",
            source_object_id="o4",
            target_object_id="o5",
            label="to junction",
        ),
        # Lines: note -> element, element -> element, note -> connection.
        ArchimateViewConnectionIn(
            id="l-ne", kind="line", source_object_id="o-note2", target_object_id="o6"
        ),
        ArchimateViewConnectionIn(
            id="l-ee", kind="line", source_object_id="o5", target_object_id="o6", label="ee"
        ),
        ArchimateViewConnectionIn(
            id="l-nc", kind="line", source_object_id="o-note2", target_object_id="c-j"
        ),
        ArchimateViewConnectionIn(
            id="l-gn", kind="line", source_object_id="o-grp2", target_object_id="o-note2"
        ),
    ]
    views = [
        ArchimateViewIn(
            id="v-probe",
            name="Probe  view",
            objects=objects,
            connections=connections,
            properties=[("k", "view")],
        )
    ]
    views += [
        ArchimateViewIn(id=f"vp-{i}", name=f"VP {v}", viewpoint=v) for i, v in enumerate(vp_ids)
    ]
    organizations = [
        ArchimateOrganization(
            id="fold-root",
            name="Probe folders",
            documentation="root doc",
            items=["n-ws"],
            children=[
                ArchimateOrganization(
                    name="Nested",
                    items=["n-nl", "r-inf"],
                    children=[
                        ArchimateOrganization(id="fold-deep", name="Deeper", items=["v-probe"])
                    ],
                )
            ],
        ),
        ArchimateOrganization(name="Business", items=["n-uni"]),
    ]
    return ArchimateSnapshot(
        concepts=concepts, relationships=rels, views=views, organizations=organizations
    )


def probe_input() -> bytes:
    return export_exchange(probe_snapshot(), model_id="probe", name="Probe", language="nl")


Diff = dict[tuple[str, str, str], tuple[Any, Any]]


def _canon(value: Any) -> Any:
    """Style dicts compare by content: key order is not part of the meaning."""
    return dict(sorted(value.items())) if isinstance(value, dict) else value


def semantic_diff(a: ArchimateSnapshot, b: ArchimateSnapshot) -> Diff:
    """Field-level differences keyed (kind, id, field); list order is its own entry."""
    out: Diff = {}

    def compare(kind: str, xs: list[Any], ys: list[Any], skip: frozenset[str] = frozenset()):
        ax = {x.id: x.model_dump(exclude=set(skip)) for x in xs}
        bx = {y.id: y.model_dump(exclude=set(skip)) for y in ys}
        for k in sorted(set(ax) | set(bx)):
            if k not in ax or k not in bx:
                out[(kind, k, "<presence>")] = (k in ax, k in bx)
                continue
            for f in sorted(set(ax[k]) | set(bx[k])):
                if ax[k].get(f) != bx[k].get(f):
                    out[(kind, k, f)] = (_canon(ax[k].get(f)), _canon(bx[k].get(f)))
        if [x.id for x in xs if x.id in bx] != [y.id for y in ys if y.id in ax]:
            out[(kind, "*", "<order>")] = ([x.id for x in xs], [y.id for y in ys])

    compare("concept", a.concepts, b.concepts)
    compare("relationship", a.relationships, b.relationships)
    va, vb = list(a.views or []), list(b.views or [])
    compare("view", va, vb, frozenset({"objects", "connections"}))
    vbi = {v.id: v for v in vb}
    for v in va:
        if v.id in vbi:
            compare(f"object@{v.id}", v.objects, vbi[v.id].objects)
            compare(f"connection@{v.id}", v.connections, vbi[v.id].connections)
    if a.organizations != b.organizations:
        out[("snapshot", "*", "organizations")] = (_folders(a), _folders(b))
    return out


def _folders(snap: ArchimateSnapshot) -> list[dict[str, Any]] | None:
    return None if snap.organizations is None else [f.model_dump() for f in snap.organizations]


def _expected(diff: Diff) -> Diff:
    return {k: (_canon(a), _canon(b)) for k, (a, b) in diff.items()}


# ---------------------------------------------------------------------------
# Pinning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,digest", sorted(ARCHI_FIXTURE_SHA256.items()))
def test_archi_fixture_is_pinned(name: str, digest: str) -> None:
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest


def test_archi_fixtures_are_not_stale() -> None:
    """The inputs Archi re-exported are still byte-for-byte what we export today."""
    assert hashlib.sha256(full_input()).hexdigest() == FULL_INPUT_SHA256
    assert hashlib.sha256(probe_input()).hexdigest() == PROBE_INPUT_SHA256


def test_probe_input_is_lossless_here() -> None:
    res = export_exchange_report(probe_snapshot(), model_id="probe", name="Probe", language="nl")
    assert res.losses == [] and schema_errors(res.data) == []
    back = import_exchange(res.data)
    assert back.losses == [] and back.snapshot == probe_snapshot()


# ---------------------------------------------------------------------------
# Archi re-export of our full export
# ---------------------------------------------------------------------------

FULL_EXPECTED: Diff = {
    # Archi omits isDirected="false" (the XSD default); absent reads as None.
    ("relationship", "r-assoc-undirected", "directed"): (False, None),
    # On the styled group Archi adds line alpha 100, truncates font size 9.5 -> 9
    # and drops line_width on an element node (it keeps it on connections).
    ("object@v-1", "o-grp", "style"): (
        {
            "fill_color": "#ffcc00",
            "fill_alpha": 80,
            "line_color": "#000000",
            "line_width": 2,
            "font_name": "Segoe UI",
            "font_size": 9.5,
            "font_style": ["bold", "italic"],
            "font_color": "#112233",
        },
        {
            "fill_color": "#ffcc00",
            "fill_alpha": 80,
            "line_color": "#000000",
            "line_alpha": 100,
            "font_name": "Segoe UI",
            "font_size": 9,
            "font_style": ["bold", "italic"],
            "font_color": "#112233",
        },
    ),
    # Unstyled occurrences get Archi's effective defaults written out.
    ("object@v-1", "o-actor-nested", "style"): (None, _archi_node("#ffffb5")),
    ("object@v-1", "o-actor-again", "style"): (None, _archi_node("#ffffb5")),
    ("object@v-1", "o-src", "style"): (None, _archi_node("#f5deaa")),
    ("object@v-1", "o-tgt", "style"): (None, _archi_node("#f5deaa")),
    ("connection@v-1", "cn-serving", "style"): (
        {"line_color": "#ff0000", "line_width": 3},
        {"line_color": "#ff0000", "line_width": 3, **ARCHI_FONT},
    ),
    ("connection@v-1", "cn-to-conn", "style"): (None, ARCHI_CONNECTION),
    ("connection@v-1", "cn-line", "style"): (
        {"line_color": "#0000ff"},
        {"line_color": "#0000ff", **ARCHI_FONT},
    ),
    # Notes and groups get Archi's defaults too (white note; the group keeps its fill).
    ("object@v-1", "o-note", "style"): (None, _archi_node("#ffffff")),
    ("object@v-1", "o-role-in-group", "style"): (None, _archi_node("#ffffb5")),
    ("object@v-1", "o-vgroup", "style"): ({"fill_color": "#e1e1e1"}, _archi_node("#e1e1e1")),
    # Archi reads no <label> on an element occurrence or a connection (it has
    # label expressions, but its exchange import/export ignores them).
    ("object@v-1", "o-tgt", "label"): ("${name} (${type})", None),
    # Archi exports a group's documentation but not a note's.
    ("object@v-1", "o-note", "documentation"): ("note doc", None),
    ("connection@v-1", "cn-serving", "label"): ("serves", None),
    ("connection@v-1", "cn-line", "label"): ("see role", None),
    # Archi groups connections by source occurrence.
    ("connection@v-1", "*", "<order>"): (
        ["cn-serving", "cn-line", "cn-to-conn"],
        ["cn-to-conn", "cn-serving", "cn-line"],
    ),
}


def test_archi_reexport_of_full_export_imports_without_loss() -> None:
    res = import_exchange((FIXTURES / "archi510_full_reexport.xml").read_bytes())
    assert res.schema_valid
    assert res.losses == []
    assert (res.model_id, res.model_name, res.model_documentation) == ("m-1", "Full", "all of it")
    assert res.language is None  # Archi writes no xml:lang unless asked to
    assert semantic_diff(full_snapshot(), res.snapshot) == _expected(FULL_EXPECTED)


def test_archi_reexport_of_full_export_round_trips_here() -> None:
    first = import_exchange((FIXTURES / "archi510_full_reexport.xml").read_bytes()).snapshot
    again = export_exchange_report(first, model_id="m-1", name="Full")
    assert again.losses == [] and schema_errors(again.data) == []
    assert import_exchange(again.data).snapshot == first


# ---------------------------------------------------------------------------
# Archi re-export of the edge-case probe
# ---------------------------------------------------------------------------

PROBE_EXPECTED: Diff = {
    # Archi makes names and property values single-line, collapses whitespace
    # runs and trims; documentation keeps inner whitespace, loses the trailing.
    ("concept", "n-ws", "name"): ("  two  spaces\tand tab  ", "two spaces and tab"),
    ("concept", "n-nl", "name"): ("line one\nline two", "line one line two"),
    ("view", "v-probe", "name"): ("Probe  view", "Probe view"),
    ("concept", "n-uni", "documentation"): ("crlf\r\nline\ttab  end  ", "crlf\r\nline\ttab  end"),
    # Archi drops the property with an empty key (but still writes its definition,
    # which is therefore PROPERTY-DEF-UNUSED here) and flattens the newline.
    ("concept", "n-prop", "properties"): (
        [("k", ""), ("", "empty key"), ("k sp/é", "v\nmulti")],
        [("k", ""), ("k sp/é", "v multi")],
    ),
    # Archi hides a connection between a nested occurrence and its parent and
    # leaves it out of its export; the connection attached to it then dangles
    # (Archi's own file fails XSD validation; here XSD-INVALID + CONNECTION-DANGLING).
    ("connection@v-probe", "c-agg", "<presence>"): (True, False),
    ("connection@v-probe", "c-inf", "<presence>"): (True, False),
    ("connection@v-probe", "c-rr", "<presence>"): (True, False),
    # Archi has no underline, no font alpha and no element line width; "plain"
    # is its default and is not written; its effective defaults are written out.
    ("object@v-probe", "o1", "style"): (
        {"font_style": ["underline"], "font_color": "#010203", "font_alpha": 50},
        {**_archi_node("#edcfe2"), "font_color": "#010203"},
    ),
    ("object@v-probe", "o2", "style"): (
        {"line_color": "#abcdef", "line_alpha": 30, "line_width": 1, "font_style": ["plain"]},
        {**_archi_node("#edcfe2"), "line_color": "#abcdef", "line_alpha": 30},
    ),
    ("object@v-probe", "o3", "style"): (
        {
            "fill_color": "#000000",
            "fill_alpha": 0,
            "font_size": 14,
            "font_style": ["bold", "italic", "underline"],
        },
        {
            **_archi_node("#000000"),
            "fill_alpha": 0,
            "font_size": 14,
            "font_style": ["bold", "italic"],
        },
    ),
    ("object@v-probe", "o4", "style"): (
        {"line_color": "#00ff00", "line_width": 3},
        {**_archi_node("#ffffb5"), "line_color": "#00ff00"},
    ),
    ("object@v-probe", "o5", "style"): (None, {**_archi_node("#000000"), "line_color": "#000000"}),
    ("object@v-probe", "o6", "style"): (None, _archi_node("#ffffb5")),
    ("connection@v-probe", "c-j", "style"): (None, ARCHI_CONNECTION),
    ("object@v-probe", "o-note2", "style"): (None, _archi_node("#ffffff")),
    ("object@v-probe", "o-grp2", "style"): (None, _archi_node("#d2d7d7")),
    ("object@v-probe", "o7", "style"): (None, _archi_node("#ffffb5")),
    ("connection@v-probe", "l-ne", "style"): (None, ARCHI_CONNECTION),
    ("connection@v-probe", "l-gn", "style"): (None, ARCHI_CONNECTION),
    # Archi reads no <label> on an element occurrence or a connection.
    ("object@v-probe", "o7", "label"): ("${type}", None),
    ("connection@v-probe", "c-j", "label"): ("to junction", None),
    # Archi's import drops a line between two ArchiMate occurrences and a line
    # attached to a connection (XMLModelImporter: "Only connect between notes and
    # groups"); note -> element and group -> note lines survive.
    ("connection@v-probe", "l-ee", "<presence>"): (True, False),
    ("connection@v-probe", "l-nc", "<presence>"): (True, False),
    # Archi keeps every concept under its type's top-level folder: a custom folder
    # tree is split across Business / Relations / Views (a top-level "Business"
    # merges into Archi's), folder identifiers are dropped, and unfiled concepts
    # land in the default folders. Element and relationship order follows that.
    ("concept", "*", "<order>"): (
        ["n-ws", "n-nl", "n-uni", "n-empty", "n-prop", "n-jname", "n-deep1", "n-deep2", "n-deep3"],
        ["n-uni", "n-empty", "n-ws", "n-nl", "n-prop", "n-jname", "n-deep1", "n-deep2", "n-deep3"],
    ),
    ("relationship", "*", "<order>"): (
        ["r-inf", "r-agg", "r-rr", "r-j"],
        ["r-agg", "r-rr", "r-j", "r-inf"],
    ),
}


def _tree(name: str, items: list[str], *children: ArchimateOrganization, **kw: Any):
    return ArchimateOrganization(name=name, items=items, children=list(children), **kw)


# The probe's folder tree after Archi (see PROBE_EXPECTED for why).
PROBE_ARCHI_FOLDERS = [
    _tree(
        "Business",
        ["n-uni", "n-empty"],
        _tree("Probe folders", ["n-ws"], _tree("Nested", ["n-nl"]), documentation="root doc"),
    ),
    _tree("Technology & Physical", ["n-prop"]),
    _tree("Other", ["n-jname", "n-deep1", "n-deep2", "n-deep3"]),
    _tree(
        "Relations",
        ["r-agg", "r-rr", "r-j"],
        _tree("Probe folders", [], _tree("Nested", ["r-inf"]), documentation="root doc"),
    ),
    _tree(
        "Views",
        [v.id for v in probe_snapshot().views or [] if v.id.startswith("vp-")],
        _tree(
            "Probe folders",
            [],
            _tree("Nested", [], _tree("Deeper", ["v-probe"])),
            documentation="root doc",
        ),
    ),
]


def test_archi_reexport_of_probe_differs_only_as_documented() -> None:
    res = import_exchange((FIXTURES / "archi510_probe_reexport.xml").read_bytes())
    # Upstream: Archi exported a connection whose ends it had just dropped.
    assert not res.schema_valid
    assert Counter(loss.code for loss in res.losses) == {
        "XSD-INVALID": 2,
        "CONNECTION-DANGLING": 1,
        "PROPERTY-DEF-UNUSED": 1,
    }
    assert [loss.id for loss in res.losses if loss.code == "CONNECTION-DANGLING"] == ["c-rr"]
    xsd = " ".join(loss.message for loss in res.losses if loss.code == "XSD-INVALID")
    assert "'c-inf'" in xsd and "'c-agg'" in xsd
    expected = _expected(PROBE_EXPECTED)
    expected[("snapshot", "*", "organizations")] = (
        _folders(probe_snapshot()),
        [f.model_dump() for f in PROBE_ARCHI_FOLDERS],
    )
    assert semantic_diff(probe_snapshot(), res.snapshot) == expected


# ---------------------------------------------------------------------------
# Archi's export of a native Archi model (Archi-only features)
# ---------------------------------------------------------------------------


def test_archi_native_export_reports_every_archi_only_feature() -> None:
    res = import_exchange((FIXTURES / "archi510_native_export.xml").read_bytes())
    assert res.schema_valid and res.language == "en"
    assert res.losses == []
    snap = res.snapshot
    # Model properties and the diagram reference are kept.
    assert snap.exchange is not None and snap.exchange.properties == [("model-prop", "mv")]
    actor = next(c for c in snap.concepts if c.id == "e-actor")
    # Archi's specialization (profile) arrives as an ordinary property.
    assert actor.properties == [("p", "v"), ("Specialization", "Special")]
    junction = next(c for c in snap.concepts if c.id == "j1")
    assert (junction.type, junction.junction_type, junction.name) == ("junction", "or", "")
    # Archi's folders, including the documented sub-folder, are kept.
    assert snap.organizations == [
        _tree("Business", ["e-role"], _tree("Sub", ["e-actor"], documentation="sub doc")),
        _tree("Relations", ["r-as", "r-j"]),
        _tree("Other", ["j1"]),
        _tree("Views", ["v1", "v2"]),
    ]
    v1 = next(v for v in snap.views or [] if v.id == "v1")
    assert v1.viewpoint == "layered"
    assert [(o.id, o.kind, o.parent_object_id, o.bounds, o.name, o.text) for o in v1.objects] == [
        ("g1", "group", None, Bounds(x=10, y=10, w=500, h=300), "Group", None),
        ("d-actor", "element", "g1", Bounds(x=20, y=30, w=120, h=55), None, None),
        ("d-role", "element", "g1", Bounds(x=250, y=30, w=120, h=55), None, None),
        ("d-j", "element", "g1", Bounds(x=300, y=150, w=15, h=15), None, None),
        ("n1", "note", None, Bounds(x=600, y=10, w=185, h=80), None, "a note\ntwo lines"),
        # A diagram reference: Archi writes it as a Label, kept as a note.
        ("ref1", "note", None, Bounds(x=600, y=200, w=120, h=55), None, "V2"),
    ]
    assert [(c.id, c.kind, c.relationship_id) for c in v1.connections] == [
        ("c-as", "relationship", "r-as"),
        ("c-j", "relationship", "r-j"),
        ("c-note", "line", None),
    ]
    # Archi wrote neither the label expression, the line's name, nor border types.
    assert all(o.label is None and o.border_type is None for o in v1.objects)
    assert all(c.label is None for c in v1.connections)
    assert {o.id: o.documentation for o in v1.objects if o.documentation} == {"g1": "group doc"}
    assert {o.id: o.view_ref for o in v1.objects if o.view_ref} == {"ref1": "v2"}


# ---------------------------------------------------------------------------
# Every stable loss code has a test
# ---------------------------------------------------------------------------


def _export_losses(snap: ArchimateSnapshot) -> list[ExchangeLoss]:
    res = export_exchange_report(snap, model_id="m", name="N")
    assert schema_errors(res.data) == []
    return res.losses


def _actor(ident: str = "a", **kw: Any) -> ArchimateConceptIn:
    return ArchimateConceptIn(id=ident, type="business_actor", name=kw.pop("name", "A"), **kw)


def _role(ident: str = "b") -> ArchimateConceptIn:
    return ArchimateConceptIn(id=ident, type="business_role", name="B")


def _obj(ident: str, concept: str, **kw: Any) -> ArchimateViewObjectIn:
    return ArchimateViewObjectIn(id=ident, concept_id=concept, **kw)


def _one_view(
    objects: list[ArchimateViewObjectIn],
    connections: list[ArchimateViewConnectionIn] | None = None,
    **kw: Any,
) -> list[ArchimateViewIn]:
    return [ArchimateViewIn(id="v", name="V", objects=objects, connections=connections or [], **kw)]


def _sized(**kw: Any) -> Bounds:
    return Bounds(**({"w": 9, "h": 9} | kw))


def _model_losses(inner: str) -> list[ExchangeLoss]:
    """Import ``<model>`` with the given content after its name."""
    data = (
        f'<model xmlns="{NS}" xmlns:xsi="{XSI}" identifier="m"><name>M</name>{inner}</model>'
    ).encode()
    return import_exchange(data).losses


_BASE = (
    '<elements><element identifier="a" xsi:type="BusinessActor"><name>A</name></element>'
    '<element identifier="b" xsi:type="BusinessRole"><name>B</name></element></elements>'
    '<relationships><relationship identifier="r" xsi:type="Assignment" source="a" target="b"/>'
    "</relationships>"
)
_NODES = (
    '<node identifier="na" elementRef="a" xsi:type="Element" x="0" y="0" w="10" h="10"/>'
    '<node identifier="nb" elementRef="b" xsi:type="Element" x="50" y="0" w="10" h="10"/>'
)


def _view_losses(content: str, attrs: str = "") -> list[ExchangeLoss]:
    return _model_losses(
        _BASE + f'<views><diagrams><view identifier="v" xsi:type="Diagram" {attrs}>'
        f"<name>V</name>{content}</view></diagrams></views>"
    )


def _fixture_losses(name: str) -> list[ExchangeLoss]:
    return import_exchange((FIXTURES / name).read_bytes()).losses


_PROPDEF = (
    '<propertyDefinitions><propertyDefinition identifier="pd" type="string"><name>k</name>{doc}'
    "</propertyDefinition></propertyDefinitions>"
)

LOSS_TRIGGERS: dict[str, Callable[[], list[ExchangeLoss]]] = {
    # --- export ---
    "ID-REWRITTEN": lambda: _export_losses(ArchimateSnapshot(concepts=[_actor("1a")])),
    "TEXT-INVALID-CHARS": lambda: _export_losses(
        ArchimateSnapshot(concepts=[_actor(name="bad\x01char")])
    ),
    "EXPORT-TYPE-UNKNOWN": lambda: _export_losses(
        ArchimateSnapshot(concepts=[ArchimateConceptIn(id="x", type="nope", name="X")])
    ),
    "EXPORT-REL-TYPE-UNKNOWN": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[_actor(), _role()],
            relationships=[ArchimateRelationshipIn(id="r", type="nope", source="a", target="b")],
        )
    ),
    "EXPORT-DANGLING": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[_actor()],
            relationships=[
                ArchimateRelationshipIn(id="r", type="association", source="a", target="gone")
            ],
        )
    ),
    "JUNCTION-TYPE-INVALID": lambda: _export_losses(
        ArchimateSnapshot(concepts=[ArchimateConceptIn(id="j", type="junction", junction_type="x")])
    ),
    "ATTR-NOT-APPLICABLE": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[_actor(), _role()],
            relationships=[
                ArchimateRelationshipIn(
                    id="r", type="assignment", source="a", target="b", access_type="read"
                )
            ],
        )
    ),
    "ACCESS-TYPE-INVALID": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[
                ArchimateConceptIn(id="p", type="business_process", name="P"),
                ArchimateConceptIn(id="o", type="business_object", name="O"),
            ],
            relationships=[
                ArchimateRelationshipIn(
                    id="r", type="access", source="p", target="o", access_type="peek"
                )
            ],
        )
    ),
    "CONCEPT-PARENT": lambda: _export_losses(
        ArchimateSnapshot(concepts=[_actor(parent_id="b"), _role()])
    ),
    "VIEWPOINT-UNKNOWN": lambda: _export_losses(
        ArchimateSnapshot(concepts=[_actor()], views=_one_view([], viewpoint="no-such"))
    ),
    "STYLE-KEY-UNSUPPORTED": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[_actor()],
            views=_one_view([_obj("o", "a", bounds=_sized(), style={"shadow": 1})]),
        )
    ),
    "STYLE-VALUE-INVALID": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[_actor()],
            views=_one_view([_obj("o", "a", bounds=_sized(), style={"fill_color": "red"})]),
        )
    ),
    "BOUNDS-MISSING": lambda: _export_losses(
        ArchimateSnapshot(concepts=[_actor()], views=_one_view([_obj("o", "a")]))
    ),
    "BOUNDS-DEFAULT-SIZE": lambda: _export_losses(
        ArchimateSnapshot(concepts=[_actor()], views=_one_view([_obj("o", "a", bounds=Bounds())]))
    ),
    "BOUNDS-CLAMPED": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[_actor()], views=_one_view([_obj("o", "a", bounds=_sized(x=-1))])
        )
    ),
    "BENDPOINT-CLAMPED": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[_actor(), _role()],
            relationships=[
                ArchimateRelationshipIn(id="r", type="assignment", source="a", target="b")
            ],
            views=_one_view(
                [_obj("oa", "a", bounds=_sized()), _obj("ob", "b", bounds=_sized(x=50))],
                [
                    ArchimateViewConnectionIn(
                        id="c",
                        relationship_id="r",
                        source_object_id="oa",
                        target_object_id="ob",
                        bendpoints=[Point(x=-5, y=3)],
                    )
                ],
            ),
        )
    ),
    "PROPERTY-KEY-RESERVED": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[_actor()],
            views=_one_view([], properties=[("archi:borderType", "x=y")]),
        )
    ),
    "EXCHANGE-CONTENT-INVALID": lambda: _export_losses(
        ArchimateSnapshot(exchange=ArchimateExchangeContent(metadata="<bogus/>"))
    ),
    "OBJECT-REPARENTED": lambda: _export_losses(
        ArchimateSnapshot(
            concepts=[_actor()],
            views=_one_view([_obj("o", "a", parent_object_id="gone", bounds=_sized())]),
        )
    ),
    # --- import ---
    "XSD-INVALID": lambda: _model_losses(_BASE + "<bogus/>"),
    "ID-REMAPPED": lambda: _view_losses(_NODES.replace('"nb"', '"na"')),
    "LANG-EXTRA": lambda: _model_losses(
        '<documentation xml:lang="en">d</documentation>'
        '<documentation xml:lang="nl">d</documentation>' + _BASE
    ),
    "TEXT-TRUNCATED": lambda: _model_losses(
        '<elements><element identifier="a" xsi:type="BusinessActor">'
        f"<name>{'n' * 301}</name></element></elements>"
    ),
    "VALUE-MISSING": lambda: _view_losses(
        '<node identifier="na" elementRef="a" xsi:type="Element" x="0" y="0" h="10"/>'
    ),
    "VALUE-INVALID": lambda: _view_losses(
        '<node identifier="na" elementRef="a" xsi:type="Element" x="q" y="0" w="1" h="1"/>'
    ),
    "TYPE-UNKNOWN": lambda: _model_losses(
        '<elements><element identifier="a" xsi:type="Fancy"><name>A</name></element></elements>'
    ),
    "MODEL-VERSION": lambda: _fixture_losses("opengroup_Basic_Model_Organization.xml"),
    "PROPERTY-TYPE": lambda: _fixture_losses("opengroup_Basic_Model_Properties.xml"),
    "PROPERTY-DOCUMENTATION": lambda: _model_losses(
        _BASE + _PROPDEF.format(doc="<documentation>d</documentation>")
    ),
    "PROPERTY-DEF-UNRESOLVED": lambda: _model_losses(
        '<elements><element identifier="a" xsi:type="BusinessActor"><name>A</name><properties>'
        '<property propertyDefinitionRef="nowhere"><value>v</value></property>'
        "</properties></element></elements>"
    ),
    "PROPERTY-DEF-UNUSED": lambda: _model_losses(_BASE + _PROPDEF.format(doc="")),
    "ORGANIZATION": lambda: _model_losses(
        _BASE + '<organizations><item identifierRef="a"/></organizations>'
    ),
    "VIEWPOINT-REF": lambda: _view_losses(_NODES, attrs='viewpointRef="vp1"'),
    "VIEW-REF": lambda: _view_losses(
        '<node identifier="na" elementRef="a" xsi:type="Element" x="0" y="0" w="9" h="9">'
        '<viewRef ref="nowhere"/></node>'
    ),
    "LABEL-CONCEPT-REF": lambda: _view_losses(
        '<node identifier="nl" xsi:type="Label" conceptRef="a" x="0" y="0" w="9" h="9">'
        "<label>L</label></node>"
    ),
    "NODE-DOCUMENTATION": lambda: _view_losses(
        '<node identifier="na" elementRef="a" xsi:type="Element" x="0" y="0" w="9" h="9">'
        "<documentation>D</documentation></node>"
    ),
    "CONNECTION-DANGLING": lambda: _fixture_losses("archi510_probe_reexport.xml"),
    "CONNECTION-ATTACHMENT": lambda: _fixture_losses("archi_bendpoint.test.xml"),
    "CONNECTION-DOCUMENTATION": lambda: _view_losses(
        _NODES + '<connection identifier="c" relationshipRef="r" xsi:type="Relationship" '
        'source="na" target="nb"><documentation>D</documentation></connection>'
    ),
    "NESTING-FLAG": lambda: _view_losses(
        _NODES + '<connection identifier="c" relationshipRef="r" '
        'xsi:type="NestingRelationship" source="na" target="nb"/>'
    ),
    "UNHANDLED": lambda: _model_losses(_BASE + "<bogus/>"),
    "EXTENSION": lambda: _model_losses(_BASE + '<v:x xmlns:v="urn:vendor"/>'),
}


def test_every_loss_code_has_a_trigger() -> None:
    assert set(LOSS_TRIGGERS) == LOSS_CODES


@pytest.mark.parametrize("code", sorted(LOSS_CODES))
def test_loss_code_is_produced(code: str) -> None:
    losses = LOSS_TRIGGERS[code]()
    hits = [loss for loss in losses if loss.code == code]
    assert hits, f"{code} not produced; got {[loss.code for loss in losses]}"
    assert all(loss.message for loss in hits)


def test_loss_codes_in_source_are_exactly_the_stable_list() -> None:
    """Every code literal the module can emit is in LOSS_CODES and in the docstring."""
    tree = ast.parse(Path(exchange_module.__file__).read_text())
    listed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "LOSS_CODES" for t in node.targets
        ):
            listed |= {id(n) for n in ast.walk(node.value)}
    found = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and id(node) not in listed
        and isinstance(node.value, str)
        and len(node.value) > 4
        and node.value.replace("-", "").isalpha()
        and node.value.isupper()
    }
    assert found == LOSS_CODES
    doc = exchange_module.__doc__ or ""
    assert {c for c in LOSS_CODES if f"``{c}``" not in doc} == set()


def test_loss_ids_fall_back_to_the_nearest_identified_ancestor() -> None:
    att = [
        loss
        for loss in _fixture_losses("archi_bendpoint.test.xml")
        if loss.code == "CONNECTION-ATTACHMENT"
    ]
    assert len(att) == 4
    for loss in att:
        assert loss.xpath and loss.xpath.rsplit("/", 1)[1].startswith(("source", "target"))
        assert loss.id  # the connection's identifier; the attachment has none


# A document using the three exchange constructs RM used to drop: model properties (and
# metadata), a custom viewpoint definition referenced by a view, and a node's viewRef.
FIDELITY_DOC = f"""<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{NS}" xmlns:xsi="{XSI}" xmlns:dc="http://purl.org/dc/elements/1.1/"
       identifier="m-fid">
  <name xml:lang="en">Fidelity</name>
  <properties>
    <property propertyDefinitionRef="pd-owner"><value xml:lang="en">EA team</value></property>
  </properties>
  <metadata>
    <schema>Dublin Core</schema>
    <schemaversion>1.1</schemaversion>
    <dc:title>Fidelity model</dc:title>
  </metadata>
  <elements>
    <element identifier="a" xsi:type="BusinessActor"><name xml:lang="en">A</name></element>
  </elements>
  <propertyDefinitions>
    <propertyDefinition identifier="pd-owner" type="string"><name>owner</name></propertyDefinition>
    <propertyDefinition identifier="pd-vp" type="string"><name>vp-key</name></propertyDefinition>
  </propertyDefinitions>
  <views>
    <viewpoints>
      <viewpoint identifier="vp-1">
        <name xml:lang="en">Actors only</name>
        <documentation xml:lang="en">Who is involved</documentation>
        <properties>
          <property propertyDefinitionRef="pd-vp"><value xml:lang="en">x</value></property>
        </properties>
        <concern>
          <label xml:lang="en">Responsibility</label>
          <stakeholders><stakeholder><label xml:lang="en">CIO</label></stakeholder></stakeholders>
        </concern>
        <viewpointPurpose>Informing</viewpointPurpose>
        <viewpointContent>Overview</viewpointContent>
        <allowedElementType type="BusinessActor"/>
        <allowedRelationshipType type="Assignment"/>
        <modelingNote type="text"><documentation xml:lang="en">keep it small</documentation></modelingNote>
      </viewpoint>
    </viewpoints>
    <diagrams>
      <view identifier="v-main" xsi:type="Diagram" viewpointRef="vp-1">
        <name xml:lang="en">Main</name>
        <node identifier="n-a" elementRef="a" xsi:type="Element" x="10" y="10" w="120" h="55">
          <viewRef ref="v-detail"/>
        </node>
      </view>
      <view identifier="v-detail" xsi:type="Diagram">
        <name xml:lang="en">Detail</name>
        <node identifier="n-a2" elementRef="a" xsi:type="Element" x="10" y="10" w="120" h="55"/>
      </view>
    </diagrams>
  </views>
</model>
""".encode()

_FIDELITY_CODES = {"MODEL-PROPERTIES", "MODEL-METADATA", "VIEWPOINT-DEFINITION"}


def test_model_properties_viewpoints_and_view_refs_round_trip() -> None:
    assert schema_errors(FIDELITY_DOC) == []
    first = import_exchange(FIDELITY_DOC, strict=True)
    assert first.losses == []
    ex = first.snapshot.exchange
    assert ex is not None and ex.properties == [("owner", "EA team")]
    assert ex.metadata is not None and "Fidelity model" in ex.metadata
    (vp,) = ex.viewpoints
    assert (vp.id, vp.properties) == ("vp-1", [("vp-key", "x")])
    assert "Actors only" in vp.xml and 'type="BusinessActor"' in vp.xml
    views = {v.id: v for v in first.snapshot.views or []}
    assert views["v-main"].viewpoint_ref == "vp-1"
    assert views["v-main"].objects[0].view_ref == "v-detail"

    exported = export_exchange_report(first.snapshot, model_id="m-fid", name="Fidelity")
    assert exported.losses == []
    assert schema_errors(exported.data) == []
    second = import_exchange(exported.data, strict=True)
    assert second.losses == []
    # Identical semantics: the snapshot that comes back is the one that went out, and a
    # second export is byte-identical (a fixed point).
    assert second.snapshot == first.snapshot
    again = export_exchange_report(second.snapshot, model_id="m-fid", name="Fidelity")
    assert again.data == exported.data
    assert not {x.code for x in [*first.losses, *exported.losses, *second.losses]} & (
        _FIDELITY_CODES | {"VIEWPOINT-REF", "VIEW-REF"}
    )


def test_dangling_viewpoint_and_view_refs_are_reported_on_export() -> None:
    snap = import_exchange(FIDELITY_DOC).snapshot
    assert snap.exchange is not None
    no_vp = snap.model_copy(
        update={"exchange": snap.exchange.model_copy(update={"viewpoints": []})}
    )
    only_main = no_vp.model_copy(
        update={"views": [v for v in snap.views or [] if v.id == "v-main"]}
    )
    losses = export_exchange_report(only_main, model_id="m", name="M").losses
    assert sorted((x.code, x.id) for x in losses) == [
        ("EXPORT-DANGLING", "n-a"),
        ("EXPORT-DANGLING", "v-main"),
    ]
