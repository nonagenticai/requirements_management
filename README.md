# Requirements Management

Multi-tenant API for managing software requirements, architecture principles, and architecture decision records (ADRs) with full-text search, semantic search, quality scoring, and prioritization. Port 8921.

| | |
|---|---|
| **Stack** | Python 3.14 · FastAPI · SQLAlchemy 2.x async · Pydantic v2 |
| **Database** | PostgreSQL 18 via async psycopg3 (PgBouncer optional in production) |
| **Runs on** | Docker, or any Kubernetes cluster |

## What Requirements Management Does

When a developer or n8n workflow needs to create, update, or query software requirements, architecture principles, or architecture decision records, it sends requests to this service. Each entity is scoped to a tenant resolved from a license token in the `X-Tenant-Id` header, so multiple teams share the same deployment without seeing each other's data. Requirements carry traceability links, acceptance criteria, and numeric fields for prioritization. Principles capture cross-project architectural guidance with an active/inactive lifecycle. Decisions follow the ADR pattern with status tracking through proposed, accepted, deprecated, and superseded states.

Beyond basic CRUD, the service provides two distinct search modes. Full-text search uses PostgreSQL trigram similarity indexes across all three entity types, returning ranked results filtered by tenant. Semantic search takes a different approach, running TF-IDF similarity scoring combined with SpaCy named entity recognition against a seed knowledge base to surface conceptually related content even when exact terms differ. Both search modes support configurable result counts and minimum similarity thresholds.

Each entity type supports semantic versioning with snapshot-based version history. When a principle or decision is updated, the service records a full-field snapshot in an `EntityVersionHistory` table. Any two snapshots can be compared to produce a structured diff showing exactly what changed between versions. Requirements also carry version metadata but use a simpler versioning model with draft, published, and deprecated states. Quality checks analyze requirement text for completeness and clarity, while WSJF and RICE scoring compute prioritization scores from effort, reach, impact, and confidence fields.

The service is backed by PostgreSQL 18, with Alembic managing schema migrations. All write operations are tenant-scoped, and — when authentication is enabled — the auth middleware enforces tenant isolation at the request boundary before any database query executes. The application runs as a single FastAPI process and is platform-agnostic: container image, plain process, or Kubernetes.

## Features

- **Three-entity architecture** — manages requirements, principles, and ADR decisions as first-class entities with distinct schemas and lifecycle rules so teams can track all aspects of software architecture in one service
- **ArchiMate architecture domain** — elements / relationships / views / standards persist alongside the strategic entities so teams describe the *intended* architecture in the same service that holds its requirements + decisions
- **N-ary realisation bindings** — each `ArchitectureElement` (Application Component) can be realised by zero-or-more coding-side artifacts AND zero-or-more provisioned repos via the structured `architecture_element_realisation_bindings` table (kinds: `coding_component` / `coding_container` / `external_repo`); a per-container validator endpoint lets downstream consumers (e.g. `fr`) refuse promotion when no element binds the target container
- **Multi-tenant isolation** — scopes every entity to a tenant resolved from license token authentication so multiple teams share the deployment without data leakage
- **Trigram full-text search** — searches across all entity types using PostgreSQL trigram similarity indexes so users find results even with partial or misspelled terms
- **TF-IDF semantic search** — combines scikit-learn TF-IDF scoring with SpaCy NER on a seed knowledge base so conceptually related content surfaces even when exact terms differ
- **Unified cross-entity search** — queries requirements, principles, and decisions in a single request so users discover related content across entity boundaries
- **Snapshot version history** — records full-field snapshots on every principle and decision update so any two versions can be structurally compared
- **WSJF/RICE prioritization** — calculates weighted-shortest-job-first and RICE scores from effort, reach, impact, and confidence fields so teams can prioritize requirements objectively
- **Automated quality scoring** — analyzes requirement text for completeness and clarity so authors get immediate feedback before publishing
- **Bulk operations** — deletes all entities for a tenant in a single request so test environments and CI pipelines can reset state cleanly
- **License token caching** — caches introspection results for 5 minutes (TTLCache, 1000 entries) so repeated requests from the same token avoid redundant validation calls
- **OpenTelemetry instrumentation** — traces FastAPI, httpx, and SQLAlchemy with OTLP export so operators can monitor request latency and database performance

## Authentication

Authentication is **opt-in**: with `ENABLE_AUTH` unset (the default) the service starts with no
licences service, no token and no configuration, and trusts the `X-Tenant-Id` header for tenant
scoping. That is the mode a fresh clone runs in.

A deployment that wants enforced tenant isolation sets `ENABLE_AUTH=true` **and**
`LICENSE_INTROSPECT_URL`. In that mode every request must carry a licence token:
`X-Tenant-Id: Bearer <token>` (else `Authorization: Bearer <token>`), validated by POST to the
introspection endpoint (result cached 5 minutes). No token ⇒ 401, whatever the source address —
there is no trusted-network / tokenless identity. Only `PUBLIC_PATHS` in
`src/auth/middleware.py` — `/health`, `/health/live`, `/health/ready`, `/openapi.json` — are
exempt; `/`, `/docs` and `/redoc` require a licence.

## User Stories

```gherkin
Feature: Requirement lifecycle
  As a developer
  I want to create and manage requirements with traceability links
  So that I can track what needs to be built and why

  Scenario: Create a requirement with acceptance criteria
    Given I am authenticated as a tenant
    When I create a requirement with title, description, and acceptance criteria
    Then the requirement is stored with an auto-generated slug
    And I can retrieve it by ID

  Scenario: Prioritize requirements using RICE
    Given a requirement with effort, reach, impact, and confidence values
    When I request a priority calculation
    Then I receive WSJF and RICE scores for comparison

Feature: Architecture decisions
  As a platform operator
  I want to record architecture decision records with status tracking
  So that future teams understand what was decided and why

  Scenario: Track decision through lifecycle
    Given a proposed decision with context and alternatives
    When the decision is accepted
    Then its status changes to accepted
    And the full version history is preserved

  Scenario: Compare decision versions
    Given a decision with multiple recorded versions
    When I compare version 1.0.0 with version 2.0.0
    Then I see a structured diff of all changed fields

Feature: Cross-entity search
  As a developer
  I want to search across requirements, principles, and decisions
  So that I can discover related content across entity types

  Scenario: Unified search
    Given requirements, principles, and decisions exist for my tenant
    When I search with a query term
    Then I receive ranked results from all three entity types
```

## Quick Start

```bash
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8921 --app-dir src
uv run pytest tests/ -v --cov=src
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
```

## API Endpoints

### Requirements `/requirements`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/` | Create a requirement (slug auto-generated from title) |
| GET | `/` | List all requirements for the authenticated tenant |
| GET | `/{rid}` | Retrieve a single requirement by ID |
| PUT | `/{rid}` | Update a requirement |
| DELETE | `/{rid}` | Delete a requirement |
| POST | `/{rid}/quality` | Run quality checks on a requirement |
| POST | `/{rid}/priority` | Calculate WSJF and RICE scores |
| DELETE | `/bulk` | Delete all requirements for the authenticated tenant |

### Principles `/principles`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/` | Create a principle |
| GET | `/` | List principles (optional `active_only` filter) |
| GET | `/{pid}` | Retrieve a single principle by ID |
| PUT | `/{pid}` | Update a principle |
| DELETE | `/{pid}` | Delete a principle |
| GET | `/{pid}/version` | Get current version info |
| GET | `/{pid}/versions` | List version history |
| GET | `/{pid}/versions/{v1}/compare/{v2}` | Compare two version snapshots |
| DELETE | `/bulk` | Delete all principles for the authenticated tenant |

### Decisions `/decisions`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/` | Create a decision (ADR) |
| GET | `/` | List decisions (optional `project_id`, `status` filters) |
| GET | `/{did}` | Retrieve a single decision by ID |
| PUT | `/{did}` | Update a decision |
| DELETE | `/{did}` | Delete a decision |
| GET | `/{did}/version` | Get current version info |
| GET | `/{did}/versions` | List version history |
| GET | `/{did}/versions/{v1}/compare/{v2}` | Compare two version snapshots |
| DELETE | `/bulk` | Delete all decisions for the authenticated tenant |

### Architecture `/architecture`

CRUD for ArchiMate elements / relationships / views / standards. Relationships are immutable upstream — create + delete only, no PUT. (Full schema in `/docs`; entries below cover only the routes added by the structured-bindings cutover.)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/elements/{eid}/bindings` | List the element's realisation bindings (the structured replacement for the old single `properties.code_binding` JSONB) |
| POST | `/elements/{eid}/bindings` | Attach a binding — body discriminated by `binding_kind` (`coding_component` requires `container` + `component_id`; `coding_container` requires `container`; `external_repo` requires `repository_url`) |
| DELETE | `/elements/{eid}/bindings/{bid}` | Detach a binding |
| GET | `/containers?projectId=<uuid>` | List distinct coding-side container names bound somewhere in the project's effective view (project-scope ∪ tenant-scope), tagged by which scope contributed each |
| GET | `/containers/{name}?projectId=<uuid>` | **Per-container validator** — `200 {name, scope, element_count, valid:true}` if at least one element in the project's effective view binds this container, otherwise `404`. `fr` calls this on repo-attach (soft → warning) and primary-promote (hard → `409 container_not_bound_to_project`) |

The `code_binding` JSONB inside `ArchitectureElement.properties` continues to be dual-written for one PR cycle so existing readers (conformance, proposals) keep working; cleanup that drops it follows once they migrate to read from the structured table.

### Search `/search`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/?q=&k=10` | Full-text trigram search on requirements |
| GET | `/principles?q=&k=10` | Full-text trigram search on principles |
| GET | `/decisions?q=&k=10` | Full-text trigram search on decisions |
| GET | `/all?q=&k=10` | Unified search across all entity types |

### Semantic Search `/semantic_search`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/?query=&k=10&min_similarity=0.1` | TF-IDF semantic search with NER on knowledge base |

### Bulk `/bulk`

| Method | Path | Description |
|--------|------|-------------|
| DELETE | `/` | Delete all entities (requirements + principles + decisions) for the authenticated tenant |

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Redirects to /docs (both require a licence) |
| GET | `/health` | Basic health check |
| GET | `/health/live` | Liveness probe |
| GET | `/health/ready` | Readiness probe (checks database) |

## Configuration

All configuration via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8921` | Listen port |
| `ENABLE_AUTH` | `false` | Enable licence-token auth middleware (requires `LICENSE_INTROSPECT_URL`) |
| `LICENSE_INTROSPECT_URL` | *(unset)* | Licence introspection endpoint; only read when auth is enabled |
| `DATABASE_URL` | *(required)* | PostgreSQL connection string (async psycopg3) |
| `API_BASE_URL` | `http://localhost:8921` | Base URL shown in OpenAPI spec |
| `CORS_ALLOW_ORIGINS` | `http://localhost:8921,http://localhost:3000,http://127.0.0.1:3000` | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | Logging level |

## Architecture

```
src/
├── main.py                        # FastAPI app factory, middleware registration, OpenAPI customization
├── db.py                          # Async SQLAlchemy engine, session factory, Base class
├── models.py                      # ORM models: Requirement, Principle, Decision, EntityVersionHistory
├── schemas.py                     # Pydantic v2 request/response DTOs with field validators
├── lifespan.py                    # App startup/shutdown lifecycle (DB init, SpaCy model load)
├── logging_config.py              # Structured logging with structlog + OpenTelemetry
├── telemetry.py                   # OpenTelemetry tracing setup (FastAPI, httpx, SQLAlchemy)
├── validation.py                  # Input validation utilities
├── prio.py                        # WSJF and RICE score calculation
├── quality.py                     # Automated requirement quality checks
├── search_light.py                # PostgreSQL trigram similarity search
├── semantic_search.py             # TF-IDF search with SpaCy NER over seed knowledge base
├── seed_knowledge.py              # Seed knowledge base content for semantic search
├── version_utils.py               # Semver comparison and breaking change detection
├── auth/
│   ├── __init__.py                # Auth module exports (get_tenant_id, AuthMiddleware, User)
│   ├── middleware.py              # License token validation middleware with TTLCache
│   ├── models.py                  # User model with tenant context
│   ├── tenant.py                  # FastAPI dependencies for tenant extraction and BOLA protection
│   └── exceptions.py             # Auth-specific exception classes
├── middleware/
│   ├── __init__.py                # Middleware registration helpers
│   ├── request_logging.py         # Structured request/response logging middleware
│   └── security_headers.py        # Security response headers middleware
├── routers/
│   ├── __init__.py                # Router registration (bulk before entity routers for path precedence)
│   ├── requirements.py            # Requirement CRUD + quality + priority endpoints
│   ├── principles.py              # Principle CRUD + version history + comparison endpoints
│   ├── decisions.py               # Decision CRUD + version history + comparison endpoints
│   ├── search.py                  # Full-text and semantic search endpoints
│   ├── bulk.py                    # Bulk delete operations
│   ├── health.py                  # Health check endpoints
│   └── responses.py               # Shared OpenAPI response definitions
├── custom_types/
│   └── constraint_types.py        # Custom type constraints
├── services/
│   └── __init__.py                # Service layer (placeholder)
└── utils/
    ├── __init__.py                # Utility exports
    ├── ownership.py               # Tenant ownership validation
    ├── pagination.py              # Pagination helpers
    ├── version_history.py         # Version snapshot recording
    └── version_refs.py            # Version reference resolution
alembic/
├── env.py                         # Alembic migration environment
└── versions/                      # Database migration scripts
```

## Data Models

### Requirement

| Field | Type | Description |
|-------|------|-------------|
| id | int | Primary key |
| slug | string(120) | URL-safe identifier derived from title, unique per tenant |
| title | string(300) | Short summary |
| description | text | Full description |
| project_id | string(100) | Optional project scope |
| creator_id | string(100) | Tenant/owner identifier |
| links | JSONB | Traceability links: business_objectives, capabilities, services |
| acceptance | JSONB | List of acceptance criteria (Gherkin format) |
| effort | float | Effort estimate (used by WSJF and RICE) |
| reach | float | Users/items affected (RICE) |
| impact | float | Impact score 0-10 (RICE) |
| confidence | float | Confidence level 0-1 (RICE) |
| version | string(20) | Semantic version, default "1.0.0" |
| version_status | string(20) | DRAFT, PUBLISHED, or DEPRECATED |
| supersedes / superseded_by | string(120) | Version lineage via slugs |
| version_notes | text | Notes about this version |
| approved_by | string(100) | Approver identifier |
| approved_at / published_at | datetime | Lifecycle timestamps |
| created_at / updated_at | datetime | Auto-managed timestamps |
| tsv | tsvector | Full-text search index column |

### Principle

| Field | Type | Description |
|-------|------|-------------|
| id | int | Primary key |
| slug | string(120) | URL-safe identifier, unique per tenant |
| title | string(300) | Principle name |
| statement | text | The principle statement |
| creator_id | string(100) | Tenant/owner identifier |
| rationale | text | Why this principle exists |
| implications | JSONB | List of implications for teams |
| tags | JSONB | Categorization tags |
| active | boolean | Whether currently in effect |
| version, version_status, supersedes, superseded_by, version_notes, approved_by, approved_at, published_at, created_at, updated_at, tsv | -- | Same as Requirement |

### Decision

| Field | Type | Description |
|-------|------|-------------|
| id | int | Primary key |
| slug | string(120) | URL-safe identifier, unique per tenant |
| title | string(300) | Decision summary |
| project_id | string(100) | Optional project scope |
| creator_id | string(100) | Tenant/owner identifier |
| status | string(20) | proposed, accepted, deprecated, or superseded |
| context | text | Background and problem statement |
| decision | text | The actual decision made |
| consequences | text | Positive and negative outcomes |
| alternatives | JSONB | List of considered alternatives |
| related_principles | JSONB | List of related principle IDs |
| version, version_status, supersedes, superseded_by, version_notes, approved_by, approved_at, published_at, created_at, updated_at, tsv | -- | Same as Requirement |

## Testing

```bash
uv run pytest tests/ -v --cov=src          # All tests with coverage
uv run pytest tests/ -m "not integration"  # Unit tests only (default)
uv run pytest tests/integration/ -v        # Integration tests (DB from TEST_DATABASE_URL or ephemeral docker)
uv run ruff check src/ tests/              # Lint
uv run ruff format --check src/ tests/     # Format check
```

Coverage threshold: 80% (enforced in pyproject.toml).

## CI/CD

CI runs on GitHub-hosted runners from [`.github/workflows/ci-public.yml`](.github/workflows/ci-public.yml):
secret scan, private-reference scan, `ruff check` + `ruff format --check`, the pytest suite
against a throwaway PostgreSQL 18, and `pip-audit`. The image job is opt-in per repository
(`PUBLISH_IMAGE=true`) and pushes to `ghcr.io/nonagenticai/requirements_management` on `main`.

Run the same commands locally before opening a pull request:

```bash
uv sync --frozen --all-extras
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

Schemathesis contract tests are currently not part of CI.

## Docker

```bash
docker build -t requirements-management .
docker run -p 8921:8921 requirements-management
docker run -p 8921:8921 -e LOG_LEVEL=DEBUG -e DATABASE_URL=postgresql+psycopg://user:pass@host/db requirements-management
```

## Dependencies

- **PostgreSQL 18** — persistent storage (a connection pooler such as PgBouncer is optional)
- **licenses** (port 8544) — license token introspection for tenant authentication
- **FastAPI** — web framework
- **Uvicorn** — ASGI server
- **Pydantic** — settings and schema validation
- **SQLAlchemy 2.x** — async ORM with psycopg3 driver
- **Alembic** — database migrations
- **SpaCy** (en_core_web_sm) — named entity recognition for semantic search
- **scikit-learn** — TF-IDF vectorization for semantic similarity scoring
- **httpx** — async HTTP client for license introspection
- **structlog** — structured logging
- **OpenTelemetry** — distributed tracing (FastAPI, httpx, SQLAlchemy instrumentation)
- **python-slugify** — URL-safe slug generation from titles

## Known Issues

- **Schemathesis contract tests disabled** — contract testing via Schemathesis was blocking CI deploys and has been temporarily disabled in the CI workflow
- **No `/api/` route prefix** — routes are mounted at the service root (`/requirements`, `/principles`, etc.) rather than under `/api/` as used by other services in the platform
- **SpaCy model installation** — the `en_core_web_sm` model requires a separate download step (`uv run python -m spacy download en_core_web_sm`) that cannot be handled purely through `pyproject.toml` dependencies in all environments

## Licensing

The code in this repository is Apache-2.0 and needs no licence key to build, test or run.

Licence-token enforcement is an **optional** gate and is **off by default**: `ENABLE_AUTH`
defaults to `false`, and with it unset the service never contacts a licence server — tenants are
taken from the `X-Tenant-Id` header. Turning it on means setting `ENABLE_AUTH=true` **and**
`LICENSE_INTROSPECT_URL` to a running licences service; enforcement is then fail-closed, so an
unreachable service denies the request rather than allowing it.

The licence stack itself is open source too — [`licenses`](https://github.com/nonagenticai/licenses)
(entitlements, plans and tokens) and [`license-authz`](https://github.com/nonagenticai/license-authz)
(the enforcement point). Neither is required to run this code.

## License

Apache 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
