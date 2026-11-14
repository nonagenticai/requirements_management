"""PNG rasterisation of a rendered (already sanitized) SVG via resvg.

resvg is a pure-Rust renderer (``resvg-py`` wheels, MIT). It is given ONLY the
bundled DejaVu Sans files (``skip_system_fonts``), and every generic family is
pointed at that font, so the same SVG yields the same PNG bytes on any host.
No external resources are loaded: the input is re-sanitized first, so it
carries no ``href`` other than in-document marker references.
"""

from __future__ import annotations

import resvg_py

from tools.rendering.fonts import FONT_FAMILY_NAME, FONT_FILES
from tools.rendering.svg import sanitize_svg


def render_png(svg: str, *, zoom: float = 1.0) -> bytes:
    """Rasterise ``svg`` to PNG with the pinned font only."""
    if not 0.1 <= zoom <= 8:
        raise ValueError("zoom must be between 0.1 and 8")
    return bytes(
        resvg_py.svg_to_bytes(
            svg_string=sanitize_svg(svg),
            zoom=zoom,
            skip_system_fonts=True,
            font_files=[str(p) for p in FONT_FILES],
            font_family=FONT_FAMILY_NAME,
            sans_serif_family=FONT_FAMILY_NAME,
            serif_family=FONT_FAMILY_NAME,
            monospace_family=FONT_FAMILY_NAME,
            cursive_family=FONT_FAMILY_NAME,
            fantasy_family=FONT_FAMILY_NAME,
        )
    )
