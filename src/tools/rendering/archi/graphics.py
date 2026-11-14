"""A tiny SVG-emitting stand-in for the SWT/draw2d ``Graphics`` and ``Path`` APIs.

The figure modules in this package are line-by-line ports of Archi 5.10.0's
figure classes (``com.archimatetool.editor.diagram.figures``). Keeping the
call shapes of the Java originals (``drawOval``, ``Path.addArc``, …) makes the
port reviewable against the source; this module turns those calls into SVG.

Conventions copied from SWT/draw2d:

* angles are degrees, 0 = 3 o'clock, positive = counter-clockwise on screen;
* ``draw*`` strokes with the foreground colour, ``fill*`` paints with the
  background colour (a ``None`` background skips the fill, as Archi's icon
  delegates do when they are passed ``backgroundColor == null``);
* Java ``int`` arithmetic is reproduced with :func:`jdiv` / :func:`jint`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from tools.rendering.svg import Node, fmt


def jdiv(a: float, b: float) -> int:
    """Java integer division (truncates toward zero)."""
    q = abs(int(a)) // abs(int(b))
    return q if (a >= 0) == (b > 0) else -q


def jint(v: float) -> int:
    """Java ``(int)`` cast (truncates toward zero)."""
    return int(v)


@dataclass
class R:
    """draw2d ``Rectangle`` (integer geometry in Archi)."""

    x: float
    y: float
    width: float
    height: float

    def copy(self) -> R:
        return R(self.x, self.y, self.width, self.height)

    def right(self) -> float:
        return self.x + self.width

    def bottom(self) -> float:
        return self.y + self.height

    def shrink(self, h: float, v: float) -> R:
        self.x += h
        self.y += v
        self.width -= 2 * h
        self.height -= 2 * v
        return self


def _arc_point(x: float, y: float, w: float, h: float, deg: float) -> tuple[float, float]:
    rad = math.radians(deg)
    return (x + w / 2 + (w / 2) * math.cos(rad), y + h / 2 - (h / 2) * math.sin(rad))


def _arc_d(x: float, y: float, w: float, h: float, start: float, extent: float) -> str:
    """SVG path segments for an SWT arc, starting AT the arc start (no M)."""
    if extent == 0:
        return ""
    segs = []
    # Split into <= 180° pieces so every SVG arc is unambiguous (full circles too).
    n = max(1, math.ceil(abs(extent) / 180))
    step = extent / n
    for i in range(1, n + 1):
        ex, ey = _arc_point(x, y, w, h, start + step * i)
        sweep = 0 if step > 0 else 1  # CCW on screen = SVG sweep-flag 0 (y down)
        segs.append(f"A{fmt(w / 2)} {fmt(h / 2)} 0 0 {sweep} {fmt(ex)} {fmt(ey)}")
    return " ".join(segs)


class Path:
    """SWT ``Path``, including its sub-path rule for arcs.

    SWT (cocoa and cairo) starts an arc with a MOVE when the path is "closed" —
    new, just ``close()``d, just ``moveTo``'d, after ``addRectangle`` or after a
    full (>= 360°) arc — and with a LINE from the current point otherwise; a
    full arc closes its own sub-path. That keeps Archi's interaction half-discs
    apart and its concentric rings unjoined, while the cylinder's two
    half-ellipses connect.
    """

    def __init__(self, _device: Any = None) -> None:
        self.parts: list[str] = []
        self.current: tuple[float, float] | None = None
        self.start: tuple[float, float] | None = None
        self.closed = True
        self.box: list[float] | None = None  # [x0, y0, x1, y1] of everything added

    def _see(self, x: float, y: float) -> None:
        if self.box is None:
            self.box = [x, y, x, y]
        else:
            b = self.box
            b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)

    def moveTo(self, x: float, y: float) -> None:  # noqa: N802 — SWT name
        self._see(x, y)
        self.parts.append(f"M{fmt(x)} {fmt(y)}")
        self.current = self.start = (x, y)
        self.closed = True

    def lineTo(self, x: float, y: float) -> None:  # noqa: N802
        if self.current is None:
            self.moveTo(x, y)
            return
        self._see(x, y)
        self.parts.append(f"L{fmt(x)} {fmt(y)}")
        self.current = (x, y)
        self.closed = False

    def quadTo(self, cx: float, cy: float, x: float, y: float) -> None:  # noqa: N802
        if self.current is None:
            self.moveTo(cx, cy)
        self._see(cx, cy)  # the control polygon bounds the curve
        self._see(x, y)
        self.parts.append(f"Q{fmt(cx)} {fmt(cy)} {fmt(x)} {fmt(y)}")
        self.current = (x, y)
        self.closed = False

    def cubicTo(  # noqa: N802
        self, c1x: float, c1y: float, c2x: float, c2y: float, x: float, y: float
    ) -> None:
        if self.current is None:
            self.moveTo(c1x, c1y)
        for px, py in ((c1x, c1y), (c2x, c2y), (x, y)):
            self._see(px, py)
        self.parts.append(f"C{fmt(c1x)} {fmt(c1y)} {fmt(c2x)} {fmt(c2y)} {fmt(x)} {fmt(y)}")
        self.current = (x, y)
        self.closed = False

    def addArc(  # noqa: N802
        self, x: float, y: float, w: float, h: float, start: float, extent: float
    ) -> None:
        if w == 0 or h == 0 or extent == 0:
            return
        sx, sy = _arc_point(x, y, w, h, start)
        if self.closed or self.current is None:
            self.moveTo(sx, sy)
        else:
            self.lineTo(sx, sy)
        steps = max(1, math.ceil(abs(extent)))  # sample every degree for the bbox
        for i in range(steps + 1):
            self._see(*_arc_point(x, y, w, h, start + extent * i / steps))
        self.parts.append(_arc_d(x, y, w, h, start, extent))
        self.current = _arc_point(x, y, w, h, start + extent)
        self.closed = abs(extent) >= 360
        if self.closed:
            self.parts.append("Z")
            self.current = (sx, sy)

    def addRectangle(self, x: float, y: float, w: float, h: float) -> None:  # noqa: N802
        self.moveTo(x, y)
        self._see(x + w, y + h)
        self.parts.append(f"H{fmt(x + w)} V{fmt(y + h)} H{fmt(x)} Z")
        self.current = (x, y)
        self.closed = True

    def close(self) -> None:
        self.parts.append("Z")
        self.current = self.start
        self.closed = True

    def dispose(self) -> None:
        pass

    def d(self) -> str:
        return " ".join(self.parts)


class G:
    """draw2d ``Graphics`` that records SVG nodes."""

    def __init__(self, fg: str = "#000000", bg: str | None = None, line_width: float = 1) -> None:
        self.nodes: list[Node] = []
        self.fg = fg
        self.bg = bg
        self.lw = line_width
        self.dash: str | None = None
        self._stack: list[tuple[str, str | None, float, str | None]] = []
        self.box: list[float] | None = None

    def _see(self, x0: float, y0: float, x1: float, y1: float) -> None:
        if self.box is None:
            self.box = [x0, y0, x1, y1]
        else:
            b = self.box
            b[0], b[1], b[2], b[3] = min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)

    def _see_attrs(self, tag: str, a: dict[str, Any]) -> None:
        if tag == "rect":
            self._see(a["x"], a["y"], a["x"] + a["width"], a["y"] + a["height"])
        elif tag == "ellipse":
            self._see(a["cx"] - a["rx"], a["cy"] - a["ry"], a["cx"] + a["rx"], a["cy"] + a["ry"])
        elif tag in ("polygon", "polyline"):
            nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", a["points"])]
            if nums:
                xs, ys = nums[0::2], nums[1::2]
                self._see(min(xs), min(ys), max(xs), max(ys))

    def _see_path(self, p: Path) -> None:
        if p.box is not None:
            self._see(*p.box)

    # -- state -----------------------------------------------------------
    def pushState(self) -> None:  # noqa: N802
        self._stack.append((self.fg, self.bg, self.lw, self.dash))

    def popState(self) -> None:  # noqa: N802
        if self._stack:
            self.fg, self.bg, self.lw, self.dash = self._stack.pop()

    def restoreState(self) -> None:  # noqa: N802
        if self._stack:
            self.fg, self.bg, self.lw, self.dash = self._stack[-1]

    def setForegroundColor(self, c: str | None) -> None:  # noqa: N802
        if c is not None:
            self.fg = c

    def setBackgroundColor(self, c: str | None) -> None:  # noqa: N802
        self.bg = c

    def setLineWidth(self, w: float) -> None:  # noqa: N802
        self.lw = w

    def setLineDash(self, dash: list[float] | None) -> None:  # noqa: N802
        self.dash = " ".join(fmt(v) for v in dash) if dash else None

    # Presentation details Archi applies that SVG handles elsewhere.
    def setAlpha(self, *_: Any) -> None:  # noqa: N802
        pass

    def setAntialias(self, *_: Any) -> None:  # noqa: N802
        pass

    def setLineCap(self, *_: Any) -> None:  # noqa: N802
        pass

    def setLineJoin(self, *_: Any) -> None:  # noqa: N802
        pass

    def setLineStyle(self, *_: Any) -> None:  # noqa: N802
        pass

    # -- emit ------------------------------------------------------------
    def _stroke(self, tag: str, attrs: dict[str, Any]) -> None:
        a = dict(attrs)
        a.update({"fill": "none", "stroke": self.fg, "stroke-width": self.lw})
        if self.dash:
            a["stroke-dasharray"] = self.dash
        self._see_attrs(tag, a)
        self.nodes.append(Node(tag, a))

    def _fill(self, tag: str, attrs: dict[str, Any], color: str | None = None) -> None:
        c = color or self.bg
        if c is None:
            return
        a = dict(attrs)
        a.update({"fill": c, "stroke": "none"})
        self._see_attrs(tag, a)
        self.nodes.append(Node(tag, a))

    def drawLine(self, x1: float, y1: float, x2: float, y2: float) -> None:  # noqa: N802
        self._see(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self._stroke("path", {"d": f"M{fmt(x1)} {fmt(y1)} L{fmt(x2)} {fmt(y2)}"})

    def drawOval(self, x: float, y: float, w: float, h: float) -> None:  # noqa: N802
        self._stroke("ellipse", {"cx": x + w / 2, "cy": y + h / 2, "rx": w / 2, "ry": h / 2})

    def fillOval(self, x: float, y: float, w: float, h: float) -> None:  # noqa: N802
        self._fill("ellipse", {"cx": x + w / 2, "cy": y + h / 2, "rx": w / 2, "ry": h / 2})

    def _rect(self, x: Any, y: Any = None, w: Any = None, h: Any = None) -> tuple:
        if isinstance(x, R):
            return x.x, x.y, x.width, x.height
        return x, y, w, h

    def drawRectangle(self, x: Any, y: Any = None, w: Any = None, h: Any = None) -> None:  # noqa: N802
        x, y, w, h = self._rect(x, y, w, h)
        self._stroke("rect", {"x": x, "y": y, "width": max(w, 0), "height": max(h, 0)})

    def fillRectangle(self, x: Any, y: Any = None, w: Any = None, h: Any = None) -> None:  # noqa: N802
        x, y, w, h = self._rect(x, y, w, h)
        self._fill("rect", {"x": x, "y": y, "width": max(w, 0), "height": max(h, 0)})

    def drawRoundRectangle(self, r: R, aw: float, ah: float) -> None:  # noqa: N802
        self._stroke(
            "rect",
            {"x": r.x, "y": r.y, "width": r.width, "height": r.height, "rx": aw / 2, "ry": ah / 2},
        )

    def fillRoundRectangle(self, r: R, aw: float, ah: float) -> None:  # noqa: N802
        self._fill(
            "rect",
            {"x": r.x, "y": r.y, "width": r.width, "height": r.height, "rx": aw / 2, "ry": ah / 2},
        )

    @staticmethod
    def _points(pts: Any) -> str:
        flat: list[float] = []
        for p in pts:
            if isinstance(p, (tuple, list)):
                flat.extend(p)
            else:
                flat.append(p)
        return " ".join(f"{fmt(flat[i])},{fmt(flat[i + 1])}" for i in range(0, len(flat) - 1, 2))

    def drawPolygon(self, pts: Any) -> None:  # noqa: N802
        self._stroke("polygon", {"points": self._points(pts)})

    def fillPolygon(self, pts: Any) -> None:  # noqa: N802
        self._fill("polygon", {"points": self._points(pts)})

    def drawPolyline(self, pts: Any) -> None:  # noqa: N802
        self._stroke("polyline", {"points": self._points(pts)})

    def drawArc(  # noqa: N802
        self, x: float, y: float, w: float, h: float, start: float, extent: float
    ) -> None:
        p = Path()
        p.addArc(x, y, w, h, start, extent)
        self.drawPath(p)

    def fillArc(  # noqa: N802
        self, x: float, y: float, w: float, h: float, start: float, extent: float
    ) -> None:
        p = Path()
        p.moveTo(x + w / 2, y + h / 2)
        p.addArc(x, y, w, h, start, extent)
        p.close()
        self.fillPath(p)

    def drawPath(self, p: Path) -> None:  # noqa: N802
        self._see_path(p)
        if p.parts:
            self._stroke("path", {"d": p.d()})

    def fillPath(self, p: Path) -> None:  # noqa: N802
        if self.bg is not None:
            self._see_path(p)
        if p.parts:
            self._fill("path", {"d": p.d()})

    def fillPathWith(self, p: Path, color: str) -> None:  # noqa: N802
        """``setBackgroundColor(fg); fillPath(p)`` shortcut used by icon ports."""
        self._see_path(p)
        if p.parts:
            self._fill("path", {"d": p.d()}, color)
