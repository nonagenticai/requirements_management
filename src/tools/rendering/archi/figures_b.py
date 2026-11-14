"""ArchiMate element figures, batch B — ported from Archi 5.10.0.

Line-by-line ports of ``com.archimatetool.editor.diagram.figures.elements``
(tag ``release_5.10.0``): Interaction, Interface, Location, Material, Meaning,
Node, Object, Outcome, Path, Plateau, Principle, Process, Product,
Representation, Requirement, Resource, Service, Stakeholder, SystemSoftware,
Value, ValueStream, WorkPackage, Junction and Grouping, plus the parts of
AbstractMotivationFigure, DistributionNetworkFigure and DeliverableFigure they
inherit. Java ``int`` arithmetic is kept via :func:`jdiv` / :func:`jint`;
gradients and user icon images are not ported (snapshots carry neither).

Copyright (c) 2013-2026 Phillip Beauvoir, Jean-Baptiste Sarrodie, The Open Group.
MIT licence — full notice in ``tools/rendering/archi/__init__.py``.
"""

from __future__ import annotations

import math

from tools.rendering.archi.graphics import G, Path, R, jdiv, jint
from tools.rendering.archi.spec import (
    TEXT_ALIGNMENT_RIGHT,
    TEXT_POSITION_TOP,
    Ctx,
    FigureSpec,
    box_delegate,
    box_text_bounds,
    cylinder_delegate,
    figure_position,
    parallelogram_delegate,
    process_delegate,
    process_text_bounds,
    rectangle_delegate,
    rounded_rectangle_delegate,
    service_delegate,
)

# AbstractTextControlContainerFigure.getTextControlMarginHeight() default.
_MARGIN_HEIGHT = 4
# AbstractTextControlContainerFigure.getTextControlMarginWidth() default.
_MARGIN_WIDTH = 4


# SWT sub-path semantics now live in graphics.Path itself.
_SwtPath = Path


def _jround(v: float) -> int:
    """Java ``Math.round`` (half up)."""
    return math.floor(v + 0.5)


def _shrunk(rect: R, ctx: Ctx) -> R:
    """``rect.shrink(ceil(lineWidth / 2.0))`` as used by the type-1 figures."""
    s = jint(math.ceil(ctx.line_width / 2.0))
    return rect.copy().shrink(s, s)


def _oval_path(x: float, y: float, w: float, h: float) -> Path:
    """FigureUtils.createOvalPath."""
    p = _SwtPath()
    p.addArc(x, y, w, h, 0, 360)
    return p


def _center(rect: R) -> tuple[int, int]:
    """draw2d ``Rectangle.getCenter()``."""
    return (jint(rect.x + jdiv(rect.width, 2)), jint(rect.y + jdiv(rect.height, 2)))


# ---------------------------------------------------------------------------
# AbstractMotivationFigure (type 0 of the motivation elements): cut corners.
# ---------------------------------------------------------------------------

_MOTIVATION_INSET = 10


def _motivation_path(rect: R) -> Path:
    i = _MOTIVATION_INSET
    p = _SwtPath()
    p.moveTo(rect.x + i, rect.y)
    p.lineTo(rect.x + rect.width - i, rect.y)
    p.lineTo(rect.x + rect.width, rect.y + i)
    p.lineTo(rect.x + rect.width, rect.y + rect.height - i)
    p.lineTo(rect.x + rect.width - i, rect.y + rect.height)
    p.lineTo(rect.x + i, rect.y + rect.height)
    p.lineTo(rect.x, rect.y + rect.height - i)
    p.lineTo(rect.x, rect.y + i)
    p.close()
    return p


def motivation_box(g: G, rect: R, ctx: Ctx) -> None:
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(_motivation_path(rect))
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(_motivation_path(rect))
    g.popState()


# ---------------------------------------------------------------------------
# InteractionFigure
# ---------------------------------------------------------------------------


def _interaction_path(rect: R) -> Path:
    fraction = 0.86
    gap = 1 - fraction
    x1 = jint(rect.x)
    if rect.width <= rect.height:
        diameter = rect.width * fraction
        x2 = jint(rect.x + rect.width)
    else:
        diameter = min(rect.height, rect.width * 0.85)
        x1 = jint(x1 + (((rect.width - diameter) / 2) - (diameter * gap / 2)))
        x2 = jint(x1 + diameter + (diameter * gap))
    y = rect.y + (rect.height - diameter) / 2
    p = _SwtPath()
    p.addArc(x1, y, diameter, diameter, 90, 180)
    p.close()
    p.addArc(x2 - diameter, y, diameter, diameter, 270, 180)
    p.close()
    return p


def interaction_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(_shrunk(rect, ctx), ctx, 1 / 0.86)
    path = _interaction_path(r)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(path)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(path)
    g.popState()


def interaction_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    p = _SwtPath()
    p.addArc(x - 5, y, 10, 12, 90, 180)
    p.lineTo(x, y - 0.5)
    g.drawPath(p)
    p = _SwtPath()
    p.addArc(x - 2, y, 10, 12, -90, 180)
    p.lineTo(x + 3, y + 12.5)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# InterfaceFigure
# ---------------------------------------------------------------------------


def interface_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(_shrunk(rect, ctx), ctx)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(_oval_path(r.x, r.y, r.width, r.height))
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(_oval_path(r.x, r.y, r.width, r.height))
    g.popState()


def interface_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    g.drawOval(x, y, 10, 10)
    g.drawLine(x, y + 5, x - 7, y + 5)
    g.popState()


# ---------------------------------------------------------------------------
# LocationFigure
# ---------------------------------------------------------------------------


def location_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(rect, ctx)
    if r.width <= r.height:
        fw = fh = jint(r.width)
    else:
        fw = fh = jint(r.height)
    y_margin = jdiv(r.height - fh + ctx.line_width + 1, 2)
    x_center = r.x + jdiv(r.width, 2)
    diameter = float(jdiv(fw, 4) * 3)
    p = _SwtPath()
    p.addArc(x_center - diameter / 2, r.y + y_margin, diameter, diameter, -35, 250)
    p.lineTo(x_center, r.y + r.height - y_margin)
    p.close()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(p)
    g.popState()


def location_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    p = _SwtPath()
    p.addArc(x - 5, y - 15, 10, 10, -20, 220)
    p.lineTo(x, y)
    p.close()
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# MaterialFigure
# ---------------------------------------------------------------------------


def material_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(_shrunk(rect, ctx), ctx, 10 / 9.0)
    fw = jint(r.width)
    fh = jint(r.height)
    if r.width <= r.height:
        fh = jint(r.width - jdiv(r.width, 10))
    else:
        fw = jint(r.height + jdiv(r.width, 10))
        fw = min(fw, jint(r.width))
    xm = jdiv(r.width - fw, 2)
    ym = jdiv(r.height - fh, 2)
    p = _SwtPath()
    p.moveTo(r.x + xm + jdiv(fw, 4), r.y + ym)
    p.lineTo(r.x + xm, r.y + ym + jdiv(fh, 2))
    p.lineTo(r.x + xm + jdiv(fw, 4), r.y + ym + fh)
    p.lineTo(r.x + xm + jdiv(3 * fw, 4), r.y + ym + fh)
    p.lineTo(r.x + xm + fw, r.y + ym + jdiv(fh, 2))
    p.lineTo(r.x + xm + jdiv(3 * fw, 4), r.y + ym)
    p.close()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(p)
    p = _SwtPath()
    p.moveTo(r.x + xm + jdiv(3 * fw, 8), r.y + ym + jdiv(fh, 10))
    p.lineTo(r.x + xm + jdiv(fw, 6), r.y + ym + jdiv(fh, 2))
    p.moveTo(r.x + xm + jdiv(fw, 3), r.y + ym + fh - jdiv(fh, 7))
    p.lineTo(r.x + xm + fw - jdiv(fw, 3), r.y + ym + fh - jdiv(fh, 7))
    p.moveTo(r.x + xm + fw - jdiv(3 * fw, 8), r.y + ym + jdiv(fh, 10))
    p.lineTo(r.x + xm + fw - jdiv(fw, 6), r.y + ym + jdiv(fh, 2))
    g.drawPath(p)
    g.popState()


def material_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1.2)
    g.drawPolygon([x + 4, y - 7, x - 4, y - 7, x - 8, y, x - 5, y + 7, x + 4, y + 7, x + 8, y])
    p = _SwtPath()
    p.moveTo(x - 2, y - 5)
    p.lineTo(x - 5.3, y + 0.5)
    p.moveTo(x - 3.7, y + 4.5)
    p.lineTo(x + 3, y + 4.5)
    p.moveTo(x + 5, y + 0.5)
    p.lineTo(x + 2, y - 5)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# MeaningFigure
# ---------------------------------------------------------------------------


def meaning_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(rect, ctx, 5 / 3.0)
    ux = r.width / 12.0
    uy = r.height / 7.0
    x, y = r.x, r.y
    p = _SwtPath()
    p.moveTo(x + ux, y + 2 * uy)
    p.cubicTo(x, y, x + 2 * ux, y, x + 3 * ux, y + uy)
    p.cubicTo(x + 4 * ux, y, x + 6 * ux, y, x + 7 * ux, y + uy)
    p.cubicTo(x + 8 * ux, y, x + 10 * ux, y, x + 10 * ux, y + uy)
    p.cubicTo(x + 12 * ux, y, x + 12 * ux, y + 2 * uy, x + 11 * ux, y + 3 * uy)
    p.cubicTo(x + 12 * ux, y + 3 * uy, x + 12 * ux, y + 4 * uy, x + 11 * ux, y + 5 * uy)
    p.cubicTo(x + 11 * ux, y + 7 * uy, x + 8 * ux, y + 7 * uy, x + 7 * ux, y + 6 * uy)
    p.cubicTo(x + 6 * ux, y + 7 * uy, x + 2 * ux, y + 7 * uy, x + 2 * ux, y + 5 * uy)
    p.cubicTo(x, y + 5 * uy, x, y + 3 * uy, x + ux, y + 2 * uy)
    p.close()
    p.moveTo(x + 0.5 * ux, y + 5.5 * uy)
    p.cubicTo(x + ux, y + 5 * uy, x + 2 * ux, y + 5.5 * uy, x + 1.5 * ux, y + 6 * uy)
    p.cubicTo(x + ux, y + 6.5 * uy, x, y + 6 * uy, x + 0.5 * ux, y + 5.5 * uy)
    p.close()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(p)
    g.popState()


def meaning_icon(g: G, x: float, y: float) -> None:
    w, h = 14, 11
    g.pushState()
    g.setLineWidth(1)
    p = _SwtPath()
    p.addArc(x, y, jdiv(w, 3) * 2, jdiv(h, 3) * 2, 60, 149)
    g.drawPath(p)
    p = _SwtPath()
    p.addArc(x + jdiv(w, 3) - 1, y, jdiv(w, 3) * 2, jdiv(h, 3) * 2, -38, 157)
    g.drawPath(p)
    p = _SwtPath()
    p.addArc(x, y + jdiv(h, 3), jdiv(w, 5) * 3, jdiv(h, 3) * 2 - 1, -41, -171)
    g.drawPath(p)
    p = _SwtPath()
    p.addArc(x + jdiv(w, 3), y + jdiv(h, 4), jdiv(w, 5) * 3, jdiv(h, 3) * 2, 7, -136)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# NodeFigure
# ---------------------------------------------------------------------------


def node_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    p = _SwtPath()
    p.addRectangle(x, y, 11, 11)
    p.moveTo(x - 0.2, y)
    p.lineTo(x + 3.2, y - 3)
    p.lineTo(x + 14, y - 3)
    p.lineTo(x + 14, y + 8)
    p.lineTo(x + 11, y + 11.2)
    p.moveTo(x + 11, y)
    p.lineTo(x + 14, y - 3)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# ObjectFigure (business object, data object; contract is batch A)
# ---------------------------------------------------------------------------

_OBJECT_TOP_MARGIN = 12


def object_alt(g: G, rect: R, ctx: Ctx) -> None:
    """ObjectFigureDelegate: rectangle with a band line."""
    rectangle_delegate(g, rect, ctx)
    g.pushState()
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawLine(
        rect.x, rect.y + _OBJECT_TOP_MARGIN, rect.x + rect.width, rect.y + _OBJECT_TOP_MARGIN
    )
    g.popState()


def object_alt_text(rect: R, ctx: Ctx) -> R:
    r = rect.copy()
    if ctx.text_position == TEXT_POSITION_TOP:
        r.y += _OBJECT_TOP_MARGIN - _MARGIN_HEIGHT
    return r


def object_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    g.drawRectangle(x, y, 13, 10)
    g.drawLine(x, y + 3, x + 13, y + 3)
    g.popState()


# ---------------------------------------------------------------------------
# OutcomeFigure
# ---------------------------------------------------------------------------


def _outcome_radius(rect: R) -> int:
    return min(_jround(rect.height / 2.2), _jround(rect.width / 2.2))


def _outcome_center(rect: R) -> tuple[int, int]:
    radius = _outcome_radius(rect)
    fw = jint(radius * 2.2)
    fh = jint(radius * 2.2)
    return (
        jint(rect.x + radius + jdiv(rect.width - fw, 2)),
        jint(rect.y + rect.height - radius - jdiv(rect.height - fh, 2)),
    )


def outcome_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(_shrunk(rect, ctx), ctx)
    radius = _outcome_radius(r)
    diameter = radius * 2
    cx, cy = _outcome_center(r)
    circle_x = cx - radius
    circle_y = cy - radius
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(_oval_path(circle_x, circle_y, diameter, diameter))
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(_oval_path(circle_x, circle_y, diameter, diameter))
    g.setBackgroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    lw_half = jdiv(ctx.line_width, 2)
    radius2 = _jround(radius * 2.0 / 3.0 - lw_half)
    g.drawOval(cx - radius2, cy - radius2, 2 * radius2, 2 * radius2)
    radius3 = _jround(radius / 3.0 - lw_half)
    g.drawOval(cx - radius3, cy - radius3, 2 * radius3, 2 * radius3)
    arrow_length = jint(radius3 * 0.8)
    arrow_lw = max(2, jdiv(arrow_length, 6))
    tmp = r.copy()
    tmp.width -= 1
    tcx, tcy = _outcome_center(tmp)
    g.fillPolygon(
        [
            tcx - arrow_lw,
            tcy + arrow_lw,
            tcx + arrow_length,
            tcy,
            tcx,
            tcy - arrow_length,
        ]
    )
    g.setLineWidth(arrow_lw)
    ratio = 1.2
    g.drawLine(
        cx,
        cy,
        jint(cx + radius * ratio - 0.5 * arrow_length),
        jint(cy - radius * ratio + 0.5 * arrow_length),
    )
    g.drawLine(
        jint(cx + ratio * (radius - arrow_length * 1.2)),
        jint(cy + ratio * (arrow_length * 1.2 - radius)),
        jint(cx + radius * ratio),
        jint(cy - radius * ratio) + arrow_length + arrow_lw,
    )
    g.drawLine(
        jint(cx + ratio * (radius - arrow_length * 1.2)),
        jint(cy + ratio * (arrow_length * 1.2 - radius)),
        jint(cx + radius * ratio) - arrow_length - arrow_lw,
        jint(cy - radius * ratio),
    )
    g.popState()


def outcome_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1.2)
    p = _SwtPath()
    p.addArc(x, y, 13, 13, 0, 360)
    p.addArc(x + 2.5, y + 2.5, 8, 8, 0, 360)
    p.addArc(x + 5, y + 5, 3, 3, 0, 360)
    p.addArc(x + 6, y + 6, 1, 1, 0, 360)
    g.drawPath(p)
    g.setLineWidth(1)
    p = _SwtPath()
    p.moveTo(x + 6.0, y + 7.0)
    p.lineTo(x + 15.5, y - 2.5)
    p.moveTo(x + 13.0, y + 0.0)
    p.lineTo(x + 14.0, y - 5.0)
    p.moveTo(x + 13.0, y + 0.0)
    p.lineTo(x + 18.0, y - 1.0)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# DistributionNetworkFigure geometry, as inherited by PathFigure
# ---------------------------------------------------------------------------

_ARROW_ANGLE = math.cos(math.radians(60))


def _arrow_size(rect: R) -> tuple[int, int]:
    width = jint(rect.width / (1 + _ARROW_ANGLE) / 2)
    size = min(jint(rect.height), width)
    return (jint(size * _ARROW_ANGLE), size)


def path_alt(g: G, rect: R, ctx: Ctx) -> None:
    """DistributionNetworkFigure.drawFigure with PathFigure's overrides
    (no filled section; one dashed horizontal line)."""
    r = figure_position(rect, ctx, 5 / 3.0)
    lw = jint(max(3, math.sqrt(rect.width * rect.height) / 24))
    r.shrink(lw, lw)
    aw, ah = _arrow_size(r)
    g.pushState()
    g.setForegroundColor(ctx.line)
    g.setLineWidth(lw)
    # drawArrows
    p = _SwtPath()
    p.moveTo(r.x + aw, r.y + jdiv(r.height, 2) - jdiv(ah, 2))
    p.lineTo(r.x, r.y + jdiv(r.height, 2))
    p.lineTo(r.x + aw, r.y + jdiv(r.height, 2) + jdiv(ah, 2))
    p.moveTo(r.x + r.width - aw, r.y + jdiv(r.height, 2) - jdiv(ah, 2))
    p.lineTo(r.x + r.width, r.y + jdiv(r.height, 2))
    p.lineTo(r.x + r.width - aw, r.y + jdiv(r.height, 2) + jdiv(ah, 2))
    g.drawPath(p)
    # PathFigure.drawHorizontalLine
    g.setLineDash([lw * 2.0, float(lw)])
    g.drawLine(r.x, r.y + jdiv(r.height, 2), r.x + r.width, r.y + jdiv(r.height, 2))
    g.popState()


def path_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1.5)
    p = _SwtPath()
    p.moveTo(x + 2.5, y)
    p.lineTo(x + 4.5, y)
    p.moveTo(x + 6.5, y)
    p.lineTo(x + 8.5, y)
    p.moveTo(x + 10.5, y)
    p.lineTo(x + 12.5, y)
    g.drawPath(p)
    p = _SwtPath()
    p.moveTo(x + 4, y - 5)
    p.lineTo(x - 1, y)
    p.lineTo(x + 4, y + 5)
    p.moveTo(x + 11, y - 5)
    p.lineTo(x + 16, y)
    p.lineTo(x + 11, y + 5)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# PlateauFigure
# ---------------------------------------------------------------------------


def plateau_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(rect, ctx, 5 / 3.5)
    g.pushState()
    g.setForegroundColor(ctx.line)
    lw = jint(max(7, math.sqrt(r.width * r.height) / 16))
    g.setLineWidth(lw)
    m = jint(min(r.width, r.height))
    xm = jdiv(r.width - m, 2)
    ym = jdiv(r.height - m, 2)
    g.drawLine(
        r.x + xm,
        r.y + ym + jdiv(3 * m, 4),
        r.x + xm + m - jdiv(2 * m, 6),
        r.y + ym + jdiv(3 * m, 4),
    )
    g.drawLine(
        r.x + xm + jdiv(1 * m, 6),
        r.y + ym + jdiv(2 * m, 4),
        r.x + xm + m - jdiv(1 * m, 6),
        r.y + ym + jdiv(2 * m, 4),
    )
    g.drawLine(
        r.x + xm + jdiv(2 * m, 6),
        r.y + ym + jdiv(1 * m, 4),
        r.x + xm + m,
        r.y + ym + jdiv(1 * m, 4),
    )
    g.popState()


def plateau_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(2)
    g.drawLine(x, y, x + 12, y)
    x, y = x + 2, y - 3
    g.drawLine(x, y, x + 12, y)
    x, y = x + 2, y - 3
    g.drawLine(x, y, x + 12, y)
    g.popState()


# ---------------------------------------------------------------------------
# PrincipleFigure
# ---------------------------------------------------------------------------


def _principle_path(rect: R) -> Path:
    divisions, div1, div2 = 24, 8, 16
    fx = jdiv(rect.width, divisions)
    fy = jdiv(rect.height, divisions)
    corner = min(fx, fy)
    x, y, w, h = rect.x, rect.y, rect.width, rect.height
    p = _SwtPath()
    p.moveTo(x + corner, y + corner)
    p.cubicTo(x + fx * div1, y, x + fx * div2, y, x + w - corner, y + corner)
    p.cubicTo(x + w, y + fy * div1, x + w, y + fy * div2, x + w - corner, y + h - corner)
    p.cubicTo(x + fx * div2, y + h, x + fx * div1, y + h, x + corner, y + h - corner)
    p.cubicTo(x, y + fy * div2, x, y + fy * div1, x + corner, y + corner)
    p.close()
    return p


def principle_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(_shrunk(rect, ctx), ctx)
    path = _principle_path(r)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(path)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(path)
    cx, _cy = _center(r)
    width = max(1, _jround((r.height - 2.0 * ctx.line_width) / 20.0))
    if width >= jdiv(r.width, 2):
        width = max(1, jdiv(r.width, 4))
    g.setBackgroundColor(ctx.line)
    w8 = _jround(0.8 * width)
    g.fillPolygon(
        [
            cx - width,
            r.y + 3 * width,
            cx + width,
            r.y + 3 * width,
            cx + w8,
            r.y + r.height - 7 * width,
            cx - w8,
            r.y + r.height - 7 * width,
        ]
    )
    g.fillPolygon(
        [
            cx + w8,
            r.y + r.height - 5 * width,
            cx - w8,
            r.y + r.height - 5 * width,
            cx - w8,
            r.y + r.height - 3 * width,
            cx + w8,
            r.y + r.height - 3 * width,
        ]
    )
    g.popState()


def principle_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    g.drawRoundRectangle(R(x, y, 12, 14), 4, 4)
    p = _SwtPath()
    p.moveTo(x + 5.5, y + 2)
    p.lineTo(x + 5.5, y + 9)
    p.moveTo(x + 6.5, y + 2)
    p.lineTo(x + 6.5, y + 9)
    p.moveTo(x + 5.5, y + 10.5)
    p.lineTo(x + 5.5, y + 12.5)
    p.moveTo(x + 6.5, y + 10.5)
    p.lineTo(x + 6.5, y + 12.5)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# ProcessFigure
# ---------------------------------------------------------------------------


def process_icon(g: G, x: float, y: float) -> None:
    pts = []
    px, py = x, y
    pts.append((px, py))
    px += 8
    pts.append((px, py))
    py -= 3
    pts.append((px, py))
    px, py = px + 6, py + 5
    pts.append((px, py))
    px, py = px - 6, py + 5
    pts.append((px, py))
    py -= 3
    pts.append((px, py))
    px -= 8
    pts.append((px, py))
    g.pushState()
    g.setLineWidth(1)
    g.drawPolygon(pts)
    g.popState()


# ---------------------------------------------------------------------------
# ProductFigure
# ---------------------------------------------------------------------------

_PRODUCT_TOP_MARGIN = 12


def product_alt(g: G, rect: R, ctx: Ctx) -> None:
    rectangle_delegate(g, rect, ctx)
    cx, _cy = _center(rect)
    g.pushState()
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    p = _SwtPath()
    p.moveTo(rect.x, rect.y + _PRODUCT_TOP_MARGIN)
    p.lineTo(cx, rect.y + _PRODUCT_TOP_MARGIN)
    p.lineTo(cx, rect.y)
    g.drawPath(p)
    g.popState()


def _top_margin_text(rect: R, ctx: Ctx) -> R:
    """Product / Representation: ``y += TOP_MARGIN - marginHeight - 1``."""
    r = rect.copy()
    if ctx.text_position == TEXT_POSITION_TOP:
        r.y += _PRODUCT_TOP_MARGIN - _MARGIN_HEIGHT - 1
    return r


def product_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    g.drawRectangle(x, y, 13, 10)
    g.drawRectangle(x, y, 6, 3)
    g.popState()


# ---------------------------------------------------------------------------
# DeliverableFigure geometry, as inherited by RepresentationFigure
# ---------------------------------------------------------------------------


def _deliverable_path(curve_height: float, rect: R, inset: float) -> Path:
    curve_y = rect.bottom() - curve_height
    p = _SwtPath()
    p.moveTo(rect.x + inset, rect.y + inset)
    p.lineTo(rect.x + inset, curve_y - 1)
    p.quadTo(
        rect.x + jdiv(rect.width, 4),
        rect.bottom() + curve_height - inset,
        rect.x + jdiv(rect.width, 2) + 1,
        curve_y,
    )
    p.quadTo(
        rect.right() - jdiv(rect.width, 4),
        curve_y - curve_height - 1,
        rect.right() - inset,
        curve_y,
    )
    p.lineTo(rect.x + rect.width - inset, rect.y + inset)
    p.close()
    return p


def representation_alt(g: G, rect: R, ctx: Ctx) -> None:
    path = _deliverable_path(6, rect, 0)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(path)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawPath(path)
    g.drawLine(
        rect.x, rect.y + _PRODUCT_TOP_MARGIN, rect.x + rect.width, rect.y + _PRODUCT_TOP_MARGIN
    )
    g.popState()


def representation_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    # DeliverableFigure's icon …
    g.setLineWidth(1)
    g.drawPath(_deliverable_path(1.5, R(x, y, 14, 10), 0.5))
    # … plus the band line.
    g.setLineWidth(1)
    g.drawLine(x, y + 3, x + 14, y + 3)
    g.popState()


# ---------------------------------------------------------------------------
# RequirementFigure
# ---------------------------------------------------------------------------


def requirement_alt(g: G, rect: R, ctx: Ctx) -> None:
    parallelogram_delegate(g, rect, ctx, with_slash=False)


def requirement_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    g.drawPolygon([x, y, x + 12, y, x + 8, y + 9, x - 4, y + 9])
    g.popState()


# ---------------------------------------------------------------------------
# ResourceFigure
# ---------------------------------------------------------------------------


def _resource_path(rect: R, nw: int, nh: int) -> Path:
    a1, a2 = 5, 3
    x, y, w, h = rect.x, rect.y, rect.width, rect.height
    mid = y + jdiv(h - nh, 2)
    p = _SwtPath()
    p.moveTo(x + a1, y)
    p.lineTo(x + w - nw - a1, y)
    p.cubicTo(x + w - nw - a1, y, x + w - nw, y, x + w - nw, y + a1)
    p.lineTo(x + w - nw, mid)
    p.lineTo(x + w - a1, mid)
    p.cubicTo(x + w - a2, mid, x + w, mid, x + w, mid + a2)
    p.lineTo(x + w, mid + nh - a1)
    p.cubicTo(x + w, mid + nh - a2, x + w, mid + nh, x + w - a2, mid + nh)
    p.lineTo(x + w - nw, mid + nh)
    p.lineTo(x + w - nw, y + h - a1)
    p.cubicTo(x + w - nw, y + h - a1, x + w - nw, y + h, x + w - nw - a1, y + h)
    p.lineTo(x + a1, y + h)
    p.cubicTo(x + a1, y + h, x, y + h, x, y + h - a1)
    p.lineTo(x, y + a1)
    p.cubicTo(x, y + a1, x, y, x + a1, y)
    return p


def resource_alt(g: G, rect: R, ctx: Ctx) -> None:
    nw, nh = jdiv(rect.width, 12), jdiv(rect.height, 3)
    path = _resource_path(rect, nw, nh)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(path)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(path)
    lx = rect.x + rect.width - nw
    ly = rect.y + jdiv(rect.height - nh, 2)
    p = _SwtPath()
    p.moveTo(lx, ly)
    p.lineTo(lx, ly + nh)
    g.drawPath(p)
    top = rect.y + jdiv(rect.height, 5)
    bottom = rect.y + jdiv(rect.height * 4, 5)
    gap = jdiv(rect.width, 6)
    g.drawLine(rect.x + gap, top, rect.x + gap, bottom)
    g.drawLine(rect.x + gap * 2, top, rect.x + gap * 2, bottom)
    g.drawLine(rect.x + gap * 3, top, rect.x + gap * 3, bottom)
    g.popState()


def resource_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    g.drawRoundRectangle(R(x, y, 15, 10), 3, 3)
    g.drawRoundRectangle(R(x + 15, y + 3, 2, 4), 1, 1)
    p = _SwtPath()
    p.moveTo(x + 3, y + 2)
    p.lineTo(x + 3, y + 8)
    p.moveTo(x + 6, y + 2)
    p.lineTo(x + 6, y + 8)
    p.moveTo(x + 9, y + 2)
    p.lineTo(x + 9, y + 8)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# ServiceFigure
# ---------------------------------------------------------------------------


def service_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    g.drawRoundRectangle(R(x, y, 16, 9), 8, 8)
    g.popState()


# ---------------------------------------------------------------------------
# StakeholderFigure
# ---------------------------------------------------------------------------


def stakeholder_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    p = _SwtPath()
    p.addArc(x, y, 8, 7, 90, 180)
    p.lineTo(x + 11, y + 7)
    p.moveTo(x + 3.5, y)
    p.lineTo(x + 11, y)
    g.drawPath(p)
    g.drawOval(x + 8, y, 7, 7)
    g.popState()


# ---------------------------------------------------------------------------
# SystemSoftwareFigure
# ---------------------------------------------------------------------------


def system_software_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(rect, ctx)
    diameter = jdiv(r.width, 3) * 2
    x1 = r.x + jdiv(r.width - diameter, 4)
    x2 = x1 + jdiv(diameter, 6)
    y1 = r.y + jdiv(r.height - diameter, 2)
    y2 = y1 + jdiv(diameter, 6)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.fillOval(x2, y1, diameter, diameter)
    g.drawOval(x2, y1, diameter, diameter)
    g.fillOval(x1, y2, diameter, diameter)
    g.drawOval(x1, y2, diameter, diameter)
    g.popState()


def system_software_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    p = _SwtPath()
    p.addArc(x, y, 11, 11, 90, 360)
    p.addArc(x + 2, y - 2, 11, 11, -60, 210)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# ValueFigure
# ---------------------------------------------------------------------------


def value_alt(g: G, rect: R, ctx: Ctx) -> None:
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(_oval_path(rect.x, rect.y, rect.width, rect.height))
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawPath(_oval_path(rect.x, rect.y, rect.width, rect.height))
    g.popState()


def value_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    g.drawOval(x, y, 14, 9)
    g.popState()


# ---------------------------------------------------------------------------
# ValueStreamFigure
# ---------------------------------------------------------------------------


def value_stream_alt(g: G, rect: R, ctx: Ctx) -> None:
    indent = min(jdiv(rect.height, 2), jdiv(rect.width, 2))
    centre_y = rect.y + jdiv(rect.height, 2)
    point_startx = rect.x + rect.width - indent
    lo = ctx.line_width / 2.0
    p = _SwtPath()
    p.moveTo(rect.x + lo, rect.y)
    p.lineTo(rect.x + indent, centre_y)
    p.lineTo(rect.x + lo, rect.y + rect.height)
    p.lineTo(point_startx, rect.y + rect.height)
    p.lineTo(rect.x + rect.width, centre_y)
    p.lineTo(point_startx, rect.y)
    p.close()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(p)
    g.popState()


def value_stream_icon(g: G, x: float, y: float) -> None:
    pts = []
    px, py = x, y
    pts.append((px, py))
    px += 10
    pts.append((px, py))
    px, py = px + 5, py + 5
    pts.append((px, py))
    px, py = px - 5, py + 5
    pts.append((px, py))
    px -= 10
    pts.append((px, py))
    px, py = px + 5, py - 5
    pts.append((px, py))
    g.pushState()
    g.setLineWidth(1)
    g.drawPolygon(pts)
    g.popState()


# ---------------------------------------------------------------------------
# WorkPackageFigure
# ---------------------------------------------------------------------------


def work_package_alt(g: G, rect: R, ctx: Ctx) -> None:
    r = figure_position(rect, ctx, 8 / 7.0)
    g.pushState()
    g.setForegroundColor(ctx.line)
    g.setBackgroundColor(ctx.line)
    unit = r.width / 8.0
    g.setLineWidth(jint(unit))
    radius = float(jint(2.5 * unit))
    p = _SwtPath()
    p.addArc(r.x + unit / 2, r.y + unit / 2, radius * 2, radius * 2, -25, 295)
    assert p.current is not None
    cx, cy = p.current
    p.lineTo(cx + 3 * unit, cy)
    g.drawPath(p)
    assert p.current is not None
    cx, cy = p.current
    t = _SwtPath()
    t.moveTo(cx, cy)
    t.lineTo(cx, cy - 1.5 * unit)
    t.lineTo(cx + 2 * unit, cy)
    t.lineTo(cx, cy + 1.5 * unit)
    t.close()
    g.fillPath(t)
    g.popState()


def work_package_icon(g: G, x: float, y: float) -> None:
    cw = 9.0
    ch = cw / 2
    g.pushState()
    g.setLineWidth(1)
    p = _SwtPath()
    p.addArc(x, y, cw, cw, 340, 295)
    p.moveTo(x + ch, y + cw)
    p.lineTo(x + 11, y + cw)
    g.drawPath(p)
    t = _SwtPath()
    t.moveTo(x + 11, y + cw - 3)
    t.lineTo(x + 15, y + cw)
    t.lineTo(x + 11, y + cw + 3)
    t.close()
    g.fillPathWith(t, g.fg)
    g.popState()


# ---------------------------------------------------------------------------
# JunctionFigure — Junction has no alternative figure and no box/icon split.
# The and/or switch comes from ``ctx.junction_type`` ("and" | "or"; default
# "and", as in Archi).
# ---------------------------------------------------------------------------


def junction_box(g: G, rect: R, ctx: Ctx) -> None:
    g.pushState()
    if ctx.junction_type == "or":
        g.setForegroundColor(ctx.line)
        g.setLineWidth(ctx.line_width)
        g.drawPath(_oval_path(rect.x, rect.y, rect.width, rect.height))
    else:
        g.setBackgroundColor(ctx.fill)
        g.fillOval(rect.x, rect.y, rect.width, rect.height)
    g.popState()


def junction_icon(g: G, x: float, y: float) -> None:
    """The palette icon (Archi draws it with the foreground colour)."""
    g.pushState()
    g.setLineWidth(1)
    g.drawRectangle(x + 2, y + 2, 2, 2)
    g.drawRectangle(x + 2, y + 12, 2, 2)
    g.drawRectangle(x + 14, y + 7, 2, 2)
    g.drawLine(x + 4, y + 4, x + 6, y + 6)
    g.drawLine(x + 10, y + 8, x + 14, y + 8)
    g.drawLine(x + 4, y + 12, x + 6, y + 10)
    g.setBackgroundColor(g.fg)
    g.fillOval(x + 5, y + 5, 6, 6)
    g.popState()


# ---------------------------------------------------------------------------
# GroupingFigure. Archi's default line style for Grouping is DASHED (8, 4)
# (GroupingUIProvider) and its default text alignment is LEFT. Type 1 is the
# tabbed figure; its tab width needs the label's text extent:
# ``ctx.text_width`` / ``ctx.text_height`` (default 0).
# ---------------------------------------------------------------------------

_GROUPING_TOPBAR = 18
_GROUPING_INSET = 1.4
_GROUPING_DASH: list[float] = [8.0, 4.0]


def grouping_box(g: G, rect: R, ctx: Ctx) -> None:
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillRectangle(rect)
    g.setForegroundColor(ctx.line)
    g.setLineDash(_GROUPING_DASH)
    g.setLineWidth(ctx.line_width)
    g.drawRectangle(rect)
    g.popState()


def grouping_tab_size(rect: R, ctx: Ctx) -> tuple[int, int]:
    tab_w = jint(rect.width / _GROUPING_INSET)
    tab_h = _GROUPING_TOPBAR
    if ctx.text_position == TEXT_POSITION_TOP:
        text_w = jint(ctx.text_width)
        tab_w = min(max(tab_w, text_w + 8), jint(rect.width))
        tab_h = max(_GROUPING_TOPBAR, jint(ctx.text_height))
    return tab_w, tab_h


def grouping_alt(g: G, rect: R, ctx: Ctx) -> None:
    tab_w, tab_h = grouping_tab_size(rect, ctx)
    main = R(rect.x, rect.y + tab_h, rect.width, rect.height - tab_h)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillRectangle(R(rect.x, rect.y, tab_w, tab_h))
    g.fillRectangle(main)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.setLineDash(_GROUPING_DASH)
    g.drawRectangle(main)
    p = _SwtPath()
    p.moveTo(rect.x, rect.y + tab_h)
    p.lineTo(rect.x, rect.y)
    p.lineTo(rect.x + tab_w - 1, rect.y)
    p.lineTo(rect.x + tab_w - 1, rect.y + tab_h)
    g.drawPath(p)
    g.popState()


def _grouping_text(icon_offset: int):
    def text(rect: R, ctx: Ctx) -> R:
        r = rect.copy()
        if ctx.text_position == TEXT_POSITION_TOP:
            r.y += 5 - _MARGIN_HEIGHT
            r.y -= 3  # max(3, font leading): leading is not modelled
            if icon_offset and ctx.text_alignment == TEXT_ALIGNMENT_RIGHT:
                r.width -= icon_offset - _MARGIN_WIDTH
        return r

    return text


def grouping_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    g.setLineWidth(1)
    g.drawRectangle(x, y, 6, 3)
    g.drawRectangle(x, y + 3, 13, 7)
    g.popState()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _origin(dx: int, dy: int):
    """getIconOrigin: ``(rect.x + rect.width - dx, rect.y + dy)``."""

    def origin(rect: R) -> tuple[float, float]:
        return (rect.x + rect.width - dx, rect.y + dy)

    return origin


def _node_alt_text(rect: R, ctx: Ctx) -> R:
    return box_text_bounds(rect, ctx, 0)


SPECS: dict[str, FigureSpec] = {
    "InteractionFigure": FigureSpec(
        "InteractionFigure",
        box=rounded_rectangle_delegate,
        icon=interaction_icon,
        icon_origin=_origin(12, 5),
        icon_offset=20,
        alt=interaction_alt,
        alt_text_margin_height=0,
    ),
    "InterfaceFigure": FigureSpec(
        "InterfaceFigure",
        box=rectangle_delegate,
        icon=interface_icon,
        icon_origin=_origin(14, 6),
        icon_offset=22,
        alt=interface_alt,
        alt_text_margin_height=0,
    ),
    "LocationFigure": FigureSpec(
        "LocationFigure",
        box=rectangle_delegate,
        icon=location_icon,
        icon_origin=_origin(9, 20),
        icon_offset=16,
        alt=location_alt,
        alt_text_margin_height=0,
    ),
    "MaterialFigure": FigureSpec(
        "MaterialFigure",
        box=rectangle_delegate,
        icon=material_icon,
        icon_origin=_origin(12, 12),
        icon_offset=22,
        alt=material_alt,
        alt_text_margin_height=0,
    ),
    "MeaningFigure": FigureSpec(
        "MeaningFigure",
        box=motivation_box,
        icon=meaning_icon,
        icon_origin=_origin(17, 8),
        icon_offset=19,
        alt=meaning_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "NodeFigure": FigureSpec(
        "NodeFigure",
        box=rectangle_delegate,
        icon=node_icon,
        icon_origin=_origin(18, 8),
        icon_offset=20,
        alt=box_delegate,
        alt_text=_node_alt_text,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ObjectFigure": FigureSpec(
        "ObjectFigure",
        box=rectangle_delegate,
        icon=object_icon,
        icon_origin=_origin(17, 6),
        icon_offset=20,
        alt=object_alt,
        alt_text=object_alt_text,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "OutcomeFigure": FigureSpec(
        "OutcomeFigure",
        box=motivation_box,
        icon=outcome_icon,
        icon_origin=_origin(24, 9),
        icon_offset=27,
        alt=outcome_alt,
        alt_text_margin_height=0,
    ),
    "PathFigure": FigureSpec(
        "PathFigure",
        box=rectangle_delegate,
        icon=path_icon,
        icon_origin=_origin(20, 10),
        icon_offset=22,
        alt=path_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "PlateauFigure": FigureSpec(
        "PlateauFigure",
        box=rectangle_delegate,
        icon=plateau_icon,
        icon_origin=_origin(20, 13),
        icon_offset=17,
        alt=plateau_alt,
        alt_text_margin_height=0,
    ),
    "PrincipleFigure": FigureSpec(
        "PrincipleFigure",
        box=motivation_box,
        icon=principle_icon,
        icon_origin=_origin(20, 6),
        icon_offset=23,
        alt=principle_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ProcessFigure": FigureSpec(
        "ProcessFigure",
        box=rounded_rectangle_delegate,
        icon=process_icon,
        icon_origin=_origin(18, 10),
        icon_offset=20,
        alt=process_delegate,
        alt_text=process_text_bounds,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ProductFigure": FigureSpec(
        "ProductFigure",
        box=rectangle_delegate,
        icon=product_icon,
        icon_origin=_origin(17, 6),
        icon_offset=20,
        alt=product_alt,
        alt_text=_top_margin_text,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "RepresentationFigure": FigureSpec(
        "RepresentationFigure",
        box=rectangle_delegate,
        icon=representation_icon,
        icon_origin=_origin(18, 6),
        icon_offset=21,
        alt=representation_alt,
        alt_text=_top_margin_text,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "RequirementFigure": FigureSpec(
        "RequirementFigure",
        box=motivation_box,
        icon=requirement_icon,
        icon_origin=_origin(19, 8),
        icon_offset=22,
        alt=requirement_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ResourceFigure": FigureSpec(
        "ResourceFigure",
        box=rectangle_delegate,
        icon=resource_icon,
        icon_origin=_origin(20, 7),
        icon_offset=22,
        alt=resource_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ServiceFigure": FigureSpec(
        "ServiceFigure",
        box=rounded_rectangle_delegate,
        icon=service_icon,
        icon_origin=_origin(20, 7),
        icon_offset=22,
        alt=service_delegate,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "StakeholderFigure": FigureSpec(
        "StakeholderFigure",
        box=motivation_box,
        icon=stakeholder_icon,
        icon_origin=_origin(20, 9),
        icon_offset=23,
        alt=cylinder_delegate,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "SystemSoftwareFigure": FigureSpec(
        "SystemSoftwareFigure",
        box=rectangle_delegate,
        icon=system_software_icon,
        icon_origin=_origin(17, 7),
        icon_offset=20,
        alt=system_software_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ValueFigure": FigureSpec(
        "ValueFigure",
        box=motivation_box,
        icon=value_icon,
        icon_origin=_origin(19, 7),
        icon_offset=21,
        alt=value_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ValueStreamFigure": FigureSpec(
        "ValueStreamFigure",
        box=rounded_rectangle_delegate,
        icon=value_stream_icon,
        icon_origin=_origin(19, 7),
        icon_offset=25,
        alt=value_stream_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "WorkPackageFigure": FigureSpec(
        "WorkPackageFigure",
        box=rounded_rectangle_delegate,
        icon=work_package_icon,
        icon_origin=_origin(17, 6),
        icon_offset=19,
        alt=work_package_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "JunctionFigure": FigureSpec(
        "JunctionFigure",
        box=junction_box,
        icon=junction_icon,
        icon_origin=None,
        icon_offset=0,
        alt=None,
        extra={"junction_type": "ctx.junction_type"},
    ),
    "GroupingFigure": FigureSpec(
        "GroupingFigure",
        box=grouping_box,
        icon=grouping_icon,
        icon_origin=_origin(17, 6),
        icon_offset=20,
        alt=grouping_alt,
        box_text=_grouping_text(20),
        alt_text=_grouping_text(0),
        alt_text_margin_height=_MARGIN_HEIGHT,
        extra={
            "default_line_dash": _GROUPING_DASH,
            "default_text_alignment": "left",
            "tab_size": grouping_tab_size,
            "text_extent": "ctx.text_width / ctx.text_height",
        },
    ),
}
