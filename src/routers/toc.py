"""Logical Thinking Process (Theory of Constraints) endpoints.

Trees and nodes use the generic ``EntityService`` CRUD; edges are a thin
wrapper over ``architecture_relationships`` forced to ``toc_nodes``. The
``/config`` routes are declared first so the static path is matched before
the ``/{tree_id}`` wildcard.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response
from pydantic import BeforeValidator
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from routers.responses import AUTH_RESPONSES
from schemas.architecture import ArchitectureRelationshipIn, ArchitectureRelationshipOut
from schemas.common import DeleteResult, ErrorResponse, TocTreeDeleteResult
from schemas.toc import (
    TOC_EDGE_TYPES,
    ClrReport,
    TocEdgeIn,
    TocGraph,
    TocNodeIn,
    TocNodeOut,
    TocSettingsIn,
    TocSettingsOut,
    TocTreeIn,
    TocTreeOut,
    TocTreeType,
)
from services import architecture_relationship_service as edge_svc
from services import toc_node_service as node_svc
from services import toc_service
from services import toc_tree_service as tree_svc
from services.quality_service import QualityResult, persist_quality

router = APIRouter()


def _normalise_tree_type_filter(v: Any) -> Any:
    """Strip + lowercase the ``tree_type`` QUERY value; empty means "no filter".

    ``TocTreeIn.tree_type`` has always run ``_normalise_vocab`` (strip + lower)
    on the request BODY, so ``POST {"tree_type": "CRT"}`` stores ``crt``. The
    query side was raw ``str | None`` and was passed to the repository filter
    verbatim, so ``GET /toc?tree_type=CRT`` compared ``"CRT"`` against the
    stored ``"crt"`` and matched nothing — HTTP 200 with an empty list, which
    reads as "you have no CRTs" rather than as an error. Reachable from the fr
    UI (``knowledge/toc/+page.svelte`` casts the search param unvalidated) and
    from the n8n node, where Tree Type is a free-text box.

    Empty / whitespace-only collapses to ``None`` rather than 422: a UI that
    unconditionally appends ``?tree_type=`` means "no filter", and 422-ing it
    would be a new failure on a caller that works today.
    """
    if isinstance(v, str):
        return v.strip().lower() or None
    return v


# The filter is normalise-THEN-validate: the ``BeforeValidator`` runs first, so
# ``CRT`` / ``"  crt  "`` reach the Literal as ``crt``; anything outside the
# vocabulary is a request-validation error (422), not a silently empty list.
#
# ⛔ This alias MUST be spelled ``Annotated[TocTreeTypeFilter, Query(...)] = None``
# at the call site — never ``tree_type: TocTreeTypeFilter = Query(None, ...)``.
# With a ``Query()`` object as the DEFAULT, ``fastapi.dependencies.utils.
# analyze_param`` takes the ``isinstance(value, FieldInfo)`` branch and does
# ``field_info.annotation = type_annotation``, where ``type_annotation`` is
# ``get_args(annotation)[0]`` — i.e. the bare ``TocTreeType | None`` with every
# non-FastAPI ``Annotated`` member DISCARDED. The ``BeforeValidator`` is then
# silently gone and ``?tree_type=CRT`` 422s instead of normalising. Measured:
# that spelling failed 4 of the 10 tests in
# ``tests/integration/test_toc_routes.py``. With ``Query`` INSIDE the
# ``Annotated``, ``copy_field_info`` merges the FULL annotation and the
# validator survives.
TocTreeTypeFilter = Annotated[TocTreeType | None, BeforeValidator(_normalise_tree_type_filter)]


# ---------------------------------------------------------------------------
# Config (declared before /{tree_id} so the static path wins)
# ---------------------------------------------------------------------------


@router.get(
    "/config",
    response_model=TocSettingsOut,
    operation_id="get_toc_config",
    tags=["TOC Config"],
    responses=AUTH_RESPONSES,
)
async def get_toc_config(
    project_id: str = Query("", max_length=100, description="'' for the tenant-level config"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TocSettingsOut:
    """Resolve effective config (global → tenant → project merge)."""
    settings = await toc_service.resolve_settings(session, tenant_id, project_id)
    return TocSettingsOut(tenant_id=tenant_id, project_id=project_id, settings=settings)


@router.put(
    "/config",
    response_model=TocSettingsOut,
    operation_id="upsert_toc_config",
    tags=["TOC Config"],
    responses=AUTH_RESPONSES,
)
async def upsert_toc_config(
    payload: TocSettingsIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TocSettingsOut:
    """Create or replace this tenant's (or tenant+project's) config row."""
    await toc_service.upsert_settings(session, tenant_id, payload.project_id, payload.settings)
    settings = await toc_service.resolve_settings(session, tenant_id, payload.project_id)
    return TocSettingsOut(tenant_id=tenant_id, project_id=payload.project_id, settings=settings)


# ---------------------------------------------------------------------------
# Trees
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=TocTreeOut,
    operation_id="create_toc_tree",
    responses={**AUTH_RESPONSES, 422: {"description": "Validation error", "model": ErrorResponse}},
)
async def create_toc_tree(
    payload: TocTreeIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TocTreeOut:
    """Create a TOC tree (io_map, evaporating_cloud, crt, frt, prt, trt).

    When ``project_id`` is set this is a get-or-create keyed on
    ``(tenant_id, project_id, tree_type, title)`` so an AA-intake re-run updates
    the existing tree in place (best-effort gate backed by the WS1 partial
    unique index). TOC uses structural CLR scoring, not LLM eval, so
    ``evaluate=False`` on both paths.
    """
    if payload.project_id:
        obj, _created = await tree_svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            evaluate=False,
            identity_fields=("project_id", "tree_type", "title"),
        )
        return obj
    return await tree_svc.create(
        session, payload, tenant_id, subject_id, visibility=visibility, evaluate=False
    )


@router.get(
    "",
    response_model=list[TocTreeOut],
    operation_id="list_toc_trees",
    responses=AUTH_RESPONSES,
)
async def list_toc_trees(
    response: Response,
    tree_type: Annotated[
        TocTreeTypeFilter,
        Query(description="Filter by tree type (case-insensitive; empty means no filter)"),
    ] = None,
    project_id: str | None = Query(None, description="Filter by project"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[TocTreeOut]:
    """List TOC trees, optionally filtered by type/project."""
    items, total = await tree_svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        tree_type=tree_type,
        project_id=project_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get(
    "/{tree_id}",
    response_model=TocTreeOut,
    operation_id="get_toc_tree",
    responses={**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}},
)
async def get_toc_tree(
    tree_id: UUID = Path(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TocTreeOut:
    """Retrieve a tree by ID."""
    return await tree_svc.get(session, tree_id, tenant_id, subject_id, include_system=True)


@router.put(
    "/{tree_id}",
    response_model=TocTreeOut,
    operation_id="update_toc_tree",
    responses={**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}},
)
async def update_toc_tree(
    tree_id: UUID = Path(...),
    payload: TocTreeIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TocTreeOut:
    """Update a tree's metadata."""
    return await tree_svc.update(session, tree_id, payload, tenant_id, subject_id, evaluate=False)


@router.delete(
    "/{tree_id}",
    operation_id="delete_toc_tree",
    response_model=TocTreeDeleteResult,
    responses={**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}},
)
async def delete_toc_tree(
    tree_id: UUID = Path(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TocTreeDeleteResult:
    """Delete a tree, its nodes (cascade), and all edges between them."""
    tree = await tree_svc._get_mutable(session, tree_id, tenant_id=tenant_id, subject_id=subject_id)
    return TocTreeDeleteResult.model_validate(
        await toc_service.delete_tree(session, tree, tenant_id=tenant_id, subject_id=subject_id)
    )


# ---------------------------------------------------------------------------
# Graph + CLR
# ---------------------------------------------------------------------------


@router.get(
    "/{tree_id}/graph",
    response_model=TocGraph,
    operation_id="get_toc_graph",
    responses={**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}},
)
async def get_toc_graph(
    tree_id: UUID = Path(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TocGraph:
    """Return the whole tree: container + nodes + edges."""
    tree = await tree_svc._get_owned(session, tree_id, tenant_id, subject_id)
    nodes = await toc_service.get_tree_nodes(
        session, tree_id, tenant_id=tenant_id, subject_id=subject_id
    )
    edges = await toc_service.get_edges_for_nodes(
        session, [n.id for n in nodes], tenant_id=tenant_id, subject_id=subject_id
    )
    return TocGraph(
        tree=TocTreeOut.model_validate(tree),
        nodes=[TocNodeOut.model_validate(n) for n in nodes],
        edges=[ArchitectureRelationshipOut.model_validate(e) for e in edges],
    )


@router.post(
    "/{tree_id}/quality",
    response_model=ClrReport,
    operation_id="get_toc_tree_quality",
    tags=["Quality"],
    responses={**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}},
)
async def toc_tree_quality(
    tree_id: UUID = Path(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ClrReport:
    """Run the structural Categories-of-Legitimate-Reservation scrutiny and persist the score."""
    tree = await tree_svc._get_owned(session, tree_id, tenant_id, subject_id)
    nodes = await toc_service.get_tree_nodes(
        session, tree_id, tenant_id=tenant_id, subject_id=subject_id
    )
    edges = await toc_service.get_edges_for_nodes(
        session, [n.id for n in nodes], tenant_id=tenant_id, subject_id=subject_id
    )
    score, passes, issues, categories = toc_service.score_tree_clr(tree.tree_type, nodes, edges)
    await persist_quality(
        session,
        tree,
        QualityResult(score=score, issues=issues, suggestions=[], agent_name="clr_structural"),
    )
    await session.commit()
    return ClrReport(score=score, passes=passes, issues=issues, categories=categories)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def _require_tree(session: AsyncSession, tree_id: UUID, tenant_id: str, subject_id: str):
    return await tree_svc._get_owned(session, tree_id, tenant_id, subject_id)


def _validate_node_kind(tree_type: str, node_kind: str) -> None:
    allowed = toc_service.valid_kinds_for(tree_type)
    if node_kind not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"node_kind '{node_kind}' invalid for {tree_type}; allowed: {', '.join(sorted(allowed))}",
        )


@router.post(
    "/{tree_id}/nodes",
    response_model=TocNodeOut,
    operation_id="create_toc_node",
    responses={**AUTH_RESPONSES, 404: {"description": "Tree not found", "model": ErrorResponse}},
)
async def create_toc_node(
    tree_id: UUID = Path(...),
    payload: TocNodeIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TocNodeOut:
    """Add a node to a tree (node_kind validated against the tree's type)."""
    tree = await _require_tree(session, tree_id, tenant_id, subject_id)
    _validate_node_kind(tree.tree_type, payload.node_kind)
    payload = payload.model_copy(update={"tree_id": tree_id})
    # get-or-create keyed on (tenant_id, tree_id, title): a re-run of the same
    # node within a tree updates in place instead of minting a duplicate
    # (best-effort gate backed by the WS1 partial unique index). tree_id is set
    # from the path by the model_copy above, so this branch always applies —
    # that stays true now TocNodeIn.tree_id is optional in the BODY: the path
    # param is a non-null UUID and uuid.UUID defines neither __bool__ nor
    # __len__, so every UUID (the nil UUID included) is truthy. Structural CLR
    # scoring, not LLM eval → evaluate=False.
    if payload.tree_id:
        obj, _created = await node_svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            evaluate=False,
            identity_fields=("tree_id", "title"),
        )
        return obj
    return await node_svc.create(
        session, payload, tenant_id, subject_id, visibility=visibility, evaluate=False
    )


@router.get(
    "/{tree_id}/nodes",
    response_model=list[TocNodeOut],
    operation_id="list_toc_nodes",
    responses=AUTH_RESPONSES,
)
async def list_toc_nodes(
    response: Response,
    tree_id: UUID = Path(...),
    node_kind: str | None = Query(None, description="Filter by node kind"),
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[TocNodeOut]:
    """List nodes of a tree."""
    items, total = await node_svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        tree_id=tree_id,
        node_kind=node_kind,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{tree_id}/nodes/{node_id}",
    response_model=TocNodeOut,
    operation_id="update_toc_node",
    responses={**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}},
)
async def update_toc_node(
    tree_id: UUID = Path(...),
    node_id: UUID = Path(...),
    payload: TocNodeIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> TocNodeOut:
    """Update a node (cannot be reparented; node_kind re-validated)."""
    tree = await _require_tree(session, tree_id, tenant_id, subject_id)
    node = await node_svc._get_owned(session, node_id, tenant_id, subject_id)
    if node.tree_id != tree_id:
        raise HTTPException(status_code=404, detail="not found")
    _validate_node_kind(tree.tree_type, payload.node_kind)
    payload = payload.model_copy(update={"tree_id": tree_id})
    return await node_svc.update(session, node_id, payload, tenant_id, subject_id, evaluate=False)


@router.delete(
    "/{tree_id}/nodes/{node_id}",
    operation_id="delete_toc_node",
    response_model=DeleteResult,
    responses={**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}},
)
async def delete_toc_node(
    tree_id: UUID = Path(...),
    node_id: UUID = Path(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete a node and any edges referencing it."""
    node = await node_svc._get_mutable(session, node_id, tenant_id=tenant_id, subject_id=subject_id)
    if node.tree_id != tree_id:
        raise HTTPException(status_code=404, detail="not found")
    await toc_service.cleanup_node_edges(session, node_id, tenant_id)
    await session.delete(node)
    await session.commit()
    return DeleteResult(status="deleted", id=node_id)


# ---------------------------------------------------------------------------
# Edges (thin wrapper over architecture_relationships, forced to toc_nodes)
# ---------------------------------------------------------------------------


@router.post(
    "/{tree_id}/edges",
    response_model=ArchitectureRelationshipOut,
    operation_id="create_toc_edge",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Tree/node not found", "model": ErrorResponse},
    },
)
async def create_toc_edge(
    tree_id: UUID = Path(...),
    payload: TocEdgeIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureRelationshipOut:
    """Connect two nodes of the same tree with a necessity/sufficiency/conflict/assumption edge."""
    await _require_tree(session, tree_id, tenant_id, subject_id)
    nodes = await toc_service.get_tree_nodes(
        session, tree_id, tenant_id=tenant_id, subject_id=subject_id
    )
    node_ids = {n.id for n in nodes}
    if payload.source_id not in node_ids or payload.target_id not in node_ids:
        raise HTTPException(status_code=404, detail="source/target node not in this tree")

    rel_in = ArchitectureRelationshipIn(
        source_entity_type="toc_nodes",
        source_id=payload.source_id,
        target_entity_type="toc_nodes",
        target_id=payload.target_id,
        relationship_type=payload.relationship_type,
        name=payload.name,
        description=payload.description,
        properties=payload.properties,
    )
    # FIX 6: idempotent on re-POST. TOC edges carry no project_id, so identity is
    # the edge tuple, backstopped by the ``uq_arch_rels_toc_edge`` partial index.
    result, _created = await edge_svc.get_or_create(
        session,
        rel_in,
        tenant_id,
        subject_id,
        visibility=visibility,
        identity_fields=(
            "source_entity_type",
            "source_id",
            "target_entity_type",
            "target_id",
            "relationship_type",
        ),
    )
    return result


@router.get(
    "/{tree_id}/edges",
    response_model=list[ArchitectureRelationshipOut],
    operation_id="list_toc_edges",
    responses=AUTH_RESPONSES,
)
async def list_toc_edges(
    tree_id: UUID = Path(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ArchitectureRelationshipOut]:
    """List all edges between nodes of a tree."""
    await _require_tree(session, tree_id, tenant_id, subject_id)
    nodes = await toc_service.get_tree_nodes(
        session, tree_id, tenant_id=tenant_id, subject_id=subject_id
    )
    edges = await toc_service.get_edges_for_nodes(
        session, [n.id for n in nodes], tenant_id=tenant_id, subject_id=subject_id
    )
    return [ArchitectureRelationshipOut.model_validate(e) for e in edges]


@router.delete(
    "/{tree_id}/edges/{edge_id}",
    operation_id="delete_toc_edge",
    response_model=DeleteResult,
    responses={**AUTH_RESPONSES, 404: {"description": "Not found", "model": ErrorResponse}},
)
async def delete_toc_edge(
    tree_id: UUID = Path(...),
    edge_id: UUID = Path(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeleteResult:
    """Delete a single edge of this tree.

    ``edge_svc`` is the GENERIC relationship service over
    ``architecture_relationships``, the same table that carries ArchiMate
    relationships and governance links. This route used to
    hand it ``edge_id`` and ignore ``tree_id`` entirely, so ANY relationship row
    in the tenant was deletable through a TOC URL carrying an arbitrary tree
    UUID. Every sibling route calls ``_require_tree`` first; so does this one
    now, and the edge must additionally be a ``toc_nodes`` edge whose BOTH
    endpoints are nodes of ``tree_id`` AND whose ``relationship_type`` is one of
    ``TOC_EDGE_TYPES``.

    The ``relationship_type`` leg closes the residual hole: ``create_toc_edge``
    validates ``TocEdgeIn.relationship_type`` against the ``TocEdgeType``
    Literal, so this route can only ever MINT one of the four TOC types. An
    ``association`` (or any other ``RELATIONSHIP_TYPES`` member) that happens to
    join two nodes of this tree was therefore created through
    ``POST /architecture/relationships`` — a different route family, with its own
    ``DELETE /architecture/relationships/{rid}``. Without this leg the TOC path
    stayed a delete primitive for rows it cannot create, which is the same class
    of defect as the tree-scoping one above, just one axis narrower. No caller is
    stranded: the owning route still deletes such a row, and ``delete_tree``
    still sweeps it with the tree.

    A non-matching edge is **404**, not 403: a 403 would confirm the row exists.
    """
    await _require_tree(session, tree_id, tenant_id, subject_id)
    edge = await edge_svc._get_mutable(session, edge_id, tenant_id=tenant_id, subject_id=subject_id)
    node_ids = {
        n.id
        for n in await toc_service.get_tree_nodes(
            session, tree_id, tenant_id=tenant_id, subject_id=subject_id
        )
    }
    if (
        edge.source_entity_type != toc_service.NODE_ENTITY_TYPE
        or edge.target_entity_type != toc_service.NODE_ENTITY_TYPE
        or edge.source_id not in node_ids
        or edge.target_id not in node_ids
        or edge.relationship_type not in TOC_EDGE_TYPES
    ):
        raise HTTPException(status_code=404, detail="not found")
    return DeleteResult.model_validate(
        await edge_svc.delete(session, edge_id, tenant_id, subject_id)
    )
