"""ArchiMate element figures, batch A — ported from Archi 5.10.0.

Line-by-line ports of ``com.archimatetool.editor.diagram.figures.elements``
(tag ``release_5.10.0``): ApplicationComponent, Artifact, Assessment,
BusinessActor, BusinessRole, Capability, Collaboration, CommunicationNetwork,
Constraint, Contract, CourseOfAction, Deliverable, Device, DistributionNetwork,
Driver, Equipment, Event, Facility, Function, Gap and Goal figures (plus the
parts of ``AbstractMotivationFigure``, ``ObjectFigure``, ``FigureUtils`` and
``PolarPoint`` they use). Java ``int`` arithmetic is kept via ``jdiv``/``jint``
and :func:`_jround` (``Math.round``).

Not ported (snapshots carry neither): gradients and custom icon images.

One deliberate deviation: where the Java appends several independent arcs to
one ``Path`` (concentric target rings, network blobs, the event notch after a
stray ``moveTo``), each arc starts its own sub-path (:func:`_sub_arc`), so no
connecting chord is drawn between them — matching how Archi renders them.

Copyright (c) 2013-2026 Phillip Beauvoir, Jean-Baptiste Sarrodie, The Open Group.
MIT licence; full notice in ``tools/rendering/archi/__init__.py``.
"""

from __future__ import annotations

import math

from tools.rendering.archi.graphics import G, Path, R, jdiv, jint
from tools.rendering.archi.spec import (
    TEXT_POSITION_BOTTOM,
    TEXT_POSITION_TOP,
    Ctx,
    FigureSpec,
    cylinder_delegate,
    darker,
    default_text_bounds,
    figure_position,
    parallelogram_delegate,
    rectangle_delegate,
    rounded_rectangle_delegate,
)

# AbstractTextControlContainerFigure.getTextControlMarginHeight() default.
_MARGIN_HEIGHT = 4


# ---------------------------------------------------------------------------
# Java helpers
# ---------------------------------------------------------------------------


def _jround(v: float) -> int:
    """Java ``Math.round`` (half up)."""
    return math.floor(v + 0.5)


def _center(r: R) -> tuple[int, int]:
    """draw2d ``Rectangle.getCenter()`` (integer)."""
    return (jint(r.x + jdiv(r.width, 2)), jint(r.y + jdiv(r.height, 2)))


def _sub_arc(p: Path, x: float, y: float, w: float, h: float, start: float, ext: float) -> None:
    """``path.addArc`` as a separate sub-path (no chord from the previous point)."""
    p.current = None
    p.addArc(x, y, w, h, start, ext)


def _shrunk_for_line(bounds: R, ctx: Ctx) -> R:
    """``rect.shrink(ceil(lineWidth / 2.0))`` used before several alt figures."""
    s = jint(math.ceil(ctx.line_width / 2.0))
    return bounds.copy().shrink(s, s)


def _circle_intersection(
    x1: float, y1: float, x2: float, y2: float, xc: float, yc: float, radius: float
) -> tuple[float, float] | None:
    """getCircleIntersection (Assessment / CourseOfAction)."""
    ba_x, ba_y = x2 - x1, y2 - y1
    ca_x, ca_y = xc - x1, yc - y1
    a = ba_x * ba_x + ba_y * ba_y
    if a == 0:
        return None
    b_by2 = ba_x * ca_x + ba_y * ca_y
    c = ca_x * ca_x + ca_y * ca_y - radius * radius
    p_by2 = b_by2 / a
    q = c / a
    disc = p_by2 * p_by2 - q
    if disc < 0:
        return None
    f1 = -p_by2 + math.sqrt(disc)
    return (x1 - ba_x * f1, y1 - ba_y * f1)


def _icon_setup(g: G, width: float = 1) -> None:
    g.setLineWidth(width)


# ---------------------------------------------------------------------------
# AbstractMotivationFigure (type 0 for motivation elements: cut corners)
# ---------------------------------------------------------------------------

_MOTIVATION_INSET = 10


def _motivation_path(rect: R) -> Path:
    i = _MOTIVATION_INSET
    p = Path()
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
    rect = rect.copy()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(_motivation_path(rect))
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(_motivation_path(rect))
    g.popState()


# ---------------------------------------------------------------------------
# ApplicationComponentFigure
# ---------------------------------------------------------------------------


def _app_component_alt(g: G, rect: R, ctx: Ctx) -> None:
    indent = 10
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillRectangle(rect.x + indent, rect.y, rect.width - indent, rect.height)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    p = Path()
    p.moveTo(rect.x + indent, rect.y + 10)
    p.lineTo(rect.x + indent, rect.y)
    p.lineTo(rect.x + rect.width, rect.y)
    p.lineTo(rect.x + rect.width, rect.y + rect.height)
    p.lineTo(rect.x + indent, rect.y + rect.height)
    p.lineTo(rect.x + indent, rect.y + 43)
    p.moveTo(rect.x + indent, rect.y + 23)
    p.lineTo(rect.x + indent, rect.y + 30)
    g.drawPath(p)
    g.setBackgroundColor(darker(ctx.fill))
    g.fillRectangle(rect.x, rect.y + 10, indent * 2, 13)
    g.fillRectangle(rect.x, rect.y + 30, indent * 2, 13)
    g.setForegroundColor(ctx.line)
    g.drawRectangle(rect.x, rect.y + 10, indent * 2, 13)
    g.drawRectangle(rect.x, rect.y + 30, indent * 2, 13)
    g.popState()


def _app_component_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    p = Path()
    p.moveTo(x, y)
    p.lineTo(x, y - 4)
    p.moveTo(x, y - 6)
    p.lineTo(x, y - 8)
    p.moveTo(x, y - 11)
    p.lineTo(x, y - 13)
    p.lineTo(x + 10, y - 13)
    p.lineTo(x + 10, y)
    p.lineTo(x - 0.5, y)
    p.addRectangle(x - 3, y - 11, 6, 2.5)
    p.addRectangle(x - 3, y - 6, 6, 2.5)
    g.drawPath(p)
    g.popState()


def _app_component_alt_text(rect: R, ctx: Ctx) -> R:
    r = rect.copy()
    r.x += 18
    r.width -= 18
    return r


# ---------------------------------------------------------------------------
# ArtifactFigure
# ---------------------------------------------------------------------------

_FOLD = 18


def _artifact_alt(g: G, rect: R, ctx: Ctx) -> None:
    f = _FOLD
    main = Path()
    main.moveTo(rect.x, rect.y)
    main.lineTo(rect.x + rect.width - f, rect.y)
    main.lineTo(rect.x + rect.width, rect.y + f)
    main.lineTo(rect.x + rect.width, rect.y + rect.height)
    main.lineTo(rect.x, rect.y + rect.height)
    main.close()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(main)
    g.setBackgroundColor(darker(ctx.fill))
    fold = Path()
    fold.moveTo(rect.x + rect.width - f, rect.y)
    fold.lineTo(rect.x + rect.width, rect.y + f)
    fold.lineTo(rect.x + rect.width - f, rect.y + f)
    g.fillPath(fold)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawPath(main)
    fold2 = Path()
    fold2.moveTo(rect.x + rect.width, rect.y + f)
    fold2.lineTo(rect.x + rect.width - f, rect.y + f)
    fold2.lineTo(rect.x + rect.width - f, rect.y)
    g.drawPath(fold2)
    g.popState()


def _artifact_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    p = Path()
    p.moveTo(x, y)
    p.lineTo(x + 7, y)
    p.lineTo(x + 12, y + 5)
    p.lineTo(x + 12, y + 15)
    p.lineTo(x, y + 15)
    p.lineTo(x, y - 0.5)
    p.moveTo(x + 7, y)
    p.lineTo(x + 7, y + 5)
    p.lineTo(x + 12, y + 5)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# AssessmentFigure
# ---------------------------------------------------------------------------


def _assessment_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(bounds, ctx)
    radius = min(_jround(rect.height / 2.5), _jround(rect.width / 2.5))
    fw = jint(radius * 2.5) - ctx.line_width - 1
    fh = jint(radius * 2.5) - ctx.line_width - 1
    cx = rect.x + rect.width - radius - jdiv(rect.width - fw, 2)
    cy = rect.y + radius + jdiv(rect.height - fh, 2)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    p = Path()
    p.addArc(cx - radius, cy - radius, radius * 2, radius * 2, 0, 360)
    g.fillPath(p)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(p)
    x1 = jint(cx - radius * 2.5)
    y1 = jint(cy + radius * 2.5)
    hit = _circle_intersection(x1, y1, cx, cy, cx, cy, radius)
    if hit is not None:
        g.drawLine(_jround(hit[0]), _jround(hit[1] - 1), x1 + radius + 1, y1 - radius - 1)
    g.popState()


def _assessment_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    g.drawOval(x, y, 8, 8)
    g.drawLine(x + 2, y + 7, x - 3, y + 12)
    g.popState()


# ---------------------------------------------------------------------------
# BusinessActorFigure
# ---------------------------------------------------------------------------


def _actor_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(bounds, ctx, 2 / 3.0)
    lw = ctx.line_width
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    d = min(jdiv(rect.width, 2), jdiv(rect.height, 3))
    cx = rect.x + jdiv(rect.width, 2)
    cy = rect.y + jdiv(rect.height + lw, 2)
    p = Path()
    p.addArc(cx - jdiv(d, 2), cy - d - jdiv(d, 2), d, d, 0, 360)
    g.fillPath(p)
    g.setLineWidth(lw)
    g.setForegroundColor(ctx.line)
    g.drawPath(p)
    g.drawLine(cx, cy - jdiv(d, 2), cx, cy - jdiv(d, 2) + d)
    g.drawLine(cx, cy - jdiv(d, 2) + d, cx - d, cy - jdiv(d, 2) + d * 2 - lw)
    g.drawLine(cx, cy - jdiv(d, 2) + d, cx + d, cy - jdiv(d, 2) + d * 2 - lw)
    g.drawLine(cx - d, cy - jdiv(d, 4), cx + d, cy - jdiv(d, 4))
    g.popState()


def _actor_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    g.drawOval(x, y, 6, 6)
    x, y = x + 3, y + 6
    g.drawLine(x, y, x, y + 6)
    y += 6
    g.drawLine(x, y, x - 4, y + 5)
    g.drawLine(x, y, x + 4, y + 5)
    x, y = x - 4, y - 3
    g.drawLine(x, y, x + 8, y)
    g.popState()


# ---------------------------------------------------------------------------
# BusinessRoleFigure
# ---------------------------------------------------------------------------


def _role_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    p = Path()
    p.addArc(x, y, 5, 8, 90, 180)
    p.lineTo(x + 12, y + 8)
    p.moveTo(x + 2, y)
    p.lineTo(x + 12, y)
    g.drawPath(p)
    g.drawOval(x + 10, y, 5, 8)
    g.popState()


# ---------------------------------------------------------------------------
# CapabilityFigure
# ---------------------------------------------------------------------------


def _capability_path(rect: R, xm: float, ym: float, b: float) -> Path:
    p = Path()
    p.moveTo(rect.x + xm, rect.y + ym + 3 * b)
    p.lineTo(rect.x + xm, rect.y + ym + 2 * b)
    p.lineTo(rect.x + xm + 1 * b, rect.y + ym + 2 * b)
    p.lineTo(rect.x + xm + 1 * b, rect.y + ym + 1 * b)
    p.lineTo(rect.x + xm + 2 * b, rect.y + ym + 1 * b)
    p.lineTo(rect.x + xm + 2 * b, rect.y + ym)
    p.lineTo(rect.x + xm + 3 * b, rect.y + ym)
    p.lineTo(rect.x + xm + 3 * b, rect.y + ym + 3 * b)
    p.close()
    return p


def _capability_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(bounds, ctx)
    b = min(rect.height / 3.0, rect.width / 3.0)
    fl = b * 3
    xm = (rect.width - fl) / 2.0
    ym = (rect.height - fl) / 2.0
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(_capability_path(rect, xm, ym, b))
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(_capability_path(rect, xm, ym, b))
    inner = Path()
    inner.moveTo(rect.x + xm + b, rect.y + ym + 3 * b)
    inner.lineTo(rect.x + xm + b, rect.y + ym + 2 * b)
    inner.moveTo(rect.x + xm + b * 2, rect.y + ym + 3 * b)
    inner.lineTo(rect.x + xm + b * 2, rect.y + ym + b)
    inner.moveTo(rect.x + xm + b * 1, rect.y + ym + 2 * b)
    inner.lineTo(rect.x + xm + b * 3, rect.y + ym + 2 * b)
    inner.moveTo(rect.x + xm + b * 2, rect.y + ym + b)
    inner.lineTo(rect.x + xm + b * 3, rect.y + ym + b)
    g.drawPath(inner)
    g.popState()


def _capability_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    for dx, dy in ((8, 0), (4, 4), (8, 4), (0, 8), (4, 8), (8, 8)):
        g.drawRectangle(x + dx, y + dy, 4, 4)
    g.popState()


# ---------------------------------------------------------------------------
# CollaborationFigure
# ---------------------------------------------------------------------------


def _oval_path(x: float, y: float, w: float, h: float) -> Path:
    """FigureUtils.createOvalPath."""
    p = Path()
    p.addArc(x, y, w, h, 0, 360)
    return p


def _collaboration_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(_shrunk_for_line(bounds, ctx), ctx, 1.5)
    x1: float = rect.x
    if rect.width <= rect.height:
        d = (rect.width / 3) * 2
    else:
        d = min(rect.height, (rect.width / 3) * 2)
        x1 += (rect.width / 2) - (d * 0.75)
    x2 = x1 + d / 2
    y = rect.y + (rect.height - d) / 2
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(_oval_path(x1, y, d, d))
    g.fillPath(_oval_path(x2, y, d, d))
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(_oval_path(x1, y, d, d))
    g.drawPath(_oval_path(x2, y, d, d))
    g.popState()


def _collaboration_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    g.drawOval(x + 4, y, 10, 10)
    g.drawOval(x, y, 10, 10)
    g.popState()


# ---------------------------------------------------------------------------
# CommunicationNetworkFigure
# ---------------------------------------------------------------------------


def _network_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(bounds, ctx)
    g.pushState()
    g.setForegroundColor(ctx.line)
    g.setBackgroundColor(ctx.line)
    fmax = min(rect.width, rect.height)
    blob_d = float(jint(max(10, math.sqrt(rect.width * rect.height) / 7.0)))
    blob_r = blob_d / 2
    g.setLineWidth(jdiv(jint(blob_d), 4))
    h_off = float(jdiv(fmax, 4))
    w_off = float(jdiv(fmax, 3))
    cx, cy = _center(rect)
    x = cx - w_off
    y = cy - h_off
    w = w_off * 2
    h = h_off * 2
    indent = w / 5
    p = Path()
    _sub_arc(p, x, y + h - blob_d, blob_d, blob_d, 0, 360)
    _sub_arc(p, x + w - indent - blob_d, y + h - blob_d, blob_d, blob_d, 0, 360)
    _sub_arc(p, x + indent, y, blob_d, blob_d, 0, 360)
    _sub_arc(p, x + w - blob_d, y, blob_d, blob_d, 0, 360)
    g.fillPath(p)
    p = Path()
    x1 = x + blob_r
    y1 = y + h - blob_r
    x2 = x1 + w - indent - blob_d
    p.moveTo(x1, y1)
    p.lineTo(x2, y1)
    p.lineTo(x + w - blob_r, y + blob_r)
    p.lineTo(x + indent + blob_r, y + blob_r)
    p.lineTo(x1, y1)
    g.drawPath(p)
    g.popState()


def _network_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    p = Path()
    _sub_arc(p, x, y, 5, 5, 0, 360)
    _sub_arc(p, x + 2, y - 8, 5, 5, 0, 360)
    _sub_arc(p, x + 10, y - 8, 5, 5, 0, 360)
    _sub_arc(p, x + 8, y, 5, 5, 0, 360)
    p.moveTo(x + 3, y)
    p.lineTo(x + 4, y - 3)
    p.moveTo(x + 11, y)
    p.lineTo(x + 12, y - 3)
    p.moveTo(x + 5, y + 2.5)
    p.lineTo(x + 8, y + 2.5)
    p.moveTo(x + 7, y - 5.5)
    p.lineTo(x + 10, y - 5.5)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# ConstraintFigure
# ---------------------------------------------------------------------------


def _constraint_alt(g: G, rect: R, ctx: Ctx) -> None:
    parallelogram_delegate(g, rect, ctx, with_slash=True)


def _constraint_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    g.drawPolygon([x, y, x + 12, y, x + 8, y + 9, x - 4, y + 9])
    g.drawLine(x + 4, y, x, y + 9)
    g.popState()


# ---------------------------------------------------------------------------
# ObjectFigure / ContractFigure
# ---------------------------------------------------------------------------

_TOP_MARGIN = 12


def _object_delegate(g: G, rect: R, ctx: Ctx) -> None:
    """ObjectFigure.ObjectFigureDelegate."""
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillRectangle(rect)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawRectangle(rect)
    g.drawLine(rect.x, rect.y + _TOP_MARGIN, rect.x + rect.width, rect.y + _TOP_MARGIN)
    g.popState()


def _contract_alt(g: G, rect: R, ctx: Ctx) -> None:
    _object_delegate(g, rect, ctx)
    g.pushState()
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    bottom = rect.y + rect.height
    g.drawLine(rect.x, bottom - _TOP_MARGIN, rect.x + rect.width, bottom - _TOP_MARGIN)
    g.popState()


def _contract_alt_text(rect: R, ctx: Ctx) -> R:
    r = rect.copy()
    if ctx.text_position == TEXT_POSITION_BOTTOM:
        r.y -= _TOP_MARGIN - _MARGIN_HEIGHT
    elif ctx.text_position == TEXT_POSITION_TOP:
        r.y += _TOP_MARGIN - _MARGIN_HEIGHT
    return r


def _contract_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    g.drawRectangle(x, y, 13, 10)
    g.drawLine(x, y + 3, x + 13, y + 3)
    g.drawLine(x, y + 7, x + 13, y + 7)
    g.popState()


# ---------------------------------------------------------------------------
# CourseOfActionFigure
# ---------------------------------------------------------------------------


def _coa_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(_shrunk_for_line(bounds, ctx), ctx, 1.24)
    radius = min(_jround(rect.height / 2.5), _jround(rect.width / 3.1))
    fw = jint(radius * 3.1)
    fh = jint(radius * 2.5)
    cx = rect.x + rect.width - radius - jdiv(rect.width - fw, 2)
    cy = rect.y + radius + jdiv(rect.height - fh, 2)
    lw = ctx.line_width
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    p = Path()
    p.addArc(cx - radius, cy - radius, radius * 2, radius * 2, 0, 360)
    g.fillPath(p)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(lw)
    g.drawPath(p)
    g.setBackgroundColor(ctx.line)
    r2 = _jround(radius * 2 / 3 - jdiv(lw, 2))
    g.drawOval(cx - r2, cy - r2, 2 * r2, 2 * r2)
    r3 = _jround(radius / 3.0 - jdiv(lw, 2))
    g.fillOval(cx - r3, cy - r3, 2 * r3, 2 * r3)
    x = _jround(cx - radius * 2)
    y = _jround(cy + radius * 2)
    hit = _circle_intersection(x, y, cx - r3, cy - r3, cx - r3, cy - r3, radius)
    if hit is not None:
        # PrecisionPoint.x/y: floor of the precise value.
        ix, iy = math.floor(hit[0] + 1e-9), math.floor(hit[1] + 1e-9)
        al = jint(r3 * 1.5)
        alw = max(3, jdiv(al, 5))
        g.setLineWidth(alw)
        g.fillPolygon([ix, iy, ix - al, iy - jdiv(al, 3), ix - jdiv(al, 3), iy + al])
        lp = Path()
        lp.moveTo(x, rect.y + rect.height - 1)
        lp.lineTo(x + jdiv(ix - x, 3), iy + jdiv(y - iy, 3))
        lp.lineTo(ix - alw, iy + alw)
        g.drawPath(lp)
    g.popState()


def _coa_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    g.setBackgroundColor(g.fg)
    p = Path()
    ax, ay = x - 5.4, y + 9.0
    p.moveTo(ax, ay)
    p.lineTo(ax + 6, ay + 1)
    p.lineTo(ax + 3, ay + 6.2)
    g.fillPath(p)
    g.setBackgroundColor(None)
    p = Path()
    g.setLineWidth(2)
    p.addArc(x - 7.5, y + 12, 10, 10, 90, 80)
    g.drawPath(p)
    g.setLineWidth(1.2)
    p = Path()
    _sub_arc(p, x, y, 13, 13, 0, 360)
    _sub_arc(p, x + 2.5, y + 2.5, 8, 8, 0, 360)
    _sub_arc(p, x + 5, y + 5, 3, 3, 0, 360)
    _sub_arc(p, x + 6, y + 6, 1, 1, 0, 360)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# DeliverableFigure
# ---------------------------------------------------------------------------


def _deliverable_path(curve_h: float, rect: R, inset: float) -> Path:
    bottom = rect.y + rect.height
    right = rect.x + rect.width
    curve_y = bottom - curve_h
    p = Path()
    p.moveTo(rect.x + inset, rect.y + inset)
    p.lineTo(rect.x + inset, curve_y - 1)
    p.quadTo(
        rect.x + jdiv(rect.width, 4),
        bottom + curve_h - inset,
        rect.x + jdiv(rect.width, 2) + 1,
        curve_y,
    )
    p.quadTo(right - jdiv(rect.width, 4), curve_y - curve_h - 1, right - inset, curve_y)
    p.lineTo(rect.x + rect.width - inset, rect.y + inset)
    p.close()
    return p


def _deliverable_alt(g: G, rect: R, ctx: Ctx) -> None:
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    p = _deliverable_path(8, rect, 0)
    g.fillPath(p)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawPath(p)
    g.popState()


def _deliverable_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    g.drawPath(_deliverable_path(1.5, R(x, y, 14, 10), 0.5))
    g.popState()


# ---------------------------------------------------------------------------
# DeviceFigure
# ---------------------------------------------------------------------------


def _device_alt(g: G, rect: R, ctx: Ctx) -> None:
    indent = 15
    lw = ctx.line_width
    hi = jdiv(rect.height, 5)
    top = R(rect.x, rect.y, rect.width, rect.height - hi + 1)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillRoundRectangle(top, 30, 30)
    g.setBackgroundColor(darker(ctx.fill))
    p = Path()
    p.moveTo(rect.x + lw, rect.y + rect.height)
    p.lineTo(rect.x + indent + 1, rect.y + rect.height - hi + 1)
    p.lineTo(rect.x + rect.width - indent, rect.y + rect.height - hi + 1)
    p.lineTo(rect.x + rect.width - lw, rect.y + rect.height)
    p.close()
    g.fillPath(p)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(lw)
    g.drawRoundRectangle(top, 30, 30)
    g.drawPath(p)
    g.popState()


def _device_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    g.drawRoundRectangle(R(x, y, 11, 8), 3, 3)
    g.drawPolygon([x - 1, y + 12, x + 2, y + 8, x + 9, y + 8, x + 12, y + 12])
    g.popState()


# ---------------------------------------------------------------------------
# DistributionNetworkFigure
# ---------------------------------------------------------------------------

_ARROW_ANGLE = math.cos(math.radians(60))


def _arrow_size(rect: R) -> tuple[int, int]:
    width = jint(rect.width / (1 + _ARROW_ANGLE) / 2)
    size = jint(min(rect.height, width))
    return (jint(size * _ARROW_ANGLE), size)


def _dist_lines(rect: R, arrow_h: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
    a5 = jdiv(arrow_h, 5)
    mid = rect.y + jdiv(rect.height, 2)
    line1 = (rect.x + a5, mid - a5, rect.x + rect.width - a5, mid - a5)
    line2 = (rect.x + a5, mid + a5, rect.x + rect.width - a5, mid + a5)
    return line1, line2


def _distribution_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(bounds, ctx, 5 / 3.0)
    lw = jint(max(3, math.sqrt(bounds.width * bounds.height) / 24))
    rect.shrink(lw, lw)
    aw, ah = _arrow_size(rect)
    l1, l2 = _dist_lines(rect, ah)
    mid = rect.y + jdiv(rect.height, 2)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    p = Path()
    p.moveTo(l1[0], l1[1])
    p.lineTo(l1[2], l1[3])
    p.lineTo(rect.x + rect.width, mid)
    p.lineTo(l1[2], l2[1])
    p.lineTo(l2[0], l2[1])
    p.lineTo(rect.x, mid)
    p.close()
    g.fillPath(p)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(lw)
    a = Path()
    a.moveTo(rect.x + aw, mid - jdiv(ah, 2))
    a.lineTo(rect.x, mid)
    a.lineTo(rect.x + aw, mid + jdiv(ah, 2))
    a.moveTo(rect.x + rect.width - aw, mid - jdiv(ah, 2))
    a.lineTo(rect.x + rect.width, mid)
    a.lineTo(rect.x + rect.width - aw, mid + jdiv(ah, 2))
    g.drawPath(a)
    g.drawLine(*l1)
    g.drawLine(*l2)
    g.popState()


def _distribution_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g, 1.2)
    p = Path()
    p.moveTo(x + 1, y - 2)
    p.lineTo(x + 14, y - 2)
    p.moveTo(x + 1, y + 2)
    p.lineTo(x + 14, y + 2)
    p.moveTo(x + 4, y - 5)
    p.lineTo(x - 1, y)
    p.lineTo(x + 4, y + 5)
    p.moveTo(x + 11, y - 5)
    p.lineTo(x + 16, y)
    p.lineTo(x + 11, y + 5)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# DriverFigure
# ---------------------------------------------------------------------------


def _even_radius(rect: R) -> int:
    """Driver/Goal getRadius: half the smaller side, rounded down to even."""
    radius = min(jdiv(rect.height, 2), jdiv(rect.width, 2))
    return radius - radius % 2


def _driver_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(bounds, ctx)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    lw = jint(math.sqrt(bounds.width * bounds.height) / 20)
    g.setLineWidth(max(lw, 1))  # SWT line width 0 = 1px hairline
    radius = _even_radius(rect)
    actual = radius - _jround(radius / 10.0) - jdiv(lw, 2)
    cx, cy = _center(rect)
    p = Path()
    p.addArc(cx - actual, cy - actual, actual * 2, actual * 2, 0, 360)
    g.fillPath(p)
    g.setForegroundColor(ctx.line)
    g.drawPath(p)
    g.setLineWidth(1)

    def at(deg: float) -> tuple[int, int]:
        rad = math.radians(deg)
        return (_jround(cx + radius * math.cos(rad)), _jround(cy + radius * math.sin(rad)))

    tl, br, bl, tr = at(225), at(45), at(135), at(315)
    g.drawLine(tl[0], tl[1], br[0], br[1])
    g.drawLine(cx - radius, cy, cx + radius, cy)
    g.drawLine(bl[0], bl[1], tr[0], tr[1])
    g.drawLine(cx, cy - radius, cx, cy + radius)
    g.setBackgroundColor(ctx.line)
    radius = _jround(radius / 4.0)
    g.fillOval(cx - radius, cy - radius, 2 * radius, 2 * radius)
    g.popState()


def _driver_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g, 1.2)
    p = Path()
    _sub_arc(p, x, y, 13, 13, 0, 360)
    _sub_arc(p, x + 5, y + 5, 3, 3, 0, 360)
    _sub_arc(p, x + 6, y + 6, 1, 1, 0, 360)
    g.drawPath(p)
    g.setLineWidth(1)
    p = Path()
    p.moveTo(x - 2, y + 6.5)
    p.lineTo(x + 15, y + 6.5)
    p.moveTo(x + 6.5, y - 2)
    p.lineTo(x + 6.5, y + 15)
    p.moveTo(x + 0.5, y + 0.5)
    p.lineTo(x + 12.5, y + 12.5)
    p.moveTo(x + 0.5, y + 12.5)
    p.lineTo(x + 12.5, y + 0.5)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# EquipmentFigure
# ---------------------------------------------------------------------------


def _rotate(pt: tuple[float, float], c: tuple[float, float], deg: float) -> tuple[float, float]:
    rad = deg * math.pi / 180
    xm, ym = pt[0] - c[0], pt[1] - c[1]
    return (
        xm * math.cos(rad) - ym * math.sin(rad) + c[0],
        xm * math.sin(rad) + ym * math.cos(rad) + c[1],
    )


def _gear_path(rect: R) -> Path:
    if rect.width > rect.height:
        fw = fh = rect.height
    else:
        fw = fh = rect.width
    center = (float(rect.x + jdiv(fw, 2)), float(rect.y + jdiv(fh, 2)))
    a1, b1, a2, b2 = 14, 31, 22, 23
    xm = jdiv(rect.width - fw, 2)
    ym = jdiv(rect.height - fh, 2)
    first = (float(rect.x + jdiv(fw, 2)), float(rect.y))
    second = (rect.x + jdiv(fw, 2) - fw / 5.9, rect.y + fh / 6.5)
    c1 = _rotate(first, center, a1)
    c2 = _rotate(second, center, a2)
    c2 = _rotate(c2, center, b2)
    p = Path()
    p.moveTo(first[0] + xm, first[1] + ym)
    p.lineTo(c1[0] + xm, c1[1] + ym)
    p.lineTo(c2[0] + xm, c2[1] + ym)
    c2 = _rotate(c2, center, a2)
    p.lineTo(c2[0] + xm, c2[1] + ym)
    for _ in range(7):
        c1 = _rotate(c1, center, b1)
        p.lineTo(c1[0] + xm, c1[1] + ym)
        c1 = _rotate(c1, center, a1)
        p.lineTo(c1[0] + xm, c1[1] + ym)
        c2 = _rotate(c2, center, b2)
        p.lineTo(c2[0] + xm, c2[1] + ym)
        c2 = _rotate(c2, center, a2)
        p.lineTo(c2[0] + xm, c2[1] + ym)
    p.close()
    return p


def _gear_hub(g: G, r: R) -> None:
    cx, cy = _center(r)
    g.drawOval(
        cx - jdiv(r.width, 2) + jdiv(r.width, 3),
        cy - jdiv(r.height, 2) + jdiv(r.width, 3),
        jdiv(r.width, 3),
        jdiv(r.width, 3),
    )


def _equipment_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(bounds.copy().shrink(2, 2), ctx)
    xc = rect.x + jdiv(rect.width, 2)
    yc = rect.y + jdiv(rect.height, 2)
    w1 = jdiv(min(rect.width, rect.height) * 2, 3)
    r1 = R(xc - jdiv(w1 * 2, 3), (yc - jdiv(w1, 2)) + jdiv(w1, 4), w1, w1)
    w2 = jdiv(min(rect.width, rect.height) * 1, 2)
    r2 = R(xc, jint(yc - w2 * 0.96), w2, w2)
    p1, p2 = _gear_path(r1), _gear_path(r2)
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p1)
    g.fillPath(p2)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(p1)
    g.drawPath(p2)
    _gear_hub(g, r1)
    _gear_hub(g, r2)
    g.popState()


def _polar(r: float, theta: float, pole: tuple[float, float]) -> tuple[int, int]:
    """PolarPoint.set(r, theta).toAbsolutePoint(pole)."""
    return (
        _jround(r * math.cos(theta)) + jint(pole[0]),
        _jround(-r * math.sin(theta)) + jint(pole[1]),
    )


def _icon_cog(g: G, c: tuple[float, float], segments: int, r1: int, r2: int, r3: int) -> None:
    half = math.pi / (2 * segments)
    delta = half / 4
    outer: list[tuple[int, int]] = []
    for i in range(segments):
        base = 2 * math.pi * i / segments
        outer.append(_polar(r2, base - half, c))
        outer.append(_polar(r3, base - half + delta, c))
        outer.append(_polar(r3, base + half - delta, c))
        outer.append(_polar(r2, base + half, c))
    g.drawPolygon(outer)
    p = Path()
    p.addArc(c[0] - r1, c[1] - r1, 2 * r1, 2 * r1, 0, 360)
    g.drawPath(p)


def _equipment_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    _icon_cog(g, (x + 5, y + 3), 8, 3, 6, 8)
    _icon_cog(g, (x + 10, y - 8), 6, 2, 4, 5)
    g.popState()


# ---------------------------------------------------------------------------
# EventFigure
# ---------------------------------------------------------------------------


def _event_alt(g: G, rect: R, ctx: Ctx) -> None:
    indent = min(jdiv(rect.height, 3), jdiv(rect.width, 3))
    cy = rect.y + jdiv(rect.height, 2) - 1
    arc_x = rect.x + rect.width - indent
    lo = ctx.line_width / 2.0
    p = Path()
    p.moveTo(rect.x + lo, rect.y)
    p.lineTo(rect.x + indent, cy)
    p.lineTo(rect.x + lo, rect.y + rect.height)
    p.lineTo(arc_x, rect.y + rect.height)
    p.addArc(arc_x - indent, rect.y, indent * 2, rect.height, -90, 180)
    p.close()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawPath(p)
    g.popState()


def _event_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    p = Path()
    # Java: moveTo(x, y) then addArc — the notch, without a chord.
    _sub_arc(p, x - 4, y, 8, 9, 270, 180)
    g.drawPath(p)
    p = Path()
    p.addArc(x + 8, y, 8, 9, 270, 180)
    p.moveTo(x, y)
    p.lineTo(x + 12, y)
    p.moveTo(x, y + 9)
    p.lineTo(x + 12, y + 9)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# FacilityFigure
# ---------------------------------------------------------------------------


def _facility_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(bounds, ctx)
    if rect.width <= rect.height:
        fw = fh = rect.width
    else:
        fw = fh = rect.height
    xm = jdiv(rect.width - fw, 2)
    ym = jdiv(rect.height - fh, 2)
    xt = jdiv(fw, 4) + jdiv(fw, 20)
    yt = jdiv(fh, 5)
    half = fh / 2.0
    x0, y0 = rect.x + xm, rect.y + ym
    p = Path()
    p.moveTo(x0, y0)
    p.lineTo(x0, y0 + fh)
    p.lineTo(x0 + fw, y0 + fh)
    p.lineTo(x0 + fw, y0 + half)
    p.lineTo(x0 + fw - xt, y0 + half + yt)
    p.lineTo(x0 + fw - xt, y0 + half)
    p.lineTo(x0 + fw - 2 * xt, y0 + half + yt)
    p.lineTo(x0 + fw - 2 * xt, y0 + half)
    p.lineTo(x0 + fw - 3 * xt, y0 + half + yt)
    p.lineTo(x0 + fw - 3 * xt, y0)
    p.close()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(p)
    g.popState()


def _facility_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g, 1.2)
    g.drawPolygon(
        [
            x, y,
            x + 15, y,
            x + 15, y - 6,
            x + 11, y - 3,
            x + 11, y - 6,
            x + 7, y - 3,
            x + 7, y - 6,
            x + 3, y - 3,
            x + 3, y - 12,
            x, y - 12,
        ]
    )  # fmt: skip
    g.popState()


# ---------------------------------------------------------------------------
# FunctionFigure
# ---------------------------------------------------------------------------


def _function_alt(g: G, rect: R, ctx: Ctx) -> None:
    off = 5
    y1 = rect.y + jdiv(rect.height, off)
    y2 = rect.y + rect.height - jdiv(rect.height, off)
    p = Path()
    p.moveTo(rect.x, rect.y + rect.height)
    p.lineTo(rect.x, y1)
    p.lineTo(rect.x + jdiv(rect.width, 2), rect.y)
    p.lineTo(rect.x + rect.width, y1)
    p.lineTo(rect.x + rect.width, rect.y + rect.height)
    p.lineTo(rect.x + jdiv(rect.width, 2), y2)
    p.close()
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(p)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawPath(p)
    g.popState()


def _function_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    pts = [(x, y)]
    for dx, dy in ((0, -9), (6, -5), (6, 5), (0, 9), (-6, -6)):
        x, y = x + dx, y + dy
        pts.append((x, y))
    g.drawPolygon(pts)
    g.popState()


# ---------------------------------------------------------------------------
# GapFigure
# ---------------------------------------------------------------------------


def _gap_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(_shrunk_for_line(bounds, ctx), ctx, 5 / 3.0)
    wf = 3 * (rect.width / 10)
    cr = wf
    if rect.height < rect.width:
        cr = min(rect.height / 2, wf)
    xc = rect.x + rect.width / 2
    yc = rect.y + rect.height / 2
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(_oval_path(xc - cr, yc - cr, cr * 2, cr * 2))
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawPath(_oval_path(xc - cr, yc - cr, cr * 2, cr * 2))
    x1 = jint(xc - (cr + cr / 2))
    x2 = jint(xc + (cr + cr / 2))
    y1 = jint(yc - cr / 4)
    y2 = jint(yc + cr / 4)
    g.drawLine(x1, y1, x2, y1)
    g.drawLine(x1, y2, x2, y2)
    g.popState()


def _gap_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g)
    g.drawOval(x, y, 13, 13)
    x, y = x - 2, y + 5
    g.drawLine(x, y, x + 17, y)
    y += 3
    g.drawLine(x, y, x + 17, y)
    g.popState()


# ---------------------------------------------------------------------------
# GoalFigure
# ---------------------------------------------------------------------------


def _goal_alt(g: G, bounds: R, ctx: Ctx) -> None:
    rect = figure_position(_shrunk_for_line(bounds, ctx), ctx)
    radius = _even_radius(rect)
    cx, cy = _center(rect)
    x, y = cx - radius, cy - radius
    w = h = radius * 2
    lo = ctx.line_width / 2.0
    g.pushState()
    g.setBackgroundColor(ctx.fill)
    g.fillPath(_oval_path(x, y, w, h))
    g.setLineWidth(ctx.line_width)
    g.setForegroundColor(ctx.line)
    g.drawPath(_oval_path(x, y, w, h))
    g.setBackgroundColor(ctx.line)
    radius = _jround(radius * 2.0 / 3.0 - lo)
    g.drawOval(cx - radius, cy - radius, 2 * radius, 2 * radius)
    radius = _jround(radius / 3.0 - lo)
    g.fillOval(cx - radius, cy - radius, 2 * radius, 2 * radius)
    g.popState()


def _goal_icon(g: G, x: float, y: float) -> None:
    g.pushState()
    _icon_setup(g, 1.2)
    p = Path()
    _sub_arc(p, x, y, 13, 13, 0, 360)
    _sub_arc(p, x + 2.5, y + 2.5, 8, 8, 0, 360)
    _sub_arc(p, x + 5, y + 5, 3, 3, 0, 360)
    _sub_arc(p, x + 6, y + 6, 1, 1, 0, 360)
    g.drawPath(p)
    g.popState()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _origin(dx: int, dy: int):
    """getIconOrigin: ``(x + width - dx, y + dy)``."""
    return lambda r: (r.x + r.width - dx, r.y + dy)


def _alt_offset_text(offset: int):
    """Classes whose getIconOffset() is non-zero for the alt figure too."""
    return lambda r, ctx: default_text_bounds(r, ctx, offset)


SPECS: dict[str, FigureSpec] = {
    "ApplicationComponentFigure": FigureSpec(
        "ApplicationComponentFigure",
        box=rectangle_delegate,
        icon=_app_component_icon,
        icon_origin=_origin(14, 19),
        icon_offset=20,
        alt=_app_component_alt,
        alt_text=_app_component_alt_text,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ArtifactFigure": FigureSpec(
        "ArtifactFigure",
        box=rectangle_delegate,
        icon=_artifact_icon,
        icon_origin=_origin(16, 6),
        icon_offset=22,
        alt=_artifact_alt,
        alt_text=_alt_offset_text(22),
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "AssessmentFigure": FigureSpec(
        "AssessmentFigure",
        box=motivation_box,
        icon=_assessment_icon,
        icon_origin=_origin(15, 6),
        icon_offset=21,
        alt=_assessment_alt,
    ),
    "BusinessActorFigure": FigureSpec(
        "BusinessActorFigure",
        box=rectangle_delegate,
        icon=_actor_icon,
        icon_origin=_origin(11, 4),
        icon_offset=17,
        alt=_actor_alt,
    ),
    "BusinessRoleFigure": FigureSpec(
        "BusinessRoleFigure",
        box=rectangle_delegate,
        icon=_role_icon,
        icon_origin=_origin(18, 7),
        icon_offset=20,
        alt=cylinder_delegate,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "CapabilityFigure": FigureSpec(
        "CapabilityFigure",
        box=rounded_rectangle_delegate,
        icon=_capability_icon,
        icon_origin=_origin(17, 5),
        icon_offset=19,
        alt=_capability_alt,
    ),
    "CollaborationFigure": FigureSpec(
        "CollaborationFigure",
        box=rectangle_delegate,
        icon=_collaboration_icon,
        icon_origin=_origin(18, 7),
        icon_offset=20,
        alt=_collaboration_alt,
        alt_text=_alt_offset_text(20),
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "CommunicationNetworkFigure": FigureSpec(
        "CommunicationNetworkFigure",
        box=rectangle_delegate,
        icon=_network_icon,
        icon_origin=_origin(18, 14),
        icon_offset=22,
        alt=_network_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ConstraintFigure": FigureSpec(
        "ConstraintFigure",
        box=motivation_box,
        icon=_constraint_icon,
        icon_origin=_origin(19, 8),
        icon_offset=23,
        alt=_constraint_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "ContractFigure": FigureSpec(
        "ContractFigure",
        box=rectangle_delegate,
        icon=_contract_icon,
        icon_origin=_origin(17, 6),
        icon_offset=20,
        alt=_contract_alt,
        alt_text=_contract_alt_text,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "CourseOfActionFigure": FigureSpec(
        "CourseOfActionFigure",
        box=rounded_rectangle_delegate,
        icon=_coa_icon,
        icon_origin=_origin(17, 4),
        icon_offset=25,
        alt=_coa_alt,
    ),
    "DeliverableFigure": FigureSpec(
        "DeliverableFigure",
        box=rectangle_delegate,
        icon=_deliverable_icon,
        icon_origin=_origin(18, 6),
        icon_offset=21,
        alt=_deliverable_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "DeviceFigure": FigureSpec(
        "DeviceFigure",
        box=rectangle_delegate,
        icon=_device_icon,
        icon_origin=_origin(16, 5),
        icon_offset=20,
        alt=_device_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "DistributionNetworkFigure": FigureSpec(
        "DistributionNetworkFigure",
        box=rectangle_delegate,
        icon=_distribution_icon,
        icon_origin=_origin(20, 12),
        icon_offset=22,
        alt=_distribution_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "DriverFigure": FigureSpec(
        "DriverFigure",
        box=motivation_box,
        icon=_driver_icon,
        icon_origin=_origin(20, 6),
        icon_offset=23,
        alt=_driver_alt,
    ),
    "EquipmentFigure": FigureSpec(
        "EquipmentFigure",
        box=rectangle_delegate,
        icon=_equipment_icon,
        icon_origin=_origin(19, 17),
        icon_offset=22,
        alt=_equipment_alt,
    ),
    "EventFigure": FigureSpec(
        "EventFigure",
        box=rounded_rectangle_delegate,
        icon=_event_icon,
        icon_origin=_origin(20, 6),
        icon_offset=22,
        alt=_event_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "FacilityFigure": FigureSpec(
        "FacilityFigure",
        box=rectangle_delegate,
        icon=_facility_icon,
        icon_origin=_origin(19, 17),
        icon_offset=20,
        alt=_facility_alt,
    ),
    "FunctionFigure": FigureSpec(
        "FunctionFigure",
        box=rounded_rectangle_delegate,
        icon=_function_icon,
        icon_origin=_origin(16, 19),
        icon_offset=20,
        alt=_function_alt,
        alt_text_margin_height=_MARGIN_HEIGHT,
    ),
    "GapFigure": FigureSpec(
        "GapFigure",
        box=rectangle_delegate,
        icon=_gap_icon,
        icon_origin=_origin(18, 6),
        icon_offset=23,
        alt=_gap_alt,
    ),
    "GoalFigure": FigureSpec(
        "GoalFigure",
        box=motivation_box,
        icon=_goal_icon,
        icon_origin=_origin(19, 6),
        icon_offset=22,
        alt=_goal_alt,
    ),
}
