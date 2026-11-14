"""The one font every rendered diagram uses: DejaVu Sans 2.37, bundled.

Bundled under ``src/data/fonts/dejavu-2.37/`` from the upstream release
``https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip``
(zip sha256 ``7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a``)
with its ``LICENSE`` (Bitstream Vera / Arev permissive font licence; DejaVu
changes are public domain). File sha256s:

* ``DejaVuSans.ttf``             ``7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954``
* ``DejaVuSans-Bold.ttf``        ``e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724``
* ``DejaVuSans-Oblique.ttf``     ``4af75fa16ee6d3ad43e1ecec41862c24954af26a55c6bb1ebb27bd486a50f5f4``
* ``DejaVuSans-BoldOblique.ttf`` ``eb436dca0c2594b73d8b603b892e374fdfd8d885d25ffb4f18df4c4c0b49e50f``

The SVG names this family, text widths for wrapping are measured from the
font's own advance widths (``hmtx``), and the PNG rasteriser loads ONLY these
files — so layout and PNG bytes do not depend on the host's fonts.
"""

from __future__ import annotations

import struct
from functools import cache
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[2] / "data" / "fonts" / "dejavu-2.37"
FONT_FAMILY_NAME = "DejaVu Sans"
# The CSS value emitted on every document (and the only non-generic family the
# sanitizer accepts).
FONT_FAMILY = "DejaVu Sans, sans-serif"
FONT_FILES = (
    FONT_DIR / "DejaVuSans.ttf",
    FONT_DIR / "DejaVuSans-Bold.ttf",
    FONT_DIR / "DejaVuSans-Oblique.ttf",
    FONT_DIR / "DejaVuSans-BoldOblique.ttf",
)


class _Metrics:
    """Advance widths (in em) from a TrueType font's cmap (format 4) + hmtx."""

    def __init__(self, path: Path) -> None:
        data = path.read_bytes()
        num_tables = struct.unpack_from(">H", data, 4)[0]
        tables: dict[bytes, int] = {}
        for i in range(num_tables):
            tag, _cs, off, _len = struct.unpack_from(">4sIII", data, 12 + 16 * i)
            tables[tag] = off
        units = struct.unpack_from(">H", data, tables[b"head"] + 18)[0]
        n_hmetrics = struct.unpack_from(">H", data, tables[b"hhea"] + 34)[0]
        hmtx = tables[b"hmtx"]
        advances = [struct.unpack_from(">H", data, hmtx + 4 * i)[0] for i in range(n_hmetrics)]
        self.units = units
        self.advances = advances
        self.cmap = self._cmap4(data, tables[b"cmap"])
        self.fallback = advances[self.cmap.get(ord("?"), 0)] / units

    @staticmethod
    def _cmap4(data: bytes, cmap: int) -> dict[int, int]:
        n = struct.unpack_from(">H", data, cmap + 2)[0]
        sub = None
        for i in range(n):
            pid, eid, off = struct.unpack_from(">HHI", data, cmap + 4 + 8 * i)
            if pid == 3 and eid == 1:
                sub = cmap + off
        if sub is None or struct.unpack_from(">H", data, sub)[0] != 4:
            raise ValueError("font has no Windows BMP (format 4) cmap")
        seg_x2 = struct.unpack_from(">H", data, sub + 6)[0]
        segs = seg_x2 // 2
        ends = struct.unpack_from(f">{segs}H", data, sub + 14)
        starts = struct.unpack_from(f">{segs}H", data, sub + 16 + seg_x2)
        deltas = struct.unpack_from(f">{segs}h", data, sub + 16 + 2 * seg_x2)
        ro_base = sub + 16 + 3 * seg_x2
        ro = struct.unpack_from(f">{segs}H", data, ro_base)
        out: dict[int, int] = {}
        for s in range(segs):
            for c in range(starts[s], ends[s] + 1):
                if c == 0xFFFF:
                    continue
                if ro[s] == 0:
                    gid = (c + deltas[s]) & 0xFFFF
                else:
                    addr = ro_base + 2 * s + ro[s] + 2 * (c - starts[s])
                    gid = struct.unpack_from(">H", data, addr)[0]
                    if gid:
                        gid = (gid + deltas[s]) & 0xFFFF
                if gid:
                    out[c] = gid
        return out

    def width(self, ch: str) -> float:
        gid = self.cmap.get(ord(ch))
        if gid is None:
            return self.fallback
        return self.advances[min(gid, len(self.advances) - 1)] / self.units


@cache
def metrics(bold: bool = False) -> _Metrics:
    return _Metrics(FONT_FILES[1] if bold else FONT_FILES[0])
