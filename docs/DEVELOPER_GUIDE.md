# Developer Guide - JWT Authentication

## Table of Contents
- [Getting Started](#getting-started)
- [Local Development Setup](#local-development-setup)
- [Adding Authenticated Endpoints](#adding-authenticated-endpoints)
- [Testing with JWT Tokens](#testing-with-jwt-tokens)
- [Environment Configuration](#environment-configuration)
- [Database Integration](#database-integration)
- [Common Patterns](#common-patterns)
- [Debugging Authentication](#debugging-authentication)

## Getting Started

This guide helps developers implement JWT authentication in new endpoints and understand the authentication flow.

### Prerequisites

- Python 3.11+
- Docker and Docker Compose
- Access to Keycloak instance
- Familiarity with FastAPI and async Python

### Quick Start

```bash
# Clone repository
cd /Users/mini1/Movies/requirements_api

# Copy environment template
cp .env.example .env

# Edit .env with your Keycloak credentials
vim .env

# Start services
docker-compose up -d

# Check API health
curl http://localhost:8090/health
```

## Local Development Setup

### 1. Environment Configuration

Create or update `.env` file:

```bash
# Database
DATABASE_URL=postgresql+asyncpg://app:app@db:5432/reqs
POSTGRES_PASSWORD_REQUIREMENTS=your_secure_password

# Keycloak Configuration
KEYCLOAK_URL=https://keycloak.example.com
KEYCLOAK_REALM=secure-apps
KEYCLOAK_CLIENT_ID=requirements
KEYCLOAK_CLIENT_SECRET=your_client_secret
KEYCLOAK_SSL_VERIFY=true
```

### 2. Install Dependencies

Using UV (recommended):
```bash
# Sync all dependencies (including dev dependencies)
uv sync --all-extras

# Activate shell
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

Or using pip:
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run Database Migrations

```bash
# Apply all migrations
alembic upgrade head

# Create new migration (after model changes)
alembic revision --autogenerate -m "Description of changes"
```

### 4. Start Development Server

```bash
# With auto-reload
uvicorn app.main:app --reload --host 0.0.0.0 --port 8090

# Or using Docker Compose
docker-compose up -d
docker-compose logs -f api
```

### 5. Access API Documentation

- Swagger UI: http://localhost:8090/docs
- ReDoc: http://localhost:8090/redoc
- OpenAPI JSON: http://localhost:8090/openapi.json

## Adding Authenticated Endpoints

### Pattern 1: Basic Authentication (All Authenticated Users)

```python
from fastapi import Depends
from app.auth import get_current_user, KeycloakUser


@app.get("/api/profile")
async def get_profile(user: KeycloakUser = Depends(get_current_user)):
    """Get current user's profile.

    Any authenticated user can access this endpoint.
    """
    return {
        "user_id": user.sub,
        "username": user.preferred_username,
        "email": user.email,
        "roles": user.get_all_roles(),
    }
```

### Pattern 2: Resource Ownership Verification

```python
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.auth import get_current_user, KeycloakUser
from app.db import get_session
from app.models import Requirement


@app.get("/requirements/{requirement_id}")
async def get_requirement(
    requirement_id: int,
    user: KeycloakUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get requirement - verifies ownership."""

    # Fetch requirement
    result = await session.execute(select(Requirement).where(Requirement.id == requirement_id))
    requirement = result.scalar_one_or_none()

    if not requirement:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requirement not found")

    # Verify ownership
    if requirement.creator_id != user.sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this requirement",
        )

    return requirement
```

### Pattern 3: Role-Based Access Control

```python
from app.auth import require_role, require_any_role, KeycloakUser


# Single role required
@app.post("/admin/settings")
async def update_settings(
    settings: SettingsUpdate, user: KeycloakUser = Depends(require_role("admin"))
):
    """Admin-only endpoint.

    Returns 403 if user doesn't have 'admin' role.
    """
    return {"message": "Settings updated"}


# Multiple roles (any one required)
@app.put("/requirements/{id}")
async def update_requirement(
    id: int,
    payload: RequirementIn,
    user: KeycloakUser = Depends(require_any_role("editor", "admin")),
):
    """Update requirement - requires editor or admin role."""
    return {"message": "Updated successfully"}
```

### Pattern 4: Custom Authorization Logic

```python
from app.auth import get_current_user, KeycloakUser


def require_admin_or_owner(requirement_id: int):
    """Custom dependency: require admin role OR resource ownership."""

    async def checker(
        user: KeycloakUser = Depends(get_current_user), session: AsyncSession = Depends(get_session)
    ) -> KeycloakUser:
        # Admin always has access
        if user.has_realm_role("admin"):
            return user

        # Otherwise, verify ownership
        result = await session.execute(select(Requirement).where(Requirement.id == requirement_id))
        requirement = result.scalar_one_or_none()

        if not requirement or requirement.creator_id != user.sub:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role or resource ownership required",
            )

        return user

    return checker


# Usage
@app.delete("/requirements/{id}")
async def delete_requirement(
    id: int,
    user: KeycloakUser = Depends(require_admin_or_owner(id)),
    session: AsyncSession = Depends(get_session),
):
    """Delete requirement - admin or owner only."""
    # Delete logic here
    pass
```

### Pattern 5: Automatic creator_id Assignment

```python
from pydantic import BaseModel
from app.auth import get_current_user, KeycloakUser


class RequirementCreate(BaseModel):
    """Schema without creator_id (assigned automatically)."""

    title: str
    description: str
    effort: float


@app.post("/requirements")
async def create_requirement(
    payload: RequirementCreate,
    user: KeycloakUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Create requirement - creator_id assigned from token."""

    # Create requirement with authenticated user's ID
    requirement = Requirement(
        title=payload.title,
        description=payload.description,
        effort=payload.effort,
        creator_id=user.sub,  # From JWT token
        created_at=datetime.utcnow(),
    )

    session.add(requirement)
    await session.commit()
    await session.refresh(requirement)

    return requirement
```

## Testing with JWT Tokens

### 1. Obtaining Test Tokens

#### Option A: Using Keycloak Token Endpoint

```bash
# Get access token
curl -X POST "https://keycloak.example.com/realms/secure-apps/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=requirements" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "grant_type=password" \
  -d "username=testuser" \
  -d "password=testpass"

# Response:
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_in": 300,
  "refresh_expires_in": 1800,
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer"
}

# Save token to environment variable
export JWT_TOKEN="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."
```

#### Option B: Using Python Script

Create `get_token.py`:
```python
import requests
import os
from dotenv import load_dotenv

load_dotenv()


def get_token(username: str, password: str) -> str:
    """Get JWT token from Keycloak."""
    url = f"{os.getenv('KEYCLOAK_URL')}/realms/{os.getenv('KEYCLOAK_REALM')}/protocol/openid-connect/token"

    data = {
        "client_id": os.getenv("KEYCLOAK_CLIENT_ID"),
        "client_secret": os.getenv("KEYCLOAK_CLIENT_SECRET"),
        "grant_type": "password",
        "username": username,
        "password": password,
    }

    response = requests.post(url, data=data, verify=False)
    response.raise_for_status()

    return response.json()["access_token"]


if __name__ == "__main__":
    token = get_token("testuser", "testpass")
    print(f"Token: {token}")
```

Run:
```bash
python get_token.py
```

### 2. Testing Endpoints with pytest

Create `tests/test_auth.py`:
```python
import pytest
from httpx import AsyncClient
from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
def auth_headers(jwt_token):
    """Fixture providing authentication headers."""
    return {"Authorization": f"Bearer {jwt_token}"}


@pytest.mark.asyncio
async def test_create_requirement_authenticated(client, auth_headers):
    """Test creating requirement with valid token."""
    payload = {
        "title": "Test Requirement",
        "description": "Test description",
        "effort": 5.0,
        "links": {"business_objectives": ["Test"]},
    }

    response = await client.post("/requirements", json=payload, headers=auth_headers)

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Test Requirement"
    assert "creator_id" in data


@pytest.mark.asyncio
async def test_create_requirement_no_token(client):
    """Test creating requirement without token - should fail."""
    payload = {"title": "Test Requirement", "description": "Test description", "effort": 5.0}

    response = await client.post("/requirements", json=payload)

    assert response.status_code == 401
    assert "error" in response.json()


@pytest.mark.asyncio
async def test_access_other_user_requirement(client, auth_headers):
    """Test accessing another user's requirement - should fail."""
    # Create requirement as user A
    response = await client.post(
        "/requirements",
        json={"title": "User A Req", "description": "Test", "effort": 1.0},
        headers=auth_headers,
    )
    req_id = response.json()["id"]

    # Try to access as user B (different token)
    other_user_headers = {"Authorization": "Bearer OTHER_USER_TOKEN"}
    response = await client.get(f"/requirements/{req_id}", headers=other_user_headers)

    assert response.status_code == 403
```

### 3. Manual Testing with curl

```bash
# Set token variable
export TOKEN="your_jwt_token_here"

# Create requirement
curl -X POST "http://localhost:8090/requirements" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "New Requirement",
    "description": "Test requirement",
    "effort": 5.0,
    "links": {"business_objectives": ["Security"]}
  }'

# List requirements
curl -X GET "http://localhost:8090/requirements" \
  -H "Authorization: Bearer $TOKEN"

# Get specific requirement
curl -X GET "http://localhost:8090/requirements/1" \
  -H "Authorization: Bearer $TOKEN"

# Update requirement
curl -X PUT "http://localhost:8090/requirements/1" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Updated Requirement",
    "description": "Updated description",
    "effort": 8.0
  }'

# Delete requirement
curl -X DELETE "http://localhost:8090/requirements/1" \
  -H "Authorization: Bearer $TOKEN"
```

### 4. Testing Invalid Tokens

```bash
# No token (should return 401)
curl -v http://localhost:8090/requirements

# Invalid token (should return 401)
curl -H "Authorization: Bearer invalid_token" \
  http://localhost:8090/requirements

# Expired token (should return 401)
curl -H "Authorization: Bearer $EXPIRED_TOKEN" \
  http://localhost:8090/requirements

# Malformed header (should return 401)
curl -H "Authorization: invalid_format" \
  http://localhost:8090/requirements
```

## Environment Configuration

### Required Environment Variables

| Variable | Description | Example | Required |
|----------|-------------|---------|----------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql+asyncpg://app:app@db:5432/reqs` | Yes |
| `KEYCLOAK_URL` | Keycloak server URL | `https://keycloak.example.com` | Yes |
| `KEYCLOAK_REALM` | Keycloak realm name | `secure-apps` | Yes |
| `KEYCLOAK_CLIENT_ID` | OAuth2 client ID | `requirements` | Yes |
| `KEYCLOAK_CLIENT_SECRET` | OAuth2 client secret | `your_secret` | Yes |
| `KEYCLOAK_SSL_VERIFY` | Verify SSL certificates | `true` or `false` | No (default: `true`) |

### Development vs Production Settings

**Development** (`.env`):
```bash
KEYCLOAK_URL=http://localhost:8080
KEYCLOAK_SSL_VERIFY=false  # Only for local development!
```

**Production** (`.env` or secrets manager):
```bash
KEYCLOAK_URL=https://keycloak.production.com
KEYCLOAK_SSL_VERIFY=true  # Always true in production!
KEYCLOAK_CLIENT_SECRET=<use-secrets-manager>
```

### Docker Compose Configuration

`docker-compose.yml`:
```yaml
services:
  api:
    build: .
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - KEYCLOAK_URL=${KEYCLOAK_URL}
      - KEYCLOAK_REALM=${KEYCLOAK_REALM}
      - KEYCLOAK_CLIENT_ID=${KEYCLOAK_CLIENT_ID}
      - KEYCLOAK_CLIENT_SECRET=${KEYCLOAK_CLIENT_SECRET}
      - KEYCLOAK_SSL_VERIFY=${KEYCLOAK_SSL_VERIFY:-true}
    env_file:
      - .env
```

## Database Integration

### Updating Schemas to Use creator_id

#### 1. Add creator_id to Pydantic Schemas

```python
from pydantic import BaseModel, Field


class RequirementCreate(BaseModel):
    """Creation schema - no creator_id needed (from token)."""

    title: str = Field(..., min_length=1, max_length=300)
    description: str = Field(..., min_length=1)
    effort: float = Field(..., gt=0)


class RequirementResponse(BaseModel):
    """Response schema - includes creator_id."""

    id: int
    title: str
    description: str
    effort: float
    creator_id: str  # From database
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
```

#### 2. Update SQLAlchemy Models

```python
from sqlalchemy import Column, Integer, String, Float, DateTime
from sqlalchemy.sql import func


class Requirement(Base):
    __tablename__ = "requirements"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(300), nullable=False)
    description = Column(String, nullable=False)
    effort = Column(Float, nullable=False)
    creator_id = Column(String(100), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())
```

#### 3. Create Migration

```bash
# Generate migration
alembic revision --autogenerate -m "Add creator_id to requirements"

# Review migration file in alembic/versions/
# Edit if necessary

# Apply migration
alembic upgrade head
```

#### 4. Update Endpoint Logic

```python
@app.post("/requirements", response_model=RequirementResponse)
async def create_requirement(
    payload: RequirementCreate,
    user: KeycloakUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Create requirement with automatic creator_id."""

    requirement = Requirement(
        **payload.model_dump(),
        creator_id=user.sub,  # From authenticated user
    )

    session.add(requirement)
    await session.commit()
    await session.refresh(requirement)

    return requirement


@app.get("/requirements", response_model=List[RequirementResponse])
async def list_requirements(
    user: KeycloakUser = Depends(get_current_user), session: AsyncSession = Depends(get_session)
):
    """List user's own requirements."""

    result = await session.execute(select(Requirement).where(Requirement.creator_id == user.sub))
    requirements = result.scalars().all()

    return requirements
```

## Common Patterns

### Pattern: Optional Authentication

Make authentication optional for certain endpoints:

```python
from typing import Optional
from fastapi import Request


def get_optional_user(request: Request) -> Optional[KeycloakUser]:
    """Get user if authenticated, None otherwise."""
    return getattr(request.state, "user", None)


@app.get("/requirements")
async def list_requirements(
    user: Optional[KeycloakUser] = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
):
    """List requirements - authenticated users see only theirs."""

    query = select(Requirement)

    # Filter by user if authenticated
    if user:
        query = query.where(Requirement.creator_id == user.sub)

    result = await session.execute(query)
    return result.scalars().all()
```

### Pattern: Audit Logging

Log authentication events:

```python
import logging

logger = logging.getLogger(__name__)


@app.post("/requirements")
async def create_requirement(
    payload: RequirementCreate,
    user: KeycloakUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Create requirement with audit logging."""

    logger.info(
        f"User {user.preferred_username} ({user.sub}) creating requirement: {payload.title}"
    )

    requirement = Requirement(**payload.model_dump(), creator_id=user.sub)

    session.add(requirement)
    await session.commit()

    logger.info(f"Requirement {requirement.id} created by {user.sub}")

    return requirement
```

### Pattern: Multi-Tenancy

Support multiple organizations/tenants:

```python
from app.auth import get_current_user, KeycloakUser


def get_user_tenant_id(user: KeycloakUser = Depends(get_current_user)) -> str:
    """Extract tenant ID from user's token."""
    # Assuming tenant_id is in token claims
    return user.resource_access.get("requirements", {}).get("tenant_id", "default")


@app.get("/requirements")
async def list_requirements(
    user: KeycloakUser = Depends(get_current_user),
    tenant_id: str = Depends(get_user_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """List requirements filtered by tenant."""

    result = await session.execute(
        select(Requirement).where(
            Requirement.creator_id == user.sub, Requirement.tenant_id == tenant_id
        )
    )
    return result.scalars().all()
```

## Debugging Authentication

### Enable Debug Logging

Add to `app/main.py`:
```python
import logging

# Configure logging
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# Enable auth module logging
logging.getLogger("app.auth").setLevel(logging.DEBUG)
```

### Check Middleware Execution

Add logging to middleware:
```python
# app/auth/middleware.py
logger.debug(f"Request path: {request.url.path}")
logger.debug(f"Authorization header: {request.headers.get('Authorization', 'None')}")
logger.debug(f"Is excluded: {self._is_excluded(request.url.path)}")
```

### Inspect Token Contents

```python
import base64
import json


def decode_token_payload(token: str) -> dict:
    """Decode JWT payload (without verification - for debugging only)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid token format")

    # Decode payload (add padding if needed)
    payload = parts[1]
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += "=" * padding

    decoded = base64.urlsafe_b64decode(payload)
    return json.loads(decoded)


# Usage
token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."
payload = decode_token_payload(token)
print(json.dumps(payload, indent=2))
```

### Test Authentication Flow

```python
# tests/debug_auth.py
import asyncio
from app.auth.validator import get_validator


async def test_token_validation():
    validator = get_validator()

    token = "your_test_token_here"

    try:
        payload = await validator.validate_token(token)
        print("Token valid!")
        print(f"User ID: {payload['sub']}")
        print(f"Username: {payload['preferred_username']}")
        print(f"Roles: {payload['realm_access']['roles']}")
    except Exception as e:
        print(f"Token validation failed: {e}")


if __name__ == "__main__":
    asyncio.run(test_token_validation())
```

### Common Debug Scenarios

#### Scenario: Middleware not executing
```bash
# Check middleware is registered
docker-compose exec api python -c "from app.main import app; print(app.middleware)"
```

#### Scenario: Wrong public key
```bash
# Test public key fetch
curl https://keycloak.example.com/realms/secure-apps/protocol/openid-connect/certs
```

#### Scenario: Token expiration issues
```bash
# Decode token and check exp claim
python -c "import jwt; print(jwt.decode('$TOKEN', options={'verify_signature': False}))"
```

## Quality Evaluation

Most evaluable entities (Requirement, Principle, Decision, ArchitectureElement,
ArchitectureView, Activity, Risk, Benefit, Buffer) expose two evaluation
endpoints and persist the results on the row itself.

### Heuristic vs LLM

- **Heuristic** — pure-Python, fast, deterministic. Lives under `src/quality.py`,
  `src/quality_arch.py`, `src/quality_pm.py`. Run on every CRUD path through
  `EntityService.create/update` (see [Lifecycle Quality
  Evaluation](#lifecycle-quality-evaluation-phase-5)) and exposed at
  `POST /<entity>/{id}/quality`.
- **LLM** — calls a worker_management agent (per Section 4 of the
  [tooling spec](ARCHITECTURE_PM_TOOLING_SPEC.md)). Slow, debounced, async.
  Exposed at `POST /<entity>/{id}/evaluate`. Tenant ID is forwarded as
  `X-Tenant-Id`.

### `/quality` vs `/evaluate`

| Endpoint | Backing | Persistence | Latency |
|---|---|---|---|
| `POST /<entity>/{id}/quality` | Heuristic (in-process) | Synchronous, persists to row | < 50 ms |
| `POST /<entity>/{id}/evaluate` | LLM agent via worker_management | Synchronous call, persists to row | seconds–tens of seconds |

Both endpoints write the same persisted columns:

| Column | Type | Notes |
|---|---|---|
| `quality_score` | Float (0.0–1.0) | Latest score |
| `quality_issues` | JSONB array | List of issue strings |
| `quality_suggestions` | JSONB array | List of remediation suggestions |
| `last_evaluated_at` | DateTime | UTC timestamp |
| `last_evaluated_by_agent` | String | `"heuristic"` for `/quality`; the agent name for `/evaluate` |

`QualityMixin` (in `src/models/quality_mixin.py`) provides these columns; nine
ORM models inherit from it. The persistence layer lives in
`src/services/quality_persistence.py`.

### Two heuristic-only entities

`activity` and `principle` have no per-entity LLM agent. The LLM-quality path
for these is project- or tenant-scoped instead:

- `activity` → `POST /pm/projects/{eid}/wbs/evaluate` (whole WBS at once)
- `principle` → `POST /principles/consistency` (cross-principle check)

`register_all()` in `src/services/eval_registrations.py` does not register an
LLM dispatcher for these types, so create/update on them never schedules a
background LLM call.

## Adding a new LLM agent

The plumbing is uniform — adding an agent is mechanical:

1. **Add an `AgentConfig` entry** in `src/agents/config.py` with the agent's
   `name`, `temperature`, `max_tokens`, and `system_prompt`.
2. **Add a Pydantic result model** in `src/agents/models.py` (e.g.
   `MyAgentResult(BaseModel): ...`). The schema this model produces becomes
   the agent's `output_schema` at registration time.
3. **Create `src/agents/<name>.py`** subclassing `BaseAgent`. Override
   `build_user_prompt(payload)` and `parse(raw)` (parse usually just
   `MyAgentResult.model_validate(raw)`).
4. **Register `output_schema`** in
   `scripts/register_workers.py:OUTPUT_SCHEMA_BY_AGENT` so `register_workers.py`
   pushes the schema to worker_management on next run.
5. **Wire the endpoint** in `src/routers/llm_tools.py` via the shared
   `_run_agent` helper. The helper handles agent invocation, tenant-ID
   forwarding, retry/backoff, and structured-error mapping.
6. **Add a request schema** in `src/schemas/tools.py` (e.g.
   `MyAgentRequest(BaseModel)`).
7. **(Optional) Register an LLM dispatcher** in
   `src/services/eval_registrations.py` if the agent should run automatically
   on entity create/update with debouncing. Heuristic-only entities skip this
   step.
8. **Run** `uv run python scripts/register_workers.py` against
   worker_management (operational; the script is idempotent — re-run on every
   agent change).

Tests for new agents live in `tests/test_llm_agents_new.py` (per-agent unit
test) and `tests/test_llm_wiring.py` (router/wiring test).

## Architecture vs PM module map

Quick reference of which entities expose which evaluation paths.

| Entity | `/quality` (heuristic) | `/evaluate` (LLM) | LLM agent | Auto-eval on create/update |
|---|:---:|:---:|---|:---:|
| Requirement | yes | via `/llm/tools/requirement-quality-llm` | `requirement_quality_llm` | yes |
| Principle | yes | yes | `arch_description_quality` | yes (heuristic only) |
| Decision | yes | yes | `arch_decision_quality` | yes |
| Architecture Element | yes | yes | `arch_description_quality` | yes |
| Architecture View | yes | yes | `arch_view_coherence` | yes |
| Architecture Standard | — | — | — | — |
| Programme / Project | — | wbs-evaluate (project) | `pm_wbs_assessor` | — |
| Activity | yes | (heuristic only — see WBS evaluate) | — | yes (heuristic only) |
| Resource | — | match-activity | `pm_resource_skill_match` | — |
| Risk | yes | yes | `pm_risk_quality` | yes |
| Benefit | yes | yes | `pm_benefit_quality` | yes |
| Buffer | yes | yes | `pm_buffer_justification` | yes |

Tenant-scoped or project-scoped agents (no per-entity endpoint):

- `arch_principles_consistency` → `POST /principles/consistency` (whole tenant)
- `pm_wbs_assessor` → `POST /pm/projects/{eid}/wbs/evaluate` (whole project)
- `pm_resource_skill_match` → `POST /pm/resources/match-activity/{activity_id}`
- `arch_gap_narrator`, `arch_relationship_suggester`,
  `pm_dependency_suggester`, `pm_stakeholder_impact` — exposed only as
  `/llm/tools/*` standalone endpoints; not wired to entity lifecycle.

## Lifecycle Quality Evaluation (Phase 5)

Every CRUD path that goes through `EntityService.create/update` or
`SimpleEntityService.create/update` runs the registered quality heuristic
synchronously and persists the resulting score before returning. The
heuristic functions are pure / fast and live in `src/quality.py`,
`src/quality_arch.py`, `src/quality_pm.py`. Mapping is wired in
`src/services/eval_registrations.py` and committed at app startup via
`register_all()`.

If a `BackgroundTasks` instance is injected by the router (and an LLM
dispatcher is registered for that entity type), the LLM evaluation is
scheduled in the background, debounced to ≤1 per `DEBOUNCE_S` (default 60s)
per `(entity_type, entity_id)`. The dispatcher opens its own DB session via
`AsyncSessionLocal`, calls the agent, persists results with
`agent_name=<dispatcher_name>`, and commits.

**Single-replica caveat:** the debounce timestamp store is in-memory
(`services.eval_dispatcher._last_llm_eval`). When the API is horizontally
scaled, swap this for a Redis-backed store with TTL.

### Bulk-import contract

`EntityService.create(..., evaluate=False)` and
`EntityService.update(..., evaluate=False)` skip both the heuristic and the
LLM dispatch. This is the contract any future bulk-import path **must** use
to avoid fanning the LLM out to N items per import. The default (`True`)
preserves the on-CRUD evaluate-on-change behavior.

There is no `/api/bulk/<entity>` import endpoint as of Phase 5; only bulk
DELETE endpoints exist in `src/routers/bulk.py`. When bulk import is added,
plumb `evaluate=False` from the router through to the service.

### Heuristic-only entities

Two evaluable entities are intentionally heuristic-only:

- `principle` — uses the tenant-scoped consistency endpoint
  (`POST /principles/consistency`); no per-entity LLM agent exists.
- `activity` — uses the project-scoped WBS evaluate endpoint
  (`POST /pm/projects/{id}/wbs/evaluate`); no per-entity LLM agent exists.

`register_all()` does not register an LLM dispatcher for these entity types,
so create/update on them never schedules a background LLM call.

## Next Steps

- Review [JWT_AUTHENTICATION.md](JWT_AUTHENTICATION.md) for authentication details
- See [API_ENDPOINTS.md](API_ENDPOINTS.md) for endpoint examples
- Read [SECURITY_AUDIT.md](SECURITY_AUDIT.md) for security best practices

## Support

For issues:
1. Check logs: `docker-compose logs -f api`
2. Verify environment variables: `docker-compose exec api env | grep KEYCLOAK`
3. Test Keycloak connectivity: `curl -v $KEYCLOAK_URL`
4. Review [Troubleshooting section](JWT_AUTHENTICATION.md#troubleshooting-common-issues) in JWT_AUTHENTICATION.md
