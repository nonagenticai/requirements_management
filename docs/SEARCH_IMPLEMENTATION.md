# Full-Text Search Implementation for Requirements API

**Implementation Date:** 2025-12-02
**Migration:** `2025_12_02_0007_add_full_text_search_principles_decisions.py`
**Status:** Ready for deployment

---

## Overview

This document describes the implementation of PostgreSQL full-text search with trigram similarity across all entity types in the Requirements API:
- **Requirements** (existing, enhanced)
- **Principles** (new)
- **Decisions** (new)

## Features

### 1. Full-Text Search (FTS)
- Uses PostgreSQL `tsvector` columns with English dictionary
- Leverages `unaccent` extension for diacritic-insensitive matching
- Automatically indexes title, description, and JSONB fields
- Uses `ts_rank_cd` for relevance scoring

### 2. Trigram Similarity Search
- Uses `pg_trgm` extension for fuzzy matching
- Enables typo-tolerant search with the `%` similarity operator
- Supports fast `LIKE/ILIKE` queries via GIN indexes
- Complements FTS for partial matches

### 3. Hybrid Scoring
All search endpoints use a weighted combination:
- **60%** full-text search score
- **40%** trigram similarity score

This balances exact term matching with fuzzy/partial matching.

### 4. Synonym Expansion
The `search_light.py` module includes domain-specific synonym expansion:
```python
SYNONYMS = {
    "mttr": ["mean time to restore", "mean time to repair"],
    "incident": ["outage", "failure", "major incident"],
    "nps": ["net promoter score"],
    # Add more domain-specific terms as needed
}
```

---

## Database Implementation

### Extensions Enabled

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- Trigram matching
CREATE EXTENSION IF NOT EXISTS unaccent;     -- Diacritic removal
```

These were enabled in migration `2025_08_11_0002` and are reused for all tables.

### Indexes Created

#### Principles Table

**Full-Text Search:**
```sql
-- Generated tsvector column
ALTER TABLE principles ADD COLUMN tsv tsvector
GENERATED ALWAYS AS (
    to_tsvector('english',
        coalesce(unaccent(title), '') || ' ' ||
        coalesce(unaccent(statement), '') || ' ' ||
        coalesce(unaccent(rationale), '') || ' ' ||
        -- JSONB arrays expanded
        coalesce((SELECT string_agg(unaccent(elem::text), ' ')
                  FROM jsonb_array_elements_text(implications)), '') || ' ' ||
        coalesce((SELECT string_agg(unaccent(elem::text), ' ')
                  FROM jsonb_array_elements_text(tags)), '')
    )
) STORED;

-- GIN index for fast FTS
CREATE INDEX idx_principles_tsv ON principles USING GIN (tsv);
```

**Trigram Indexes:**
```sql
-- Individual field indexes
CREATE INDEX idx_principles_title_trgm
  ON principles USING GIN (title gin_trgm_ops);

CREATE INDEX idx_principles_statement_trgm
  ON principles USING GIN (statement gin_trgm_ops);

CREATE INDEX idx_principles_rationale_trgm
  ON principles USING GIN (rationale gin_trgm_ops);

-- Combined multi-field index
CREATE INDEX idx_principles_search_trgm
  ON principles USING GIN (
    (title || ' ' || statement || ' ' || coalesce(rationale, ''))
    gin_trgm_ops
  );
```

#### Decisions Table

**Full-Text Search:**
```sql
-- Generated tsvector column
ALTER TABLE decisions ADD COLUMN tsv tsvector
GENERATED ALWAYS AS (
    to_tsvector('english',
        coalesce(unaccent(title), '') || ' ' ||
        coalesce(unaccent(context), '') || ' ' ||
        coalesce(unaccent(decision), '') || ' ' ||
        coalesce(unaccent(consequences), '') || ' ' ||
        -- JSONB array expanded
        coalesce((SELECT string_agg(unaccent(elem::text), ' ')
                  FROM jsonb_array_elements_text(alternatives)), '')
    )
) STORED;

-- GIN index for fast FTS
CREATE INDEX idx_decisions_tsv ON decisions USING GIN (tsv);
```

**Trigram Indexes:**
```sql
-- Individual field indexes
CREATE INDEX idx_decisions_title_trgm
  ON decisions USING GIN (title gin_trgm_ops);

CREATE INDEX idx_decisions_context_trgm
  ON decisions USING GIN (context gin_trgm_ops);

CREATE INDEX idx_decisions_decision_trgm
  ON decisions USING GIN (decision gin_trgm_ops);

CREATE INDEX idx_decisions_consequences_trgm
  ON decisions USING GIN (consequences gin_trgm_ops);

-- Combined multi-field index
CREATE INDEX idx_decisions_search_trgm
  ON decisions USING GIN (
    (title || ' ' || context || ' ' || decision || ' ' || coalesce(consequences, ''))
    gin_trgm_ops
  );
```

---

## API Endpoints

### 1. Search Requirements (Existing, Enhanced)

**Endpoint:** `GET /search`

**Description:** Search requirements by title, description, and acceptance criteria.

**Parameters:**
- `q` (string, required): Search query
- `k` (int, optional): Maximum results (default: 10, max: 100)

**Response:** `List[SearchHit]`
```json
[
  {
    "id": 1,
    "slug": "user-authentication",
    "title": "User Authentication",
    "snippet": "Implement secure user authentication using...",
    "similarity": 0.8523
  }
]
```

**Example:**
```bash
curl -X GET "http://localhost:8090/search?q=authentication&k=5" \
  -H "Authorization: Bearer $TOKEN"
```

---

### 2. Search Principles (New)

**Endpoint:** `GET /search/principles`

**Description:** Search architecture principles by title, statement, rationale, implications, and tags.

**Parameters:**
- `q` (string, required): Search query
- `k` (int, optional): Maximum results (default: 10, max: 100)

**Response:** `List[SearchHit]`
```json
[
  {
    "id": 2,
    "slug": "everything-computable",
    "title": "Everything Computable Shall Be Computed",
    "snippet": "Automate all tasks that can be automated to reduce manual effort...",
    "similarity": 0.7891
  }
]
```

**Example:**
```bash
curl -X GET "http://localhost:8090/search/principles?q=automation&k=5" \
  -H "Authorization: Bearer $TOKEN"
```

---

### 3. Search Decisions (New)

**Endpoint:** `GET /search/decisions`

**Description:** Search architecture decisions by title, context, decision, consequences, and alternatives.

**Parameters:**
- `q` (string, required): Search query
- `k` (int, optional): Maximum results (default: 10, max: 100)

**Response:** `List[SearchHit]`
```json
[
  {
    "id": 3,
    "slug": "use-postgresql-for-storage",
    "title": "Use PostgreSQL for Primary Storage",
    "snippet": "We need a reliable, ACID-compliant database for our application...",
    "similarity": 0.8234
  }
]
```

**Example:**
```bash
curl -X GET "http://localhost:8090/search/decisions?q=database&k=5" \
  -H "Authorization: Bearer $TOKEN"
```

---

### 4. Unified Search Across All Entities (New)

**Endpoint:** `GET /search/all`

**Description:** Search across requirements, principles, AND decisions simultaneously. Returns the most relevant results regardless of entity type.

**Parameters:**
- `q` (string, required): Search query
- `k` (int, optional): Maximum results (default: 10, max: 100)

**Response:** `List[UnifiedSearchHit]`
```json
[
  {
    "entity_type": "principle",
    "id": 2,
    "slug": "api-first-design",
    "title": "API-First Design",
    "snippet": "All features must be accessible via documented APIs...",
    "similarity": 0.8523
  },
  {
    "entity_type": "decision",
    "id": 5,
    "slug": "rest-api-design",
    "title": "Use RESTful API Design",
    "snippet": "We will use REST principles for our API design...",
    "similarity": 0.7891
  },
  {
    "entity_type": "requirement",
    "id": 12,
    "slug": "api-authentication",
    "title": "API Authentication",
    "snippet": "All API endpoints must require authentication...",
    "similarity": 0.7456
  }
]
```

**Example:**
```bash
curl -X GET "http://localhost:8090/search/all?q=api+design&k=10" \
  -H "Authorization: Bearer $TOKEN"
```

---

## Search Function Details

### `search_light(session, q, k)`
Search requirements only (existing function).

**Returns:** `List[Tuple[int, float]]` - List of (requirement_id, score) tuples.

---

### `search_principles(session, q, k)`
Search principles only (new function).

**Returns:** `List[Tuple[int, float]]` - List of (principle_id, score) tuples.

**Searches across:**
- title
- statement
- rationale
- implications (JSONB array)
- tags (JSONB array)

---

### `search_decisions(session, q, k)`
Search decisions only (new function).

**Returns:** `List[Tuple[int, float]]` - List of (decision_id, score) tuples.

**Searches across:**
- title
- context
- decision
- consequences
- alternatives (JSONB array)

---

### `search_all(session, q, k)`
Unified search across all entities (new function).

**Returns:** `List[Tuple[EntityType, int, float]]` - List of (entity_type, entity_id, score) tuples.

**Entity types:** `"requirement"`, `"principle"`, `"decision"`

**SQL Strategy:** Uses `UNION ALL` to combine scored results from all three tables, then sorts by score.

---

## Deployment Instructions

### 1. Apply the Migration

**Using Docker (recommended):**

```bash
cd /Users/mini1/Movies/requirements_api

# Access the running container
docker exec -it requirements-api bash

# Run the migration
cd /code
alembic upgrade head
```

**Using local environment:**

```bash
cd /Users/mini1/Movies/requirements_api

# Activate virtual environment (if using venv)
source venv/bin/activate

# Or use uv (if available)
uv run alembic upgrade head
```

### 2. Verify Migration

Connect to PostgreSQL and verify the indexes:

```bash
# Access PostgreSQL via pgbouncer
psql -h pgbouncer.example.com -p 6432 -U requirements_user -d requirements_api

# Check indexes on principles table
\d principles

# Check indexes on decisions table
\d decisions

# Expected columns: tsv (tsvector)
# Expected indexes:
#   - idx_principles_tsv (GIN on tsv)
#   - idx_principles_title_trgm (GIN on title)
#   - idx_principles_statement_trgm (GIN on statement)
#   - idx_principles_rationale_trgm (GIN on rationale)
#   - idx_principles_search_trgm (GIN on combined fields)
#   - idx_decisions_tsv (GIN on tsv)
#   - idx_decisions_title_trgm (GIN on title)
#   - idx_decisions_context_trgm (GIN on context)
#   - idx_decisions_decision_trgm (GIN on decision)
#   - idx_decisions_consequences_trgm (GIN on consequences)
#   - idx_decisions_search_trgm (GIN on combined fields)
```

### 3. Test the Search Endpoints

**Test Requirements Search:**
```bash
curl -X GET "http://localhost:8090/search?q=authentication" \
  -H "Authorization: Bearer $TOKEN"
```

**Test Principles Search:**
```bash
curl -X GET "http://localhost:8090/search/principles?q=automation" \
  -H "Authorization: Bearer $TOKEN"
```

**Test Decisions Search:**
```bash
curl -X GET "http://localhost:8090/search/decisions?q=database" \
  -H "Authorization: Bearer $TOKEN"
```

**Test Unified Search:**
```bash
curl -X GET "http://localhost:8090/search/all?q=api+design" \
  -H "Authorization: Bearer $TOKEN"
```

---

## Performance Characteristics

### Index Sizes (Estimated)

For a typical dataset:
- **tsvector columns:** ~30-40% of text content size
- **GIN indexes:** ~2-3x the column size
- **Trigram indexes:** ~3-4x the column size

**Example:** For 1000 principles with average 500 characters each:
- Text data: ~500 KB
- tsvector: ~175 KB
- FTS GIN index: ~350 KB
- Trigram indexes (4 total): ~2 MB
- **Total:** ~2.5 MB per 1000 principles

### Query Performance

With proper indexes, search queries should complete in:
- **< 10ms** for small datasets (< 1000 records)
- **< 50ms** for medium datasets (1000-10,000 records)
- **< 200ms** for large datasets (10,000-100,000 records)

Performance degrades gracefully with dataset size due to GIN index efficiency.

---

## Maintenance

### Updating Synonyms

Edit `/app/search_light.py` and add new synonym mappings:

```python
SYNONYMS = {
    "mttr": ["mean time to restore", "mean time to repair"],
    "your_term": ["expansion1", "expansion2"],
}
```

No database changes needed - synonyms are expanded at query time.

### Monitoring Index Health

```sql
-- Check index sizes
SELECT
    schemaname,
    tablename,
    indexname,
    pg_size_pretty(pg_relation_size(indexname::regclass)) AS index_size
FROM pg_indexes
WHERE tablename IN ('requirements', 'principles', 'decisions')
ORDER BY pg_relation_size(indexname::regclass) DESC;

-- Check index usage
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch
FROM pg_stat_user_indexes
WHERE tablename IN ('requirements', 'principles', 'decisions')
ORDER BY idx_scan DESC;
```

### Rebuilding Indexes (if needed)

```sql
-- Reindex all search indexes
REINDEX TABLE principles;
REINDEX TABLE decisions;
REINDEX TABLE requirements;
```

---

## Future Enhancements

### 1. Advanced Ranking
- Add document length normalization
- Implement BM25 scoring
- Add recency boosting for newer documents

### 2. Search Analytics
- Log search queries and results
- Track click-through rates
- Identify popular search terms
- Detect zero-result queries

### 3. Query Suggestions
- Implement "Did you mean...?" suggestions using trigram distance
- Add autocomplete based on existing titles
- Suggest related searches

### 4. Faceted Search
- Filter by entity type
- Filter by version status
- Filter by project_id
- Filter by tags (for principles)
- Filter by status (for decisions)

### 5. Highlighting
- Return search term highlights in snippets
- Use `ts_headline()` for better snippets
- Highlight matching terms in UI

---

## Rollback Procedure

If needed, rollback the migration:

```bash
# Using Docker
docker exec -it requirements-api bash
cd /code
alembic downgrade -1

# Or using local environment
cd /Users/mini1/Movies/requirements_api
alembic downgrade -1
```

This will:
- Drop all search indexes
- Remove tsvector columns
- Keep the tables intact (no data loss)

---

## Support

For issues or questions:
1. Check the migration file: `alembic/versions/2025_12_02_0007_add_full_text_search_principles_decisions.py`
2. Review search functions in: `app/search_light.py`
3. Check API endpoints in: `app/main.py` (lines 566-771)
4. Review schemas in: `app/schemas.py` (UnifiedSearchHit)

---

## Summary

This implementation provides:
✅ Full-text search across all entity types
✅ Fuzzy/typo-tolerant matching with trigrams
✅ Unified search across requirements, principles, and decisions
✅ Synonym expansion for domain-specific terms
✅ Hybrid scoring (FTS + trigram similarity)
✅ Efficient GIN indexes for fast queries
✅ Creator-filtered results (respects JWT authentication)
✅ Comprehensive API endpoints with consistent responses

**Ready for deployment and use in production!**
