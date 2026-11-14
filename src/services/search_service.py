"""Search service — consolidates entity search, unified search, and semantic search."""

import asyncio
import math
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureElement, ArchitectureView, Standard
from models.decisions import Decision
from models.pm import Programme, Project
from models.principles import Principle
from models.requirements import Requirement
from schemas.common import (
    EntityInfo,
    SearchHit,
    SemanticSearchResponse,
    SemanticSearchResult,
    UnifiedSearchHit,
)
from search_light import (
    search_all,
    search_architecture_elements,
    search_architecture_standards,
    search_architecture_views,
    search_decisions,
    search_light,
    search_principles,
    search_programmes,
    search_projects,
)
from seed_knowledge import SOURCES
from semantic_search import enhance_query_with_entities, semantic_search_knowledge

# Entity type → (ORM model class, snippet field name)
ENTITY_CONFIG: dict[str, tuple[Any, str]] = {
    "requirement": (Requirement, "description"),
    "principle": (Principle, "statement"),
    "decision": (Decision, "context"),
    "architecture_element": (ArchitectureElement, "description"),
    "architecture_view": (ArchitectureView, "description"),
    "architecture_standard": (Standard, "description"),
    "programme": (Programme, "description"),
    "project": (Project, "description"),
}

# Maps entity type → its search_light function
_SEARCH_FUNCTIONS = {
    "requirement": search_light,
    "principle": search_principles,
    "decision": search_decisions,
    "architecture_element": search_architecture_elements,
    "architecture_view": search_architecture_views,
    "architecture_standard": search_architecture_standards,
    "programme": search_programmes,
    "project": search_projects,
}

# Entity types whose search function takes ``offset`` + ``filters`` and
# returns a stable total order (see ``search_light._STABLE_SCORE_SQL``).
_FILTERABLE_SEARCH_FUNCTIONS = {
    "requirement": search_light,
    "principle": search_principles,
    "decision": search_decisions,
}


def _normalize_score(score: float) -> float:
    if math.isnan(score) or math.isinf(score):
        return 0.0
    return round(max(0, min(score, 1)), 4)


def _build_snippet(entity, snippet_field: str) -> str:
    return (getattr(entity, snippet_field, "") or "")[:240].replace("\n", " ")


def build_search_hits(
    entities_by_id: dict,
    scores: list[tuple],
    snippet_field: str,
) -> list[SearchHit]:
    """Build SearchHit list from scored search results."""
    hits = []
    for entity_id, score in scores:
        entity = entities_by_id.get(entity_id)
        if not entity:
            continue
        hits.append(
            SearchHit(
                id=entity.id,
                slug=entity.slug,
                title=entity.title,
                snippet=_build_snippet(entity, snippet_field),
                similarity=_normalize_score(score),
            )
        )
    hits.sort(key=lambda h: h.similarity or 0.0, reverse=True)
    return hits


class SearchService:
    async def search_entity(
        self,
        session: AsyncSession,
        entity_type: str,
        q: str,
        tenant_id: str,
        k: int,
        *,
        offset: int = 0,
        filters: dict | None = None,
    ) -> list[SearchHit]:
        """Full-text search on a single entity type. Tenant-scoped.

        ``offset``/``filters`` are honoured only by the entity types whose search
        function accepts them (requirement, principle, decision); passing either
        to another type raises rather than being silently ignored.
        """
        search_fn = _SEARCH_FUNCTIONS[entity_type]
        model_class, snippet_field = ENTITY_CONFIG[entity_type]

        if offset or filters:
            filterable_fn = _FILTERABLE_SEARCH_FUNCTIONS.get(entity_type)
            if filterable_fn is None:
                raise ValueError(f"{entity_type} search does not support offset/filters")
            sims = await filterable_fn(session, q, tenant_id, k, offset=offset, filters=filters)
        else:
            sims = await search_fn(session, q, tenant_id, k)
        if not sims:
            return []

        ids = [eid for eid, _ in sims]
        query = select(model_class).where(
            model_class.id.in_(ids),
            model_class.tenant_id == tenant_id,
        )
        rows = list((await session.execute(query)).scalars().all())
        by_id = {r.id: r for r in rows}
        return build_search_hits(by_id, sims, snippet_field)

    async def search_all_types(
        self,
        session: AsyncSession,
        q: str,
        tenant_id: str,
        k: int,
    ) -> list[UnifiedSearchHit]:
        """Unified search across all entity types. Tenant-scoped."""
        results = await search_all(session, q, tenant_id, k)
        if not results:
            return []

        # Group IDs by entity type
        ids_by_type: dict[str, list[UUID]] = {}
        for etype, eid, _ in results:
            ids_by_type.setdefault(etype, []).append(eid)

        # Fetch entities for each type
        entities: dict[tuple[str, UUID], Any] = {}
        for etype, id_list in ids_by_type.items():
            model_class = ENTITY_CONFIG[etype][0]
            rows = list(
                (
                    await session.execute(
                        select(model_class).where(
                            model_class.id.in_(id_list),
                            model_class.tenant_id == tenant_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for r in rows:
                entities[(etype, r.id)] = r

        hits = []
        for entity_type, entity_id, score in results:
            entity = entities.get((entity_type, entity_id))
            if not entity:
                continue
            snippet_field = ENTITY_CONFIG[entity_type][1]
            hits.append(
                UnifiedSearchHit(
                    entity_type=entity_type,
                    id=entity.id,
                    slug=entity.slug,
                    title=entity.title,
                    snippet=_build_snippet(entity, snippet_field),
                    similarity=_normalize_score(score),
                )
            )
        hits.sort(key=lambda h: h.similarity or 0.0, reverse=True)
        return hits

    async def semantic_search(
        self,
        query: str | None,
        k: int,
        min_similarity: float,
    ) -> SemanticSearchResponse:
        """TF-IDF semantic search with NER on knowledge base."""
        if not query:
            results = [SemanticSearchResult(**source, similarity_score=1.0) for source in SOURCES]
            return SemanticSearchResponse(
                query="",
                enhanced_query="No query provided - showing all sources",
                results=results,
                entities=[],
                total_results=len(results),
            )

        # Off the event loop, deliberately. Both calls below are synchronous and
        # CPU-bound: `semantic_search_knowledge` lazily imports spaCy and loads
        # `en_core_web_sm` on first use (measured at 4.3s import + 1.0s load in
        # tests/conftest.py), runs `nlp(text)`, then fits a TfidfVectorizer and
        # computes cosine similarity via scikit-learn.
        #
        # Called bare from this `async def`, that arithmetic freezes the whole
        # process — this service runs ONE replica, and `/health/ready` has
        # `timeoutSeconds: 5, failureThreshold: 3`, so ~30s of blocking is enough
        # to drop the pod out of Service endpoints. It has no other worker to fall
        # back on.
        #
        # This is prophylactic, NOT a fix for the stall in issue #111: that stall
        # was measured in a window where `semantic_search` was invoked ZERO times,
        # so this code did not cause it. Do not close #111 on the strength of this.
        search_results, found_entities = await asyncio.to_thread(
            semantic_search_knowledge,
            query=query,
            sources=SOURCES,
            k=k,
            min_similarity=min_similarity,
        )

        entity_responses = [
            EntityInfo(text=e["text"], label=e["label"], description=e["description"])
            for e in found_entities
        ]
        enhanced_query = await asyncio.to_thread(enhance_query_with_entities, query, found_entities)

        return SemanticSearchResponse(
            query=query,
            enhanced_query=enhanced_query,
            results=[SemanticSearchResult(**r) for r in search_results],
            entities=entity_responses,
            total_results=len(search_results),
        )
