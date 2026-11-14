"""
Lightweight search using PostgreSQL full-text and trigram similarity.

This module implements a heuristic search that combines full‑text ranking
with trigram similarity to produce results that are more tolerant of
phrasing differences than simple keyword matching. It also provides a
minimal synonym expansion mechanism to map common abbreviations or jargon
to more verbose forms.

Supports searching across:
- Requirements (title, description, acceptance criteria)
- Principles (title, statement, rationale, implications, tags)
- Decisions (title, context, decision, consequences, alternatives)
- Architecture elements (title, description)
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Type alias for entity types
EntityType = Literal[
    "requirement",
    "principle",
    "decision",
    "architecture_element",
    "architecture_view",
    "architecture_standard",
    "programme",
    "project",
]


# Simple synonym/expansion list. Add more domain‑specific jargon here to
# improve search recall. Each key maps to a list of alternative phrases.
SYNONYMS: dict[str, list[str]] = {
    "mttr": ["mean time to restore", "mean time to repair", "service restoration time"],
    "incident": ["outage", "failure", "major incident"],
    "nps": ["net promoter score", "promoter score"],
    "churn": ["attrition", "customer loss"],
    "latency": ["response time", "p95", "p99"],
    "throughput": ["tps", "rps", "requests per second"],
    "devops": ["cicd", "continuous delivery", "platform engineering"],
    "risk": ["operational risk", "security risk", "risk reduction"],
}


def expand_query(q: str) -> str:
    """Append synonyms for recognised terms to the query string.

    Splits the query on whitespace, looks up each term in the ``SYNONYMS``
    dictionary, and appends any expansions. This ensures that
    ``plainto_tsquery`` sees the expansions as part of the query.
    """
    terms = q.lower().split()
    extras: list[str] = []
    for term in terms:
        if term in SYNONYMS:
            extras.extend(SYNONYMS[term])
    if extras:
        return q + " " + " ".join(extras)
    return q


# ---------------------------------------------------------------------------
# Deterministic ordering + optional filters (requirements/principles/decisions)
# ---------------------------------------------------------------------------
# These three searches are the conveyor's deterministic context source, so the
# same query over the same rows MUST return the same ids in the same order.
# ``ORDER BY score DESC`` alone does not: measured 2026-09-22, 50 hits carried
# only 26 distinct scores (largest tie group 10), and the order inside a tie was
# Postgres' choice. The total order is therefore
#
#     score (clipped to [0,1] and rounded to 4 dp, exactly as the API reports it) DESC,
#     created_at ASC, id ASC
#
# Ordering by the ROUNDED score matters: ``build_search_hits`` re-sorts
# (stably) by the rounded similarity, so ordering SQL by the raw float would let
# two rows whose raw scores differ in the 6th digit tie after rounding and come
# back in raw-score order rather than by the documented tiebreak.
_STABLE_SCORE_SQL = "round(LEAST(GREATEST({expr}, 0), 1)::numeric, 4)"

# Filter name -> SQL predicate template ({a} = the table alias). Every value is
# a BOUND parameter; only these fixed templates are ever interpolated.
_FILTERS: dict[str, dict[str, str]] = {
    "requirement": {
        "project_id": "{a}.project_id = :f_project_id",
        "version_status": "{a}.version_status = :f_version_status",
        "origin_kind": "{a}.origin_kind = :f_origin_kind",
    },
    "principle": {
        # ⛔ No ``project_id``: principles are tenant-wide by design, and
        # ``origin_project_id`` is PROVENANCE, not scope (adjudicated in
        # tests/integration/test_principles.py::test_project_id_is_not_a_principle_filter).
        "version_status": "{a}.version_status = :f_version_status",
        "origin_kind": "{a}.origin_kind = :f_origin_kind",
        "active_only": "{a}.active IS TRUE",
    },
    "decision": {
        "project_id": "{a}.project_id = :f_project_id",
        "status": "{a}.status = :f_status",
        "version_status": "{a}.version_status = :f_version_status",
        "origin_kind": "{a}.origin_kind = :f_origin_kind",
    },
}


def _filter_clause(kind: str, alias: str, filters: dict | None) -> tuple[str, dict]:
    """Return ``(" AND ...", params)`` for the recognised, non-empty filters.

    An unknown filter name raises: silently ignoring it would report a narrowed
    search that narrowed nothing.
    """
    if not filters:
        return "", {}
    allowed = _FILTERS[kind]
    parts: list[str] = []
    params: dict = {}
    for name, value in filters.items():
        if name not in allowed:
            raise ValueError(f"unsupported {kind} search filter: {name}")
        if value is None or value == "" or value is False:
            continue
        parts.append(allowed[name].format(a=alias))
        if name != "active_only":
            params[f"f_{name}"] = str(value)
    if not parts:
        return "", {}
    return "".join(f"\n              AND {p}" for p in parts), params


async def search_light(
    session: AsyncSession,
    q: str,
    tenant_id: str,
    k: int = 10,
    *,
    offset: int = 0,
    filters: dict | None = None,
) -> list[tuple[UUID, float]]:
    """Perform a lightweight full-text search on requirements.

    Returns a list of ``(requirement_id, score)`` tuples sorted by score
    descending. Uses PostgreSQL full-text ranking and trigram similarity.

    Args:
        session: Database session
        q: Search query string
        tenant_id: Tenant/creator ID for multi-tenant isolation (required)
        k: Maximum number of results
        offset: Rows to skip in the stable total order (pagination)
        filters: Optional narrowing filters, see ``_FILTERS`` for the per-kind names
    """
    qx = expand_query(q)
    filter_sql, filter_params = _filter_clause("requirement", "r", filters)

    sql = text(
        """
        WITH params AS (
            SELECT
              plainto_tsquery('english', unaccent(:qx)) AS q_ts,
              unaccent(:qx) AS q_raw
        ),
        scored AS (
            SELECT
              r.id,
              r.created_at,
              ts_rank_cd(r.tsv, p.q_ts) AS fts_score,
              GREATEST(similarity(r.title, p.q_raw), similarity(r.description, p.q_raw)) AS trigram_score
            FROM requirements r
            CROSS JOIN params p
            WHERE r.tenant_id = :tenant_id{filter_sql}
              AND (r.tsv @@ p.q_ts
               OR r.title % p.q_raw
               OR r.description % p.q_raw)
        )
        SELECT id,
               {score} AS score
        FROM scored
        ORDER BY score DESC, created_at ASC, id ASC
        LIMIT :k OFFSET :offset
        """.format(
            filter_sql=filter_sql,
            score=_STABLE_SCORE_SQL.format(expr="(0.6 * fts_score + 0.4 * trigram_score)"),
        )
    )
    rows = await session.execute(
        sql,
        {"qx": qx, "k": k, "offset": max(0, int(offset)), "tenant_id": tenant_id, **filter_params},
    )

    return [(UUID(str(rid)), float(score)) for rid, score in rows.all()]


async def search_principles(
    session: AsyncSession,
    q: str,
    tenant_id: str,
    k: int = 10,
    *,
    offset: int = 0,
    filters: dict | None = None,
) -> list[tuple[UUID, float]]:
    """Perform a lightweight full-text search on principles.

    Returns a list of ``(principle_id, score)`` tuples sorted by score
    descending. Searches across title, statement, rationale.

    Args:
        session: Database session
        q: Search query string
        tenant_id: Tenant/creator ID for multi-tenant isolation (required)
        k: Maximum number of results
        offset: Rows to skip in the stable total order (pagination)
        filters: Optional narrowing filters, see ``_FILTERS`` for the per-kind names
    """
    qx = expand_query(q)
    filter_sql, filter_params = _filter_clause("principle", "p", filters)

    sql = text(
        """
        WITH params AS (
            SELECT
              plainto_tsquery('english', unaccent(:qx)) AS q_ts,
              unaccent(:qx) AS q_raw
        ),
        scored AS (
            SELECT
              p.id,
              p.created_at,
              ts_rank_cd(p.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(p.title, params.q_raw),
                similarity(p.statement, params.q_raw),
                similarity(coalesce(p.rationale, ''), params.q_raw)
              ) AS trigram_score
            FROM principles p
            CROSS JOIN params
            WHERE p.tenant_id = :tenant_id{filter_sql}
              AND (p.tsv @@ params.q_ts
               OR p.title % params.q_raw
               OR p.statement % params.q_raw
               OR coalesce(p.rationale, '') % params.q_raw)
        )
        SELECT id,
               {score} AS score
        FROM scored
        ORDER BY score DESC, created_at ASC, id ASC
        LIMIT :k OFFSET :offset
        """.format(
            filter_sql=filter_sql,
            score=_STABLE_SCORE_SQL.format(expr="(0.6 * fts_score + 0.4 * trigram_score)"),
        )
    )
    rows = await session.execute(
        sql,
        {"qx": qx, "k": k, "offset": max(0, int(offset)), "tenant_id": tenant_id, **filter_params},
    )

    return [(UUID(str(pid)), float(score)) for pid, score in rows.all()]


async def search_decisions(
    session: AsyncSession,
    q: str,
    tenant_id: str,
    k: int = 10,
    *,
    offset: int = 0,
    filters: dict | None = None,
) -> list[tuple[UUID, float]]:
    """Perform a lightweight full-text search on decisions.

    Returns a list of ``(decision_id, score)`` tuples sorted by score
    descending. Searches across title, context, decision, consequences.

    Args:
        session: Database session
        q: Search query string
        tenant_id: Tenant/creator ID for multi-tenant isolation (required)
        k: Maximum number of results
        offset: Rows to skip in the stable total order (pagination)
        filters: Optional narrowing filters, see ``_FILTERS`` for the per-kind names
    """
    qx = expand_query(q)
    filter_sql, filter_params = _filter_clause("decision", "d", filters)

    sql = text(
        """
        WITH params AS (
            SELECT
              plainto_tsquery('english', unaccent(:qx)) AS q_ts,
              unaccent(:qx) AS q_raw
        ),
        scored AS (
            SELECT
              d.id,
              d.created_at,
              ts_rank_cd(d.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(d.title, params.q_raw),
                similarity(d.context, params.q_raw),
                similarity(d.decision, params.q_raw),
                similarity(coalesce(d.consequences, ''), params.q_raw)
              ) AS trigram_score
            FROM decisions d
            CROSS JOIN params
            WHERE d.tenant_id = :tenant_id{filter_sql}
              AND (d.tsv @@ params.q_ts
               OR d.title % params.q_raw
               OR d.context % params.q_raw
               OR d.decision % params.q_raw
               OR coalesce(d.consequences, '') % params.q_raw)
        )
        SELECT id,
               {score} AS score
        FROM scored
        ORDER BY score DESC, created_at ASC, id ASC
        LIMIT :k OFFSET :offset
        """.format(
            filter_sql=filter_sql,
            score=_STABLE_SCORE_SQL.format(expr="(0.6 * fts_score + 0.4 * trigram_score)"),
        )
    )
    rows = await session.execute(
        sql,
        {"qx": qx, "k": k, "offset": max(0, int(offset)), "tenant_id": tenant_id, **filter_params},
    )

    return [(UUID(str(did)), float(score)) for did, score in rows.all()]


async def search_architecture_elements(
    session: AsyncSession, q: str, tenant_id: str, k: int = 10
) -> list[tuple[UUID, float]]:
    """Perform a lightweight full-text search on architecture elements.

    Returns a list of ``(element_id, score)`` tuples sorted by score
    descending. Searches across title and description.

    Args:
        session: Database session
        q: Search query string
        tenant_id: Tenant/creator ID for multi-tenant isolation (required)
        k: Maximum number of results
    """
    qx = expand_query(q)

    sql = text(
        """
        WITH params AS (
            SELECT
              plainto_tsquery('english', unaccent(:qx)) AS q_ts,
              unaccent(:qx) AS q_raw
        ),
        scored AS (
            SELECT
              ae.id,
              ts_rank_cd(ae.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(ae.title, params.q_raw),
                similarity(coalesce(ae.description, ''), params.q_raw)
              ) AS trigram_score
            FROM architecture_elements ae
            CROSS JOIN params
            WHERE ae.tenant_id = :tenant_id
              AND (ae.tsv @@ params.q_ts
               OR ae.title % params.q_raw
               OR coalesce(ae.description, '') % params.q_raw)
        )
        SELECT id,
               (0.6 * fts_score + 0.4 * trigram_score) AS score
        FROM scored
        ORDER BY score DESC
        LIMIT :k
        """
    )
    rows = await session.execute(sql, {"qx": qx, "k": k, "tenant_id": tenant_id})

    return [(UUID(str(eid)), float(score)) for eid, score in rows.all()]


async def search_architecture_views(
    session: AsyncSession, q: str, tenant_id: str, k: int = 10
) -> list[tuple[UUID, float]]:
    """Full-text search on architecture views (title, description)."""
    qx = expand_query(q)

    sql = text(
        """
        WITH params AS (
            SELECT
              plainto_tsquery('english', unaccent(:qx)) AS q_ts,
              unaccent(:qx) AS q_raw
        ),
        scored AS (
            SELECT
              av.id,
              ts_rank_cd(av.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(av.title, params.q_raw),
                similarity(coalesce(av.description, ''), params.q_raw)
              ) AS trigram_score
            FROM architecture_views av
            CROSS JOIN params
            WHERE av.tenant_id = :tenant_id
              AND (av.tsv @@ params.q_ts
               OR av.title % params.q_raw
               OR coalesce(av.description, '') % params.q_raw)
        )
        SELECT id,
               (0.6 * fts_score + 0.4 * trigram_score) AS score
        FROM scored
        ORDER BY score DESC
        LIMIT :k
        """
    )
    rows = await session.execute(sql, {"qx": qx, "k": k, "tenant_id": tenant_id})

    return [(UUID(str(vid)), float(score)) for vid, score in rows.all()]


async def search_architecture_standards(
    session: AsyncSession, q: str, tenant_id: str, k: int = 10
) -> list[tuple[UUID, float]]:
    """Full-text search on architecture standards (title, description)."""
    qx = expand_query(q)

    sql = text(
        """
        WITH params AS (
            SELECT
              plainto_tsquery('english', unaccent(:qx)) AS q_ts,
              unaccent(:qx) AS q_raw
        ),
        scored AS (
            SELECT
              s.id,
              ts_rank_cd(s.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(s.title, params.q_raw),
                similarity(coalesce(s.description, ''), params.q_raw)
              ) AS trigram_score
            FROM architecture_standards s
            CROSS JOIN params
            WHERE s.tenant_id = :tenant_id
              AND (s.tsv @@ params.q_ts
               OR s.title % params.q_raw
               OR coalesce(s.description, '') % params.q_raw)
        )
        SELECT id,
               (0.6 * fts_score + 0.4 * trigram_score) AS score
        FROM scored
        ORDER BY score DESC
        LIMIT :k
        """
    )
    rows = await session.execute(sql, {"qx": qx, "k": k, "tenant_id": tenant_id})

    return [(UUID(str(sid)), float(score)) for sid, score in rows.all()]


async def search_programmes(
    session: AsyncSession, q: str, tenant_id: str, k: int = 10
) -> list[tuple[UUID, float]]:
    """Full-text search on programmes (title, description)."""
    qx = expand_query(q)

    sql = text(
        """
        WITH params AS (
            SELECT
              plainto_tsquery('english', unaccent(:qx)) AS q_ts,
              unaccent(:qx) AS q_raw
        ),
        scored AS (
            SELECT
              pg.id,
              ts_rank_cd(pg.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(pg.title, params.q_raw),
                similarity(coalesce(pg.description, ''), params.q_raw)
              ) AS trigram_score
            FROM pm_programmes pg
            CROSS JOIN params
            WHERE pg.tenant_id = :tenant_id
              AND (pg.tsv @@ params.q_ts
               OR pg.title % params.q_raw
               OR coalesce(pg.description, '') % params.q_raw)
        )
        SELECT id,
               (0.6 * fts_score + 0.4 * trigram_score) AS score
        FROM scored
        ORDER BY score DESC
        LIMIT :k
        """
    )
    rows = await session.execute(sql, {"qx": qx, "k": k, "tenant_id": tenant_id})

    return [(UUID(str(pid)), float(score)) for pid, score in rows.all()]


async def search_projects(
    session: AsyncSession, q: str, tenant_id: str, k: int = 10
) -> list[tuple[UUID, float]]:
    """Full-text search on projects (title, description)."""
    qx = expand_query(q)

    sql = text(
        """
        WITH params AS (
            SELECT
              plainto_tsquery('english', unaccent(:qx)) AS q_ts,
              unaccent(:qx) AS q_raw
        ),
        scored AS (
            SELECT
              pj.id,
              ts_rank_cd(pj.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(pj.title, params.q_raw),
                similarity(coalesce(pj.description, ''), params.q_raw)
              ) AS trigram_score
            FROM pm_projects pj
            CROSS JOIN params
            WHERE pj.tenant_id = :tenant_id
              AND (pj.tsv @@ params.q_ts
               OR pj.title % params.q_raw
               OR coalesce(pj.description, '') % params.q_raw)
        )
        SELECT id,
               (0.6 * fts_score + 0.4 * trigram_score) AS score
        FROM scored
        ORDER BY score DESC
        LIMIT :k
        """
    )
    rows = await session.execute(sql, {"qx": qx, "k": k, "tenant_id": tenant_id})

    return [(UUID(str(pid)), float(score)) for pid, score in rows.all()]


async def search_all(
    session: AsyncSession, q: str, tenant_id: str, k: int = 10
) -> list[tuple[EntityType, UUID, float]]:
    """Perform a unified search across requirements, principles, decisions, and architecture elements.

    Returns a list of ``(entity_type, entity_id, score)`` tuples sorted by score
    descending. This allows finding the most relevant content regardless of type.

    Args:
        session: Database session
        q: Search query string
        tenant_id: Tenant/creator ID for multi-tenant isolation (required)
        k: Maximum number of results to return (default: 10)

    Returns:
        List of (entity_type, entity_id, score) tuples, where:
        - entity_type is 'requirement', 'principle', 'decision', or 'architecture_element'
        - entity_id is the database ID of the entity
        - score is the relevance score (higher is more relevant)
    """
    qx = expand_query(q)

    sql = text(
        """
        WITH params AS (
            SELECT
              plainto_tsquery('english', unaccent(:qx)) AS q_ts,
              unaccent(:qx) AS q_raw
        ),
        requirements_scored AS (
            SELECT
              'requirement'::text AS entity_type,
              r.id,
              ts_rank_cd(r.tsv, p.q_ts) AS fts_score,
              GREATEST(similarity(r.title, p.q_raw), similarity(r.description, p.q_raw)) AS trigram_score
            FROM requirements r
            CROSS JOIN params p
            WHERE r.tenant_id = :tenant_id
              AND (r.tsv @@ p.q_ts
               OR r.title % p.q_raw
               OR r.description % p.q_raw)
        ),
        principles_scored AS (
            SELECT
              'principle'::text AS entity_type,
              p.id,
              ts_rank_cd(p.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(p.title, params.q_raw),
                similarity(p.statement, params.q_raw),
                similarity(coalesce(p.rationale, ''), params.q_raw)
              ) AS trigram_score
            FROM principles p
            CROSS JOIN params
            WHERE p.tenant_id = :tenant_id
              AND (p.tsv @@ params.q_ts
               OR p.title % params.q_raw
               OR p.statement % params.q_raw
               OR coalesce(p.rationale, '') % params.q_raw)
        ),
        decisions_scored AS (
            SELECT
              'decision'::text AS entity_type,
              d.id,
              ts_rank_cd(d.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(d.title, params.q_raw),
                similarity(d.context, params.q_raw),
                similarity(d.decision, params.q_raw),
                similarity(coalesce(d.consequences, ''), params.q_raw)
              ) AS trigram_score
            FROM decisions d
            CROSS JOIN params
            WHERE d.tenant_id = :tenant_id
              AND (d.tsv @@ params.q_ts
               OR d.title % params.q_raw
               OR d.context % params.q_raw
               OR d.decision % params.q_raw
               OR coalesce(d.consequences, '') % params.q_raw)
        ),
        architecture_elements_scored AS (
            SELECT
              'architecture_element'::text AS entity_type,
              ae.id,
              ts_rank_cd(ae.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(ae.title, params.q_raw),
                similarity(coalesce(ae.description, ''), params.q_raw)
              ) AS trigram_score
            FROM architecture_elements ae
            CROSS JOIN params
            WHERE ae.tenant_id = :tenant_id
              AND (ae.tsv @@ params.q_ts
               OR ae.title % params.q_raw
               OR coalesce(ae.description, '') % params.q_raw)
        ),
        architecture_views_scored AS (
            SELECT
              'architecture_view'::text AS entity_type,
              av.id,
              ts_rank_cd(av.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(av.title, params.q_raw),
                similarity(coalesce(av.description, ''), params.q_raw)
              ) AS trigram_score
            FROM architecture_views av
            CROSS JOIN params
            WHERE av.tenant_id = :tenant_id
              AND (av.tsv @@ params.q_ts
               OR av.title % params.q_raw
               OR coalesce(av.description, '') % params.q_raw)
        ),
        architecture_standards_scored AS (
            SELECT
              'architecture_standard'::text AS entity_type,
              s.id,
              ts_rank_cd(s.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(s.title, params.q_raw),
                similarity(coalesce(s.description, ''), params.q_raw)
              ) AS trigram_score
            FROM architecture_standards s
            CROSS JOIN params
            WHERE s.tenant_id = :tenant_id
              AND (s.tsv @@ params.q_ts
               OR s.title % params.q_raw
               OR coalesce(s.description, '') % params.q_raw)
        ),
        programmes_scored AS (
            SELECT
              'programme'::text AS entity_type,
              pg.id,
              ts_rank_cd(pg.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(pg.title, params.q_raw),
                similarity(coalesce(pg.description, ''), params.q_raw)
              ) AS trigram_score
            FROM pm_programmes pg
            CROSS JOIN params
            WHERE pg.tenant_id = :tenant_id
              AND (pg.tsv @@ params.q_ts
               OR pg.title % params.q_raw
               OR coalesce(pg.description, '') % params.q_raw)
        ),
        projects_scored AS (
            SELECT
              'project'::text AS entity_type,
              pj.id,
              ts_rank_cd(pj.tsv, params.q_ts) AS fts_score,
              GREATEST(
                similarity(pj.title, params.q_raw),
                similarity(coalesce(pj.description, ''), params.q_raw)
              ) AS trigram_score
            FROM pm_projects pj
            CROSS JOIN params
            WHERE pj.tenant_id = :tenant_id
              AND (pj.tsv @@ params.q_ts
               OR pj.title % params.q_raw
               OR coalesce(pj.description, '') % params.q_raw)
        ),
        all_scored AS (
            SELECT * FROM requirements_scored
            UNION ALL
            SELECT * FROM principles_scored
            UNION ALL
            SELECT * FROM decisions_scored
            UNION ALL
            SELECT * FROM architecture_elements_scored
            UNION ALL
            SELECT * FROM architecture_views_scored
            UNION ALL
            SELECT * FROM architecture_standards_scored
            UNION ALL
            SELECT * FROM programmes_scored
            UNION ALL
            SELECT * FROM projects_scored
        )
        SELECT
            entity_type,
            id,
            (0.6 * fts_score + 0.4 * trigram_score) AS score
        FROM all_scored
        ORDER BY score DESC
        LIMIT :k
        """
    )
    rows = await session.execute(sql, {"qx": qx, "k": k, "tenant_id": tenant_id})

    return [(row[0], UUID(str(row[1])), float(row[2])) for row in rows.all()]
