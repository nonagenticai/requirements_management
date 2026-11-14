"""Logical-Thinking-Process service helpers.

Tree and node CRUD reuse the generic ``EntityService`` instances
(``toc_tree_service`` / ``toc_node_service`` in ``services/__init__.py``).
This module adds the TOC-specific logic that doesn't fit the generic
shape: 3-level config resolution, whole-tree graph aggregation, cascade-
aware deletes that also clean the universal edge table, and the structural
Categories-of-Legitimate-Reservation scorer.
"""

from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID

import sqlalchemy.exc
from sqlalchemy import CursorResult, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureRelationship
from models.toc import (
    DEFAULT_TOC_SETTINGS,
    GLOBAL_SETTINGS_TENANT,
    NODE_KINDS_BY_TREE_TYPE,
    TocNode,
    TocSettings,
    TocTree,
)
from repositories.visibility import scope

NODE_ENTITY_TYPE = TocNode.__tablename__  # "toc_nodes"

# Which logic edge each tree type is built from (causality_existence check).
_NECESSITY_TREES = frozenset({"io_map", "prt", "evaporating_cloud"})
_SUFFICIENCY_TREES = frozenset({"crt", "frt", "trt"})

# Severity ordering for the EC auto-gate threshold comparison.
SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


# ---------------------------------------------------------------------------
# Config (global → tenant → project)
# ---------------------------------------------------------------------------


async def _settings_row(
    session: AsyncSession, tenant_id: str | None, project_id: str
) -> TocSettings | None:
    """The one settings row for a tenant/project, or the global row for ``None``.

    ``tenant_id is None`` addresses the global-defaults row and must be spelled
    ``IS NULL``: ``tenant_id == None`` renders as ``tenant_id = NULL`` in raw
    SQL terms and would match nothing, silently turning the global tier into
    "no global settings exist". SQLAlchemy does translate ``== None`` to
    ``IS NULL``, but the intent is spelled out here so nobody has to know that
    to read this.
    """
    tenant_match = (
        TocSettings.tenant_id.is_(None) if tenant_id is None else TocSettings.tenant_id == tenant_id
    )
    return (
        await session.execute(
            select(TocSettings).where(
                tenant_match,
                TocSettings.project_id == project_id,
            )
        )
    ).scalar_one_or_none()


async def resolve_settings(
    session: AsyncSession, tenant_id: str, project_id: str = ""
) -> dict[str, Any]:
    """Merge defaults ← global ← tenant ← project (later wins, key-by-key)."""
    effective: dict[str, Any] = dict(DEFAULT_TOC_SETTINGS)

    layers = [
        await _settings_row(session, GLOBAL_SETTINGS_TENANT, ""),
        await _settings_row(session, tenant_id, ""),
    ]
    if project_id:
        layers.append(await _settings_row(session, tenant_id, project_id))

    for row in layers:
        if row and row.settings:
            effective.update(row.settings)
    return effective


async def upsert_settings(
    session: AsyncSession, tenant_id: str, project_id: str, settings: dict[str, Any]
) -> TocSettings:
    """Create or replace the (tenant_id, project_id) config row."""
    row = await _settings_row(session, tenant_id, project_id)
    if row is None:
        row = TocSettings(tenant_id=tenant_id, project_id=project_id, settings=settings)
        session.add(row)
        try:
            await session.commit()
        except sqlalchemy.exc.IntegrityError:
            # A concurrent first-time write for the same (tenant_id, project_id)
            # already inserted the row (uq_toc_settings_tenant_project). Roll
            # back, re-fetch the survivor, and apply this call's settings on it.
            await session.rollback()
            row = await _settings_row(session, tenant_id, project_id)
            if row is None:
                # The violation was not the (tenant_id, project_id) uniqueness
                # we recover from — surface it rather than mask a real error.
                raise
            row.settings = settings
            await session.commit()
    else:
        row.settings = settings
        await session.commit()
    await session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Graph aggregation + cascade-aware deletes
# ---------------------------------------------------------------------------


async def get_tree_nodes(
    session: AsyncSession, tree_id: UUID, *, tenant_id: str, subject_id: str
) -> list[TocNode]:
    stmt = scope(
        select(TocNode).where(TocNode.tree_id == tree_id),
        TocNode,
        tenant_id=tenant_id,
        subject_id=subject_id,
    ).order_by(TocNode.created_at.asc())
    return list((await session.execute(stmt)).scalars().all())


async def get_edges_for_nodes(
    session: AsyncSession, node_ids: list[UUID], *, tenant_id: str, subject_id: str
) -> list[ArchitectureRelationship]:
    """All toc_nodes edges where either endpoint is one of ``node_ids``."""
    if not node_ids:
        return []
    stmt = scope(
        select(ArchitectureRelationship).where(
            ArchitectureRelationship.source_entity_type == NODE_ENTITY_TYPE,
            or_(
                ArchitectureRelationship.source_id.in_(node_ids),
                ArchitectureRelationship.target_id.in_(node_ids),
            ),
        ),
        ArchitectureRelationship,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )
    return list((await session.execute(stmt)).scalars().all())


async def delete_tree(
    session: AsyncSession, tree: TocTree, *, tenant_id: str, subject_id: str
) -> dict[str, Any]:
    """Delete a tree, its nodes (FK CASCADE), and all edges between its nodes."""
    nodes = await get_tree_nodes(session, tree.id, tenant_id=tenant_id, subject_id=subject_id)
    node_ids = [n.id for n in nodes]
    if node_ids:
        await session.execute(
            delete(ArchitectureRelationship).where(
                ArchitectureRelationship.tenant_id == tenant_id,
                ArchitectureRelationship.source_entity_type == NODE_ENTITY_TYPE,
                or_(
                    ArchitectureRelationship.source_id.in_(node_ids),
                    ArchitectureRelationship.target_id.in_(node_ids),
                ),
            )
        )
    await session.delete(tree)  # toc_nodes.tree_id FK ON DELETE CASCADE removes nodes
    await session.commit()
    return {"status": "deleted", "id": tree.id, "nodes_deleted": len(node_ids)}


async def cleanup_node_edges(session: AsyncSession, node_id: UUID, tenant_id: str) -> int:
    """Delete edges referencing a single node (called before deleting it)."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            delete(ArchitectureRelationship).where(
                ArchitectureRelationship.tenant_id == tenant_id,
                ArchitectureRelationship.source_entity_type == NODE_ENTITY_TYPE,
                or_(
                    ArchitectureRelationship.source_id == node_id,
                    ArchitectureRelationship.target_id == node_id,
                ),
            )
        ),
    )
    return result.rowcount or 0


def valid_kinds_for(tree_type: str) -> frozenset[str]:
    """Node kinds allowed for a tree type (empty set for unknown types)."""
    return NODE_KINDS_BY_TREE_TYPE.get(tree_type, frozenset())


# ---------------------------------------------------------------------------
# Structural Categories of Legitimate Reservation
# ---------------------------------------------------------------------------


def _has_cycle(node_ids: list[UUID], causal_edges: list[tuple[UUID, UUID]]) -> bool:
    """DFS cycle detection over the directed causal sub-graph.

    The traversal uses an **explicit stack**, not recursion. WHITE/GREY/BLACK
    colouring detects *cycles*; it is not a recursion guard, so the recursive
    form this replaced exhausted Python's ~1000-frame stack on a deep but
    perfectly **acyclic** chain and raised ``RecursionError`` — a 500 on
    well-formed input.

    Latent, not observed: the largest live tree is 23 nodes and the longest
    directed causal chain is 2 edges, so nothing in production has ever come
    close. The iterative form is preferred over a depth cap because it removes
    the failure mode rather than trading one error for another, and it needs no
    arbitrary constant to tune.

    Verdicts are unchanged — same colouring, same rules, same visit order — so
    every graph that scored a cycle (or no cycle) before still does.
    """
    adj: dict[UUID, list[UUID]] = {nid: [] for nid in node_ids}
    for src, tgt in causal_edges:
        if src in adj:
            adj[src].append(tgt)
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[UUID, int] = {nid: WHITE for nid in node_ids}

    def visit(start: UUID) -> bool:
        # A frame is (node, iterator over its not-yet-walked successors). The
        # iterator is what the recursive call's resume point used to be: on
        # returning to a frame we continue where that frame left off, which is
        # what makes this the same depth-first order as before.
        color[start] = GREY
        stack: list[tuple[UUID, Iterator[UUID]]] = [(start, iter(adj.get(start, [])))]
        while stack:
            node, successors = stack[-1]
            descended = False
            for m in successors:
                if color.get(m, BLACK) == GREY:
                    return True  # back-edge into the currently-open path
                if color.get(m, BLACK) == WHITE:
                    color[m] = GREY
                    stack.append((m, iter(adj.get(m, []))))
                    descended = True
                    break
            if not descended:
                color[node] = BLACK
                stack.pop()
        return False

    return any(color[nid] == WHITE and visit(nid) for nid in node_ids)


def score_tree_clr(
    tree_type: str,
    nodes: list[TocNode],
    edges: list[ArchitectureRelationship],
) -> tuple[float, list[str], list[str], dict[str, bool]]:
    """Run the structurally-testable subset of the 8 CLR over a tree.

    Returns ``(score, passes, issues, categories)``. ``score`` is the
    fraction of the eight categories that pass. Semantic CLR scrutiny
    (does the cause *really* produce the effect?) is delegated to the LLM
    reasoning lenses; this is well-formedness only.
    """
    passes: list[str] = []
    issues: list[str] = []
    categories: dict[str, bool] = {}

    def record(cat: str, ok: bool, pass_msg: str, issue_msg: str) -> None:
        categories[cat] = ok
        (passes if ok else issues).append(pass_msg if ok else issue_msg)

    node_ids = [n.id for n in nodes]
    statements = [(n.statement or "").strip() for n in nodes]
    multi = len(nodes) > 1

    # Causal sub-graph excludes conflict/assumption edges (not causal arrows).
    causal_edges = [
        (e.source_id, e.target_id)
        for e in edges
        if e.relationship_type in ("toc_necessity", "toc_sufficiency")
    ]
    sources = {s for s, _ in causal_edges}
    targets = {t for _, t in causal_edges}
    touched = {nid for e in edges for nid in (e.source_id, e.target_id)}

    if not nodes:
        # An empty tree fails every category.
        for cat in (
            "clarity",
            "entity_existence",
            "causality_existence",
            "cause_insufficiency",
            "additional_cause",
            "cause_effect_reversal",
            "predicted_effect_existence",
            "tautology",
        ):
            categories[cat] = False
        issues.append("Tree has no nodes.")
        return 0.0, passes, issues, categories

    # 1. Clarity — every statement present and >= 3 words.
    vague = [s for s in statements if not s or len(s.split()) < 3]
    record(
        "clarity",
        not vague,
        "Every node statement is present and reasonably specific.",
        f"{len(vague)} node statement(s) are empty or too terse to be clear.",
    )

    # 2. Entity existence — no empty statements.
    record(
        "entity_existence",
        all(statements),
        "All node entities have a stated existence.",
        "One or more nodes have an empty statement.",
    )

    # 3. Causality existence — expected logic edge present for the tree type.
    if tree_type == "evaporating_cloud":
        ok_caus = any(e.relationship_type == "toc_conflict" for e in edges) and any(
            e.relationship_type == "toc_necessity" for e in edges
        )
        caus_issue = "Evaporating Cloud needs both necessity arrows and a conflict wedge."
    elif tree_type in _SUFFICIENCY_TREES:
        ok_caus = any(e.relationship_type == "toc_sufficiency" for e in edges)
        caus_issue = f"A {tree_type} needs at least one sufficiency (if-then) edge."
    elif tree_type in _NECESSITY_TREES:
        ok_caus = any(e.relationship_type == "toc_necessity" for e in edges)
        caus_issue = f"A {tree_type} needs at least one necessity (in-order-to) edge."
    else:
        ok_caus = bool(edges)
        caus_issue = "Tree has no logical connections."
    # A single-node tree is trivially exempt (nothing to connect yet).
    record(
        "causality_existence",
        ok_caus or not multi,
        "The expected logic connections are present.",
        caus_issue,
    )

    # 4. Cause insufficiency (proxy) — no orphan nodes left unconnected.
    orphans = [n for n in nodes if n.id not in touched] if multi else []
    record(
        "cause_insufficiency",
        not orphans,
        "Every entity is connected into the logic.",
        f"{len(orphans)} node(s) are not connected to any cause/effect — mapped causes may be insufficient.",
    )

    # 5. Additional cause — semantic; structurally acknowledged.
    record(
        "additional_cause",
        True,
        "No structural sign of an unaccounted-for additional cause (semantic check delegated to scrutiny).",
        "",
    )

    # 6. Cause-effect reversal — no directed cycle in the causal sub-graph.
    has_cycle = _has_cycle(node_ids, causal_edges)
    record(
        "cause_effect_reversal",
        not has_cycle,
        "No reversed cause/effect (the causal graph is acyclic).",
        "A directed cycle exists — a cause and its effect may be reversed.",
    )

    # 7. Predicted effect existence — there is a final effect/goal (a sink).
    has_sink = bool(targets - sources) if causal_edges else not multi
    record(
        "predicted_effect_existence",
        has_sink,
        "The tree culminates in a predicted effect/objective.",
        "No terminal effect/objective — the logic does not lead anywhere.",
    )

    # 8. Tautology — no duplicate statements, no self-loops.
    normalized = [s.lower() for s in statements if s]
    dup = len(normalized) != len(set(normalized))
    self_loop = any(e.source_id == e.target_id for e in edges)
    record(
        "tautology",
        not (dup or self_loop),
        "No tautologies (duplicate statements or self-referential edges).",
        "Tautology detected: duplicate node statements or a self-referential edge.",
    )

    score = sum(1 for ok in categories.values() if ok) / len(categories)
    return score, passes, issues, categories
