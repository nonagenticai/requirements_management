"""Pure, deterministic SVG renderers for ArchiMate views and UML diagrams.

Nothing here touches the database, the network or the filesystem beyond the
bundled language data. Same snapshot + options in, byte-identical SVG out.
The renderers draw what the snapshot says; they have no authority over
semantics — validity is the validators' job (``tools.architecture.archimate_rules``,
``tools.uml.rules``). Problems that stop something being drawn faithfully come
back as diagnostics (``schemas.modeling.Finding``), never as exceptions.
"""

from tools.rendering.archimate_svg import RENDERER_VERSION as ARCHIMATE_RENDERER_VERSION
from tools.rendering.archimate_svg import render_archimate_view
from tools.rendering.svg import (
    RenderOptions,
    RenderResult,
    RenderTargetNotFoundError,
    UnsafeSvgError,
    sanitize_svg,
)
from tools.rendering.uml_svg import RENDERER_VERSION as UML_RENDERER_VERSION
from tools.rendering.uml_svg import render_uml_diagram

__all__ = [
    "ARCHIMATE_RENDERER_VERSION",
    "UML_RENDERER_VERSION",
    "RenderOptions",
    "RenderResult",
    "RenderTargetNotFoundError",
    "UnsafeSvgError",
    "render_archimate_view",
    "render_uml_diagram",
    "sanitize_svg",
]
