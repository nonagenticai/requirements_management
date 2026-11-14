# PostgreSQL Full-Text Search - SQL Usage Examples

This document provides SQL examples for using the full-text search features directly in PostgreSQL.

---

## Basic Search Queries

### 1. Full-Text Search on Requirements

```sql
-- Simple FTS query
SELECT
    id,
    title,
    ts_rank_cd(tsv, plainto_tsquery('english', 'authentication')) AS rank
FROM requirements
WHERE tsv @@ plainto_tsquery('english', 'authentication')
ORDER BY rank DESC
LIMIT 10;
```

### 2. Full-Text Search on Principles

```sql
-- Search principles
SELECT
    id,
    title,
    statement,
    ts_rank_cd(tsv, plainto_tsquery('english', 'automation')) AS rank
FROM principles
WHERE tsv @@ plainto_tsquery('english', 'automation')
ORDER BY rank DESC
LIMIT 10;
```

### 3. Full-Text Search on Decisions

```sql
-- Search decisions
SELECT
    id,
    title,
    context,
    ts_rank_cd(tsv, plainto_tsquery('english', 'database')) AS rank
FROM decisions
WHERE tsv @@ plainto_tsquery('english', 'database')
ORDER BY rank DESC
LIMIT 10;
```

---

## Trigram Similarity Queries

### 4. Fuzzy Match on Title

```sql
-- Find principles with titles similar to "automate"
SELECT
    id,
    title,
    similarity(title, 'automate') AS sim_score
FROM principles
WHERE title % 'automate'  -- The % operator means "is similar to"
ORDER BY sim_score DESC
LIMIT 10;
```

### 5. Multi-Field Trigram Search

```sql
-- Search across multiple fields
SELECT
    id,
    title,
    GREATEST(
        similarity(title, 'container orchestration'),
        similarity(statement, 'container orchestration'),
        similarity(coalesce(rationale, ''), 'container orchestration')
    ) AS best_sim
FROM principles
WHERE title % 'container orchestration'
   OR statement % 'container orchestration'
   OR coalesce(rationale, '') % 'container orchestration'
ORDER BY best_sim DESC
LIMIT 10;
```

---

## Combined FTS + Trigram Queries

### 6. Hybrid Search (Recommended)

```sql
-- Combines full-text and trigram scores
WITH params AS (
    SELECT
        plainto_tsquery('english', unaccent('kubernetes')) AS q_ts,
        unaccent('kubernetes') AS q_raw
)
SELECT
    p.id,
    p.title,
    p.statement,
    ts_rank_cd(p.tsv, params.q_ts) AS fts_score,
    GREATEST(
        similarity(p.title, params.q_raw),
        similarity(p.statement, params.q_raw),
        similarity(coalesce(p.rationale, ''), params.q_raw)
    ) AS trigram_score,
    (0.6 * ts_rank_cd(p.tsv, params.q_ts) +
     0.4 * GREATEST(
        similarity(p.title, params.q_raw),
        similarity(p.statement, params.q_raw),
        similarity(coalesce(p.rationale, ''), params.q_raw)
     )) AS combined_score
FROM principles p
CROSS JOIN params
WHERE p.tsv @@ params.q_ts
   OR p.title % params.q_raw
   OR p.statement % params.q_raw
   OR coalesce(p.rationale, '') % params.q_raw
ORDER BY combined_score DESC
LIMIT 10;
```

---

## Advanced Queries

### 7. Search with Highlighting

```sql
-- Show search results with highlighted matches
SELECT
    id,
    title,
    ts_headline(
        'english',
        statement,
        plainto_tsquery('english', 'docker'),
        'StartSel=<mark>, StopSel=</mark>, MaxWords=50, MinWords=20'
    ) AS highlighted_snippet
FROM principles
WHERE tsv @@ plainto_tsquery('english', 'docker')
ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', 'docker')) DESC
LIMIT 10;
```

### 8. Multi-Word Query with Phrase Search

```sql
-- Search for exact phrases
SELECT
    id,
    title,
    description,
    ts_rank_cd(tsv, phraseto_tsquery('english', 'user authentication')) AS rank
FROM requirements
WHERE tsv @@ phraseto_tsquery('english', 'user authentication')
ORDER BY rank DESC
LIMIT 10;
```

### 9. Boolean Search Queries

```sql
-- AND query: must contain both terms
SELECT
    id,
    title,
    ts_rank_cd(tsv, to_tsquery('english', 'docker & kubernetes')) AS rank
FROM decisions
WHERE tsv @@ to_tsquery('english', 'docker & kubernetes')
ORDER BY rank DESC;

-- OR query: contains either term
SELECT
    id,
    title,
    ts_rank_cd(tsv, to_tsquery('english', 'docker | kubernetes')) AS rank
FROM decisions
WHERE tsv @@ to_tsquery('english', 'docker | kubernetes')
ORDER BY rank DESC;

-- NOT query: contains first term but not second
SELECT
    id,
    title,
    ts_rank_cd(tsv, to_tsquery('english', 'docker & !kubernetes')) AS rank
FROM decisions
WHERE tsv @@ to_tsquery('english', 'docker & !kubernetes')
ORDER BY rank DESC;
```

### 10. Unified Search Across All Tables

```sql
-- Search all entity types with unified scoring
WITH params AS (
    SELECT
        plainto_tsquery('english', unaccent('api design')) AS q_ts,
        unaccent('api design') AS q_raw
),
requirements_scored AS (
    SELECT
        'requirement'::text AS entity_type,
        r.id,
        r.slug,
        r.title,
        r.description AS content,
        ts_rank_cd(r.tsv, p.q_ts) AS fts_score,
        GREATEST(
            similarity(r.title, p.q_raw),
            similarity(r.description, p.q_raw)
        ) AS trigram_score
    FROM requirements r
    CROSS JOIN params p
    WHERE r.tsv @@ p.q_ts
       OR r.title % p.q_raw
       OR r.description % p.q_raw
),
principles_scored AS (
    SELECT
        'principle'::text AS entity_type,
        p.id,
        p.slug,
        p.title,
        p.statement AS content,
        ts_rank_cd(p.tsv, params.q_ts) AS fts_score,
        GREATEST(
            similarity(p.title, params.q_raw),
            similarity(p.statement, params.q_raw),
            similarity(coalesce(p.rationale, ''), params.q_raw)
        ) AS trigram_score
    FROM principles p
    CROSS JOIN params
    WHERE p.tsv @@ params.q_ts
       OR p.title % params.q_raw
       OR p.statement % params.q_raw
       OR coalesce(p.rationale, '') % params.q_raw
),
decisions_scored AS (
    SELECT
        'decision'::text AS entity_type,
        d.id,
        d.slug,
        d.title,
        d.context AS content,
        ts_rank_cd(d.tsv, params.q_ts) AS fts_score,
        GREATEST(
            similarity(d.title, params.q_raw),
            similarity(d.context, params.q_raw),
            similarity(d.decision, params.q_raw),
            similarity(coalesce(d.consequences, ''), params.q_raw)
        ) AS trigram_score
    FROM decisions d
    CROSS JOIN params
    WHERE d.tsv @@ params.q_ts
       OR d.title % params.q_raw
       OR d.context % params.q_raw
       OR d.decision % params.q_raw
       OR coalesce(d.consequences, '') % params.q_raw
),
all_scored AS (
    SELECT * FROM requirements_scored
    UNION ALL
    SELECT * FROM principles_scored
    UNION ALL
    SELECT * FROM decisions_scored
)
SELECT
    entity_type,
    id,
    slug,
    title,
    substring(content from 1 for 200) AS snippet,
    (0.6 * fts_score + 0.4 * trigram_score) AS score
FROM all_scored
ORDER BY score DESC
LIMIT 10;
```

---

## Analyzing Search Performance

### 11. Check Index Usage

```sql
-- Show which indexes are being used
EXPLAIN ANALYZE
SELECT
    id,
    title,
    ts_rank_cd(tsv, plainto_tsquery('english', 'docker')) AS rank
FROM decisions
WHERE tsv @@ plainto_tsquery('english', 'docker')
ORDER BY rank DESC
LIMIT 10;
```

### 12. Index Statistics

```sql
-- Show index scan counts
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch
FROM pg_stat_user_indexes
WHERE tablename IN ('requirements', 'principles', 'decisions')
  AND indexname LIKE '%search%'
ORDER BY idx_scan DESC;
```

---

## Similarity Threshold Tuning

### 13. Set Custom Similarity Threshold

```sql
-- Default similarity threshold is 0.3
-- You can adjust it per query:
SET pg_trgm.similarity_threshold = 0.5;

SELECT
    id,
    title,
    similarity(title, 'automation') AS sim
FROM principles
WHERE title % 'automation'
ORDER BY sim DESC;

-- Reset to default
SET pg_trgm.similarity_threshold = 0.3;
```

### 14. Find Optimal Threshold

```sql
-- Test different thresholds
WITH test_query AS (
    SELECT 'container orchestration' AS q
),
results AS (
    SELECT
        p.id,
        p.title,
        similarity(p.title, tq.q) AS sim
    FROM principles p
    CROSS JOIN test_query tq
    WHERE similarity(p.title, tq.q) > 0
)
SELECT
    COUNT(*) FILTER (WHERE sim >= 0.7) AS matches_70,
    COUNT(*) FILTER (WHERE sim >= 0.5) AS matches_50,
    COUNT(*) FILTER (WHERE sim >= 0.3) AS matches_30,
    COUNT(*) FILTER (WHERE sim >= 0.1) AS matches_10,
    COUNT(*) AS total_with_similarity
FROM results;
```

---

## Debugging and Diagnostics

### 15. View Parsed Query

```sql
-- See how PostgreSQL parses your search query
SELECT plainto_tsquery('english', 'user authentication system');
-- Result: 'user' & 'authent' & 'system'

SELECT to_tsquery('english', 'docker & kubernetes');
-- Result: 'docker' & 'kubernet'

SELECT phraseto_tsquery('english', 'machine learning algorithm');
-- Result: 'machin' <-> 'learn' <-> 'algorithm'
```

### 16. Inspect tsvector Content

```sql
-- See what's indexed in a tsvector column
SELECT
    id,
    title,
    tsv
FROM principles
WHERE id = 1;

-- Example output:
-- tsv: 'automat':3,15 'effort':9 'manual':8 'reduc':7 'task':5
```

### 17. Test Trigram Similarity

```sql
-- Compare different strings
SELECT
    similarity('kubernetes', 'kubernetes') AS exact_match,        -- 1.0
    similarity('kubernetes', 'kubernete') AS one_char_off,        -- ~0.9
    similarity('kubernetes', 'container') AS unrelated,           -- ~0.1
    similarity('kubernetes', 'k8s') AS abbreviation;              -- ~0.3
```

---

## Performance Tips

1. **Use the right query type:**
   - `plainto_tsquery()`: Simple queries, automatic AND between words
   - `phraseto_tsquery()`: Exact phrase matching
   - `to_tsquery()`: Boolean queries with AND/OR/NOT

2. **Combine FTS and trigram for best results:**
   - FTS: Fast exact term matching
   - Trigram: Handles typos and partial matches

3. **Use EXPLAIN ANALYZE to verify index usage:**
   ```sql
   EXPLAIN ANALYZE SELECT ... WHERE tsv @@ ...;
   ```
   Look for "Bitmap Index Scan" - good!
   Avoid "Seq Scan" - bad for large tables!

4. **Limit result sets:**
   Always use `LIMIT` to avoid fetching too many results

5. **Use materialized views for complex searches:**
   ```sql
   CREATE MATERIALIZED VIEW search_index AS
   SELECT ... FROM requirements
   UNION ALL
   SELECT ... FROM principles
   UNION ALL
   SELECT ... FROM decisions;

   CREATE INDEX ON search_index USING GIN (tsv);
   ```

---

## Common Patterns

### Search with Filters

```sql
-- Search active principles only
SELECT
    id,
    title,
    ts_rank_cd(tsv, plainto_tsquery('english', 'security')) AS rank
FROM principles
WHERE tsv @@ plainto_tsquery('english', 'security')
  AND active = true
  AND creator_id = 'user_123'
ORDER BY rank DESC
LIMIT 10;
```

### Search with Date Range

```sql
-- Search recent decisions
SELECT
    id,
    title,
    created_at,
    ts_rank_cd(tsv, plainto_tsquery('english', 'database')) AS rank
FROM decisions
WHERE tsv @@ plainto_tsquery('english', 'database')
  AND created_at >= NOW() - INTERVAL '30 days'
ORDER BY rank DESC
LIMIT 10;
```

### Search by Project

```sql
-- Search requirements for specific project
SELECT
    id,
    title,
    project_id,
    ts_rank_cd(tsv, plainto_tsquery('english', 'api')) AS rank
FROM requirements
WHERE tsv @@ plainto_tsquery('english', 'api')
  AND project_id = 'project_xyz'
ORDER BY rank DESC
LIMIT 10;
```

---

## References

- [PostgreSQL Full-Text Search](https://www.postgresql.org/docs/current/textsearch.html)
- [pg_trgm Extension](https://www.postgresql.org/docs/current/pgtrgm.html)
- [unaccent Extension](https://www.postgresql.org/docs/current/unaccent.html)
- [GIN Indexes](https://www.postgresql.org/docs/current/gin.html)

---

**Happy Searching! 🔍**
