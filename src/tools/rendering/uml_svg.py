"""UML (profile ``uml-2.5.1-p1``) diagram → SVG.

Class and component diagrams: classifier boxes with name / attribute /
operation (or literal) compartments, visibility symbols, keywords
(«interface», «dataType», «enumeration», «primitive», «component»), abstract
names and operations in italics, static features underlined; packages as
folders, actors as stick figures, components with the component icon, ports
as small squares on their owner's border, provided interfaces as lollipops and
required interfaces as sockets. Relationships: generalization (hollow
triangle), (interface) realization (dashed, hollow triangle), dependency
(dashed, open arrow), usage (dashed, open arrow, «use»), association (end
roles/multiplicities, aggregation diamonds, navigability arrows), connector
(assembly ball-and-socket, delegation «delegate»).

Sequence diagrams: lifeline heads and dashed lifelines; messages placed in the
interaction's stored occurrence order (``occurrences`` list position is the
time order — the diagram's connection order is NOT); synch = solid + filled
arrow, asynch = solid + open arrow, reply = dashed + open arrow, create =
dashed + open arrow to the created head, delete = filled arrow ending in ✕;
execution bars; combined fragments framed with an operator tab, operand
guards and dashed operand separators.

Association ends follow the UML 2.5.1 metamodel (§11.5): each end is a
member-end Property typed by ``element_ref``. ``navigable`` puts an open
arrowhead at that end's own element; ``aggregation`` (shared/composite) on a
property means the OPPOSITE classifier is the whole, so the hollow/filled
diamond is drawn at the opposite end. When one terminal carries both, the
diamond sits on the box and the navigability arrowhead just beyond it. Bounds are absolute (UML diagram objects have no parent);
``w``/``h`` of -1 mean "size to content".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schemas.modeling import TargetKind
from schemas.uml import (
    UmlDiagramConnectionIn,
    UmlDiagramIn,
    UmlDiagramObjectIn,
    UmlElementIn,
    UmlInteractionIn,
    UmlMessage,
    UmlOperation,
    UmlProperty,
    UmlRelationshipIn,
    UmlSnapshot,
)
from tools.rendering.layout import layered_layout, route
from tools.rendering.svg import (
    LINE_HEIGHT,
    Diagnostics,
    Extent,
    Fitted,
    Look,
    MarkerSet,
    Node,
    Pt,
    Rect,
    RenderOptions,
    RenderResult,
    RenderTargetNotFoundError,
    StyleReader,
    along,
    backdrop,
    clip_rect,
    document,
    fit_line,
    fit_text,
    fmt,
    path_d,
    perpendicular,
    polyline_midpoint,
    text_node,
    text_width,
)

RENDERER_VERSION = "uml-svg/1.0.0"
RULESET_ID = "render/uml-svg-1"

LINE = "#000000"
FILL = "#fefece"
TEXT = "#000000"
UNRESOLVED_STROKE = "#c00000"
UNRESOLVED_FILL = "#fff0f0"

VISIBILITY = {"public": "+", "private": "-", "protected": "#", "package": "~"}
KEYWORD = {
    "interface": "«interface»",
    "data_type": "«dataType»",
    "enumeration": "«enumeration»",
    "primitive_type": "«primitive»",
    "component": "«component»",
}
CLASSIFIER_KINDS = frozenset(
    {"class", "interface", "data_type", "enumeration", "primitive_type", "component"}
)
DASH = "6 4"
# Relationship kinds whose TARGET is drawn above the source in auto-layout.
UPWARD_KINDS = frozenset({"generalization", "realization", "interface_realization"})

# relationship kind → (dasharray, target marker, keyword label)
RELATIONSHIP_STYLE: dict[str, tuple[str | None, str | None, str | None]] = {
    "generalization": (None, "triangle-hollow", None),
    "interface_realization": (DASH, "triangle-hollow", None),
    "realization": (DASH, "triangle-hollow", None),
    "dependency": (DASH, "arrow-open", None),
    "usage": (DASH, "arrow-open", "«use»"),
    "association": (None, None, None),
    "connector": (None, None, None),
}

# message kind → (dasharray, marker)
MESSAGE_STYLE: dict[str, tuple[str | None, str]] = {
    "synch_call": (None, "arrow-filled"),
    "asynch_call": (None, "arrow-open"),
    "asynch_signal": (None, "arrow-open"),
    "reply": (DASH, "arrow-open"),
    "create": (DASH, "arrow-open"),
    "delete": (None, "arrow-filled"),
}


def render_uml_diagram(
    snapshot: UmlSnapshot,
    diagram_id: str,
    *,
    options: RenderOptions | None = None,
) -> RenderResult:
    """Render one diagram. Raises :class:`RenderTargetNotFoundError` only when
    the diagram itself is absent; everything else degrades to placeholders
    plus diagnostics."""
    diagram = next((d for d in snapshot.diagrams or [] if d.id == diagram_id), None)
    if diagram is None:
        raise RenderTargetNotFoundError(f"diagram {diagram_id!r} is not in the snapshot")
    opts = options or RenderOptions()
    if diagram.kind == "sequence":
        return _SequenceRenderer(snapshot, diagram, opts).render()
    return _StructureRenderer(snapshot, diagram, opts).render()


class _Base:
    def __init__(self, snapshot: UmlSnapshot, diagram: UmlDiagramIn, opts: RenderOptions) -> None:
        self.snapshot = snapshot
        self.diagram = diagram
        self.opts = opts
        self.fs = opts.font_size
        self.lh = self.fs * LINE_HEIGHT
        self.diags = Diagnostics(RULESET_ID)
        self.markers = MarkerSet()
        self.extent = Extent()
        self.lw = 1.0
        self.text_style: dict[str, Any] = {}
        self.cur_fs = self.fs
        self.label_target: tuple[TargetKind, str] | None = None
        self.elements: dict[str, UmlElementIn] = {}
        for e in snapshot.elements:
            self.elements.setdefault(e.id, e)
        self.rels: dict[str, UmlRelationshipIn] = {}
        for r in snapshot.relationships:
            self.rels.setdefault(r.id, r)
        self.objs: dict[str, UmlDiagramObjectIn] = {}
        for o in diagram.objects:
            if o.id in self.objs:
                self._dup("diagram_object", o.id)
                continue
            self.objs[o.id] = o
        self.conns: dict[str, UmlDiagramConnectionIn] = {}
        for c in diagram.connections:
            if c.id in self.conns:
                self._dup("diagram_connection", c.id)
                continue
            self.conns[c.id] = c
        self.obj_style = StyleReader(self.diags, "RENDER-UML", "diagram_object")
        self.con_style = StyleReader(self.diags, "RENDER-UML", "diagram_connection")

    def _dup(self, kind: TargetKind, oid: str) -> None:
        self.diags.add(
            "RENDER-UML-DUP-001",
            "warning",
            f"{kind.replace('_', ' ')} id {oid} appears more than once; first occurrence drawn",
            [(kind, oid)],
        )

    def type_name(self, ref: str | None) -> str | None:
        if ref is None:
            return None
        e = self.elements.get(ref)
        return (e.name or e.id) if e else ref

    def text(
        self,
        s: str,
        x: float,
        y_top: float,
        *,
        anchor: str = "middle",
        fs: float | None = None,
        color: str = TEXT,
        extra: dict[str, Any] | None = None,
        width: float | None = None,
        halo: bool = False,
    ) -> Node:
        """One line of text; with ``width`` it is shrunk/ellipsized to stay inside."""
        fs = fs or self.cur_fs
        merged = dict(self.text_style)
        merged.update(extra or {})
        if width is not None:
            fitted = fit_line(s, max(width, 0.0), fs, bold=merged.get("font-weight") == "bold")
            if fitted.truncated:
                self.text_overflow(s, fitted.font_size)
                merged["data-truncated"] = "true"
            if fitted.shrunk:
                merged["data-shrunk"] = "true"
            s, fs = fitted.lines[0], fitted.font_size
        self.extent.add_text([s], x, y_top, fs, anchor)
        node = text_node([s], x, y_top, fs, anchor=anchor, color=color, extra=merged)
        if not halo:
            return node
        wrapper = Node("g", {"data-part": "label-group"})
        wrapper.add(backdrop([s], x, y_top, fs, anchor, self.opts.background or "#ffffff"))
        wrapper.add(node)
        return wrapper

    def text_overflow(self, s: str, font_size: float) -> None:
        if self.label_target is None:
            return
        kind, ident = self.label_target
        self.diags.add(
            "RENDER-UML-TEXT-001",
            "warning",
            f"text of {ident} does not fit its box even at {fmt(font_size)}px; "
            "truncated with an ellipsis or dropped",
            [(kind, ident)],
            {"text": s, "font_size": font_size},
        )

    def reset_look(self) -> None:
        self.label_target = None
        self.lw = 1.0
        self.text_style = {}
        self.cur_fs = self.fs

    def placeholder(self, oid: str, r: Rect, label: str) -> Node:
        self.label_target = ("diagram_object", oid)
        self.extent.add_rect(r.x, r.y, r.w, r.h)
        g = Node(
            "g",
            {"data-kind": "unresolved", "data-object-id": oid, "data-x": r.x, "data-y": r.y},
        )
        g.add(
            Node(
                "rect",
                {
                    "x": r.x,
                    "y": r.y,
                    "width": r.w,
                    "height": r.h,
                    "fill": UNRESOLVED_FILL,
                    "stroke": UNRESOLVED_STROKE,
                    "stroke-dasharray": "4 2",
                },
            )
        )
        g.add(self.text(f"? {label}", r.cx, r.y + 4, color=UNRESOLVED_STROKE, width=r.w - 8))
        return g

    def finish(self, body: list[Node], extra_root: dict[str, Any]) -> RenderResult:
        root = {"data-renderer": RENDERER_VERSION, "data-diagram-id": self.diagram.id}
        root.update(extra_root)
        svg = document(body, self.markers.defs(), self.extent, self.opts, root)
        return RenderResult(
            svg=svg, diagnostics=self.diags.result(), renderer_version=RENDERER_VERSION
        )


# ============================================================================
# Class / component diagrams
# ============================================================================


@dataclass
class _Line:
    text: str
    italic: bool = False
    underline: bool = False
    bold: bool = False
    feature_id: str | None = None
    visibility: str | None = None


@dataclass
class _Content:
    header: list[_Line]
    compartments: list[tuple[str, list[_Line]]] = field(default_factory=list)


class _StructureRenderer(_Base):
    def __init__(self, snapshot: UmlSnapshot, diagram: UmlDiagramIn, opts: RenderOptions) -> None:
        super().__init__(snapshot, diagram, opts)
        self.rects: dict[str, Rect] = {}
        self.auto_ports: dict[str, int] = {}
        self.synthesized: set[str] = set()
        self.port_owner: dict[str, str] = {}
        self.right_slots_taken: dict[str, int] = {}
        self.obj_of_element: dict[str, str] = {}
        for oid, o in self.objs.items():
            if o.element_ref is not None:
                self.obj_of_element.setdefault(o.element_ref, oid)
        if diagram.kind not in ("class", "component"):
            self.diags.add(
                "RENDER-UML-DIA-001",
                "warning",
                f"diagram {diagram.id} has unsupported kind {diagram.kind!r}; drawn as a class diagram",
                [("diagram", diagram.id)],
            )

    # -------------------------------------------------------------- content

    def _vis(self, v: str | None) -> str:
        return VISIBILITY.get(v or "", "")

    def _mult(self, m: str | None) -> str:
        return f"[{m}]" if m not in (None, "", "1") else ""

    def _property_line(self, p: UmlProperty) -> _Line:
        s = f"{self._vis(p.visibility)}{p.name}"
        t = self.type_name(p.type_ref)
        if t:
            s += f" : {t}"
        s += self._mult(p.multiplicity)
        if p.default is not None:
            s += f" = {p.default}"
        return _Line(s, underline=p.is_static, feature_id=p.id, visibility=p.visibility)

    def _operation_line(self, op: UmlOperation) -> _Line:
        params = []
        ret = self.type_name(op.return_type_ref)
        for prm in op.parameters:
            if prm.direction == "return":
                ret = ret or self.type_name(prm.type_ref)
                continue
            s = f"{prm.direction} " if prm.direction in ("out", "inout") else ""
            s += prm.name
            t = self.type_name(prm.type_ref)
            if t:
                s += f" : {t}"
            s += self._mult(prm.multiplicity)
            params.append(s)
        s = f"{self._vis(op.visibility)}{op.name}({', '.join(params)})"
        if ret:
            s += f" : {ret}"
        return _Line(
            s,
            italic=op.is_abstract,
            underline=op.is_static,
            feature_id=op.id,
            visibility=op.visibility,
        )

    def _content(self, e: UmlElementIn) -> _Content:
        a = e.attributes
        header: list[_Line] = []
        if e.kind in KEYWORD:
            header.append(_Line(KEYWORD[e.kind]))
        header.append(_Line(e.name or e.id, bold=True, italic=a.is_abstract))
        c = _Content(header)
        props = [self._property_line(p) for p in a.properties]
        ops = [self._operation_line(o) for o in a.operations]
        if e.kind == "enumeration":
            c.compartments.append(("literals", [_Line(lit) for lit in a.literals]))
            if props:
                c.compartments.append(("attributes", props))
            if ops:
                c.compartments.append(("operations", ops))
        elif e.kind in ("class", "interface", "data_type"):
            c.compartments.append(("attributes", props))
            c.compartments.append(("operations", ops))
        elif e.kind == "component":
            if props:
                c.compartments.append(("attributes", props))
            if ops:
                c.compartments.append(("operations", ops))
        return c

    @staticmethod
    def _head_width(w: float, icon: bool) -> float:
        """Room for the centred name lines (the component icon sits top-right)."""
        return w - 12 - (2 * 22 if icon else 0)

    def _fit(self, ln: _Line, width: float, fs: float) -> Fitted:
        """A feature/name line wrapped (and shrunk if a word is too wide) to ``width``."""
        return fit_text(ln.text, max(width, 1.0), 1e9, fs, bold=ln.bold)

    @staticmethod
    def _height(f: Fitted) -> float:
        return len(f.lines) * f.font_size * LINE_HEIGHT

    def _content_size(
        self, c: _Content, icon: bool, fs: float, fixed_w: float | None = None
    ) -> tuple[float, float]:
        if fixed_w is None:
            head = max((text_width(ln.text, fs, bold=ln.bold) for ln in c.header), default=0)
            body = max(
                (text_width(ln.text, fs, bold=ln.bold) for _, cp in c.compartments for ln in cp),
                default=0,
            )
            w = max(110.0, head + 12 + (44 if icon else 0), body + 12)
        else:
            w = fixed_w
        h = 10 + sum(self._height(self._fit(ln, self._head_width(w, icon), fs)) for ln in c.header)
        for _, comp in c.compartments:
            h += max(sum(self._height(self._fit(ln, w - 12, fs)) for ln in comp) + 6, 10)
        return w, h

    def _style_fs(self, o: UmlDiagramObjectIn) -> float:
        """The object's font size as drawn (style ``font_size`` in points), for layout."""
        v = (o.style or {}).get("font_size")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 4 <= v <= 72:
            return round(float(v) * 4 / 3, 2)
        return self.fs

    def _natural_size(self, oid: str) -> tuple[float, float]:
        o = self.objs[oid]
        e = self.elements.get(o.element_ref or "")
        if e is None:
            return (120, 50)
        if e.kind == "port":
            return (14, 14)
        if e.kind == "actor":
            return (max(60.0, text_width(e.name, self.fs) + 10), 70 + self.lh)
        if e.kind == "package":
            return (max(120.0, text_width(e.name, self.fs, bold=True) + 20), 70)
        if e.kind in CLASSIFIER_KINDS:
            fixed = o.bounds.w if o.bounds is not None and o.bounds.w > 0 else None
            w, h = self._content_size(
                self._content(e), e.kind == "component", self._style_fs(o), fixed
            )
            if e.kind == "component":
                right = len(e.attributes.provided) + self.auto_ports.get(e.id, 0)
                slots = max(right, len(e.attributes.required))
                h = max(h, (slots + 1) * 26.0)
            return w, h
        return (120, 50)

    # -------------------------------------------------------------- layout

    def _layout(self) -> None:
        for o in self.objs.values():
            e = self.elements.get(o.element_ref or "")
            if e is not None and e.kind == "port" and o.bounds is None and e.owner_id:
                self.auto_ports[e.owner_id] = self.auto_ports.get(e.owner_id, 0) + 1
        sizes: dict[str, tuple[float, float]] = {}
        for oid, o in self.objs.items():
            nw, nh = self._natural_size(oid)
            if o.bounds is not None:
                nw = o.bounds.w if o.bounds.w > 0 else nw
                nh = o.bounds.h if o.bounds.h > 0 else nh
            sizes[oid] = (nw, nh)
        bottom = 0.0
        for oid, o in self.objs.items():
            if o.bounds is not None:
                w, h = sizes[oid]
                self.rects[oid] = Rect(o.bounds.x, o.bounds.y, w, h)
                bottom = max(bottom, o.bounds.y + h)
        synthesized: list[str] = []
        ports: list[str] = []
        loose: list[str] = []
        for oid, o in self.objs.items():
            if oid in self.rects:
                continue
            e = self.elements.get(o.element_ref or "")
            if e is not None and e.kind == "port" and self.obj_of_element.get(e.owner_id or ""):
                ports.append(oid)  # sits on its owner's border
            else:
                loose.append(oid)
        # Layered layout: general/realized/supplier above, source above target.
        loose_set = set(loose)
        edges: list[tuple[str, str]] = []
        for c in self.conns.values():
            a, b = c.source_object_id, c.target_object_id
            rel = self.rels.get(c.relationship_ref or "")
            if rel is not None and rel.kind in UPWARD_KINDS:
                a, b = b, a
            if a in loose_set and b in loose_set and a != b:
                edges.append((a, b))
        component = self.diagram.kind == "component"
        pos = layered_layout(
            [(oid, *sizes[oid]) for oid in loose],
            edges,
            gap_x=150.0 if component else 90.0,
            gap_y=70.0,
            max_per_row=4,
        )
        top = bottom + 60 if bottom else 0.0
        for oid in loose:
            x, y = pos[oid]
            w, h = sizes[oid]
            self.rects[oid] = Rect(x, top + y, w, h)
            synthesized.append(oid)
        by_owner: dict[str, list[str]] = {}
        for oid in ports:
            owner = self.elements[self.objs[oid].element_ref or ""].owner_id
            owner_obj = self.obj_of_element[owner or ""]
            by_owner.setdefault(owner_obj, []).append(oid)
            synthesized.append(oid)
        # Right-border slots are shared with the owner's provided lollipops.
        for owner_obj, plist in by_owner.items():
            r = self.rects[owner_obj]
            owner_el = self.elements.get(self.objs[owner_obj].element_ref or "")
            n = len(plist) + (len(owner_el.attributes.provided) if owner_el else 0)
            for i, oid in enumerate(plist):
                cy = r.y + (i + 1) * r.h / (n + 1)
                self.rects[oid] = Rect(r.x + r.w - 7, cy - 7, 14, 14)
            self.right_slots_taken[owner_obj] = len(plist)
        self.synthesized = set(synthesized)
        for owner_obj, plist in by_owner.items():
            for oid in plist:
                self.port_owner[oid] = owner_obj
        if synthesized:
            ids = sorted(synthesized)
            self.diags.add(
                "RENDER-UML-LAYOUT-001",
                "advice",
                f"layout synthesized for {len(ids)} diagram object(s) without bounds",
                [("diagram_object", i) for i in ids],
                {"object_ids": ids},
            )

    # -------------------------------------------------------------- objects

    def _draw_object(self, oid: str) -> Node:
        o = self.objs[oid]
        r = self.rects[oid]
        self.label_target = ("diagram_object", oid)
        if o.element_ref is None:
            self.diags.add(
                "RENDER-UML-OBJ-001",
                "warning",
                f"diagram object {oid} has no element_ref on a {self.diagram.kind} diagram",
                [("diagram_object", oid)],
            )
            return self.placeholder(oid, r, "no element")
        e = self.elements.get(o.element_ref)
        if e is None:
            self.diags.add(
                "RENDER-UML-OBJ-001",
                "warning",
                f"diagram object {oid} references element {o.element_ref}, which is not in the snapshot",
                [("diagram_object", oid)],
                {"element_ref": o.element_ref},
            )
            return self.placeholder(oid, r, f"unresolved: {o.element_ref}")
        look = self.obj_style.read(
            o.style, oid, fill=FILL, line=LINE, font_color=TEXT, font_size=self.fs
        )
        fill, line, color = look.fill, look.line, look.font_color
        self.lw = look.line_width
        self.cur_fs = look.font_size
        self.text_style = look.text_attrs()
        if look.fill_opacity is not None and look.font_opacity is None:
            self.text_style["fill-opacity"] = 1
        self.extent.add_rect(r.x, r.y, r.w, r.h)
        g = Node(
            "g",
            {
                "data-kind": "element",
                "data-object-id": oid,
                "data-element-id": e.id,
                "data-element-kind": e.kind,
                "data-x": r.x,
                "data-y": r.y,
                "data-w": r.w,
                "data-h": r.h,
                "fill-opacity": look.fill_opacity,
                "stroke-opacity": look.line_opacity,
            },
        )
        if e.attributes.is_abstract:
            g.attrs["data-abstract"] = "true"
        if e.kind in CLASSIFIER_KINDS:
            self._classifier(g, e, r, fill, line, color)
        elif e.kind == "package":
            self._package(g, e, r, fill, line, color)
        elif e.kind == "actor":
            self._actor(g, e, r, line, color)
        elif e.kind == "port":
            self._port(g, e, r, fill, line, color)
        else:
            self.diags.add(
                "RENDER-UML-OBJ-002",
                "warning",
                f"element {e.id} has unsupported kind {e.kind!r}; drawn as a plain box",
                [("diagram_object", oid), ("uml_element", e.id)],
            )
            g.add(
                Node(
                    "rect",
                    {"x": r.x, "y": r.y, "width": r.w, "height": r.h, "fill": fill, "stroke": line},
                )
            )
            g.add(self.text(e.name or e.id, r.cx, r.y + 4, color=color, width=r.w - 8))
        return g

    def _line_node(self, ln: _Line, x: float, y: float, anchor: str, color: str, f: Fitted) -> Node:
        extra: dict[str, Any] = dict(self.text_style)
        if ln.bold:
            extra["font-weight"] = "bold"
        if ln.italic:
            extra["font-style"] = "italic"
        if ln.underline:
            extra["text-decoration"] = "underline"
        if ln.feature_id:
            extra["data-feature-id"] = ln.feature_id
        if ln.visibility:
            extra["data-visibility"] = ln.visibility
        if f.shrunk:
            extra["data-shrunk"] = "true"
        self.extent.add_text(f.lines, x, y, f.font_size, anchor)
        return text_node(f.lines, x, y, f.font_size, anchor=anchor, color=color, extra=extra)

    def _classifier(
        self, g: Node, e: UmlElementIn, r: Rect, fill: str, line: str, color: str
    ) -> None:
        c = self._content(e)
        g.add(
            Node(
                "rect",
                {
                    "data-part": "box",
                    "x": r.x,
                    "y": r.y,
                    "width": r.w,
                    "height": r.h,
                    "fill": fill,
                    "stroke": line,
                    "stroke-width": self.lw,
                },
            )
        )
        y = r.y + 5
        bottom = r.y + r.h
        dropped: list[str] = []
        head = Node("g", {"data-compartment": "name"})
        fs = self.cur_fs
        head_w = self._head_width(r.w, e.kind == "component")
        for ln in c.header:
            f = self._fit(ln, head_w, fs)
            if y + self._height(f) > bottom:
                # The name must show: squeeze it into what is left, with an ellipsis.
                f = fit_text(ln.text, max(head_w, 1.0), bottom - y, fs, bold=ln.bold)
                if f.truncated:
                    self.text_overflow(ln.text, f.font_size)
                if y + self._height(f) > bottom + 0.01:
                    dropped.append(ln.text)
                    continue
            node = self._line_node(ln, r.cx, y, "middle", color, f)
            if f.truncated:
                node.attrs["data-truncated"] = "true"
            head.add(node)
            y += self._height(f)
        g.add(head)
        y += 5
        if e.kind == "component":
            g.add(_component_icon(r.x + r.w - 20, r.y + 5, line, fill))
        for name, comp in c.compartments:
            if y >= bottom:
                break
            g.add(
                Node(
                    "path",
                    {
                        "d": f"M{fmt(r.x)} {fmt(y)} H{fmt(r.x + r.w)}",
                        "stroke": line,
                        "stroke-width": self.lw,
                        "fill": "none",
                    },
                )
            )
            cg = Node("g", {"data-compartment": name})
            yy = y + 3
            for ln in comp:
                f = self._fit(ln, r.w - 12, fs)
                if yy + self._height(f) > bottom:
                    dropped.append(ln.text)
                else:
                    cg.add(self._line_node(ln, r.x + 6, yy, "start", color, f))
                yy += self._height(f)
            g.add(cg)
            y += max(yy - y - 3 + 6, 10)
        for name, comp in c.compartments:
            if not any(n == name for n in (x.attrs.get("data-compartment") for x in g.children)):
                dropped.extend(ln.text for ln in comp)
        if dropped:
            self.text_overflow("; ".join(dropped), self.cur_fs)
        if e.kind == "component":
            self._interfaces(
                g, e, r, line, color, side=None, owner_obj=self.obj_of_element.get(e.id)
            )

    def _package(self, g: Node, e: UmlElementIn, r: Rect, fill: str, line: str, color: str) -> None:
        tab_w = min(r.w * 0.4, 60.0)
        common = {"fill": fill, "stroke": line, "stroke-width": self.lw}
        g.add(
            Node(
                "rect",
                {"data-part": "tab", "x": r.x, "y": r.y, "width": tab_w, "height": 12} | common,
            )
        )
        g.add(
            Node(
                "rect",
                {"data-part": "box", "x": r.x, "y": r.y + 12, "width": r.w, "height": r.h - 12}
                | common,
            )
        )
        g.add(
            self.text(
                e.name or e.id,
                r.cx,
                r.y + 18,
                color=color,
                extra={"font-weight": "bold"},
                width=r.w - 8,
            )
        )

    def _actor(self, g: Node, e: UmlElementIn, r: Rect, line: str, color: str) -> None:
        cx = r.cx
        fig_h = r.h - self.lh - 4
        s = fig_h / 20
        top = r.y
        g.add(
            Node(
                "g",
                {"data-part": "figure", "stroke": line, "fill": "none", "stroke-width": self.lw},
            )
        )
        fig = g.children[-1]
        fig.add(Node("circle", {"cx": cx, "cy": top + 3 * s, "r": 3 * s}))
        fig.add(
            Node(
                "path",
                {
                    "d": (
                        f"M{fmt(cx)} {fmt(top + 6 * s)} V{fmt(top + 13 * s)} "
                        f"M{fmt(cx - 6 * s)} {fmt(top + 8 * s)} H{fmt(cx + 6 * s)} "
                        f"M{fmt(cx - 5 * s)} {fmt(top + 20 * s)} L{fmt(cx)} {fmt(top + 13 * s)} "
                        f"L{fmt(cx + 5 * s)} {fmt(top + 20 * s)}"
                    )
                },
            )
        )
        g.add(self.text(e.name or e.id, cx, top + fig_h + 4, color=color, width=r.w))

    def _port(self, g: Node, e: UmlElementIn, r: Rect, fill: str, line: str, color: str) -> None:
        g.add(
            Node(
                "rect",
                {
                    "data-part": "port",
                    "x": r.x,
                    "y": r.y,
                    "width": r.w,
                    "height": r.h,
                    "fill": fill,
                    "stroke": line,
                    "stroke-width": self.lw,
                },
            )
        )
        if e.name:
            g.add(
                self.text(
                    e.name, r.cx, r.y - self.fs * 1.3 - 2, color=color, extra={"data-kind": "label"}
                )
            )
        side = "right"
        owner_obj = self.obj_of_element.get(e.owner_id or "")
        if owner_obj is not None and owner_obj in self.rects:
            side = "right" if r.cx >= self.rects[owner_obj].cx else "left"
        self._interfaces(g, e, r, line, color, side=side)

    def _interfaces(
        self,
        g: Node,
        e: UmlElementIn,
        r: Rect,
        line: str,
        color: str,
        side: str | None,
        owner_obj: str | None = None,
    ) -> None:
        """Provided → lollipop, required → socket. Components: provided right,
        required left. Ports: both outward on ``side``."""
        items = [("provided", ref) for ref in e.attributes.provided] + [
            ("required", ref) for ref in e.attributes.required
        ]
        if not items:
            return
        groups: dict[str, list[tuple[str, str]]] = {}
        for kind, ref in items:
            s = side or ("right" if kind == "provided" else "left")
            groups.setdefault(s, []).append((kind, ref))
        for s, lst in groups.items():
            taken = self.right_slots_taken.get(owner_obj or "", 0) if s == "right" else 0
            for i, (kind, ref) in enumerate(lst):
                if side is not None:  # port: fan out around the port centre
                    y = r.cy + (i - (len(lst) - 1) / 2) * 22
                else:
                    y = r.y + (i + 1 + taken) * r.h / (len(lst) + taken + 1)
                sign = 1 if s == "right" else -1
                x0 = r.x + r.w if s == "right" else r.x
                x1 = x0 + sign * 22
                ig = Node(
                    "g",
                    {
                        "data-kind": kind,
                        "data-interface": ref,
                        "stroke": line,
                        "fill": "none",
                        "stroke-width": self.lw,
                    },
                )
                if kind == "provided":
                    ig.add(Node("path", {"d": f"M{fmt(x0)} {fmt(y)} H{fmt(x1)}"}))
                    ig.add(
                        Node("circle", {"cx": x1 + sign * 6, "cy": y, "r": 6, "fill": "#ffffff"})
                    )
                    tip = x1 + sign * 12
                else:
                    ig.add(Node("path", {"d": f"M{fmt(x0)} {fmt(y)} H{fmt(x1 + sign * 2)}"}))
                    sweep = 1 if s == "left" else 0
                    ax = x1 + sign * 9
                    ig.add(
                        Node(
                            "path",
                            {
                                "d": f"M{fmt(ax)} {fmt(y - 8)} A8 8 0 0 {sweep} {fmt(ax)} {fmt(y + 8)}"
                            },
                        )
                    )
                    tip = ax
                self.extent.add_point(tip, y - 8)
                self.extent.add_point(tip, y + 8)
                name = self.type_name(ref) or ref
                ig.add(
                    self.text(
                        name,
                        tip + sign * 4,
                        y - self.fs * 0.6,
                        anchor="start" if s == "right" else "end",
                        color=color,
                        extra={"data-kind": "label", "stroke": "none"},
                        halo=True,
                    )
                )
                g.add(ig)

    # -------------------------------------------------------------- connections

    def _geometry(self, c: UmlDiagramConnectionIn) -> list[Pt] | None:
        s = self.rects.get(c.source_object_id)
        t = self.rects.get(c.target_object_id)
        if s is None or t is None:
            missing = [
                x for x, rr in ((c.source_object_id, s), (c.target_object_id, t)) if rr is None
            ]
            self.diags.add(
                "RENDER-UML-CON-003",
                "warning",
                f"diagram connection {c.id}: endpoint(s) {', '.join(missing)} not on the diagram; not drawn",
                [("diagram_connection", c.id)],
                {"missing": missing},
            )
            return None
        bends: list[Pt] = [(float(p.x), float(p.y)) for p in c.bendpoints or []]
        if not bends and c.source_object_id == c.target_object_id:
            bends = [(s.x + s.w + 30, s.cy), (s.x + s.w + 30, s.y - 25), (s.cx, s.y - 25)]
        elif not bends and (
            c.source_object_id in self.synthesized or c.target_object_id in self.synthesized
        ):
            ends = {c.source_object_id, c.target_object_id}
            for ref in list(ends):  # a component's own ports are not obstacles to it
                ends |= {o for o, r in self.port_owner.items() if r == ref}
            obstacles = [r for o, r in self.rects.items() if o not in ends]
            bends = route(s, s.center, t, t.center, obstacles)
        start = clip_rect(s, bends[0] if bends else t.center)
        end = clip_rect(t, bends[-1] if bends else s.center)
        return [start, *bends, end]

    def _end_labels(
        self,
        g: Node,
        pts: list[Pt],
        at_start: bool,
        role: str | None,
        mult: str | None,
        color: str,
        box: Rect | None = None,
    ) -> None:
        a, b = (pts[0], pts[1]) if at_start else (pts[-1], pts[-2])
        px, py = perpendicular(a, b)
        for txt, sign, kind in ((role, 1, "role"), (mult, -1, "multiplicity")):
            if not txt:
                continue
            ox, oy = px * sign, py * sign
            # Anchor away from the line so the text never lies across it.
            anchor = "start" if ox > 0.3 else ("end" if ox < -0.3 else "middle")
            w = text_width(txt, self.fs)
            lh = self.fs * LINE_HEIGHT
            dist = 24.0  # clear of the end's diamond / arrowhead
            for _ in range(12):  # walk along the line until the label clears the end's box
                base = along(a, b, dist)
                x, y = base[0] + ox * 8, base[1] + oy * 8
                y_top = y - lh if oy < -0.3 else (y if oy > 0.3 else y - lh / 2)
                left = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
                pad = 4  # backdrop + descenders
                clear = box is None or not (
                    left - pad < box.x + box.w
                    and left + w + pad > box.x
                    and y_top - pad < box.y + box.h
                    and y_top + lh + pad > box.y
                )
                if clear:
                    break
                dist += 6
            g.add(
                self.text(
                    txt,
                    x,
                    y_top,
                    anchor=anchor,
                    color=color,
                    extra={"data-kind": kind, "data-end": "source" if at_start else "target"},
                    halo=True,
                )
            )

    def _draw_connection(self, cid: str) -> Node | None:
        c = self.conns[cid]
        pts = self._geometry(c)
        if pts is None:
            return None
        for x, y in pts:
            self.extent.add_point(x, y)
        look = self.con_style.read(
            c.style, cid, fill="none", line=LINE, font_color=TEXT, font_size=self.fs
        )
        line, color = look.line, look.font_color
        self.lw = look.line_width
        self.text_style = look.text_attrs()
        g = Node(
            "g",
            {
                "data-kind": "connection",
                "data-connection-id": cid,
                "data-source": c.source_object_id,
                "data-target": c.target_object_id,
            },
        )
        r = self.rels.get(c.relationship_ref or "")
        if r is None or r.kind not in RELATIONSHIP_STYLE:
            if r is None:
                self.diags.add(
                    "RENDER-UML-CON-001",
                    "warning",
                    f"diagram connection {cid} references relationship {c.relationship_ref}, "
                    "which is not in the snapshot",
                    [("diagram_connection", cid)],
                )
            else:
                self.diags.add(
                    "RENDER-UML-CON-002",
                    "warning",
                    f"diagram connection {cid}: relationship {r.id} has unsupported kind {r.kind}",
                    [("diagram_connection", cid), ("uml_relationship", r.id)],
                )
            g.attrs["data-kind"] = "unresolved-connection"
            g.add(
                Node(
                    "path",
                    {
                        "d": path_d(pts),
                        "fill": "none",
                        "stroke": UNRESOLVED_STROKE,
                        "stroke-dasharray": "2 2",
                    },
                )
            )
            return g
        g.attrs["data-relationship-id"] = r.id
        g.attrs["data-relationship-type"] = r.kind
        dash, m_end, keyword = RELATIONSHIP_STYLE[r.kind]
        m_start: str | None = None
        attrs: dict[str, Any] = {
            "d": path_d(pts),
            "fill": "none",
            "stroke": line,
            "stroke-width": self.lw,
        }
        if dash:
            attrs["stroke-dasharray"] = dash
        labels = [x for x in (keyword, r.name) if x]
        if r.kind == "association":
            ends = r.ends or []
            if len(ends) == 2:
                src_el = self.objs[c.source_object_id].element_ref
                first, second = (
                    (ends[1], ends[0])
                    if (src_el == ends[1].element_ref and src_el != ends[0].element_ref)
                    else (ends[0], ends[1])
                )
                for end, other, at_start in ((first, second, True), (second, first, False)):
                    diamond = {"composite": "diamond-filled", "shared": "diamond-hollow"}.get(
                        other.aggregation
                    )
                    which = "source" if at_start else "target"
                    adorn = [
                        x
                        for x in (diamond and other.aggregation, end.navigable and "navigable")
                        if x
                    ]
                    g.attrs[f"data-{which}-end"] = " ".join(adorn) if adorn else "none"
                    mark = diamond or ("arrow-open" if end.navigable else None)
                    if diamond and end.navigable:
                        g.add(self._arrow_beyond_diamond(pts, at_start, line))
                    if at_start:
                        m_start = mark
                    else:
                        m_end = mark
                    end_box = self.rects.get(c.source_object_id if at_start else c.target_object_id)
                    self._end_labels(g, pts, at_start, end.role, end.multiplicity, color, end_box)
            else:
                self.diags.add(
                    "RENDER-UML-CON-004",
                    "warning",
                    f"association {r.id} does not have exactly two ends; drawn as a plain line",
                    [("uml_relationship", r.id)],
                )
        elif r.kind == "connector":
            g.attrs["data-connector-kind"] = r.connector_kind or ""
            if r.connector_kind == "delegation":
                m_end = "arrow-open"
                labels.insert(0, "«delegate»")
            elif r.connector_kind == "assembly":
                g.add(self._ball_socket(pts, line))
        if m_start:
            attrs["marker-start"] = self.markers.ref(m_start, line)
        if m_end:
            attrs["marker-end"] = self.markers.ref(m_end, line)
        g.children.insert(0, Node("path", attrs))
        if labels:
            mx, my = polyline_midpoint(pts)
            g.add(
                self.text(
                    " ".join(labels),
                    mx,
                    my - self.lh / 2,
                    color=color,
                    extra={"data-kind": "label"},
                    halo=True,
                )
            )
        return g

    def _arrow_beyond_diamond(self, pts: list[Pt], at_start: bool, line: str) -> Node:
        """Open navigability arrowhead placed just outside an end's diamond."""
        a, b = (pts[0], pts[1]) if at_start else (pts[-1], pts[-2])
        tip = along(a, b, 15)  # the diamond marker is 14 long
        back = along(a, b, 25)
        px, py = perpendicular(a, b)
        d = (
            f"M{fmt(back[0] + px * 5)} {fmt(back[1] + py * 5)} L{fmt(tip[0])} {fmt(tip[1])} "
            f"L{fmt(back[0] - px * 5)} {fmt(back[1] - py * 5)}"
        )
        return Node(
            "path",
            {
                "data-kind": "navigability",
                "d": d,
                "fill": "none",
                "stroke": line,
                "stroke-width": self.lw,
            },
        )

    def _ball_socket(self, pts: list[Pt], line: str) -> Node:
        mx, my = polyline_midpoint(pts)
        g = Node("g", {"data-kind": "ball-socket", "stroke": line, "stroke-width": self.lw})
        g.add(Node("circle", {"cx": mx, "cy": my, "r": 5, "fill": "#ffffff"}))
        g.add(
            Node(
                "circle", {"cx": mx, "cy": my, "r": 9, "fill": "none", "stroke-dasharray": "14 14"}
            )
        )
        return g

    def render(self) -> RenderResult:
        self._layout()
        objs = Node("g", {"data-kind": "objects"})
        # Ports after their owners so they sit on top of the border.
        order = sorted(
            self.objs,
            key=lambda oid: (
                1
                if (self.elements.get(self.objs[oid].element_ref or "") or _NoKind).kind == "port"
                else 0,
                list(self.objs).index(oid),
            ),
        )
        for oid in order:
            self.reset_look()
            objs.add(self._draw_object(oid))
        conns = Node("g", {"data-kind": "connections"})
        for cid in self.conns:
            self.reset_look()
            n = self._draw_connection(cid)
            if n is not None:
                conns.add(n)
        return self.finish([objs, conns], {"data-diagram-kind": self.diagram.kind})


class _NoKind:
    kind = ""


def _component_icon(x: float, y: float, line: str, fill: str) -> Node:
    g = Node(
        "g",
        {
            "data-kind": "icon",
            "data-glyph": "component",
            "stroke": line,
            "fill": fill,
            "stroke-width": 1,
        },
    )
    g.add(Node("rect", {"x": x + 3, "y": y, "width": 12, "height": 14}))
    g.add(Node("rect", {"x": x, "y": y + 3, "width": 6, "height": 3}))
    g.add(Node("rect", {"x": x, "y": y + 8, "width": 6, "height": 3}))
    return g


# ============================================================================
# Sequence diagrams
# ============================================================================

HEAD_H = 36.0
ROW = 36.0
HEADER_GAP = 26.0
EXEC_W = 10.0


class _SequenceRenderer(_Base):
    def __init__(self, snapshot: UmlSnapshot, diagram: UmlDiagramIn, opts: RenderOptions) -> None:
        super().__init__(snapshot, diagram, opts)
        self.interaction: UmlInteractionIn | None = None
        self.lifeline_obj: dict[str, str] = {}
        self.x_of: dict[str, float] = {}
        self.head_w: dict[str, float] = {}
        self.head_top: dict[str, float] = {}
        self.occ_y: dict[str, float] = {}
        self.msg_y: dict[str, float] = {}
        self.header_y: dict[tuple[int, int], float] = {}
        self.bottom = 0.0

    def _pick_interaction(self) -> None:
        owners: dict[str, UmlInteractionIn] = {}
        for it in self.snapshot.interactions:
            for ll in it.lifelines:
                owners.setdefault(ll.id, it)
        picked: list[str] = []
        for o in self.objs.values():
            if o.lifeline_id and o.lifeline_id in owners:
                it = owners[o.lifeline_id]
                if it.id not in picked:
                    picked.append(it.id)
        if not picked:
            return
        self.interaction = next(i for i in self.snapshot.interactions if i.id == picked[0])
        if len(picked) > 1:
            self.diags.add(
                "RENDER-UML-SEQ-001",
                "warning",
                f"diagram {self.diagram.id} shows lifelines of {len(picked)} interactions; "
                f"only {picked[0]} is drawn",
                [("diagram", self.diagram.id)],
                {"interactions": picked},
            )

    def _messages(self) -> list[UmlMessage]:
        assert self.interaction is not None
        by_id = {m.id: m for m in self.interaction.messages}
        chosen: list[UmlMessage] = []
        msg_conns = [c for c in self.conns.values() if c.message_id is not None]
        for c in self.conns.values():
            if c.message_id is None or c.message_id not in by_id:
                self.diags.add(
                    "RENDER-UML-SEQ-002",
                    "warning",
                    f"diagram connection {c.id} references message {c.message_id}, "
                    f"which is not in interaction {self.interaction.id}",
                    [("diagram_connection", c.id)],
                )
                continue
            if by_id[c.message_id] not in chosen:
                chosen.append(by_id[c.message_id])
        if not msg_conns:
            shown = set(self.lifeline_obj)
            occ_ll = {o.id: o.lifeline for o in self.interaction.occurrences}
            chosen = [
                m
                for m in self.interaction.messages
                if occ_ll.get(m.send_occurrence) in shown
                and occ_ll.get(m.receive_occurrence) in shown
            ]
            if chosen:
                self.diags.add(
                    "RENDER-UML-SEQ-003",
                    "advice",
                    f"diagram {self.diagram.id} lists no message connections; "
                    f"all {len(chosen)} messages between its lifelines are drawn",
                    [("diagram", self.diagram.id)],
                )
        return chosen

    def _time_axis(self, messages: list[UmlMessage]) -> None:
        """y for every occurrence, walking ``occurrences`` in stored order."""
        assert self.interaction is not None
        it = self.interaction
        msg_of: dict[str, UmlMessage] = {}
        for m in it.messages:
            msg_of.setdefault(m.send_occurrence, m)
            msg_of.setdefault(m.receive_occurrence, m)
        order = {o.id: i for i, o in enumerate(it.occurrences)}
        # Fragment/operand headers open at the operand's first occurrence.
        frag_sets = [{x for op in f.operands for x in op.occurrences} for f in it.fragments]
        headers: dict[str, list[tuple[int, int, int]]] = {}
        closers: dict[str, int] = {}
        for fi, f in enumerate(it.fragments):
            for oi, op in enumerate(f.operands):
                known = [x for x in op.occurrences if x in order]
                if known:
                    first = min(known, key=order.__getitem__)
                    headers.setdefault(first, []).append((-len(frag_sets[fi]), fi, oi))
            known_all = [x for x in frag_sets[fi] if x in order]
            if known_all:
                last = max(known_all, key=order.__getitem__)
                closers[last] = closers.get(last, 0) + 1
        y = HEAD_H + 30
        pending_close = 0
        for o in it.occurrences:
            m = msg_of.get(o.id)
            if m is not None and m.id in self.msg_y:
                self.occ_y[o.id] = self.msg_y[m.id]
            else:
                y += pending_close * 12
                pending_close = 0
                for _, fi, oi in sorted(headers.get(o.id, [])):
                    self.header_y[(fi, oi)] = y
                    y += HEADER_GAP
                self.occ_y[o.id] = y
                if m is not None:
                    self.msg_y[m.id] = y
                y += ROW
            pending_close += closers.get(o.id, 0)
        self.bottom = y + pending_close * 12

    def _layout_heads(self) -> None:
        assert self.interaction is not None
        lifelines = {ll.id: ll for ll in self.interaction.lifelines}
        x = 0.0
        for oid, o in self.objs.items():
            ll = lifelines.get(o.lifeline_id or "")
            if ll is None or ll.id in self.lifeline_obj:
                continue
            self.lifeline_obj[ll.id] = oid
            label = self._head_label(ll.id)
            w = max(90.0, text_width(label, self.fs) + 16)
            if o.bounds is not None and o.bounds.w > 0:
                w = o.bounds.w
            if o.bounds is not None:
                cx = o.bounds.x + w / 2
            else:
                cx = x + w / 2
            self.x_of[ll.id] = cx
            self.head_w[ll.id] = w
            self.head_top[ll.id] = 0.0
            x = max(x, cx + w / 2) + 50

    def _message_look(self, message_id: str) -> Look:
        conn = next((c for c in self.conns.values() if c.message_id == message_id), None)
        if conn is None:
            return Look(fill="none", line=LINE, font_color=TEXT, font_size=self.fs)
        return self.con_style.read(
            conn.style, conn.id, fill="none", line=LINE, font_color=TEXT, font_size=self.fs
        )

    def _head_label(self, lifeline_id: str) -> str:
        assert self.interaction is not None
        ll = next(x for x in self.interaction.lifelines if x.id == lifeline_id)
        t = self.type_name(ll.represents_ref)
        return f"{ll.name} : {t}" if t else (ll.name or ll.id)

    def render(self) -> RenderResult:
        self._pick_interaction()
        body: list[Node] = []
        if self.interaction is None:
            self.diags.add(
                "RENDER-UML-SEQ-004",
                "warning",
                f"diagram {self.diagram.id}: no diagram object names a lifeline of any interaction",
                [("diagram", self.diagram.id)],
            )
        else:
            body = self._render_interaction()
        # Objects that are not lifelines of the drawn interaction.
        extra = Node("g", {"data-kind": "unresolved-objects"})
        y = self.bottom + 40
        for oid, o in self.objs.items():
            if self.interaction is not None and oid in self.lifeline_obj.values():
                continue
            self.diags.add(
                "RENDER-UML-OBJ-001",
                "warning",
                f"diagram object {oid} does not reference a lifeline of the drawn interaction",
                [("diagram_object", oid)],
                {"lifeline_id": o.lifeline_id},
            )
            extra.add(self.placeholder(oid, Rect(0, y, 140, 40), f"lifeline {o.lifeline_id}"))
            y += 50
        if extra.children:
            body.append(extra)
        return self.finish(
            body,
            {
                "data-diagram-kind": "sequence",
                "data-interaction-id": self.interaction.id if self.interaction else "",
            },
        )

    def _render_interaction(self) -> list[Node]:
        it = self.interaction
        assert it is not None
        self._layout_heads()
        messages = self._messages()
        self._time_axis(messages)
        occ_ll = {o.id: o.lifeline for o in it.occurrences}
        placeable: list[UmlMessage] = []
        for m in messages:
            s_ll, r_ll = occ_ll.get(m.send_occurrence), occ_ll.get(m.receive_occurrence)
            if s_ll not in self.x_of or r_ll not in self.x_of or m.id not in self.msg_y:
                self.diags.add(
                    "RENDER-UML-SEQ-005",
                    "warning",
                    f"message {m.id}: an occurrence or lifeline is missing from the diagram; not drawn",
                    [("interaction", it.id)],
                    {"message_id": m.id},
                )
                continue
            placeable.append(m)
        placeable.sort(key=lambda m: self.msg_y[m.id])
        # Created lifelines start at their create message; destroyed ones end at delete.
        end_y: dict[str, float] = {}
        for m in placeable:
            r_ll = occ_ll[m.receive_occurrence]
            if m.kind == "create":
                self.head_top[r_ll] = self.msg_y[m.id] - HEAD_H / 2
            elif m.kind == "delete":
                end_y[r_ll] = self.msg_y[m.id]
        bottom = self.bottom + 10

        frames = Node("g", {"data-kind": "fragments"})
        heads = Node("g", {"data-kind": "lifelines"})
        execs = Node("g", {"data-kind": "executions"})
        msgs = Node("g", {"data-kind": "messages"})

        for ll_id, cx in self.x_of.items():
            w = self.head_w[ll_id]
            top = self.head_top[ll_id]
            label = self._head_label(ll_id)
            oid = self.lifeline_obj[ll_id]
            self.reset_look()
            self.label_target = ("diagram_object", oid)
            look = self.obj_style.read(
                self.objs[oid].style, oid, fill=FILL, line=LINE, font_color=TEXT, font_size=self.fs
            )
            self.text_style = look.text_attrs()
            self.cur_fs = look.font_size
            g = Node(
                "g",
                {
                    "data-kind": "lifeline",
                    "data-lifeline-id": ll_id,
                    "data-object-id": self.lifeline_obj[ll_id],
                    "data-x": cx,
                },
            )
            g.add(
                Node(
                    "rect",
                    {
                        "data-part": "head",
                        "x": cx - w / 2,
                        "y": top,
                        "width": w,
                        "height": HEAD_H,
                    }
                    | look.shape_attrs(),
                )
            )
            g.add(
                self.text(
                    label,
                    cx,
                    top + (HEAD_H - self.cur_fs * 1.2) / 2,
                    color=look.font_color,
                    extra={"data-kind": "label"},
                    width=w - 8,
                )
            )
            ly1 = end_y.get(ll_id, bottom)
            g.add(
                Node(
                    "path",
                    {
                        "data-part": "line",
                        "d": f"M{fmt(cx)} {fmt(top + HEAD_H)} V{fmt(ly1)}",
                        "stroke": LINE,
                        "stroke-dasharray": "5 4",
                        "fill": "none",
                    },
                )
            )
            if ll_id in end_y:
                y = end_y[ll_id]
                g.add(
                    Node(
                        "path",
                        {
                            "data-part": "destruction",
                            "d": f"M{fmt(cx - 8)} {fmt(y - 8)} L{fmt(cx + 8)} {fmt(y + 8)} M{fmt(cx - 8)} {fmt(y + 8)} L{fmt(cx + 8)} {fmt(y - 8)}",
                            "stroke": LINE,
                            "stroke-width": 2,
                        },
                    )
                )
            self.extent.add_rect(cx - w / 2, top, w, max(ly1 - top, HEAD_H))
            heads.add(g)

        # Execution bars (nested ones on the same lifeline offset to the right).
        active: dict[str, list[tuple[float, float]]] = {}
        for ex in it.executions:
            y0, y1 = self.occ_y.get(ex.start), self.occ_y.get(ex.finish)
            if ex.lifeline not in self.x_of or y0 is None or y1 is None:
                self.diags.add(
                    "RENDER-UML-SEQ-006",
                    "warning",
                    f"execution {ex.id}: lifeline or start/finish occurrence not placeable; not drawn",
                    [("interaction", it.id)],
                    {"execution_id": ex.id},
                )
                continue
            y0, y1 = min(y0, y1), max(y0, y1)
            spans = active.setdefault(ex.lifeline, [])
            depth = sum(1 for a, b in spans if a <= y0 and y1 <= b)
            spans.append((y0, y1))
            x = self.x_of[ex.lifeline] - EXEC_W / 2 + depth * EXEC_W / 2
            execs.add(
                Node(
                    "rect",
                    {
                        "data-kind": "execution",
                        "data-execution-id": ex.id,
                        "data-lifeline-id": ex.lifeline,
                        "x": x,
                        "y": y0,
                        "width": EXEC_W,
                        "height": max(y1 - y0, 4),
                        "fill": "#ffffff",
                        "stroke": LINE,
                        "stroke-width": 1,
                    },
                )
            )

        def bar_offset(ll: str, y: float) -> float:
            return EXEC_W / 2 if any(a <= y <= b for a, b in active.get(ll, [])) else 0.0

        for seq, m in enumerate(placeable):
            s_ll, r_ll = occ_ll[m.send_occurrence], occ_ll[m.receive_occurrence]
            y = self.msg_y[m.id]
            sx, rx = self.x_of[s_ll], self.x_of[r_ll]
            dash, marker = MESSAGE_STYLE.get(m.kind, (None, "arrow-open"))
            if m.kind not in MESSAGE_STYLE:
                self.diags.add(
                    "RENDER-UML-SEQ-007",
                    "warning",
                    f"message {m.id} has unsupported kind {m.kind!r}; drawn as asynch",
                    [("interaction", it.id)],
                )
            self.reset_look()
            mlook = self._message_look(m.id)
            self.text_style = mlook.text_attrs()
            g = Node(
                "g",
                {
                    "data-kind": "message",
                    "data-message-id": m.id,
                    "data-message-kind": m.kind,
                    "data-seq": seq,
                    "data-y": y,
                },
            )
            if s_ll == r_ll:
                off = bar_offset(s_ll, y)
                x0 = sx + off
                pts: list[Pt] = [(x0, y), (x0 + 36, y), (x0 + 36, y + 16), (x0 + off, y + 16)]
            else:
                sign = 1 if rx > sx else -1
                x0 = sx + sign * bar_offset(s_ll, y)
                if m.kind == "create":
                    x1 = rx - sign * self.head_w[r_ll] / 2
                else:
                    x1 = rx - sign * bar_offset(r_ll, y)
                pts = [(x0, y), (x1, y)]
            attrs: dict[str, Any] = {
                "d": path_d(pts),
                "fill": "none",
                "marker-end": self.markers.ref(marker, mlook.line),
            } | mlook.stroke_attrs()
            if dash:
                attrs["stroke-dasharray"] = dash
            g.add(Node("path", attrs))
            label = m.name + (
                f"({', '.join(m.arguments)})"
                if m.arguments or m.kind in ("synch_call", "asynch_call")
                else ""
            )
            if m.kind == "create" and not m.name:
                label = "«create»"
            if m.kind == "delete" and not m.name:
                label = "«destroy»"
            if label:
                lx = (pts[0][0] + pts[1][0]) / 2 if s_ll != r_ll else pts[0][0] + 40
                anchor = "middle" if s_ll != r_ll else "start"
                g.add(
                    self.text(
                        label,
                        lx,
                        y - self.fs * 1.2 - 3,
                        anchor=anchor,
                        extra={"data-kind": "label"},
                        halo=True,
                    )
                )
            for x, yy in pts:
                self.extent.add_point(x, yy)
            msgs.add(g)

        self._fragments(frames)
        frame = self._sd_frame(it.name or it.id)
        return [frame, heads, execs, frames, msgs]

    def _fragments(self, out: Node) -> None:
        it = self.interaction
        assert it is not None
        occ_ll = {o.id: o.lifeline for o in it.occurrences}
        sets = [{x for op in f.operands for x in op.occurrences} for f in it.fragments]
        for fi, f in enumerate(it.fragments):
            ys = [self.occ_y[x] for x in sets[fi] if x in self.occ_y]
            if not ys:
                self.diags.add(
                    "RENDER-UML-SEQ-008",
                    "warning",
                    f"fragment {f.id} covers no placed occurrence; not drawn",
                    [("interaction", it.id)],
                    {"fragment_id": f.id},
                )
                continue
            lls = [ll for ll in f.covered if ll in self.x_of] or sorted(
                {occ_ll[x] for x in sets[fi] if x in occ_ll and occ_ll[x] in self.x_of}
            )
            if not lls:
                continue
            depth = sum(1 for j, s in enumerate(sets) if j != fi and sets[fi] < s)
            left = min(self.x_of[ll] - self.head_w[ll] / 2 for ll in lls) - 10 + depth * 8
            right = max(self.x_of[ll] + self.head_w[ll] / 2 for ll in lls) + 10 - depth * 8
            tw = text_width(f.operator, self.fs, bold=True) + 18
            guard_w = max(
                (text_width(f"[{op.guard}]", self.fs) for op in f.operands if op.guard), default=0
            )
            right = max(right, left + tw + guard_w + 24)
            for m in it.messages:  # self-messages loop out to the right of the lifeline
                if m.send_occurrence in sets[fi] and m.receive_occurrence in sets[fi]:
                    ll = occ_ll.get(m.send_occurrence)
                    if (
                        ll is not None
                        and ll == occ_ll.get(m.receive_occurrence)
                        and ll in self.x_of
                    ):
                        right = max(right, self.x_of[ll] + 90)
            op_tops = [self.header_y.get((fi, oi)) for oi in range(len(f.operands))]
            top = next((t for t in op_tops if t is not None), min(ys) - HEADER_GAP)
            bottom = max(ys) + 14
            g = Node(
                "g",
                {
                    "data-kind": "fragment",
                    "data-fragment-id": f.id,
                    "data-operator": f.operator,
                    "data-x": left,
                    "data-y": top,
                    "data-w": right - left,
                    "data-h": bottom - top,
                },
            )
            g.add(
                Node(
                    "rect",
                    {
                        "data-part": "frame",
                        "x": left,
                        "y": top,
                        "width": right - left,
                        "height": bottom - top,
                        "fill": "none",
                        "stroke": LINE,
                        "stroke-width": 1,
                    },
                )
            )
            th = self.fs + 6
            g.add(
                Node(
                    "path",
                    {
                        "data-part": "operator-tab",
                        "d": f"M{fmt(left)} {fmt(top)} H{fmt(left + tw)} V{fmt(top + th - 5)} L{fmt(left + tw - 6)} {fmt(top + th)} H{fmt(left)} Z",
                        "fill": "#ffffff",
                        "stroke": LINE,
                        "stroke-width": 1,
                    },
                )
            )
            g.add(
                self.text(
                    f.operator,
                    left + 5,
                    top + 2,
                    anchor="start",
                    extra={"font-weight": "bold", "data-kind": "operator"},
                )
            )
            self.extent.add_rect(left, top, right - left, bottom - top)
            for oi, op in enumerate(f.operands):
                oy = op_tops[oi]
                if oy is None:
                    continue
                if oi > 0:
                    g.add(
                        Node(
                            "path",
                            {
                                "data-part": "separator",
                                "data-operand": oi,
                                "d": f"M{fmt(left)} {fmt(oy)} H{fmt(right)}",
                                "stroke": LINE,
                                "stroke-dasharray": "6 4",
                                "fill": "none",
                            },
                        )
                    )
                if op.guard:
                    gx = left + (tw + 8 if oi == 0 else 8)
                    g.add(
                        self.text(
                            f"[{op.guard}]",
                            gx,
                            oy + 3,
                            anchor="start",
                            extra={"data-kind": "guard", "data-operand": oi},
                            halo=True,
                        )
                    )
            out.add(g)

    def _sd_frame(self, name: str) -> Node:
        x0, y0 = self.extent.x0 - 14, self.extent.y0 - 30
        x1, y1 = self.extent.x1 + 14, self.extent.y1 + 14
        label = f"sd {name}"
        tw = text_width(label, self.fs, bold=True) + 18
        th = self.fs + 6
        g = Node("g", {"data-kind": "interaction-frame"})
        g.add(
            Node(
                "rect",
                {
                    "x": x0,
                    "y": y0,
                    "width": x1 - x0,
                    "height": y1 - y0,
                    "fill": "none",
                    "stroke": LINE,
                    "stroke-width": 1,
                },
            )
        )
        g.add(
            Node(
                "path",
                {
                    "d": f"M{fmt(x0)} {fmt(y0)} H{fmt(x0 + tw)} V{fmt(y0 + th - 5)} L{fmt(x0 + tw - 6)} {fmt(y0 + th)} H{fmt(x0)} Z",
                    "fill": "#ffffff",
                    "stroke": LINE,
                    "stroke-width": 1,
                },
            )
        )
        g.add(self.text(label, x0 + 5, y0 + 2, anchor="start", extra={"font-weight": "bold"}))
        self.extent.add_rect(x0, y0, x1 - x0, y1 - y0)
        return g
