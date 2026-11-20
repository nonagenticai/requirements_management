"""Shared SVG plumbing: a tiny deterministic DOM, text metrics, geometry,
markers, style sanitising, diagnostics and the output sanitizer.

Everything is pure and deterministic: attributes serialise in insertion order,
numbers through one formatter, text widths from the bundled font's own
advance widths (``tools.rendering.fonts``), and the final document always passes through
:func:`sanitize_svg`, which re-parses it and keeps only an allowlist.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from lxml import etree  # ty: ignore[missing-module-member]  # C extension submodule ty cannot see

from schemas.modeling import Finding, FindingTarget, Severity, TargetKind, sort_findings
from tools.rendering.fonts import FONT_FAMILY, FONT_FAMILY_NAME, metrics

SVG_NS = "http://www.w3.org/2000/svg"
LINE_HEIGHT = 1.25


class RenderTargetNotFoundError(LookupError):
    """The requested view/diagram id is not in the snapshot."""


class UnsafeSvgError(ValueError):
    """Input to :func:`sanitize_svg` that cannot be made safe (not SVG, DTD, unparseable)."""


@dataclass(frozen=True)
class RenderOptions:
    """Presentation knobs. Part of the cache key, so keep them few and explicit."""

    margin: int = 20
    font_size: float = 11.0
    background: str | None = "#ffffff"


@dataclass
class RenderResult:
    svg: str
    diagnostics: list[Finding] = field(default_factory=list)
    renderer_version: str = ""


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class Diagnostics:
    def __init__(self, ruleset_id: str) -> None:
        self.ruleset_id = ruleset_id
        self._items: list[Finding] = []

    def add(
        self,
        rule_id: str,
        severity: Severity,
        message: str,
        targets: list[tuple[TargetKind, str]] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self._items.append(
            Finding(
                rule_id=rule_id,
                ruleset_id=self.ruleset_id,
                severity=severity,
                targets=[FindingTarget(kind=k, id=i) for k, i in (targets or [])],
                message=message,
                evidence=evidence,
            )
        )

    def result(self) -> list[Finding]:
        return sort_findings(self._items)


# ---------------------------------------------------------------------------
# Minimal DOM + serializer
# ---------------------------------------------------------------------------


def fmt(v: float | int) -> str:
    """One number formatter for the whole document (2 decimals, no exponent)."""
    if isinstance(v, bool):
        raise TypeError("bool is not a coordinate")
    s = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
        .replace("\t", "&#9;")
    )


_XML_INVALID = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def clean_text(s: str) -> str:
    """Drop characters XML 1.0 cannot carry (a hostile name must not break parsing).

    Everything outside ``#x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] |
    [#x10000-#x10FFFF]`` goes. Every writer that puts a user string into an XML tree (SVG,
    PDF title band, XMI, Open Group exchange) passes it through here, because lxml refuses
    such a string with ``ValueError`` and a sealed revision cannot be edited to remove it.
    """
    return _XML_INVALID.sub("", s)


class Node:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(
        self,
        tag: str,
        attrs: dict[str, Any] | None = None,
        text: str | None = None,
    ) -> None:
        self.tag = tag
        self.attrs: dict[str, Any] = dict(attrs or {})
        self.children: list[Node] = []
        self.text = text

    def add(self, child: Node) -> Node:
        self.children.append(child)
        return child

    def extend(self, children: list[Node]) -> None:
        self.children.extend(children)


def _attr_value(v: Any) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return fmt(v)
    return esc(clean_text(str(v)))


def serialize(node: Node) -> str:
    out: list[str] = []
    _ser(node, out)
    return "".join(out)


def _ser(node: Node, out: list[str]) -> None:
    out.append("<" + node.tag)
    for k, v in node.attrs.items():
        if v is None:
            continue
        out.append(f' {k}="{_attr_value(v)}"')
    if not node.children and node.text is None:
        out.append("/>")
        return
    out.append(">")
    if node.text is not None:
        out.append(esc(clean_text(node.text)).replace("&quot;", '"'))
    for c in node.children:
        _ser(c, out)
    out.append(f"</{node.tag}>")


# ---------------------------------------------------------------------------
# Text metrics (the bundled DejaVu Sans advance widths)
# ---------------------------------------------------------------------------


def text_width(s: str, font_size: float, *, bold: bool = False) -> float:
    """Advance width of ``s`` in the bundled DejaVu Sans (no kerning)."""
    m = metrics(bold)
    return sum(m.width(c) for c in s) * font_size


def wrap(
    text: str,
    max_width: float,
    font_size: float,
    *,
    bold: bool = False,
    break_words: bool = True,
) -> list[str] | None:
    """Greedy word wrap. A word wider than a line is broken by character
    (no hyphen) when ``break_words``; otherwise the result is ``None``."""
    lines: list[str] = []
    for para in text.split("\n"):
        current = ""
        for word in para.split(" "):
            candidate = word if not current else f"{current} {word}"
            if text_width(candidate, font_size, bold=bold) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = ""
            if text_width(word, font_size, bold=bold) > max_width and not break_words:
                return None
            while text_width(word, font_size, bold=bold) > max_width and len(word) > 1:
                cut = 1
                while (
                    cut < len(word)
                    and text_width(word[: cut + 1], font_size, bold=bold) <= max_width
                ):
                    cut += 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


@dataclass(frozen=True)
class Fitted:
    """Text laid out inside a box: never wider or taller than the box."""

    lines: list[str]
    font_size: float
    shrunk: bool = False
    truncated: bool = False


def _ellipsize(line: str, width: float, size: float, bold: bool) -> str:
    line = line.rstrip()
    while line and text_width(line + "…", size, bold=bold) > width:
        line = line[:-1].rstrip()
    return line + "…"


def min_font_size(font_size: float) -> float:
    """Shrink-to-fit floor: 80 % of the requested size, never below 7px."""
    return max(7.0, round(font_size * 0.8 * 2) / 2)


def fit_text(
    text: str, width: float, height: float, font_size: float, *, bold: bool = False
) -> Fitted:
    """Fit wrapped text into ``width`` × ``height``.

    1. word wrap at the requested size; 2. shrink in 0.5px steps down to
    :func:`min_font_size` while a word is too wide or the lines too many;
    3. at the floor, break long words by character (no hyphen); 4. last resort,
    drop the lines that do not fit and end the last one with an ellipsis.
    """
    if not text:
        return Fitted([], font_size)
    floor = min(font_size, min_font_size(font_size))
    size = font_size
    while True:
        lines = wrap(text, width, size, bold=bold, break_words=False)
        if lines is not None and len(lines) * size * LINE_HEIGHT <= height + 0.01:
            return Fitted(lines, size, shrunk=size < font_size)
        if size - 0.5 < floor:
            break
        size -= 0.5
    lines = wrap(text, width, floor, bold=bold) or [""]
    max_lines = max(1, int((height + 0.01) // (floor * LINE_HEIGHT)))
    if len(lines) <= max_lines:
        return Fitted(lines, floor, shrunk=floor < font_size)
    kept = lines[:max_lines]
    kept[-1] = _ellipsize(kept[-1], width, floor, bold)
    return Fitted(kept, floor, shrunk=floor < font_size, truncated=True)


def fit_line(text: str, width: float, font_size: float, *, bold: bool = False) -> Fitted:
    """One line that must stay within ``width``: shrink to the floor, then ellipsis."""
    size = font_size
    floor = min(font_size, min_font_size(font_size))
    while text_width(text, size, bold=bold) > width and size - 0.5 >= floor:
        size -= 0.5
    if text_width(text, size, bold=bold) <= width:
        return Fitted([text], size, shrunk=size < font_size)
    return Fitted([_ellipsize(text, width, size, bold)], size, shrunk=True, truncated=True)


def backdrop(
    lines: list[str], x: float, y_top: float, font_size: float, anchor: str, fill: str
) -> Node:
    """A background rectangle behind a label that sits on lines (connection labels)."""
    w = max((text_width(s, font_size) for s in lines), default=0) + 4
    h = font_size * LINE_HEIGHT * len(lines) + 1
    left = x - w / 2 if anchor == "middle" else (x - w + 2 if anchor == "end" else x - 2)
    return Node(
        "rect",
        {
            "data-kind": "backdrop",
            "x": left,
            "y": y_top + font_size * 0.15,
            "width": w,
            "height": h,
            "fill": fill,
            "fill-opacity": 0.85,
            "stroke": "none",
        },
    )


def text_node(
    lines: list[str],
    x: float,
    y_top: float,
    font_size: float,
    *,
    anchor: str = "middle",
    color: str = "#000000",
    extra: dict[str, Any] | None = None,
) -> Node:
    """A ``<text>`` whose first baseline sits one font-size below ``y_top``."""
    attrs: dict[str, Any] = {
        "x": x,
        "y": y_top + font_size,
        "font-size": font_size,
        "text-anchor": anchor,
        "fill": color,
    }
    attrs.update(extra or {})
    t = Node("text", attrs)
    if len(lines) == 1:
        t.text = lines[0]
        return t
    for i, line in enumerate(lines):
        t.add(Node("tspan", {"x": x, "dy": 0 if i == 0 else font_size * LINE_HEIGHT}, line))
    return t


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

Pt = tuple[float, float]


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def center(self) -> Pt:
        return (self.cx, self.cy)


def clip_rect(r: Rect, toward: Pt) -> Pt:
    """Where the ray from ``r``'s centre toward ``toward`` leaves the rectangle."""
    dx, dy = toward[0] - r.cx, toward[1] - r.cy
    if dx == 0 and dy == 0:
        return r.center
    hw, hh = r.w / 2, r.h / 2
    tx = hw / abs(dx) if dx else math.inf
    ty = hh / abs(dy) if dy else math.inf
    t = min(tx, ty)
    return (r.cx + dx * t, r.cy + dy * t)


def clip_circle(c: Pt, radius: float, toward: Pt) -> Pt:
    dx, dy = toward[0] - c[0], toward[1] - c[1]
    d = math.hypot(dx, dy)
    if d == 0:
        return c
    return (c[0] + dx / d * radius, c[1] + dy / d * radius)


def polyline_midpoint(points: list[Pt]) -> Pt:
    if len(points) == 1:
        return points[0]
    segs = [math.dist(points[i], points[i + 1]) for i in range(len(points) - 1)]
    half = sum(segs) / 2
    for i, s in enumerate(segs):
        if half <= s or i == len(segs) - 1:
            f = half / s if s else 0.0
            a, b = points[i], points[i + 1]
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        half -= s
    return points[-1]  # pragma: no cover


def path_d(points: list[Pt]) -> str:
    head, *rest = points
    return f"M{fmt(head[0])} {fmt(head[1])}" + "".join(f" L{fmt(x)} {fmt(y)}" for x, y in rest)


def along(a: Pt, b: Pt, dist: float) -> Pt:
    d = math.dist(a, b)
    if d == 0:
        return a
    return (a[0] + (b[0] - a[0]) / d * dist, a[1] + (b[1] - a[1]) / d * dist)


def perpendicular(a: Pt, b: Pt) -> Pt:
    d = math.dist(a, b)
    if d == 0:
        return (0.0, -1.0)
    return (-(b[1] - a[1]) / d, (b[0] - a[0]) / d)


class Extent:
    """Running bounding box of everything drawn (for the viewBox)."""

    def __init__(self) -> None:
        self.x0 = math.inf
        self.y0 = math.inf
        self.x1 = -math.inf
        self.y1 = -math.inf

    def add_point(self, x: float, y: float) -> None:
        self.x0, self.y0 = min(self.x0, x), min(self.y0, y)
        self.x1, self.y1 = max(self.x1, x), max(self.y1, y)

    def add_rect(self, x: float, y: float, w: float, h: float) -> None:
        self.add_point(x, y)
        self.add_point(x + w, y + h)

    def add_text(self, lines: list[str], x: float, y_top: float, fs: float, anchor: str) -> None:
        if not lines:
            return
        w = max(text_width(s, fs) for s in lines)
        h = fs * (1 + LINE_HEIGHT * (len(lines) - 1)) + fs * 0.3
        left = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
        self.add_rect(left, y_top, w, h)

    def empty(self) -> bool:
        return self.x0 == math.inf


def document(
    body: list[Node],
    defs: list[Node],
    extent: Extent,
    options: RenderOptions,
    root_attrs: dict[str, Any],
) -> str:
    if extent.empty():
        extent.add_rect(0, 0, 1, 1)
    m = options.margin
    x0, y0 = math.floor(extent.x0) - m, math.floor(extent.y0) - m
    w = math.ceil(extent.x1) + m - x0
    h = math.ceil(extent.y1) + m - y0
    attrs: dict[str, Any] = {
        "xmlns": SVG_NS,
        "version": "1.1",
        "width": w,
        "height": h,
        "viewBox": f"{fmt(x0)} {fmt(y0)} {fmt(w)} {fmt(h)}",
        "font-family": FONT_FAMILY,
        "font-size": options.font_size,
    }
    attrs.update(root_attrs)
    root = Node("svg", attrs)
    if defs:
        d = root.add(Node("defs"))
        d.extend(defs)
    bg = safe_color(options.background) if options.background else None
    if bg:
        root.add(
            Node(
                "rect",
                {"data-kind": "background", "x": x0, "y": y0, "width": w, "height": h, "fill": bg},
            )
        )
    root.extend(body)
    return sanitize_svg(serialize(root))


# ---------------------------------------------------------------------------
# Style values (user-supplied, presentation only)
# ---------------------------------------------------------------------------

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def safe_color(value: Any) -> str | None:
    """Normalise a hex colour to ``#rrggbb``; anything else → None."""
    if not isinstance(value, str) or not _HEX.match(value.strip()):
        return None
    v = value.strip().lower()
    if len(v) == 4:
        v = "#" + "".join(c * 2 for c in v[1:])
    return v


# Snapshot style vocabulary, shared with the Open Group exchange slice
# (tools.architecture.exchange). Anything else is ignored with a diagnostic.
STYLE_KEYS = frozenset(
    {
        "fill_color",
        "fill_alpha",
        "line_color",
        "line_alpha",
        "line_width",
        "font_name",
        "font_size",
        "font_style",
        "font_color",
        "font_alpha",
        "notation",
        "text_alignment",
    }
)
FONT_STYLES = frozenset({"plain", "bold", "italic", "underline"})


@dataclass(frozen=True)
class Look:
    """Resolved presentation of one object or connection."""

    fill: str
    line: str
    font_color: str
    fill_opacity: float | None = None
    line_opacity: float | None = None
    font_opacity: float | None = None
    line_width: float = 1.0
    font_size: float = 11.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    anchor: str = "middle"
    notation: str | None = None

    def shape_attrs(self) -> dict[str, Any]:
        a: dict[str, Any] = {
            "fill": self.fill,
            "stroke": self.line,
            "stroke-width": self.line_width,
        }
        if self.fill_opacity is not None:
            a["fill-opacity"] = self.fill_opacity
        if self.line_opacity is not None:
            a["stroke-opacity"] = self.line_opacity
        return a

    def stroke_attrs(self) -> dict[str, Any]:
        a: dict[str, Any] = {"stroke": self.line, "stroke-width": self.line_width}
        if self.line_opacity is not None:
            a["stroke-opacity"] = self.line_opacity
        return a

    def text_attrs(self) -> dict[str, Any]:
        a: dict[str, Any] = {}
        if self.bold:
            a["font-weight"] = "bold"
        if self.italic:
            a["font-style"] = "italic"
        if self.underline:
            a["text-decoration"] = "underline"
        if self.font_opacity is not None:
            a["fill-opacity"] = self.font_opacity
        return a


class StyleReader:
    """Resolves the snapshot ``style`` dict into a :class:`Look`.

    Values outside the vocabulary are never passed through: a bad value falls
    back to the default and an unknown key is ignored, each with a diagnostic
    (``<prefix>-STYLE-001`` bad value, ``-STYLE-003`` unknown key).
    """

    def __init__(self, diags: Diagnostics, prefix: str, kind: TargetKind) -> None:
        self.diags = diags
        self.prefix = prefix
        self.kind = kind

    def _bad(self, oid: str, key: str, why: str) -> None:
        self.diags.add(
            f"{self.prefix}-STYLE-001",
            "warning",
            f"style.{key} on {oid} {why}; default used",
            [(self.kind, oid)],
            {"key": key},
        )

    def read(
        self,
        style: dict[str, Any] | None,
        oid: str,
        *,
        fill: str,
        line: str,
        font_color: str,
        font_size: float,
    ) -> Look:
        style = style or {}
        unknown = sorted(k for k in style if k not in STYLE_KEYS)
        if unknown:
            self.diags.add(
                f"{self.prefix}-STYLE-003",
                "advice",
                f"style on {oid} has unknown key(s) {', '.join(unknown)}; ignored",
                [(self.kind, oid)],
                {"keys": unknown},
            )

        def color(key: str, default: str) -> str:
            v = style.get(key)
            if v is None:
                return default
            c = safe_color(v)
            if c is None:
                self._bad(oid, key, "is not a #rrggbb colour")
                return default
            return c

        def alpha(key: str) -> float | None:
            v = style.get(key)
            if v is None:
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
                self._bad(oid, key, "is not a number 0-100")
                return None
            return None if v == 100 else round(v / 100, 2)

        width = 1.0
        if "line_width" in style:
            v = style["line_width"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 20:
                self._bad(oid, "line_width", "is not a number in (0, 20]")
            else:
                width = float(v)

        size = font_size
        if "font_size" in style:
            v = style["font_size"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 4 <= v <= 72:
                self._bad(oid, "font_size", "is not a point size in [4, 72]")
            else:
                size = round(float(v) * 4 / 3, 2)  # points → px

        flags: set[str] = set()
        if "font_style" in style:
            v = style["font_style"]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                self._bad(oid, "font_style", "is not a list of plain/bold/italic/underline")
            elif not set(v) <= FONT_STYLES:
                self._bad(oid, "font_style", "has values outside plain/bold/italic/underline")
            else:
                flags = set(v)

        name = style.get("font_name")
        if name is not None:
            if not isinstance(name, str):
                self._bad(oid, "font_name", "is not a string")
            elif name != FONT_FAMILY_NAME:
                # One pinned font keeps layout and PNG bytes host-independent.
                self.diags.add(
                    f"{self.prefix}-STYLE-004",
                    "advice",
                    f"style.font_name {name!r} on {oid} is not available; "
                    f"{FONT_FAMILY_NAME} is used",
                    [(self.kind, oid)],
                    {"font_name": name},
                )

        anchor = "middle"
        ta = style.get("text_alignment")
        if ta is not None:
            if ta in ("left", "center", "right"):
                anchor = {"left": "start", "center": "middle", "right": "end"}[ta]
            else:
                self._bad(oid, "text_alignment", "is not left/center/right")

        notation = style.get("notation")
        if notation is not None and notation not in ("box", "icon"):
            self._bad(oid, "notation", "is not box/icon")
            notation = None

        return Look(
            fill=color("fill_color", fill),
            line=color("line_color", line),
            font_color=color("font_color", font_color),
            fill_opacity=alpha("fill_alpha"),
            line_opacity=alpha("line_alpha"),
            font_opacity=alpha("font_alpha"),
            line_width=width,
            font_size=size,
            bold="bold" in flags,
            italic="italic" in flags,
            underline="underline" in flags,
            anchor=anchor,
            notation=notation,
        )


# ---------------------------------------------------------------------------
# Markers (arrowheads). Drawn pointing along +x with the tip at refX, and
# oriented ``auto-start-reverse`` so one marker serves either end of a line.
# ---------------------------------------------------------------------------

_MARKERS: dict[str, tuple[float, float, float, float, str, bool]] = {
    # kind: (width, height, refX, refY, path, filled)
    "arrow-filled": (10, 8, 10, 4, "M0 0 L10 4 L0 8 Z", True),
    "arrow-open": (10, 10, 10, 5, "M0 0 L10 5 L0 10", False),
    "access-arrow": (7, 7, 7, 3.5, "M0 0 L7 3.5 L0 7", False),
    "half-arrow": (10, 6, 10, 6, "M0 0 L10 6", False),
    "triangle-hollow": (12, 12, 12, 6, "M0 0 L12 6 L0 12 Z", False),
    "diamond-filled": (14, 8, 14, 4, "M0 4 L7 0 L14 4 L7 8 Z", True),
    "diamond-hollow": (14, 8, 14, 4, "M0 4 L7 0 L14 4 L7 8 Z", False),
    "dot": (8, 8, 7, 4, "", True),
}

MARKER_KINDS = tuple(_MARKERS)


class MarkerSet:
    """Collects the markers a document uses (one per kind × colour)."""

    def __init__(self) -> None:
        self._used: dict[str, tuple[str, str]] = {}

    def ref(self, kind: str, color: str) -> str:
        mid = f"m-{kind}-{color.lstrip('#')}"
        self._used[mid] = (kind, color)
        return f"url(#{mid})"

    def defs(self) -> list[Node]:
        out: list[Node] = []
        for mid in sorted(self._used):
            kind, color = self._used[mid]
            w, h, rx, ry, d, filled = _MARKERS[kind]
            m = Node(
                "marker",
                {
                    "id": mid,
                    "data-marker": kind,
                    "viewBox": f"-1 -1 {fmt(w + 2)} {fmt(h + 2)}",
                    "markerWidth": w + 2,
                    "markerHeight": h + 2,
                    "refX": rx,
                    "refY": ry,
                    "orient": "auto-start-reverse",
                    "markerUnits": "userSpaceOnUse",
                },
            )
            if kind == "dot":
                m.add(Node("circle", {"cx": 4, "cy": 4, "r": 3.5, "fill": color}))
            else:
                m.add(
                    Node(
                        "path",
                        {
                            "d": d,
                            "fill": color if filled else ("none" if d[-1] != "Z" else "#ffffff"),
                            "stroke": color,
                            "stroke-width": 1,
                            "stroke-linejoin": "miter",
                        },
                    )
                )
            out.append(m)
        return out


# ---------------------------------------------------------------------------
# Output sanitizer
# ---------------------------------------------------------------------------

ALLOWED_ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "defs",
        "marker",
        "title",
        "desc",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "path",
        "text",
        "tspan",
    }
)
_TEXT_ELEMENTS = frozenset({"text", "tspan", "title", "desc"})
ALLOWED_ATTRIBUTES = frozenset(
    {
        "xmlns",
        "version",
        "id",
        "class",
        "width",
        "height",
        "viewBox",
        "preserveAspectRatio",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "dx",
        "dy",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "transform",
        "fill",
        "fill-opacity",
        "fill-rule",
        "stroke",
        "stroke-width",
        "stroke-dasharray",
        "stroke-linejoin",
        "stroke-linecap",
        "stroke-opacity",
        "opacity",
        "vector-effect",
        "font-family",
        "font-size",
        "font-weight",
        "font-style",
        "text-anchor",
        "dominant-baseline",
        "text-decoration",
        "marker-start",
        "marker-mid",
        "marker-end",
        "markerWidth",
        "markerHeight",
        "markerUnits",
        "refX",
        "refY",
        "orient",
        "href",
    }
)
_DATA_ATTR = re.compile(r"^data-[a-z0-9-]{1,40}$")
_URL_REF = re.compile(r"url\(\s*#[A-Za-z0-9_.:-]+\s*\)")
_LOCAL_HREF = re.compile(r"^#[A-Za-z0-9_.:-]+$")
_DANGEROUS = re.compile(
    r"(javascript|vbscript|data)\s*:|expression\s*\(|@import|url\s*\(|[<>]|\\",
    re.IGNORECASE,
)
_ALLOWED_FONTS = frozenset({"sans-serif", FONT_FAMILY})


def _safe_value(name: str, value: str) -> bool:
    if name == "href":
        return bool(_LOCAL_HREF.match(value))
    if name == "font-family":
        return value in _ALLOWED_FONTS
    # The only url() allowed is a same-document fragment reference (markers).
    stripped = _URL_REF.sub("", value)
    return not _DANGEROUS.search(stripped)


def _parser() -> etree.XMLParser:
    # Same hardening as the exchange importer: no entities, no DTD, no network.
    # Built per call: lxml parsers must not be shared across threads, and
    # rendering runs in the threadpool.
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_blank_text=False,
        collect_ids=False,
    )


def sanitize_svg(svg: str) -> str:
    """Re-parse ``svg`` and keep only allowlisted SVG elements/attributes.

    Drops (with their subtrees) script, style, foreignObject, image, use, a,
    animation and anything non-SVG; drops event handlers and every attribute
    not on the allowlist; drops ``url()`` other than ``url(#id)``, any
    ``javascript:``/``data:`` value, ``href`` to anything but ``#id`` and
    non-generic font families. Refuses DTDs and entity declarations outright.
    The result is re-serialised deterministically, so sanitising is idempotent.
    """
    if re.search(r"<!(DOCTYPE|ENTITY)", svg, re.IGNORECASE):
        raise UnsafeSvgError("DTD / entity declarations are not accepted")
    try:
        root = etree.fromstring(svg.encode("utf-8"), parser=_parser())
    except etree.XMLSyntaxError as e:
        raise UnsafeSvgError(f"not well-formed XML: {e}") from e
    if _local(root.tag) != "svg" or _ns(root.tag) != SVG_NS:
        raise UnsafeSvgError("root element is not <svg> in the SVG namespace")
    clean = _clean(root, parent_tag=None)
    assert clean is not None
    clean.attrs["xmlns"] = SVG_NS
    return serialize(clean)


def _ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _clean(e: etree._Element, parent_tag: str | None) -> Node | None:
    if not isinstance(e.tag, str) or _ns(e.tag) != SVG_NS:
        return None
    tag = _local(e.tag)
    if tag not in ALLOWED_ELEMENTS:
        return None
    attrs: dict[str, Any] = {}
    for name, value in e.attrib.items():
        if name.startswith("{"):
            continue  # namespaced (xlink:href, xml:base, …)
        if name not in ALLOWED_ATTRIBUTES and not _DATA_ATTR.match(name):
            continue
        if _safe_value(name, value):
            attrs[name] = value
    node = Node(tag, attrs)
    if tag in _TEXT_ELEMENTS and e.text:
        node.text = e.text
    for child in e:
        c = _clean(child, tag)
        if c is not None:
            node.add(c)
        if tag == "text" and child.tail and child.tail.strip():
            # Tail text after a tspan: keep it as its own tspan to stay a tree.
            node.add(Node("tspan", {}, child.tail))
    return node
