"""Archi's visual-only diagram objects: Note and Group.

Ported from Archi 5.10.0 ``diagram/NoteFigure.java`` and
``diagram/GroupFigure.java`` (MIT; notice in ``tools/rendering/archi/__init__.py``).
Copyright (c) 2013-2026 Phillip Beauvoir, Jean-Baptiste Sarrodie, The Open Group.
"""

from __future__ import annotations

from tools.rendering.archi.graphics import G, Path, R, jint
from tools.rendering.archi.spec import Ctx, darker

# NoteUIProvider / GroupUIProvider defaults.
NOTE_DEFAULT_SIZE = (185, 80)
NOTE_DEFAULT_FILL = "#ffffff"
GROUP_DEFAULT_SIZE = (400, 140)
GROUP_DEFAULT_FILL = "#d2d7d7"
NOTE_BORDERS = ("dogear", "rectangle", "none")
GROUP_BORDERS = ("tabbed", "rectangle")

GROUP_TOPBAR_HEIGHT = 18
GROUP_INSET = 2.0
NOTE_DOGEAR = 13


def _dog_ear_path(rect: R) -> Path:
    p = Path()
    p.moveTo(rect.x, rect.y)
    p.lineTo(rect.x + rect.width, rect.y)
    p.lineTo(rect.x + rect.width, rect.y + rect.height - NOTE_DOGEAR)
    p.lineTo(rect.x + rect.width - NOTE_DOGEAR, rect.y + rect.height)
    p.lineTo(rect.x, rect.y + rect.height)
    p.close()
    return p


def note_figure(g: G, rect: R, ctx: Ctx, border: str) -> None:
    """NoteFigure.paintFigure."""
    draw_border = border != "none"
    g.pushState()
    if border == "dogear":
        g.setBackgroundColor(ctx.fill)
        g.fillPath(_dog_ear_path(rect))
        if draw_border:
            g.setForegroundColor(ctx.line)
            g.setLineWidth(ctx.line_width)
            g.drawPath(_dog_ear_path(rect))
    else:
        g.setBackgroundColor(ctx.fill)
        g.fillRectangle(rect)
        if draw_border:
            g.setForegroundColor(ctx.line)
            g.setLineWidth(ctx.line_width)
            g.drawRectangle(rect)
    g.popState()


def group_tab_size(rect: R, text_width: float, text_height: float) -> tuple[int, int]:
    """Tab width/height for the tabbed group (text position top)."""
    tab_w = jint(rect.width / GROUP_INSET)
    tab_w = jint(min(max(tab_w, text_width + 8), rect.width))
    tab_h = jint(max(GROUP_TOPBAR_HEIGHT, text_height))
    return tab_w, tab_h


def group_figure(g: G, rect: R, ctx: Ctx, border: str) -> int:
    """GroupFigure.drawFigure; returns the tab height (0 for the rectangle style)."""
    g.pushState()
    if border == "tabbed":
        tab_w, tab_h = group_tab_size(rect, ctx.text_width, ctx.text_height)
        g.setBackgroundColor(darker(ctx.fill))
        g.fillRectangle(rect.x, rect.y, tab_w, tab_h)
        g.setBackgroundColor(ctx.fill)
        g.fillRectangle(rect.x, rect.y + tab_h, rect.width, rect.height - tab_h)
        g.setForegroundColor(ctx.line)
        g.setLineWidth(ctx.line_width)
        g.drawRectangle(rect.x, rect.y + tab_h, rect.width, rect.height - tab_h)
        p = Path()
        p.moveTo(rect.x, rect.y + tab_h)
        p.lineTo(rect.x, rect.y)
        p.lineTo(rect.x + tab_w - 1, rect.y)
        p.lineTo(rect.x + tab_w - 1, rect.y + tab_h)
        g.drawPath(p)
        g.popState()
        return tab_h
    g.setBackgroundColor(ctx.fill)
    g.fillRectangle(rect)
    g.setForegroundColor(ctx.line)
    g.setLineWidth(ctx.line_width)
    g.drawRectangle(rect)
    g.popState()
    return 0
