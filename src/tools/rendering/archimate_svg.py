"""ArchiMate 3.2 view → SVG.

Input: an :class:`~schemas.modeling.ArchimateSnapshot` and a view id. Output: a
sanitized, deterministic SVG plus diagnostics. Every element is drawn with the
figure geometry ported from Archi 5.10.0 (``tools.rendering.archi``): figure
type 0 plus the small type icon for ``style.notation == "box"`` (the default),
figure type 1 for ``"icon"``; Archi's default fills, junction and grouping
figures, and its Note and Group diagram objects (``kind`` note/group). Labels
honour Archi label expressions (``${name}``, ``${type}``, ``${documentation}``,
``${property:key}``); plain ``line`` connections and relationship connections
carry optional labels. The 11 relationship types keep their line/marker styles.

Geometry conventions (Archi / Open Group exchange):

* object ``bounds`` are relative to the parent occurrence; ``w``/``h`` of -1
  mean the default size; objects with no bounds get a deterministic layered
  layout (``tools.rendering.layout``: rows by ArchiMate layer, barycenter
  ordering, children inside parents) and a ``RENDER-AM-LAYOUT-001`` diagnostic;
  connections touching an auto-placed object are routed orthogonally around
  unrelated boxes where a simple detour exists;
* connection ``bendpoints`` are absolute view coordinates;
* a connection may end on another connection: it attaches at that
  connection's path midpoint.

The renderer has no authority over semantics: it draws what it is given and
only reports what it could not draw faithfully. Legality is the validator's job.

Every SVG element carries ``data-*`` attributes naming what it draws
(``data-object-id``, ``data-concept-type``, ``data-relationship-type`` …) so
callers and tests can address shapes without parsing geometry.
"""

from __future__ import annotations

import re
from typing import Any

from schemas.modeling import (
    ArchimateConceptIn,
    ArchimateRelationshipIn,
    ArchimateSnapshot,
    ArchimateViewConnectionIn,
    ArchimateViewIn,
    ArchimateViewObjectIn,
    TargetKind,
)
from tools.architecture.registry import get_registry
from tools.rendering.archi import FIGURES, TYPE_FIGURE
from tools.rendering.archi.diagram import (
    GROUP_BORDERS,
    GROUP_DEFAULT_FILL,
    GROUP_DEFAULT_SIZE,
    NOTE_BORDERS,
    NOTE_DEFAULT_FILL,
    NOTE_DEFAULT_SIZE,
    group_figure,
    note_figure,
)
from tools.rendering.archi.graphics import G, R
from tools.rendering.archi.spec import (
    TEXT_ALIGNMENT_CENTER,
    TEXT_ALIGNMENT_LEFT,
    TEXT_ALIGNMENT_RIGHT,
    Ctx,
    default_text_bounds,
)
from tools.rendering.layout import layered_layout, route
from tools.rendering.svg import (
    LINE_HEIGHT,
    Diagnostics,
    Extent,
    Look,
    MarkerSet,
    Node,
    Pt,
    Rect,
    RenderOptions,
    RenderResult,
    RenderTargetNotFoundError,
    StyleReader,
    backdrop,
    clip_circle,
    clip_rect,
    document,
    fit_text,
    fmt,
    path_d,
    polyline_midpoint,
    text_node,
    text_width,
)

RENDERER_VERSION = "archimate-svg/1.0.0"
RULESET_ID = "render/archimate-svg-1"

DEFAULT_SIZE = (120, 55)
TYPE_DEFAULT_SIZE = {"junction": (15, 15), "grouping": (400, 140)}

# Archi's default fill colours (Preferences → Colours, "Default").
LAYER_FILL = {
    "strategy": "#f5deaa",
    "business": "#ffffb5",
    "application": "#b5ffff",
    "technology": "#c9e7b7",
    "physical": "#c9e7b7",
    "motivation": "#ccccff",
    "implementation_migration": "#ffe0e0",
    "other": "#ffffff",
}
TYPE_FILL = {
    "plateau": "#e0ffe0",
    "gap": "#e0ffe0",
    "location": "#fbb875",
    "grouping": "#d2d7d7",
    "junction": "#000000",
}
LINE_COLOR = "#000000"
FONT_COLOR = "#000000"
UNRESOLVED_STROKE = "#c00000"
UNRESOLVED_FILL = "#fff0f0"

# Auto-layout row order (top to bottom).
LAYER_ORDER = (
    "motivation",
    "strategy",
    "business",
    "application",
    "technology",
    "physical",
    "implementation_migration",
    "other",
    "",
)

# Archi's text control margins (AbstractTextControlContainerFigure).
TEXT_MARGIN_WIDTH = 4
TEXT_MARGIN_HEIGHT = 4
LABEL_EXPR = re.compile(r"\$\{([a-z]+(?::[^}]*)?)\}")

# relationship type → (dasharray, source marker, target marker)
RELATIONSHIP_STYLE: dict[str, tuple[str | None, str | None, str | None]] = {
    "composition": (None, "diamond-filled", None),
    "aggregation": (None, "diamond-hollow", None),
    "assignment": (None, "dot", "arrow-filled"),
    "realization": ("4 3", None, "triangle-hollow"),
    "serving": (None, None, "arrow-open"),
    "access": ("1 3", None, None),  # markers depend on access_type
    "influence": ("8 4", None, "arrow-open"),
    "triggering": (None, None, "arrow-filled"),
    "flow": ("6 3", None, "arrow-filled"),
    "specialization": (None, None, "triangle-hollow"),
    "association": (None, None, None),  # directed → half arrow at target
}

# access_type → (marker at source, marker at target). Archi's default is write.
ACCESS_MARKERS: dict[str, tuple[str | None, str | None]] = {
    "write": (None, "access-arrow"),
    "read": ("access-arrow", None),
    "readwrite": ("access-arrow", "access-arrow"),
    "access": (None, None),
}


def render_archimate_view(
    snapshot: ArchimateSnapshot,
    view_id: str,
    *,
    options: RenderOptions | None = None,
) -> RenderResult:
    """Render one view of ``snapshot``. Raises :class:`RenderTargetNotFoundError`
    only when the view itself is absent; everything else degrades to drawn
    placeholders plus diagnostics."""
    view = next((v for v in snapshot.views or [] if v.id == view_id), None)
    if view is None:
        raise RenderTargetNotFoundError(f"view {view_id!r} is not in the snapshot")
    return _ViewRenderer(snapshot, view, options or RenderOptions()).render()


class _ViewRenderer:
    def __init__(
        self, snapshot: ArchimateSnapshot, view: ArchimateViewIn, opts: RenderOptions
    ) -> None:
        self.reg = get_registry()
        self.view = view
        self.opts = opts
        self.fs = opts.font_size
        self.diags = Diagnostics(RULESET_ID)
        self.obj_style = StyleReader(self.diags, "RENDER-AM", "view_object")
        self.con_style = StyleReader(self.diags, "RENDER-AM", "view_connection")
        self.markers = MarkerSet()
        self.extent = Extent()
        self.look = Look(fill="#ffffff", line=LINE_COLOR, font_color=FONT_COLOR)
        self._has_alignment = False
        self._label_target: tuple[TargetKind, str] | None = None

        self.concepts: dict[str, ArchimateConceptIn] = {}
        for c in snapshot.concepts:
            self.concepts.setdefault(c.id, c)
        self.rels: dict[str, ArchimateRelationshipIn] = {}
        for r in snapshot.relationships:
            self.rels.setdefault(r.id, r)

        self.objs: dict[str, ArchimateViewObjectIn] = {}
        for o in view.objects:
            if o.id in self.objs:
                self.diags.add(
                    "RENDER-AM-DUP-001",
                    "warning",
                    f"view object id {o.id} appears more than once; first occurrence drawn",
                    [("view_object", o.id)],
                )
                continue
            self.objs[o.id] = o
        self.conns: dict[str, ArchimateViewConnectionIn] = {}
        for c in view.connections:
            if c.id in self.conns or c.id in self.objs:
                self.diags.add(
                    "RENDER-AM-DUP-001",
                    "warning",
                    f"view connection id {c.id} is not unique in the view; first occurrence drawn",
                    [("view_connection", c.id)],
                )
                continue
            self.conns[c.id] = c

        self.parent: dict[str, str | None] = {}
        self.children: dict[str | None, list[str]] = {}
        self.size: dict[str, tuple[float, float]] = {}
        self.rel_pos: dict[str, tuple[float, float]] = {}
        self.abs: dict[str, Rect] = {}
        self.synthesized: list[str] = []
        self.placed: set[str | None] = set()
        self.geom: dict[str, list[Pt] | None] = {}
        self._resolving: set[str] = set()

    # ------------------------------------------------------------------ helpers

    def _concept_type(self, oid: str) -> str | None:
        c = self.concepts.get(self.objs[oid].concept_id or "")
        if c is None or c.type not in self.reg.concepts:
            return None
        return c.type

    def _layer(self, oid: str) -> str:
        t = self._concept_type(oid)
        return self.reg.concepts[t].layer if t else ""

    def _default_size(self, oid: str) -> tuple[float, float]:
        kind = self.objs[oid].kind
        if kind == "note":
            return NOTE_DEFAULT_SIZE
        if kind == "group":
            return GROUP_DEFAULT_SIZE
        t = self._concept_type(oid)
        return TYPE_DEFAULT_SIZE.get(t or "", DEFAULT_SIZE)

    # ------------------------------------------------------------------ layout

    def _build_tree(self) -> None:
        for oid, o in self.objs.items():
            p = o.parent_object_id
            if p is not None and p not in self.objs:
                self.diags.add(
                    "RENDER-AM-NEST-001",
                    "warning",
                    f"view object {oid} names parent {p}, which is not in the view; drawn at top level",
                    [("view_object", oid)],
                )
                p = None
            self.parent[oid] = p
        # Break parent cycles deterministically (the first object found on a cycle).
        for oid in self.objs:
            seen: list[str] = []
            cur: str | None = oid
            while cur is not None and cur not in seen:
                seen.append(cur)
                cur = self.parent[cur]
            if cur is not None:
                self.diags.add(
                    "RENDER-AM-NEST-002",
                    "warning",
                    f"view object {cur} is its own ancestor; drawn at top level",
                    [("view_object", cur)],
                )
                self.parent[cur] = None
        for oid in self.objs:
            self.children.setdefault(self.parent[oid], []).append(oid)

    def _obj_size(self, oid: str) -> tuple[float, float]:
        if oid in self.size:
            return self.size[oid]
        o = self.objs[oid]
        dw, dh = self._default_size(oid)
        if o.bounds is not None:
            w = o.bounds.w if o.bounds.w > 0 else dw
            h = o.bounds.h if o.bounds.h > 0 else dh
            self.size[oid] = (w, h)
            self._place_children(oid)
            return self.size[oid]
        right, bottom = self._place_children(oid)
        w = max(dw, right + 10) if oid in self.children else dw
        h = max(dh, bottom + 10) if oid in self.children else dh
        self.size[oid] = (w, h)
        return self.size[oid]

    def _place_children(self, pid: str | None) -> tuple[float, float]:
        """Assign relative positions to ``pid``'s children; return their max extent."""
        kids = self.children.get(pid, [])
        if pid in self.placed:
            return (0.0, 0.0)
        self.placed.add(pid)
        right = bottom = 0.0
        loose: list[str] = []
        for k in kids:
            o = self.objs[k]
            w, h = self._obj_size(k)
            if o.bounds is None:
                loose.append(k)
                continue
            self.rel_pos[k] = (o.bounds.x, o.bounds.y)
            right, bottom = max(right, o.bounds.x + w), max(bottom, o.bounds.y + h)
        if not loose:
            return right, bottom
        left = 0.0 if pid is None else 10.0
        top = (bottom + 40 if bottom else 0.0) if pid is None else max(bottom + 10, 30.0)
        loose_set = set(loose)
        edges: list[tuple[str, str]] = []
        for c in self.conns.values():
            a = self._ancestor_in(c.source_object_id, pid)
            b = self._ancestor_in(c.target_object_id, pid)
            if a in loose_set and b in loose_set and a != b:
                edges.append((a, b))
        ranks = {k: LAYER_ORDER.index(self._layer(k)) for k in loose} if pid is None else None
        pos = layered_layout(
            [(k, *self._obj_size(k)) for k in loose],
            edges,
            ranks=ranks,
            gap_x=30.0 if pid is None else 20.0,
            gap_y=50.0 if pid is None else 20.0,
            max_per_row=6 if pid is None else 4,
        )
        for k in loose:
            x, y = pos[k]
            w, h = self._obj_size(k)
            self.rel_pos[k] = (left + x, top + y)
            self.synthesized.append(k)
            right = max(right, left + x + w)
            bottom = max(bottom, top + y + h)
        return right, bottom

    def _ancestor_in(self, ref: str, pid: str | None) -> str | None:
        """The ancestor-or-self of object ``ref`` whose parent is ``pid``."""
        cur: str | None = ref if ref in self.objs else None
        while cur is not None and self.parent.get(cur) != pid:
            cur = self.parent.get(cur)
        return cur

    def _absolute(self, oid: str, origin: tuple[float, float]) -> None:
        rx, ry = self.rel_pos[oid]
        w, h = self.size[oid]
        r = Rect(origin[0] + rx, origin[1] + ry, w, h)
        self.abs[oid] = r
        for k in self.children.get(oid, []):
            self._absolute(k, (r.x, r.y))

    def _layout(self) -> None:
        self._build_tree()
        self._place_children(None)
        for oid in self.children.get(None, []):
            self._absolute(oid, (0.0, 0.0))
        if self.synthesized:
            ids = sorted(self.synthesized)
            self.diags.add(
                "RENDER-AM-LAYOUT-001",
                "advice",
                f"layout synthesized for {len(ids)} view object(s) without bounds",
                [("view_object", i) for i in ids],
                {"object_ids": ids},
            )

    # ------------------------------------------------------------------ objects

    def _draw_tree(self, oid: str, out: list[Node]) -> None:
        out.append(self._draw_object(oid))
        for k in self.children.get(oid, []):
            self._draw_tree(k, out)

    def _label(
        self,
        text: str,
        x: float,
        y: float,
        w: float,
        h: float,
        color: str,
        fs: float,
        anchor: str = "middle",
        *,
        valign: str = "top",
        look: Look | None = None,
    ) -> Node | None:
        if not text:
            return None
        extra: dict[str, Any] = {"data-kind": "label"}
        if look is not None:
            extra |= look.text_attrs()
            if look.fill_opacity is not None and look.font_opacity is None:
                extra["fill-opacity"] = 1  # do not inherit the shape's fill alpha
        bold = look is not None and look.bold
        fitted = fit_text(text, max(w, 0.0), max(h, 0.0), fs, bold=bold)
        if fitted.truncated and self._label_target is not None:
            kind, ident = self._label_target
            self.diags.add(
                "RENDER-AM-TEXT-001",
                "warning",
                f"text of {ident} does not fit its {fmt(w)}x{fmt(h)} area even at "
                f"{fmt(fitted.font_size)}px; truncated with an ellipsis",
                [(kind, ident)],
                {"text": text, "font_size": fitted.font_size},
            )
        lines, size = fitted.lines, fitted.font_size
        block_h = size * LINE_HEIGHT * len(lines)
        top = y + (h - block_h) / 2 if valign == "middle" else y
        tx = x + w / 2 if anchor == "middle" else (x if anchor == "start" else x + w)
        self.extent.add_text(lines, tx, top, size, anchor)
        if fitted.shrunk:
            extra["data-shrunk"] = "true"
        if fitted.truncated:
            extra["data-truncated"] = "true"
        return text_node(lines, tx, top, size, anchor=anchor, color=color, extra=extra)

    def _ctx(self, look: Look, fill: str, *, default_alignment: str = "middle") -> Ctx:
        anchor = look.anchor if self._has_alignment else default_alignment
        return Ctx(
            fill=fill,
            line=look.line,
            line_width=max(1, round(look.line_width)),
            text_alignment={
                "start": TEXT_ALIGNMENT_LEFT,
                "middle": TEXT_ALIGNMENT_CENTER,
                "end": TEXT_ALIGNMENT_RIGHT,
            }[anchor],
        )

    def _figure_group(self, g: G, part: str, **data: str) -> Node:
        n = Node("g", {"data-part": part} | {f"data-{k}": v for k, v in data.items()})
        n.extend(g.nodes)
        return n

    def _text_in(
        self,
        text: str,
        area: R,
        margin_h: float,
        look: Look,
        anchor: str,
        *,
        margin_w: float = TEXT_MARGIN_WIDTH,
    ) -> Node | None:
        """Archi's text control: wrapped text inside ``area`` minus its margins."""
        return self._label(
            text,
            area.x + margin_w,
            area.y + margin_h,
            area.width - 2 * margin_w,
            area.height - 2 * margin_h,
            look.font_color,
            look.font_size,
            anchor,
            look=look,
        )

    def _draw_object(self, oid: str) -> Node:
        o = self.objs[oid]
        r = self.abs[oid]
        self.extent.add_rect(r.x, r.y, r.w, r.h)
        self._has_alignment = bool(o.style and "text_alignment" in o.style)
        self._label_target = ("view_object", oid)
        if o.kind == "note":
            return self._draw_note(o, r)
        if o.kind == "group":
            return self._draw_group(o, r)
        c = self.concepts.get(o.concept_id or "")
        if c is None:
            self.diags.add(
                "RENDER-AM-OBJ-001",
                "warning",
                f"view object {oid} references concept {o.concept_id}, which is not in the snapshot",
                [("view_object", oid)],
                {"concept_id": o.concept_id},
            )
            return self._placeholder(o, r, f"unresolved: {o.concept_id}")
        if c.type not in self.reg.concepts:
            self.diags.add(
                "RENDER-AM-OBJ-002",
                "warning",
                f"view object {oid}: concept {c.id} has unknown type {c.type}",
                [("view_object", oid), ("element", c.id)],
                {"type": c.type},
            )
            return self._placeholder(o, r, f"{c.name or c.id} (unknown type {c.type})")

        meta = self.reg.concepts[c.type]
        look = self.obj_style.read(
            o.style,
            oid,
            fill=TYPE_FILL.get(c.type, LAYER_FILL.get(meta.layer, "#ffffff")),
            line=LINE_COLOR,
            font_color=FONT_COLOR,
            font_size=self.fs,
        )
        self.look = look
        figure = TYPE_FIGURE[c.type]
        spec = FIGURES[figure]
        notation = look.notation or "box"
        if notation == "icon" and spec.alt is None:
            notation = "box"  # Archi has no alternative figure (junction)
        default_anchor = "start" if c.type == "grouping" else "middle"
        ctx = self._ctx(look, look.fill, default_alignment=default_anchor)
        anchor = {TEXT_ALIGNMENT_LEFT: "start", TEXT_ALIGNMENT_RIGHT: "end"}.get(
            ctx.text_alignment, "middle"
        )
        text = self._label_text(o, c)
        ctx.text_width = text_width(text.split("\n")[0], look.font_size)
        ctx.text_height = look.font_size * LINE_HEIGHT
        g = Node(
            "g",
            {
                "data-kind": "element",
                "data-object-id": oid,
                "data-concept-id": c.id,
                "data-concept-type": c.type,
                "data-layer": meta.layer,
                "data-notation": notation,
                "data-figure": figure,
                "data-x": r.x,
                "data-y": r.y,
                "data-w": r.w,
                "data-h": r.h,
                "fill-opacity": look.fill_opacity,
                "stroke-opacity": look.line_opacity,
            },
        )
        if c.type == "junction":
            jt = c.junction_type or "and"
            if jt not in ("and", "or"):
                self.diags.add(
                    "RENDER-AM-OBJ-003",
                    "warning",
                    f"junction {c.id} has junction_type {jt!r}; drawn as and",
                    [("element", c.id)],
                )
                jt = "and"
            g.attrs["data-junction"] = jt
            ctx.junction_type = jt
        rect = R(r.x, r.y, r.w, r.h)
        fg = ctx.g()
        if notation == "icon" and c.type == "grouping":
            # The tabbed Grouping's title lives in its tab: that IS the notation.
            assert spec.alt is not None
            spec.alt(fg, rect, ctx)
            g.add(self._figure_group(fg, "figure"))
            area = spec.alt_text(rect, ctx) if spec.alt_text else rect.copy()
            margin_h = spec.alt_text_margin_height
        elif notation == "icon":
            # Label below the figure, never over it: reserve the band the text
            # needs (at most half the object), draw the figure above it, then
            # lay the text out under the figure's measured bounding box.
            assert spec.alt is not None
            inner_w = r.w - 2 * TEXT_MARGIN_WIDTH
            want = fit_text(text, inner_w, r.h / 2 - 2, look.font_size, bold=look.bold)
            band = len(want.lines) * want.font_size * LINE_HEIGHT + 3 if want.lines else 0
            limit = r.y + r.h - band
            fig_h = r.h - band
            for _ in range(4):  # some figures draw past their rect: give them less room
                fg = ctx.g()
                spec.alt(fg, R(r.x, r.y, r.w, max(fig_h, 1)), ctx)
                overshoot = (fg.box[3] if fg.box else limit) - limit
                if overshoot <= 0.01:
                    break
                fig_h -= overshoot
            fig = self._figure_group(fg, "figure")
            bottom = r.y + r.h - band
            if fg.box is not None:
                fig.attrs["data-bbox"] = " ".join(fmt(v) for v in fg.box)
                bottom = max(bottom, fg.box[3])
            g.add(fig)
            area = R(r.x, bottom + 2, r.w, r.y + r.h - bottom - 2)
            margin_h = 0
        else:
            spec.box(fg, rect, ctx)
            g.add(self._figure_group(fg, "figure"))
            if spec.icon is not None and spec.icon_origin is not None:
                ig = G(fg=look.line, bg=None, line_width=1)
                spec.icon(ig, *spec.icon_origin(rect))
                g.add(self._figure_group(ig, "icon", kind="icon", glyph=figure))
            area = (
                spec.box_text(rect, ctx)
                if spec.box_text
                else default_text_bounds(rect, ctx, spec.icon_offset)
            )
            margin_h = TEXT_MARGIN_HEIGHT
        if c.type != "junction":
            if notation == "icon" and c.type != "grouping":
                anchor = "middle"
            lbl = self._text_in(text, area, margin_h, look, anchor)
            if lbl is not None:
                g.add(lbl)
        return g

    def _draw_note(self, o: ArchimateViewObjectIn, r: Rect) -> Node:
        look = self.obj_style.read(
            o.style,
            o.id,
            fill=NOTE_DEFAULT_FILL,
            line=LINE_COLOR,
            font_color=FONT_COLOR,
            font_size=self.fs,
        )
        border = o.border_type or "dogear"
        if border not in NOTE_BORDERS:
            self._bad_border(o, border, NOTE_BORDERS)
            border = "dogear"
        ctx = self._ctx(look, look.fill, default_alignment="start")
        g = Node(
            "g",
            {
                "data-kind": "note",
                "data-object-id": o.id,
                "data-border": border,
                "data-x": r.x,
                "data-y": r.y,
                "data-w": r.w,
                "data-h": r.h,
                "fill-opacity": look.fill_opacity,
                "stroke-opacity": look.line_opacity,
            },
        )
        fg = ctx.g()
        note_figure(fg, R(r.x, r.y, r.w, r.h), ctx, border)
        g.add(self._figure_group(fg, "figure"))
        anchor = {TEXT_ALIGNMENT_RIGHT: "end", TEXT_ALIGNMENT_CENTER: "middle"}.get(
            ctx.text_alignment, "start"
        )
        lbl = self._text_in(o.text or "", R(r.x, r.y, r.w, r.h), TEXT_MARGIN_HEIGHT, look, anchor)
        if lbl is not None:
            g.add(lbl)
        return g

    def _draw_group(self, o: ArchimateViewObjectIn, r: Rect) -> Node:
        look = self.obj_style.read(
            o.style,
            o.id,
            fill=GROUP_DEFAULT_FILL,
            line=LINE_COLOR,
            font_color=FONT_COLOR,
            font_size=self.fs,
        )
        border = o.border_type or "tabbed"
        if border not in GROUP_BORDERS:
            self._bad_border(o, border, GROUP_BORDERS)
            border = "tabbed"
        ctx = self._ctx(look, look.fill, default_alignment="start")
        name = o.name or ""
        ctx.text_width = text_width(name, look.font_size)
        ctx.text_height = look.font_size * LINE_HEIGHT
        g = Node(
            "g",
            {
                "data-kind": "group",
                "data-object-id": o.id,
                "data-border": border,
                "data-x": r.x,
                "data-y": r.y,
                "data-w": r.w,
                "data-h": r.h,
                "fill-opacity": look.fill_opacity,
                "stroke-opacity": look.line_opacity,
            },
        )
        fg = ctx.g()
        tab_h = group_figure(fg, R(r.x, r.y, r.w, r.h), ctx, border)
        g.add(self._figure_group(fg, "figure"))
        anchor = {TEXT_ALIGNMENT_RIGHT: "end", TEXT_ALIGNMENT_CENTER: "middle"}.get(
            ctx.text_alignment, "start"
        )
        # GroupFigure.calculateTextControlBounds: the title sits in the tab.
        area = R(r.x, r.y, r.w, max(tab_h, look.font_size * LINE_HEIGHT) + 2 * 2)
        lbl = self._text_in(name, area, 2, look, anchor)
        if lbl is not None:
            g.add(lbl)
        return g

    def _bad_border(self, o: ArchimateViewObjectIn, border: str, allowed: tuple[str, ...]) -> None:
        self.diags.add(
            "RENDER-AM-STYLE-001",
            "warning",
            f"border_type {border!r} on {o.kind} {o.id} is not one of {', '.join(allowed)}; "
            "default used",
            [("view_object", o.id)],
            {"key": "border_type"},
        )

    def _label_text(self, o: ArchimateViewObjectIn, c: ArchimateConceptIn) -> str:
        if o.label is None:
            return c.name
        return self._expand(
            o.label,
            c.name,
            self._type_label(c.type),
            c.documentation,
            c.properties,
            o.id,
            "view_object",
        )

    def _type_label(self, t: str) -> str:
        """Archi's display name for a type: the class name split on capitals."""
        meta = self.reg.concepts.get(t)
        if meta is None:
            return t
        return re.sub(r"(?<!^)(?=[A-Z])", " ", meta.archi_class)

    def _expand(
        self,
        expr: str,
        name: str,
        type_label: str,
        documentation: str | None,
        properties: list[tuple[str, str]] | None,
        target_id: str,
        target_kind: TargetKind,
    ) -> str:
        """Archi label expressions: ${name} ${type} ${documentation} ${property:key}."""

        def sub(m: re.Match[str]) -> str:
            key = m.group(1)
            if key == "name":
                return name
            if key == "type":
                return type_label
            if key == "documentation":
                return documentation or ""
            if key.startswith("property:"):
                wanted = key.split(":", 1)[1]
                return next((v for k, v in properties or [] if k == wanted), "")
            self.diags.add(
                "RENDER-AM-LABEL-001",
                "advice",
                f"label expression ${{{key}}} on {target_id} is not supported; shown verbatim",
                [(target_kind, target_id)],
                {"expression": key},
            )
            return m.group(0)

        return LABEL_EXPR.sub(sub, expr)

    def _placeholder(self, o: ArchimateViewObjectIn, r: Rect, text: str) -> Node:
        g = Node(
            "g",
            {
                "data-kind": "unresolved",
                "data-object-id": o.id,
                "data-concept-id": o.concept_id,
                "data-x": r.x,
                "data-y": r.y,
                "data-w": r.w,
                "data-h": r.h,
            },
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
        lbl = self._label(
            f"? {text}", r.x + 4, r.y + 4, r.w - 8, r.h - 8, UNRESOLVED_STROKE, self.fs
        )
        if lbl is not None:
            g.add(lbl)
        return g

    # ------------------------------------------------------------------ connections

    def _anchor_of(self, ref: str) -> tuple[str, Any] | None:
        """("obj", Rect, is_circle) | ("conn", midpoint) | None."""
        if ref in self.objs:
            is_circle = self._concept_type(ref) == "junction"
            return ("obj", (self.abs[ref], is_circle))
        if ref in self.conns:
            pts = self._resolve(ref)
            if pts is None:
                return None
            return ("conn", polyline_midpoint(pts))
        return None

    @staticmethod
    def _ref_point(anchor: tuple[str, Any]) -> Pt:
        kind, val = anchor
        return val[0].center if kind == "obj" else val

    @staticmethod
    def _clip(anchor: tuple[str, Any], toward: Pt) -> Pt:
        kind, val = anchor
        if kind == "conn":
            return val
        rect, is_circle = val
        if is_circle:
            return clip_circle(rect.center, min(rect.w, rect.h) / 2, toward)
        return clip_rect(rect, toward)

    def _resolve(self, cid: str) -> list[Pt] | None:
        if cid in self.geom:
            return self.geom[cid]
        if cid in self._resolving:
            self.diags.add(
                "RENDER-AM-CON-004",
                "warning",
                f"view connection {cid} attaches to itself through other connections; not drawn",
                [("view_connection", cid)],
            )
            self.geom[cid] = None
            return None
        self._resolving.add(cid)
        c = self.conns[cid]
        s = self._anchor_of(c.source_object_id)
        t = self._anchor_of(c.target_object_id)
        self._resolving.discard(cid)
        if cid in self.geom:  # set by cycle detection while resolving
            return self.geom[cid]
        if s is None or t is None:
            missing = [
                x for x, a in ((c.source_object_id, s), (c.target_object_id, t)) if a is None
            ]
            self.diags.add(
                "RENDER-AM-CON-003",
                "warning",
                f"view connection {cid}: endpoint(s) {', '.join(missing)} cannot be placed; not drawn",
                [("view_connection", cid)],
                {"missing": missing},
            )
            self.geom[cid] = None
            return None
        bends: list[Pt] = [(float(p.x), float(p.y)) for p in c.bendpoints or []]
        if not bends and c.source_object_id == c.target_object_id and s[0] == "obj":
            # Self relationship: a compact loop over the top-right corner, leaving
            # the right edge and re-entering the top edge at right angles.
            rect = s[1][0]
            out_y, in_x = rect.y + rect.h * 0.3, rect.x + rect.w * 0.7
            pts = [
                (rect.x + rect.w, out_y),
                (rect.x + rect.w + 16, out_y),
                (rect.x + rect.w + 16, rect.y - 16),
                (in_x, rect.y - 16),
                (in_x, rect.y),
            ]
            self.geom[cid] = pts
            return pts
        sp = self._ref_point(s)
        tp = self._ref_point(t)
        if not bends and c.source_object_id != c.target_object_id and self._auto_placed(c):
            bends = self._route(c, s, t, sp, tp)
        if bends:
            start = self._end_point(s, bends[0])
            end = self._end_point(t, bends[-1])
        elif self._contains(s, tp):
            # Target nested in the source: leave the source through its border
            # nearest the target, never across the target or the far border.
            start = _nearest_border(s[1][0], tp)
            end = self._clip(t, start)
        elif self._contains(t, sp):
            end = _nearest_border(t[1][0], sp)
            start = self._clip(s, end)
        else:
            start = self._clip(s, tp)
            end = self._clip(t, sp)
        pts = [start, *bends, end]
        self.geom[cid] = pts
        return pts

    @staticmethod
    def _contains(anchor: tuple[str, Any], p: Pt) -> bool:
        if anchor[0] != "obj":
            return False
        r = anchor[1][0]
        return r.x < p[0] < r.x + r.w and r.y < p[1] < r.y + r.h

    def _end_point(self, anchor: tuple[str, Any], toward: Pt) -> Pt:
        """Clip toward a bendpoint; a bendpoint inside the box exits by the nearest border."""
        if self._contains(anchor, toward):
            return _nearest_border(anchor[1][0], toward)
        return self._clip(anchor, toward)

    def _lineage(self, oid: str) -> set[str]:
        """``oid``, its ancestors and its descendants."""
        out = {oid}
        cur = self.parent.get(oid)
        while cur is not None:
            out.add(cur)
            cur = self.parent.get(cur)
        stack = [oid]
        while stack:
            for k in self.children.get(stack.pop(), []):
                out.add(k)
                stack.append(k)
        return out

    def _auto_placed(self, c: ArchimateViewConnectionIn) -> bool:
        """Route only where the layout is ours: an end (or its ancestor) was synthesized."""
        synth = set(self.synthesized)
        for ref in (c.source_object_id, c.target_object_id):
            cur: str | None = ref if ref in self.objs else None
            while cur is not None:
                if cur in synth:
                    return True
                cur = self.parent.get(cur)
        return False

    def _route(
        self,
        c: ArchimateViewConnectionIn,
        s: tuple[str, Any],
        t: tuple[str, Any],
        sp: Pt,
        tp: Pt,
    ) -> list[Pt]:
        skip: set[str] = set()
        for ref in (c.source_object_id, c.target_object_id):
            if ref in self.objs:
                skip |= self._lineage(ref)
        obstacles = [self.abs[o] for o in self.objs if o not in skip and o in self.abs]
        src = s[1][0] if s[0] == "obj" else None
        tgt = t[1][0] if t[0] == "obj" else None
        return route(src, sp, tgt, tp, obstacles)

    def _draw_connection(self, cid: str) -> Node | None:
        c = self.conns[cid]
        pts = self._resolve(cid)
        if pts is None:
            return None
        for x, y in pts:
            self.extent.add_point(x, y)
        look = self.con_style.read(
            c.style,
            cid,
            fill="none",
            line=LINE_COLOR,
            font_color=FONT_COLOR,
            font_size=self.fs,
        )
        line = look.line
        g = Node(
            "g",
            {
                "data-kind": "connection",
                "data-connection-id": cid,
                "data-relationship-id": c.relationship_id,
                "data-source": c.source_object_id,
                "data-target": c.target_object_id,
                "data-source-endpoint": "connection"
                if c.source_object_id in self.conns
                else "object",
                "data-target-endpoint": "connection"
                if c.target_object_id in self.conns
                else "object",
            },
        )
        if c.kind == "line":
            # Archi's plain diagram connection: a solid line, no semantics, no arrowheads.
            g.attrs["data-kind"] = "line"
            g.add(Node("path", {"d": path_d(pts), "fill": "none"} | look.stroke_attrs()))
            self._connection_label(g, c.label or "", pts, look)
            return g
        r = self.rels.get(c.relationship_id or "")
        if r is None or r.type not in RELATIONSHIP_STYLE:
            if r is None:
                self.diags.add(
                    "RENDER-AM-CON-001",
                    "warning",
                    f"view connection {cid} references relationship {c.relationship_id}, "
                    "which is not in the snapshot",
                    [("view_connection", cid)],
                    {"relationship_id": c.relationship_id},
                )
            else:
                self.diags.add(
                    "RENDER-AM-CON-002",
                    "warning",
                    f"view connection {cid}: relationship {r.id} has unknown type {r.type}",
                    [("view_connection", cid), ("relationship", r.id)],
                    {"type": r.type},
                )
            g.attrs["data-kind"] = "unresolved-connection"
            g.add(
                Node(
                    "path",
                    {
                        "d": path_d(pts),
                        "fill": "none",
                        "stroke": UNRESOLVED_STROKE,
                        "stroke-width": 1,
                        "stroke-dasharray": "2 2",
                    },
                )
            )
            return g

        dash, m_start, m_end = RELATIONSHIP_STYLE[r.type]
        g.attrs["data-relationship-type"] = r.type
        if r.type == "access":
            at = r.access_type or "write"
            if at not in ACCESS_MARKERS:
                self.diags.add(
                    "RENDER-AM-CON-005",
                    "warning",
                    f"relationship {r.id}: unknown access_type {at!r}; drawn without arrowheads",
                    [("relationship", r.id)],
                )
                m_start, m_end = None, None
            else:
                m_start, m_end = ACCESS_MARKERS[at]
            g.attrs["data-access-type"] = at
        elif r.type == "association":
            directed = bool(r.directed)
            g.attrs["data-directed"] = "true" if directed else "false"
            m_end = "half-arrow" if directed else None
        attrs: dict[str, Any] = {"d": path_d(pts), "fill": "none"} | look.stroke_attrs()
        if dash:
            attrs["stroke-dasharray"] = dash
        if m_start:
            attrs["marker-start"] = self.markers.ref(m_start, line)
        if m_end:
            attrs["marker-end"] = self.markers.ref(m_end, line)
        g.add(Node("path", attrs))

        if c.label is not None:
            label = self._expand(
                c.label,
                r.name,
                f"{r.type.title()} relationship",
                r.documentation,
                r.properties,
                cid,
                "view_connection",
            )
        else:
            label = r.name
        if r.type == "influence" and r.strength:
            g.attrs["data-strength"] = r.strength
            if c.label is None:
                label = f"{label} {r.strength}".strip()
        self._connection_label(g, label, pts, look)
        return g

    def _connection_label(self, g: Node, label: str, pts: list[Pt], look: Look) -> None:
        if not label:
            return
        mx, my = polyline_midpoint(pts)
        lines = label.split("\n")
        top = my - look.font_size * LINE_HEIGHT * len(lines) / 2
        self.extent.add_text(lines, mx, top, look.font_size, "middle")
        g.add(backdrop(lines, mx, top, look.font_size, "middle", self.opts.background or "#ffffff"))
        g.add(
            text_node(
                lines,
                mx,
                top,
                look.font_size,
                color=look.font_color,
                extra={"data-kind": "label"} | look.text_attrs(),
            )
        )

    # ------------------------------------------------------------------ main

    def render(self) -> RenderResult:
        self._layout()
        body: list[Node] = []
        objects = Node("g", {"data-kind": "objects"})
        drawn: list[Node] = []
        for oid in self.children.get(None, []):
            self._draw_tree(oid, drawn)
        objects.extend(drawn)
        body.append(objects)
        conns = Node("g", {"data-kind": "connections"})
        for cid in self.conns:
            n = self._draw_connection(cid)
            if n is not None:
                conns.add(n)
        body.append(conns)
        svg = document(
            body,
            self.markers.defs(),
            self.extent,
            self.opts,
            {"data-renderer": RENDERER_VERSION, "data-view-id": self.view.id},
        )
        return RenderResult(
            svg=svg, diagnostics=self.diags.result(), renderer_version=RENDERER_VERSION
        )


def _nearest_border(r: Rect, p: Pt) -> Pt:
    """The point on ``r``'s border closest to ``p`` (a point inside ``r``)."""
    options = [
        (p[0] - r.x, (r.x, p[1])),
        (r.x + r.w - p[0], (r.x + r.w, p[1])),
        (p[1] - r.y, (p[0], r.y)),
        (r.y + r.h - p[1], (p[0], r.y + r.h)),
    ]
    return min(options, key=lambda o: o[0])[1]
