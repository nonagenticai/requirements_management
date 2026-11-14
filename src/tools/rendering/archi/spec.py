"""Figure contract + Archi's shared figure delegates, ported from Archi 5.10.0.

Each ArchiMate element figure is described by a :class:`FigureSpec`:

* ``box`` — Archi figure *type 0* (the rectangle-ish figure; the default). It
  draws the delegate shape only; the small type icon is drawn separately via
  ``icon`` at ``icon_origin(bounds)``.
* ``alt`` — Archi figure *type 1* (the "icon notation" figure), or ``None``
  when Archi has no alternative.
* ``icon`` — Archi's ``IIconDelegate.drawIcon(graphics, fg, bg=null, pt)``.
* ``icon_offset`` — ``getIconOffset()`` for type 0: the horizontal text margin
  Archi reserves on each side for the icon (centred text at top).
* ``box_text`` / ``alt_text`` — optional ``calculateTextControlBounds`` ports.

Copyright (c) 2013-2026 Phillip Beauvoir, Jean-Baptiste Sarrodie, The Open Group.
Ported from https://github.com/archimatetool/archi (tag release_5.10.0) under the
MIT licence; the full notice is in ``tools/rendering/archi/__init__.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tools.rendering.archi.graphics import G, Path, R, jdiv, jint

# ITextAlignment / ITextPosition constants (com.archimatetool.model).
TEXT_ALIGNMENT_LEFT = 1
TEXT_ALIGNMENT_CENTER = 2
TEXT_ALIGNMENT_RIGHT = 4
TEXT_POSITION_TOP = 0
TEXT_POSITION_CENTRE = 1
TEXT_POSITION_BOTTOM = 2


@dataclass
class Ctx:
    """What Archi's figure reads from its diagram object while drawing."""

    fill: str
    line: str
    line_width: int = 1
    text_alignment: int = TEXT_ALIGNMENT_CENTER
    text_position: int = TEXT_POSITION_TOP
    # JunctionFigure: "and" (filled) | "or" (hollow).
    junction_type: str = "and"
    # GroupingFigure sizes its tab from the measured label.
    text_width: float = 0
    text_height: float = 0

    def g(self) -> G:
        return G(fg=self.line, bg=self.fill, line_width=self.line_width)


Draw = Callable[[G, R, Ctx], None]
TextBounds = Callable[[R, Ctx], R]


@dataclass(frozen=True)
class FigureSpec:
    archi_class: str
    box: Draw
    icon: Callable[[G, float, float], None] | None = None
    icon_origin: Callable[[R], tuple[float, float]] | None = None
    icon_offset: int = 0
    alt: Draw | None = None
    box_text: TextBounds | None = None
    alt_text: TextBounds | None = None
    # Text control margin height Archi uses for the alt figure (0 for most).
    alt_text_margin_height: int = 0
    extra: dict[str, object] = field(default_factory=dict)


def darker(color: str, factor: float = 0.9) -> str:
    """ColorFactory.getDarkerColor."""
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(jint(r * factor), jint(g * factor), jint(b * factor))


def lighter(color: str, factor: float = 0.9) -> str:
    """ColorFactory.getLighterColor."""
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(*(max(2, min(jint(c / factor), 255)) for c in (r, g, b)))


def figure_position(rect: R, ctx: Ctx, ratio: float = 1.0) -> R:
    """AbstractTextControlContainerFigure.getFigurePositionFromTextPosition."""
    new = rect.copy()
    if rect.height * ratio <= rect.width:
        new.width = jint(rect.height * ratio)
        if ctx.text_alignment == TEXT_ALIGNMENT_CENTER:
            new.x += jdiv(rect.width - new.width, 2)
        elif ctx.text_alignment == TEXT_ALIGNMENT_LEFT:
            new.x += rect.width - new.width
    else:
        new.height = jint(rect.width / ratio)
        if ctx.text_position == TEXT_POSITION_TOP:
            new.y += rect.height - new.height
        elif ctx.text_position == TEXT_POSITION_CENTRE:
            new.y += jdiv(rect.height - new.height, 2)
    return new


def default_text_bounds(rect: R, ctx: Ctx, icon_offset: int, margin_width: int = 4) -> R:
    """AbstractTextControlContainerFigure.calculateTextControlBounds (icon margin only)."""
    if icon_offset == 0:
        return rect.copy()
    off = icon_offset - margin_width
    r = rect.copy()
    if ctx.text_position == TEXT_POSITION_TOP:
        if ctx.text_alignment == TEXT_ALIGNMENT_CENTER:
            r.x += off
            r.width -= off * 2
        elif ctx.text_alignment == TEXT_ALIGNMENT_RIGHT:
            r.width -= off
    return r


# ---------------------------------------------------------------------------
# Shared delegates (figure type 0 shapes). Gradients and icon images are not
# ported: snapshots carry neither.
# ---------------------------------------------------------------------------


def rectangle_delegate(g: G, rect: R, ctx: Ctx) -> None:
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillRectangle(rect)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawRectangle(rect)
    g.popState()


def rounded_rectangle_delegate(g: G, rect: R, ctx: Ctx, arc: tuple[int, int] = (20, 20)) -> None:
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillRoundRectangle(rect, arc[0], arc[1])
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawRoundRectangle(rect, arc[0], arc[1])
    g.popState()


BOX_EDGE_SIZE = 14


def box_delegate(g: G, rect: R, ctx: Ctx) -> None:
    """BoxFigureDelegate: the 3D box (node, device-ish figures)."""
    e = BOX_EDGE_SIZE
    g.pushState()
    g.setBackgroundColor(darker(ctx.fill))
    p = Path()
    p.moveTo(rect.x, rect.y + e)
    p.lineTo(rect.x + e, rect.y)
    p.lineTo(rect.x + rect.width, rect.y)
    p.lineTo(rect.x + rect.width, rect.y + rect.height - e)
    p.lineTo(rect.x + rect.width - e, rect.y + rect.height)
    p.lineTo(rect.x, rect.y + rect.height)
    g.fillPath(p)
    g.setBackgroundColor(ctx.fill)
    g.fillRectangle(rect.x, rect.y + e, rect.width - e, rect.height - e)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    p1 = Path()
    p1.moveTo(rect.x, rect.y + e)
    p1.lineTo(rect.x + e, rect.y)
    p1.lineTo(rect.x + rect.width, rect.y)
    p1.lineTo(rect.x + rect.width, rect.y + rect.height - e)
    p1.lineTo(rect.x + rect.width - e, rect.y + rect.height)
    p1.lineTo(rect.x, rect.y + rect.height)
    p1.close()
    g.drawPath(p1)
    p2 = Path()
    p2.moveTo(rect.x, rect.y + e)
    p2.lineTo(rect.x + rect.width - e, rect.y + e)
    p2.lineTo(rect.x + rect.width, rect.y)
    p2.moveTo(rect.x + rect.width - e, rect.y + e)
    p2.lineTo(rect.x + rect.width - e, rect.y + rect.height)
    g.drawPath(p2)
    g.popState()


def box_text_bounds(rect: R, ctx: Ctx, icon_offset: int) -> R:
    """BoxFigureDelegate.calculateTextControlBounds."""
    r = rect.copy()
    edge_offset = BOX_EDGE_SIZE + 1
    if ctx.text_position == TEXT_POSITION_TOP:
        r.y += BOX_EDGE_SIZE
    elif ctx.text_position == TEXT_POSITION_CENTRE:
        r.y += jdiv(BOX_EDGE_SIZE, 2)
    if ctx.text_alignment == TEXT_ALIGNMENT_CENTER:
        if ctx.text_position == TEXT_POSITION_TOP:
            r.x += icon_offset
            r.width = r.width - icon_offset * 2 - edge_offset
        else:
            r.width -= edge_offset
    elif ctx.text_alignment == TEXT_ALIGNMENT_RIGHT:
        if ctx.text_position == TEXT_POSITION_TOP:
            r.width -= icon_offset + jdiv(edge_offset, 2)
        else:
            r.width -= edge_offset
    return r


CYLINDER_OFFSET = 4


def cylinder_delegate(g: G, rect: R, ctx: Ctx) -> None:
    o = CYLINDER_OFFSET
    g.pushState()
    p = Path()
    p.addArc(rect.x, rect.y, jdiv(rect.width, o), rect.height, 90, 180)
    p.addArc(
        (rect.x + rect.width) - jdiv(rect.width, o),
        rect.y,
        jdiv(rect.width, o),
        rect.height,
        270,
        180,
    )
    p.close()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawPath(p)
    p2 = Path()
    p2.addArc(
        (rect.x + rect.width) - jdiv(rect.width, o),
        rect.y,
        jdiv(rect.width, o),
        rect.height,
        90,
        180,
    )
    g.drawPath(p2)
    g.popState()


def ellipse_delegate(g: G, rect: R, ctx: Ctx) -> None:
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillOval(rect.x, rect.y, rect.width, rect.height)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawOval(rect.x, rect.y, rect.width, rect.height)
    g.popState()


PARALLELOGRAM_FLANGE = 16


def parallelogram_delegate(g: G, rect: R, ctx: Ctx, with_slash: bool = False) -> None:
    f = PARALLELOGRAM_FLANGE
    g.pushState()
    p = Path()
    p.moveTo(rect.x + f, rect.y)
    p.lineTo(rect.x + rect.width, rect.y)
    p.lineTo(rect.x + rect.width - f, rect.y + rect.height)
    p.lineTo(rect.x, rect.y + rect.height)
    p.close()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(p)
    if with_slash:
        s = Path()
        s.moveTo(rect.x + f + 20, rect.y)
        s.lineTo(rect.x + 20, rect.y + rect.height)
        g.drawPath(s)
    g.popState()


def process_delegate(g: G, rect: R, ctx: Ctx) -> None:
    x1 = rect.x + rect.width * 0.7
    y1 = rect.y + jdiv(rect.height, 5)
    y2 = rect.y + rect.height - jdiv(rect.height, 5)
    lo = ctx.line_width / 2.0
    p = Path()
    p.moveTo(rect.x, y1)
    p.lineTo(x1, y1)
    p.lineTo(x1, rect.y + lo)
    p.lineTo(rect.x + rect.width, rect.y + jdiv(rect.height, 2))
    p.lineTo(x1, rect.y + rect.height - lo)
    p.lineTo(x1, y2)
    p.lineTo(rect.x, y2)
    p.close()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawPath(p)
    g.popState()


def process_text_bounds(rect: R, ctx: Ctx) -> R:
    r = rect.copy()
    if ctx.text_position == TEXT_POSITION_TOP:
        r.y += jdiv(rect.height, 5)
    return r


def service_delegate(g: G, rect: R, ctx: Ctx) -> None:
    arc = (min(rect.height, jdiv(rect.width * 8, 10)), rect.height)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillRoundRectangle(rect, arc[0], arc[1])
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawRoundRectangle(rect, arc[0], arc[1])
    g.popState()
