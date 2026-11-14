"""Search endpoints across all entity types."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id
from db import get_session
from routers.responses import AUTH_RESPONSES
from schemas.common import SearchHit, SemanticSearchResponse, UnifiedSearchHit
from services import search_service as svc

# Re-export for tests that import from here
from services.search_service import build_search_hits  # noqa: F401
from utils.pagination import normalize_k_param, normalize_min_similarity, normalize_search_query

router = APIRouter()

# Optional narrowing + pagination for the three governance searches. All are
# additive: a caller that sends only ``q``/``k`` gets exactly the hits it got
# before, now in a stable total order (score desc, created_at asc, id asc — see
# ``search_light._STABLE_SCORE_SQL``), so the same query returns the same ids in
# the same order on every run and ``offset`` pages are disjoint.
_VERSION_STATUS_PATTERN = "^(DRAFT|PUBLISHED|DEPRECATED)$"
_ORIGIN_KIND_PATTERN = "^(tenant|project|workflow)$"


def _offset_q():
    return Query(0, ge=0, le=10000, description="Hits to skip in the stable order (pagination)")


def _version_status_q():
    return Query(None, pattern=_VERSION_STATUS_PATTERN, description="Only this version_status")


def _origin_kind_q():
    return Query(None, pattern=_ORIGIN_KIND_PATTERN, description="Only this origin_kind")


@router.get(
    "/search",
    response_model=list[SearchHit],
    operation_id="search_requirements",
    responses=AUTH_RESPONSES,
)
async def search(
    q: str | None = None,
    k: int = 10,
    offset: int = _offset_q(),
    project_id: str | None = Query(None, max_length=100, description="Only this project"),
    version_status: str | None = _version_status_q(),
    origin_kind: str | None = _origin_kind_q(),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[SearchHit]:
    """Full-text search on requirements."""
    q = normalize_search_query(q)
    if not q:
        return []
    return await svc.search_entity(
        session,
        "requirement",
        q,
        tenant_id,
        normalize_k_param(k),
        offset=offset,
        filters={
            "project_id": project_id,
            "version_status": version_status,
            "origin_kind": origin_kind,
        },
    )


@router.get(
    "/search/principles",
    response_model=list[SearchHit],
    operation_id="search_principles",
    responses=AUTH_RESPONSES,
)
async def search_principles_endpoint(
    q: str | None = None,
    k: int = 10,
    offset: int = _offset_q(),
    version_status: str | None = _version_status_q(),
    origin_kind: str | None = _origin_kind_q(),
    active_only: bool = Query(False, description="Only active principles"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[SearchHit]:
    """Full-text search on principles."""
    q = normalize_search_query(q)
    if not q:
        return []
    return await svc.search_entity(
        session,
        "principle",
        q,
        tenant_id,
        normalize_k_param(k),
        offset=offset,
        filters={
            "version_status": version_status,
            "origin_kind": origin_kind,
            "active_only": active_only,
        },
    )


@router.get(
    "/search/decisions",
    response_model=list[SearchHit],
    operation_id="search_decisions",
    responses=AUTH_RESPONSES,
)
async def search_decisions_endpoint(
    q: str | None = None,
    k: int = 10,
    offset: int = _offset_q(),
    project_id: str | None = Query(None, max_length=100, description="Only this project"),
    status: str | None = Query(None, max_length=20, description="Only this decision status"),
    version_status: str | None = _version_status_q(),
    origin_kind: str | None = _origin_kind_q(),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[SearchHit]:
    """Full-text search on decisions."""
    q = normalize_search_query(q)
    if not q:
        return []
    return await svc.search_entity(
        session,
        "decision",
        q,
        tenant_id,
        normalize_k_param(k),
        offset=offset,
        filters={
            "project_id": project_id,
            "status": status,
            "version_status": version_status,
            "origin_kind": origin_kind,
        },
    )


@router.get(
    "/search/all",
    response_model=list[UnifiedSearchHit],
    operation_id="search_all_entities",
    responses=AUTH_RESPONSES,
)
async def search_all_endpoint(
    q: str | None = None,
    k: int = 10,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[UnifiedSearchHit]:
    """Unified search across all entity types."""
    q = normalize_search_query(q)
    if not q:
        return []
    return await svc.search_all_types(session, q, tenant_id, normalize_k_param(k))


@router.get(
    "/semantic_search",
    response_model=SemanticSearchResponse,
    operation_id="search_knowledge_base",
    responses=AUTH_RESPONSES,
)
async def semantic_search(
    query: str | None = None,
    k: int = 10,
    min_similarity: float = 0.1,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
) -> SemanticSearchResponse:
    """TF-IDF semantic search with NER on knowledge base."""
    query = normalize_search_query(query)
    return await svc.semantic_search(
        query, normalize_k_param(k), normalize_min_similarity(min_similarity)
    )
