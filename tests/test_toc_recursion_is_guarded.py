"""``_has_cycle`` must survive a chain deeper than Python's recursion limit.

THE DEFECT. ``services/toc_service._has_cycle`` detected cycles with a nested
recursive ``visit()``. WHITE/GREY/BLACK colouring is a *cycle* detector, not a
recursion guard: a **deep but perfectly acyclic** chain still puts one frame on
the Python stack per node, so at ~1000 nodes the scorer raised
``RecursionError``. Reached from ``score_tree_clr`` (CLR category 6,
``cause_effect_reversal``), that surfaced to the caller as an HTTP 500 on
input that is entirely well-formed.

⚠️ LATENT, not observed. Measured on production: 1,935 trees / 11,656 nodes,
largest tree **23 nodes**, longest directed causal chain **2 edges** — roughly
43x under on node count and 500x under on depth. Nothing has ever hit this. It
is pinned here so the guard cannot be removed by a later "simplification", not
because it is firing.

THE FIX under test is the iterative rewrite (an explicit stack), so the
depth limit is the machine's heap rather than ``sys.getrecursionlimit()``.

WHAT IS PINNED HERE:

  1. a 2,000-node acyclic chain — deeper than the default recursion limit —
     does not raise ``RecursionError`` and correctly reports "no cycle". This
     is the leg that fails if the guard is reverted, so the chain is built for
     real rather than mocked;
  2. THE BEHAVIOUR CONTROL: a genuine directed cycle is still reported as a
     cycle. Without this leg, a ``_has_cycle`` that returned ``False``
     unconditionally would satisfy leg 1;
  3. THE REGRESSION CONTROL: small acyclic graphs still return exactly what
     they returned before, including the "edge points outside ``node_ids``"
     case that the ``color.get(m, BLACK)`` default handles;
  4. a self-loop (node -> itself) is still a cycle — the easiest thing to lose
     to an off-by-one when converting recursion to an explicit stack.

Pure unit tests: ``_has_cycle`` takes plain UUIDs and tuples, so no database,
no ORM rows and no HTTP client are involved.
"""

from __future__ import annotations

import sys
from uuid import UUID

from services.toc_service import _has_cycle

# Deeper than CPython's default recursion limit (1000) by enough margin that a
# raised limit somewhere in the test environment would not accidentally hide a
# revert. Asserted against the live limit in the test itself.
_CHAIN_LENGTH = 2_000


def _ids(n: int) -> list[UUID]:
    """``n`` distinct, deterministic node UUIDs."""
    return [UUID(int=i + 1) for i in range(n)]


def test_deep_acyclic_chain_does_not_blow_the_stack() -> None:
    """A 2,000-node straight chain: no cycle, no ``RecursionError``."""
    node_ids = _ids(_CHAIN_LENGTH)
    edges = [(node_ids[i], node_ids[i + 1]) for i in range(_CHAIN_LENGTH - 1)]

    # The premise of this test: the chain really is deeper than the recursive
    # form could have walked. If someone raises the limit above the chain
    # length, this test stops proving anything and says so.
    assert sys.getrecursionlimit() < _CHAIN_LENGTH, (
        f"recursion limit is {sys.getrecursionlimit()}, which is not below the "
        f"{_CHAIN_LENGTH}-node chain — this test can no longer distinguish the "
        "iterative traversal from the recursive one it replaced."
    )

    # No pytest.raises / try here on purpose: a RecursionError propagating is
    # the failure, and it must be reported as one.
    assert _has_cycle(node_ids, edges) is False


def test_a_real_cycle_is_still_reported_as_a_cycle() -> None:
    """Behaviour control — a -> b -> c -> a is a cycle."""
    a, b, c = _ids(3)
    assert _has_cycle([a, b, c], [(a, b), (b, c), (c, a)]) is True

    # And a cycle that is not reachable from the first node visited still
    # counts: the outer loop restarts DFS at every WHITE node.
    d, e = _ids(5)[3:]
    assert _has_cycle([a, b, c, d, e], [(a, b), (d, e), (e, d)]) is True


def test_small_acyclic_graphs_are_unchanged() -> None:
    """Regression control — the shapes that work today still answer the same."""
    a, b, c, d = _ids(4)

    # Empty graph, single node, no edges.
    assert _has_cycle([], []) is False
    assert _has_cycle([a], []) is False

    # Diamond: two paths converging on one sink. Reaches an already-BLACK node
    # by a second route, which must not be mistaken for a back-edge.
    assert _has_cycle([a, b, c, d], [(a, b), (a, c), (b, d), (c, d)]) is False

    # An edge whose target is not in node_ids (a cross-tree edge survives
    # get_edges_for_nodes, which matches on EITHER endpoint). Unknown targets
    # default to BLACK, i.e. they are ignored rather than treated as open.
    outside = UUID(int=999)
    assert _has_cycle([a, b], [(a, b), (b, outside)]) is False

    # An edge whose SOURCE is unknown is dropped when adj is built.
    assert _has_cycle([a, b], [(outside, a), (a, b)]) is False


def test_a_self_loop_is_a_cycle() -> None:
    """A node pointing at itself is the shortest possible cycle."""
    a, b = _ids(2)
    assert _has_cycle([a], [(a, a)]) is True

    # Also when the self-loop hangs off a node that is not the DFS root, so it
    # is found on a descended frame rather than the starting one.
    assert _has_cycle([a, b], [(a, b), (b, b)]) is True
