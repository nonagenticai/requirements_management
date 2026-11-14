"""Deterministic layered layout and simple orthogonal connection routing.

Layout (a small Sugiyama-style pipeline, used only for objects that have no
stored bounds):

1. **Rank assignment** — either given by the caller (ArchiMate: one row per
   language layer) or longest-path layering over the edges, with cycles broken
   by a deterministic DFS (back edges ignored).
2. **Ordering** — barycenter sweeps (down then up, a fixed number of rounds);
   ties keep the previous order, so input order is the final tie-breaker.
3. **Coordinates** — rows top to bottom, nodes left to right with fixed gaps,
   each row centred on the widest one. A row longer than ``max_per_row`` wraps
   into further rows. Boxes never overlap by construction.

Routing: a connection without stored bendpoints is drawn straight unless that
line crosses an unrelated box; then the shortest of a few orthogonal
alternatives (L-shapes, Z-shapes through the midline or through the channels
beside the blocking boxes) that crosses nothing is used, provided it is at most
twice the straight distance plus 120px. If none qualifies the straight line
stays — the renderer never invents a long detour.

Everything iterates in input order or sorted order; no sets are iterated
unsorted, so the same input gives the same output.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence

from tools.rendering.svg import Pt, Rect, clip_rect

ROUNDS = 4


def assign_ranks(ids: Sequence[Hashable], edges: Sequence[tuple[Hashable, Hashable]]) -> dict:
    """Longest-path ranks: for an edge (upper, lower), rank(lower) > rank(upper)."""
    idx = {n: i for i, n in enumerate(ids)}
    succ: dict = {n: [] for n in ids}
    for a, b in edges:
        if a in idx and b in idx and a != b and b not in succ[a]:
            succ[a].append(b)
    # Deterministic DFS: drop back edges to make the graph acyclic.
    state: dict = {}
    dag: dict = {n: [] for n in ids}
    for root in ids:
        if root in state:
            continue
        stack: list[tuple[Hashable, int]] = [(root, 0)]
        state[root] = 1
        while stack:
            node, i = stack[-1]
            if i < len(succ[node]):
                stack[-1] = (node, i + 1)
                nxt = succ[node][i]
                if state.get(nxt) == 1:
                    continue  # back edge
                dag[node].append(nxt)
                if nxt not in state:
                    state[nxt] = 1
                    stack.append((nxt, 0))
            else:
                state[node] = 2
                stack.pop()
    rank = {n: 0 for n in ids}
    # Topological order via Kahn (stable by input index).
    indeg = {n: 0 for n in ids}
    for a in ids:
        for b in dag[a]:
            indeg[b] += 1
    ready = [n for n in ids if indeg[n] == 0]
    while ready:
        ready.sort(key=idx.__getitem__)
        n = ready.pop(0)
        for b in dag[n]:
            rank[b] = max(rank[b], rank[n] + 1)
            indeg[b] -= 1
            if indeg[b] == 0:
                ready.append(b)
    return rank


def layered_layout(
    nodes: Sequence[tuple[Hashable, float, float]],
    edges: Sequence[tuple[Hashable, Hashable]],
    *,
    ranks: dict | None = None,
    gap_x: float = 40.0,
    gap_y: float = 50.0,
    max_per_row: int = 6,
) -> dict:
    """Positions (top-left, relative to 0,0) for ``nodes`` = [(id, w, h)]."""
    if not nodes:
        return {}
    ids = [n for n, _, _ in nodes]
    size = {n: (w, h) for n, w, h in nodes}
    if ranks is None:
        ranks = assign_ranks(ids, edges)
    distinct = sorted({ranks[n] for n in ids})
    rows: list[list[Hashable]] = [[n for n in ids if ranks[n] == r] for r in distinct]

    neighbours: dict = {n: [] for n in ids}
    for a, b in edges:
        if a in neighbours and b in neighbours and a != b:
            neighbours[a].append(b)
            neighbours[b].append(a)

    def sweep(order: list[list[Hashable]], ref_rows: range) -> None:
        for ri in ref_rows:
            ref = order[ri - 1] if ref_rows.step > 0 else order[ri + 1]
            pos = {n: i for i, n in enumerate(ref)}
            row = order[ri]
            cur = {n: i for i, n in enumerate(row)}

            def key(n: Hashable, pos: dict = pos, cur: dict = cur) -> tuple[float, int]:
                ps = [pos[m] for m in neighbours[n] if m in pos]
                return (sum(ps) / len(ps) if ps else float(cur[n]), cur[n])

            order[ri] = sorted(row, key=key)

    for _ in range(ROUNDS):
        if len(rows) > 1:
            sweep(rows, range(1, len(rows)))
            sweep(rows, range(len(rows) - 2, -1, -1))

    wrapped: list[list[Hashable]] = []
    for row in rows:
        wrapped.extend(row[i : i + max_per_row] for i in range(0, len(row), max_per_row))

    widths = [sum(size[n][0] for n in row) + gap_x * (len(row) - 1) for row in wrapped]
    full = max(widths)
    out: dict = {}
    y = 0.0
    for row, w in zip(wrapped, widths, strict=True):
        x = (full - w) / 2
        row_h = max(size[n][1] for n in row)
        for n in row:
            out[n] = (x, y)
            x += size[n][0] + gap_x
        y += row_h + gap_y
    return out


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _seg_hits(a: Pt, b: Pt, r: Rect, pad: float = 1.0) -> bool:
    """Does segment a-b pass through the interior of ``r`` (shrunk by ``pad``)?"""
    x0, y0, x1, y1 = r.x + pad, r.y + pad, r.x + r.w - pad, r.y + r.h - pad
    if x0 >= x1 or y0 >= y1:
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _clear(points: list[Pt], obstacles: Sequence[Rect]) -> bool:
    return not any(
        _seg_hits(points[i], points[i + 1], r) for i in range(len(points) - 1) for r in obstacles
    )


def _inside(p: Pt, r: Rect | None) -> bool:
    return r is not None and r.x < p[0] < r.x + r.w and r.y < p[1] < r.y + r.h


def _length(points: list[Pt]) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def route(
    src: Rect | None,
    src_point: Pt,
    tgt: Rect | None,
    tgt_point: Pt,
    obstacles: Sequence[Rect],
) -> list[Pt]:
    """Bendpoints for a connection (``[]`` = straight is fine or nothing better).

    ``src``/``tgt`` are the end boxes (``None`` for an end on another
    connection, whose anchor is ``*_point``); ``obstacles`` are the boxes the
    line must not cross (callers exclude the ends and their ancestors).
    """

    def full(bends: list[Pt]) -> list[Pt]:
        first = bends[0] if bends else tgt_point
        last = bends[-1] if bends else src_point
        a = clip_rect(src, first) if src is not None else src_point
        b = clip_rect(tgt, last) if tgt is not None else tgt_point
        return [a, *bends, b]

    straight = full([])
    blockers = [r for r in obstacles if _seg_hits(straight[0], straight[1], r)]
    if not blockers:
        return []
    (sx, sy), (tx, ty) = src_point, tgt_point
    xs = [(sx + tx) / 2] + sorted({v for r in blockers for v in (r.x - 12, r.x + r.w + 12)})
    ys = [(sy + ty) / 2] + sorted({v for r in blockers for v in (r.y - 12, r.y + r.h + 12)})
    candidates: list[list[Pt]] = [[(sx, ty)], [(tx, sy)]]
    candidates += [[(sx, y), (tx, y)] for y in ys]
    candidates += [[(x, sy), (x, ty)] for x in xs]
    # A "simple" detour: at most twice the straight distance plus a box's height.
    budget = 2 * _length(straight) + 120
    best: tuple[float, int, list[Pt]] | None = None
    for i, bends in enumerate(candidates):
        if any(_inside(p, src) or _inside(p, tgt) for p in bends):
            continue
        pts = full(bends)
        if not _clear(pts, obstacles):
            continue
        cand = (round(_length(pts), 6), i, bends)
        if cand[0] > budget:
            continue
        if best is None or cand[:2] < best[:2]:
            best = cand
    return best[2] if best else []
