"""End-to-end pin: ``score_tree_clr`` survives a chain deeper than Python's
default recursion limit, exercised through the actual public entry point a
router calls rather than the private ``_has_cycle`` helper directly.

CONTEXT. ``tests/test_toc_recursion_is_guarded.py`` already pins the unit-level
fix inside ``services/toc_service._has_cycle``: the WHITE/GREY/BLACK cycle
detector was rewritten from a recursive ``visit()`` (one Python stack frame
per node — ``RecursionError`` on a long but perfectly acyclic chain, surfaced
as an HTTP 500 on well-formed input) to an iterative, explicit-stack DFS. That
rewrite removes the failure mode entirely rather than trading it for a depth
cap with an arbitrary constant: an iterative traversal has no call-stack depth
to exhaust, so there is no "guard" threshold to size or to test against.

This file adds the one thing that unit test does not cover: that the fix
holds through ``score_tree_clr`` — CLR category 6 (``cause_effect_reversal``),
the only caller of ``_has_cycle`` — using real ``TocNode`` /
``ArchitectureRelationship`` ORM instances (constructed directly, not
persisted; ``score_tree_clr`` only reads plain attributes, no DB/session
needed) instead of bare UUID tuples. If a future refactor reintroduces
recursion anywhere between the router's call and ``_has_cycle``, this is the
test that would catch it even if the unit test above kept passing.

⚠️ LATENT, not observed: measured production largest tree is 23 nodes, longest
causal chain 2 edges (see the sibling test file) — nothing has ever come close
to the 2,000-node chain built here.
"""

from __future__ import annotations

import sys
from uuid import UUID

from models.architecture import ArchitectureRelationship
from models.toc import TocNode
from services.toc_service import score_tree_clr

# Deeper than CPython's default recursion limit (1000), matching the sibling
# unit test's margin.
_CHAIN_LENGTH = 2_000


def _chain_ids(n: int) -> list[UUID]:
    return [UUID(int=i + 1) for i in range(n)]


def test_score_tree_clr_survives_a_deep_acyclic_chain() -> None:
    """A 2,000-node CRT chain scores cleanly instead of raising RecursionError."""
    node_ids = _chain_ids(_CHAIN_LENGTH)

    # The premise this test relies on: real recursion could not have walked
    # this chain, so a pass here is evidence the traversal is genuinely
    # iterative, not evidence the machine happens to allow deep recursion.
    assert sys.getrecursionlimit() < _CHAIN_LENGTH, (
        f"recursion limit is {sys.getrecursionlimit()}, which is not below the "
        f"{_CHAIN_LENGTH}-node chain — this test can no longer distinguish an "
        "iterative traversal from a recursive one."
    )

    nodes = [
        TocNode(id=nid, statement=f"statement number {i} is long enough")
        for i, nid in enumerate(node_ids)
    ]
    edges = [
        ArchitectureRelationship(
            source_id=node_ids[i],
            target_id=node_ids[i + 1],
            relationship_type="toc_sufficiency",
            source_entity_type="toc_nodes",
            target_entity_type="toc_nodes",
        )
        for i in range(_CHAIN_LENGTH - 1)
    ]

    # No pytest.raises / try here on purpose: a RecursionError propagating out
    # of score_tree_clr (via _has_cycle) IS the failure this test exists to
    # catch, and it must be reported as a test failure, not swallowed.
    score, _passes, _issues, categories = score_tree_clr("crt", nodes, edges)

    # The behavioural assertion, not just "it didn't crash": a straight chain
    # is acyclic, so CLR category 6 must pass.
    assert categories["cause_effect_reversal"] is True
    assert 0.0 <= score <= 1.0
