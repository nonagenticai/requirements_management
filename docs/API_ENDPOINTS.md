# API Endpoints Reference

## Table of Contents
- [Authentication](#authentication)
- [Public Endpoints](#public-endpoints)
- [Requirements Endpoints](#requirements-endpoints)
- [Principles Endpoints](#principles-endpoints)
- [Decisions Endpoints](#decisions-endpoints)
- [Architecture Endpoints](#architecture-endpoints)
- [Project Management Endpoints](#project-management-endpoints)
- [Deterministic Tools](#deterministic-tools)
- [Versioning Endpoints](#versioning-endpoints)
- [Search Endpoints](#search-endpoints)
- [Error Responses](#error-responses)

## Authentication

All endpoints (except public ones) require JWT authentication via the `Authorization` header:

```
Authorization: Bearer <JWT_TOKEN>
```

### Getting a Token

```bash
# Request token from Keycloak
curl -X POST "https://keycloak.example.com/realms/secure-apps/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=requirements" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "grant_type=password" \
  -d "username=your_username" \
  -d "password=your_password"

# Response
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_in": 300,
  "token_type": "Bearer"
}
```

## Public Endpoints

These endpoints do not require authentication.

### GET /health

Health check endpoint.

**Request:**
```bash
curl http://localhost:8090/health
```

**Response** (200):
```json
{
  "status": "healthy",
  "timestamp": "2025-11-28T16:05:00Z"
}
```

### GET /

Root endpoint.

**Request:**
```bash
curl http://localhost:8090/
```

**Response** (200):
```json
{
  "message": "Requirements Management API",
  "version": "1.0.0",
  "docs": "http://localhost:8090/docs"
}
```

### GET /docs

Interactive API documentation (Swagger UI).

**Access:** http://localhost:8090/docs

### GET /redoc

Alternative API documentation (ReDoc).

**Access:** http://localhost:8090/redoc

## Requirements Endpoints

All requirements endpoints require authentication. The `creator_id` is automatically extracted from the JWT token.

### POST /requirements

Create a new requirement.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X POST "http://localhost:8090/requirements" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "User Authentication System",
    "description": "Implement OAuth2-based authentication with JWT tokens",
    "creator_id": "user-123",
    "effort": 8.5,
    "reach": 1000,
    "impact": 5,
    "confidence": 0.8,
    "links": {
      "business_objectives": ["Secure platform access"],
      "capabilities": ["Authentication"],
      "services": ["auth-service"]
    },
    "acceptance": [
      "Given valid credentials, user can log in",
      "Given invalid credentials, access is denied",
      "Token expires after 15 minutes"
    ],
    "version": "1.0.0",
    "version_status": "DRAFT"
  }'
```

**Response** (201):
```json
{
  "id": 42,
  "title": "User Authentication System",
  "description": "Implement OAuth2-based authentication with JWT tokens",
  "creator_id": "user-123",
  "project_id": null,
  "effort": 8.5,
  "reach": 1000,
  "impact": 5,
  "confidence": 0.8,
  "links": {
    "business_objectives": ["Secure platform access"],
    "capabilities": ["Authentication"],
    "services": ["auth-service"]
  },
  "acceptance": [
    "Given valid credentials, user can log in",
    "Given invalid credentials, access is denied",
    "Token expires after 15 minutes"
  ],
  "version": "1.0.0",
  "version_status": "DRAFT",
  "created_at": "2025-11-28T16:05:00Z",
  "updated_at": "2025-11-28T16:05:00Z"
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `422 Unprocessable Entity`: Invalid request body

### GET /requirements/{id}

Get a specific requirement by ID. Optionally verifies ownership if `creator_id` query parameter is provided.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
# Get any requirement
curl -X GET "http://localhost:8090/requirements/42" \
  -H "Authorization: Bearer $JWT_TOKEN"

# Get requirement with ownership verification
curl -X GET "http://localhost:8090/requirements/42?creator_id=user-123" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "id": 42,
  "title": "User Authentication System",
  "description": "Implement OAuth2-based authentication with JWT tokens",
  "creator_id": "user-123",
  "effort": 8.5,
  "created_at": "2025-11-28T16:05:00Z",
  "updated_at": "2025-11-28T16:05:00Z"
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `403 Forbidden`: Not the owner (when creator_id verification requested)
- `404 Not Found`: Requirement not found

### GET /requirements

List all requirements. Optionally filter by `creator_id`.

**Authentication:** Required
**Roles:** Any authenticated user

**Query Parameters:**
- `creator_id` (optional): Filter by creator ID
- `project_id` (optional): Filter by project ID
- `skip` (optional, default: 0): Number of records to skip
- `limit` (optional, default: 100): Maximum records to return

**Request:**
```bash
# List all requirements
curl -X GET "http://localhost:8090/requirements" \
  -H "Authorization: Bearer $JWT_TOKEN"

# List user's own requirements
curl -X GET "http://localhost:8090/requirements?creator_id=user-123" \
  -H "Authorization: Bearer $JWT_TOKEN"

# List with pagination
curl -X GET "http://localhost:8090/requirements?skip=0&limit=10" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
[
  {
    "id": 42,
    "title": "User Authentication System",
    "description": "Implement OAuth2-based authentication",
    "creator_id": "user-123",
    "effort": 8.5,
    "created_at": "2025-11-28T16:05:00Z"
  },
  {
    "id": 43,
    "title": "Password Reset Flow",
    "description": "Allow users to reset forgotten passwords",
    "creator_id": "user-123",
    "effort": 3.0,
    "created_at": "2025-11-28T16:10:00Z"
  }
]
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token

### PUT /requirements/{id}

Update a requirement. Optionally verifies ownership if `creator_id` query parameter is provided.

**Authentication:** Required
**Roles:** Any authenticated user (owner verification optional)

**Request:**
```bash
curl -X PUT "http://localhost:8090/requirements/42" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "User Authentication System (Updated)",
    "description": "Updated description with more details",
    "effort": 10.0,
    "version": "1.1.0",
    "version_status": "PUBLISHED"
  }'

# With ownership verification
curl -X PUT "http://localhost:8090/requirements/42?creator_id=user-123" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{...}'
```

**Response** (200):
```json
{
  "id": 42,
  "title": "User Authentication System (Updated)",
  "description": "Updated description with more details",
  "creator_id": "user-123",
  "effort": 10.0,
  "version": "1.1.0",
  "version_status": "PUBLISHED",
  "updated_at": "2025-11-28T16:15:00Z"
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `403 Forbidden`: Not the owner (when verification requested)
- `404 Not Found`: Requirement not found
- `422 Unprocessable Entity`: Invalid request body

### DELETE /requirements/{id}

Delete a requirement. Optionally verifies ownership if `creator_id` query parameter is provided.

**Authentication:** Required
**Roles:** Any authenticated user (owner verification optional)

**Request:**
```bash
# Delete without ownership check
curl -X DELETE "http://localhost:8090/requirements/42" \
  -H "Authorization: Bearer $JWT_TOKEN"

# Delete with ownership verification
curl -X DELETE "http://localhost:8090/requirements/42?creator_id=user-123" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "message": "Requirement deleted",
  "id": 42
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `403 Forbidden`: Not the owner (when verification requested)
- `404 Not Found`: Requirement not found

### DELETE /requirements/bulk, /principles/bulk, /decisions/bulk

Bulk-delete by ID list or filter. Accepts `BatchDeleteRequest` body — either `{"ids": [...]}` or `{"filter": {...}}`. Returns `{"deleted": <count>, "ids": ["..."]}`.

`DELETE /bulk` is the catch-all endpoint that accepts an `entity_type` discriminator and the same body shape, useful from automation (n8n).

### DELETE /requirements/by-creator/{creator_id}

Delete all requirements for a specific creator.

**Authentication:** Required
**Roles:** Admin or owner

**Request:**
```bash
curl -X DELETE "http://localhost:8090/requirements/by-creator/user-123" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "message": "All requirements deleted",
  "creator_id": "user-123",
  "count": 15
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `403 Forbidden`: Not authorized (not admin or owner)
- `400 Bad Request`: Missing or invalid creator_id

### POST /requirements/{id}/quality

Run quality checks on a requirement.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X POST "http://localhost:8090/requirements/42/quality" \
  -H "Authorization: Bearer $JWT_TOKEN"

# With ownership verification
curl -X POST "http://localhost:8090/requirements/42/quality?creator_id=user-123" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "requirement_id": 42,
  "quality_score": 8.5,
  "checks": {
    "has_clear_title": true,
    "has_detailed_description": true,
    "has_acceptance_criteria": true,
    "has_traceability_links": true,
    "has_effort_estimate": true
  },
  "recommendations": [
    "Add more specific acceptance criteria",
    "Include user personas"
  ]
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `403 Forbidden`: Not the owner (when verification requested)
- `404 Not Found`: Requirement not found

### POST /requirements/{id}/priority

Calculate WSJF and RICE priority scores for a requirement.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X POST "http://localhost:8090/requirements/42/priority" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "business_value": 8,
    "time_criticality": 5,
    "risk_reduction": 3
  }'

# With ownership verification
curl -X POST "http://localhost:8090/requirements/42/priority?creator_id=user-123" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{...}'
```

**Response** (200):
```json
{
  "requirement_id": 42,
  "wsjf_score": 1.88,
  "rice_score": 470.59,
  "calculation": {
    "wsjf": {
      "business_value": 8,
      "time_criticality": 5,
      "risk_reduction": 3,
      "effort": 8.5,
      "formula": "(8 + 5 + 3) / 8.5"
    },
    "rice": {
      "reach": 1000,
      "impact": 5,
      "confidence": 0.8,
      "effort": 8.5,
      "formula": "(1000 * 5 * 0.8) / 8.5"
    }
  }
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `403 Forbidden`: Not the owner (when verification requested)
- `404 Not Found`: Requirement not found

## Principles Endpoints

Architecture principles are cross-project guidelines. All endpoints require authentication.

### POST /principles

Create a new architecture principle.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X POST "http://localhost:8090/principles" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "API First Development",
    "statement": "All services must expose well-defined APIs before implementation",
    "rationale": "Enables better testing, integration, and parallel development",
    "implications": [
      "Design APIs before implementation",
      "Use OpenAPI specifications",
      "Implement API mocking for testing"
    ],
    "tags": ["api", "architecture", "design"],
    "active": true,
    "creator_id": "user-123"
  }'
```

**Response** (201):
```json
{
  "id": 10,
  "title": "API First Development",
  "statement": "All services must expose well-defined APIs before implementation",
  "rationale": "Enables better testing, integration, and parallel development",
  "implications": [
    "Design APIs before implementation",
    "Use OpenAPI specifications",
    "Implement API mocking for testing"
  ],
  "tags": ["api", "architecture", "design"],
  "active": true,
  "creator_id": "user-123",
  "created_at": "2025-11-28T16:20:00Z"
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `422 Unprocessable Entity`: Invalid request body

### GET /principles

List all architecture principles.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
# List all principles
curl -X GET "http://localhost:8090/principles" \
  -H "Authorization: Bearer $JWT_TOKEN"

# Filter by active status
curl -X GET "http://localhost:8090/principles?active=true" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
[
  {
    "id": 10,
    "title": "API First Development",
    "statement": "All services must expose well-defined APIs",
    "active": true,
    "created_at": "2025-11-28T16:20:00Z"
  },
  {
    "id": 11,
    "title": "Security by Default",
    "statement": "Security must be built-in, not bolted-on",
    "active": true,
    "created_at": "2025-11-28T16:25:00Z"
  }
]
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token

### GET /principles/{id}

Get a specific principle by ID.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X GET "http://localhost:8090/principles/10" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "id": 10,
  "title": "API First Development",
  "statement": "All services must expose well-defined APIs before implementation",
  "rationale": "Enables better testing, integration, and parallel development",
  "implications": [
    "Design APIs before implementation",
    "Use OpenAPI specifications"
  ],
  "tags": ["api", "architecture"],
  "active": true,
  "creator_id": "user-123",
  "created_at": "2025-11-28T16:20:00Z"
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `404 Not Found`: Principle not found

### PUT /principles/{id}

Update an architecture principle.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X PUT "http://localhost:8090/principles/10" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "API First Development (Updated)",
    "statement": "Updated statement",
    "active": false
  }'
```

**Response** (200):
```json
{
  "id": 10,
  "title": "API First Development (Updated)",
  "statement": "Updated statement",
  "active": false,
  "updated_at": "2025-11-28T16:30:00Z"
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `404 Not Found`: Principle not found
- `422 Unprocessable Entity`: Invalid request body

### DELETE /principles/{id}

Delete an architecture principle.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X DELETE "http://localhost:8090/principles/10" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "message": "Principle deleted",
  "id": 10
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `404 Not Found`: Principle not found

## Decisions Endpoints

Architecture Decision Records (ADRs). All endpoints require authentication.

### POST /decisions

Create a new design decision.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X POST "http://localhost:8090/decisions" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Use PostgreSQL for Primary Database",
    "project_id": "project-alpha",
    "status": "accepted",
    "context": "Need ACID transactions, JSON support, and strong consistency",
    "decision": "Use PostgreSQL 15+ as the primary database",
    "consequences": "Strong consistency guarantees, requires DBA expertise",
    "alternatives": [
      "MongoDB - rejected due to consistency concerns",
      "MySQL - rejected due to limited JSON support"
    ],
    "related_principles": [10, 11],
    "creator_id": "user-123"
  }'
```

**Response** (201):
```json
{
  "id": 25,
  "title": "Use PostgreSQL for Primary Database",
  "project_id": "project-alpha",
  "status": "accepted",
  "context": "Need ACID transactions, JSON support, and strong consistency",
  "decision": "Use PostgreSQL 15+ as the primary database",
  "consequences": "Strong consistency guarantees, requires DBA expertise",
  "alternatives": [
    "MongoDB - rejected due to consistency concerns",
    "MySQL - rejected due to limited JSON support"
  ],
  "related_principles": [10, 11],
  "creator_id": "user-123",
  "created_at": "2025-11-28T16:35:00Z"
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `422 Unprocessable Entity`: Invalid request body

### GET /decisions

List all design decisions.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
# List all decisions
curl -X GET "http://localhost:8090/decisions" \
  -H "Authorization: Bearer $JWT_TOKEN"

# Filter by project
curl -X GET "http://localhost:8090/decisions?project_id=project-alpha" \
  -H "Authorization: Bearer $JWT_TOKEN"

# Filter by status
curl -X GET "http://localhost:8090/decisions?status=accepted" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
[
  {
    "id": 25,
    "title": "Use PostgreSQL for Primary Database",
    "project_id": "project-alpha",
    "status": "accepted",
    "creator_id": "user-123",
    "created_at": "2025-11-28T16:35:00Z"
  },
  {
    "id": 26,
    "title": "Implement Microservices Architecture",
    "project_id": "project-alpha",
    "status": "proposed",
    "creator_id": "user-456",
    "created_at": "2025-11-28T16:40:00Z"
  }
]
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token

### GET /decisions/{id}

Get a specific decision by ID.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X GET "http://localhost:8090/decisions/25" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "id": 25,
  "title": "Use PostgreSQL for Primary Database",
  "project_id": "project-alpha",
  "status": "accepted",
  "context": "Need ACID transactions, JSON support, and strong consistency",
  "decision": "Use PostgreSQL 15+ as the primary database",
  "consequences": "Strong consistency guarantees, requires DBA expertise",
  "alternatives": [
    "MongoDB - rejected due to consistency concerns"
  ],
  "related_principles": [10, 11],
  "creator_id": "user-123",
  "created_at": "2025-11-28T16:35:00Z"
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `404 Not Found`: Decision not found

### PUT /decisions/{id}

Update a design decision.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X PUT "http://localhost:8090/decisions/25" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "status": "implemented",
    "consequences": "Successfully deployed, performance excellent"
  }'
```

**Response** (200):
```json
{
  "id": 25,
  "title": "Use PostgreSQL for Primary Database",
  "status": "implemented",
  "consequences": "Successfully deployed, performance excellent",
  "updated_at": "2025-11-28T16:45:00Z"
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `404 Not Found`: Decision not found
- `422 Unprocessable Entity`: Invalid request body

### DELETE /decisions/{id}

Delete a design decision.

**Authentication:** Required
**Roles:** Any authenticated user

**Request:**
```bash
curl -X DELETE "http://localhost:8090/decisions/25" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "message": "Decision deleted",
  "id": 25
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `404 Not Found`: Decision not found

## Architecture Endpoints

Enterprise/solution architecture artifacts following the ArchiMate 3.2 metamodel. All endpoints require authentication. Entity IDs are UUID v7.

### Architecture Elements

`POST /architecture/elements`, `GET /architecture/elements`, `GET /architecture/elements/{eid}`, `PUT /architecture/elements/{eid}`, `DELETE /architecture/elements/{eid}` — standard CRUD with `layer`, `element_type`, `status`, `architecture_state`, `project_id` filters.

#### POST /architecture/elements/batch-delete

Delete multiple elements in one call. Either pass an explicit ID list or a filter object.

**Request:**
```bash
curl -X POST "http://localhost:8090/architecture/elements/batch-delete" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ids": ["uuid-1", "uuid-2"]}'

# Or by filter
curl -X POST "http://localhost:8090/architecture/elements/batch-delete" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filter": {"project_id": "proj-alpha", "status": "retired"}}'
```

**Response** (200): `{"deleted": <count>, "ids": ["..."]}`

#### GET /architecture/elements/{eid}/children

Direct children of an element (single hop via `parent_id`).

#### GET /architecture/elements/{eid}/ancestors

Full ancestor chain to the root, ordered from immediate parent upward.

#### POST /architecture/elements/{eid}/quality

Run the heuristic quality scorer for a single element. Persists `quality_score`, `quality_issues`, `quality_suggestions`, `last_evaluated_at`, `last_evaluated_by_agent` on the row. See the [Quality Evaluation](DEVELOPER_GUIDE.md#quality-evaluation) section.

### Architecture Relationships

`POST /architecture/relationships`, `GET /architecture/relationships`, `GET /architecture/relationships/{rid}`, `DELETE /architecture/relationships/{rid}` — CRUD. The `POST` validates against the ArchiMate metamodel (returns 422 on invalid).

`GET /architecture/relationships/graph/{entity_type}/{entity_id}` — all relationships for an entity (both directions).

#### PUT /architecture/relationships/{rid}

Update an existing relationship. Source/target/type changes are re-validated against the metamodel.

**Request body:** `ArchitectureRelationshipUpdate` — all fields optional: `source_entity_type`, `source_id`, `target_entity_type`, `target_id`, `relationship_type`, `project_id`, `name`, `description`, `properties`.

### Architecture Views

Standard CRUD at `/architecture/views`.

#### POST /architecture/views/{vid}/quality

Heuristic quality scorer for a view (checks viewpoint coverage, included elements/relationships, layout completeness). Persists quality columns.

### Architecture Standards

Standard CRUD at `/architecture/standards`.

#### GET /architecture/standards/element/{eid}/applicable-standards

Returns all standards whose `element_type_filter`/`layer_filter` match the given element. Used by the compliance check.

### Decisions — Quality

#### POST /decisions/{did}/quality

Heuristic quality scorer for a decision (context/decision/consequences/alternatives completeness, related-principle coverage). Persists quality columns.

### Principles — Quality

#### POST /principles/{pid}/quality

Heuristic quality scorer for a single principle (statement, rationale, implications). Persists quality columns.

## Project Management Endpoints

PM artifacts (programmes, projects, resources, risks, benefits). All endpoints require authentication. RM does no scheduling: activities, dependencies, buffers, schedule snapshots, what-if and `/pm/tools` were dropped in R352.

### Programmes / Projects / Resources / Risks / Benefits

Standard CRUD under `/pm/programmes`, `/pm/projects`, `/pm/resources`, `/pm/risks`, `/pm/benefits`. Filters per entity (`project_id`, `programme_id`, `status`, etc.).

### Risks / Benefits — quality

- `POST /pm/risks/{eid}/quality` — heuristic.
- `POST /pm/benefits/{eid}/quality` — heuristic.

## Deterministic Tools

Pure-computation tools — deterministic results. All POST.

### Architecture Tools (`/architecture/tools`)

| Endpoint | Purpose |
|---|---|
| `POST /validate-relationship` | Validate a proposed relationship against ArchiMate 3.2 metamodel |
| `POST /traceability-matrix` | Cross-layer traceability for a project |
| `POST /gap-analysis` | Baseline vs target architecture gap classification |
| `POST /impact-analysis` | Propagate a change through the relationship graph |
| `POST /adm-completeness` | TOGAF ADM phase deliverable completeness |
| `POST /capability-heatmap` | Score capabilities across maturity / strategic importance / investment need |
| `POST /generate-view` | Auto-select elements & relationships for a viewpoint |
| `POST /compliance-check` | Check elements against approved standards & principles |

### Integration Tools (`/integration/tools`)

| Endpoint | Purpose |
|---|---|
| `POST /gap-to-workpackages` | Generate work-package proposals from gap analysis |
| `POST /requirement-trace` | Requirement ↔ architecture coverage |

## Versioning Endpoints

Every versioned entity exposes the same uniform set of read endpoints:

| Entity | List versions | Get current | Compare two |
|---|---|---|---|
| Requirement | `GET /requirements/{rid}/versions` | `GET /requirements/{rid}/version` | `GET /requirements/{rid}/versions/{v1}/compare/{v2}` |
| Principle | `GET /principles/{pid}/versions` | `GET /principles/{pid}/version` | `GET /principles/{pid}/versions/{v1}/compare/{v2}` |
| Decision | `GET /decisions/{did}/versions` | `GET /decisions/{did}/version` | `GET /decisions/{did}/versions/{v1}/compare/{v2}` |
| Architecture Element | `GET /architecture/elements/{eid}/versions` | `GET /architecture/elements/{eid}/version` | `GET /architecture/elements/{eid}/versions/{v1}/compare/{v2}` |
| Architecture View | `GET /architecture/views/{vid}/versions` | `GET /architecture/views/{vid}/version` | `GET /architecture/views/{vid}/versions/{v1}/compare/{v2}` |
| Architecture Standard | `GET /architecture/standards/{sid}/versions` | `GET /architecture/standards/{sid}/version` | `GET /architecture/standards/{sid}/versions/{v1}/compare/{v2}` |

**Response shape** (`/versions/{v1}/compare/{v2}`):
```json
{
  "version_a": "1.0.0",
  "version_b": "2.0.0",
  "diff": {
    "title": {"from": "...", "to": "..."},
    "description": {"from": "...", "to": "..."}
  }
}
```

## Search Endpoints

### GET /search

Full-text search with PostgreSQL trigram similarity.

**Authentication:** Required
**Roles:** Any authenticated user

**Query Parameters:**
- `q` (required): Search query
- `k` (optional, default: 10): Maximum results
- `creator_id` (optional): Filter by creator

**Request:**
```bash
# Basic search
curl -X GET "http://localhost:8090/search?q=authentication" \
  -H "Authorization: Bearer $JWT_TOKEN"

# Search with limit
curl -X GET "http://localhost:8090/search?q=authentication&k=5" \
  -H "Authorization: Bearer $JWT_TOKEN"

# Search user's requirements
curl -X GET "http://localhost:8090/search?q=authentication&creator_id=user-123" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "query": "authentication",
  "results": [
    {
      "id": 42,
      "title": "User Authentication System",
      "snippet": "...implement OAuth2-based <b>authentication</b> with JWT tokens...",
      "similarity_score": 0.92,
      "creator_id": "user-123"
    },
    {
      "id": 43,
      "title": "Multi-Factor Authentication",
      "snippet": "Add second factor <b>authentication</b> for enhanced security",
      "similarity_score": 0.87,
      "creator_id": "user-123"
    }
  ],
  "count": 2
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `400 Bad Request`: Missing search query

### GET /search/all

Search across requirements, principles, and decisions in one call. Same `q`, `k`, `creator_id` query params as `/search`.

### GET /search/decisions

Decisions-only full-text search.

### GET /search/principles

Principles-only full-text search.

### GET /semantic_search/

Semantic search with NER and TF-IDF similarity.

**Authentication:** Required
**Roles:** Any authenticated user

**Query Parameters:**
- `q` (required): Search query

**Request:**
```bash
curl -X GET "http://localhost:8090/semantic_search/?q=user+login+security" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (200):
```json
{
  "query": "user login security",
  "entities": {
    "PERSON": [],
    "ORG": [],
    "PRODUCT": ["user login"]
  },
  "results": [
    {
      "id": 42,
      "title": "User Authentication System",
      "tfidf_score": 0.85,
      "creator_id": "user-123"
    }
  ],
  "count": 1
}
```

**Error Responses:**
- `401 Unauthorized`: Missing or invalid token
- `400 Bad Request`: Missing search query

## Error Responses

### Standard Error Format

All errors follow this format:

```json
{
  "error": {
    "message": "Error description",
    "status": 401
  }
}
```

### HTTP Status Codes

| Code | Status | Description |
|------|--------|-------------|
| 200 | OK | Request successful |
| 201 | Created | Resource created successfully |
| 400 | Bad Request | Invalid request (missing parameters, etc.) |
| 401 | Unauthorized | Missing or invalid JWT token |
| 403 | Forbidden | Valid token but insufficient permissions |
| 404 | Not Found | Resource not found |
| 422 | Unprocessable Entity | Validation error in request body |
| 500 | Internal Server Error | Server error |

### Authentication Errors

#### Missing Token

**Request:**
```bash
curl http://localhost:8090/requirements
```

**Response** (401):
```json
{
  "error": {
    "message": "Missing authentication token",
    "status": 401
  }
}
```

**Headers:**
```
WWW-Authenticate: Bearer
```

#### Invalid Token

**Request:**
```bash
curl -H "Authorization: Bearer invalid_token" \
  http://localhost:8090/requirements
```

**Response** (401):
```json
{
  "error": {
    "message": "Invalid token: Signature verification failed",
    "status": 401
  }
}
```

#### Expired Token

**Request:**
```bash
curl -H "Authorization: Bearer $EXPIRED_TOKEN" \
  http://localhost:8090/requirements
```

**Response** (401):
```json
{
  "error": {
    "message": "Invalid token: Signature has expired",
    "status": 401
  }
}
```

#### Insufficient Permissions

**Request:**
```bash
curl -X DELETE "http://localhost:8090/requirements/42?creator_id=other-user" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

**Response** (403):
```json
{
  "error": {
    "message": "You don't have permission to delete this requirement",
    "status": 403
  }
}
```

### Validation Errors

**Request:**
```bash
curl -X POST "http://localhost:8090/requirements" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "",
    "effort": -5
  }'
```

**Response** (422):
```json
{
  "detail": [
    {
      "loc": ["body", "title"],
      "msg": "ensure this value has at least 1 characters",
      "type": "value_error.any_str.min_length"
    },
    {
      "loc": ["body", "effort"],
      "msg": "ensure this value is greater than 0.001",
      "type": "value_error.number.not_gt"
    },
    {
      "loc": ["body", "description"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}
```

## Next Steps

- Review [JWT_AUTHENTICATION.md](JWT_AUTHENTICATION.md) for authentication details
- See [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) for implementation guidance
- Read [SECURITY_AUDIT.md](SECURITY_AUDIT.md) for security best practices
