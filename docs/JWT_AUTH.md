# JWT Authentication Documentation

## Table of Contents
- [Overview](#overview)
- [How creator_id is Extracted from JWT Tokens](#how-creator_id-is-extracted-from-jwt-tokens)
- [Security Model](#security-model)
- [Environment Variables Required](#environment-variables-required)
- [Code Examples for Each Endpoint](#code-examples-for-each-endpoint)
- [Testing Guide](#testing-guide)
- [Troubleshooting Common Issues](#troubleshooting-common-issues)

## Overview

The Requirements API uses JWT (JSON Web Token) authentication to secure all API endpoints. JWT tokens are cryptographically signed tokens that contain user identity information and are validated on every request.

### Key Features

- **Stateless Authentication**: No server-side session storage required
- **Cryptographic Verification**: Tokens validated using RS256 algorithm with public key
- **User Isolation**: Users can only access their own data based on `creator_id`
- **Standards-Based**: Follows OAuth 2.0 and OpenID Connect standards
- **Production-Ready**: Supports multiple key sources and comprehensive error handling

### Architecture

The JWT authentication system consists of three main components:

1. **JWT Token Decoder**: Validates token signature and extracts claims
2. **Creator ID Extractor**: Extracts user identity from token payload
3. **FastAPI Dependency**: Injects `creator_id` into endpoint handlers

## How creator_id is Extracted from JWT Tokens

The `creator_id` is the unique identifier for each user in the system. It is extracted from JWT tokens in the following order of precedence:

### Claim Resolution Order

1. **`creator_id`** - Custom claim (highest priority)
2. **`sub`** - Standard OAuth/OIDC subject claim (recommended)
3. **`user_id`** - Alternative custom claim (fallback)

### Extraction Process

```python
def extract_creator_id(payload: dict[str, Any]) -> str:
    """Extract creator_id from JWT token payload."""
    # Try multiple possible claim names
    possible_claims = ["creator_id", "sub", "user_id"]

    for claim_name in possible_claims:
        creator_id = payload.get(claim_name)
        if creator_id:
            return str(creator_id)

    # If none found, raise error
    raise MissingCreatorIdError("Token does not contain creator_id")
```

### Token Payload Example

```json
{
  "sub": "auth0|12345",
  "creator_id": "user-12345",
  "email": "user@example.com",
  "name": "John Doe",
  "iat": 1699000000,
  "exp": 1699086400
}
```

In this example, the system will use `creator_id: "user-12345"` as the user identifier.

### Why This Matters

The `creator_id` is used throughout the API to:

- Filter database queries to only return user's own data
- Set ownership on newly created resources
- Enforce authorization rules
- Maintain data isolation between users

**Important**: The `creator_id` cannot be spoofed because it comes from a cryptographically validated JWT token. Only the authentication server (with the private key) can create valid tokens.

## Security Model

### Users Can Only Access Their Own Data

The API implements strict **creator-based isolation**. Every resource (requirement, principle, decision) is associated with a `creator_id`, and users can only:

- **Read**: Their own resources only
- **Create**: Resources automatically tagged with their `creator_id`
- **Update**: Only resources they created
- **Delete**: Only resources they created

### How It Works

1. **Token Validation**: Every request validates the JWT token signature
2. **Identity Extraction**: `creator_id` is extracted from validated token claims
3. **Query Filtering**: All database queries automatically filter by `creator_id`
4. **Ownership Checks**: Update/delete operations verify resource ownership

### Before vs. After JWT Implementation

#### Before (INSECURE - Fixed)

```python
# ❌ VULNERABLE - User could spoof creator_id via header
@app.get("/requirements")
async def get_requirements(creator_id: str = Header(..., alias="X-Creator-ID")):
    # Attacker could send: X-Creator-ID: victim-user-id
    # and access victim's data!
    return db.query(Requirement).filter(Requirement.creator_id == creator_id).all()
```

**Attack Vector**: Any user could set `X-Creator-ID` header to another user's ID and access their data.

#### After (SECURE)

```python
# ✅ SECURE - creator_id comes from validated JWT token
@app.get("/requirements")
async def get_requirements(creator_id: str = Depends(get_creator_id_from_token)):
    # creator_id is cryptographically verified from JWT token
    # Impossible to spoof without the private key
    return db.query(Requirement).filter(Requirement.creator_id == creator_id).all()
```

**Security Guarantee**: The `creator_id` is extracted from a cryptographically signed JWT token. Users cannot access other users' data.

### Security Properties

1. **Authentication**: Token signature proves token was issued by trusted auth server
2. **Authorization**: Token claims define what user can access
3. **Non-repudiation**: Tokens include timestamps and issuer information
4. **Data Isolation**: Database queries automatically filtered by validated `creator_id`
5. **No Privilege Escalation**: Users cannot modify their own `creator_id` claim

## Environment Variables Required

The following environment variables must be configured for JWT authentication to work:

### Required Variables

#### JWT_PUBLIC_KEY or JWT_PUBLIC_KEY_PATH

**Purpose**: RSA public key used to verify JWT token signatures.

**Usage**:
- **JWT_PUBLIC_KEY**: Directly provide the public key as a string (recommended for containers)
- **JWT_PUBLIC_KEY_PATH**: Path to a PEM file containing the public key (recommended for development)

**Example (Direct Key)**:
```bash
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA...
-----END PUBLIC KEY-----"
```

**Example (File Path)**:
```bash
JWT_PUBLIC_KEY_PATH=/app/certs/public_key.pem
```

**Format**: RSA public key in PEM format (PKCS#8)

**How to Obtain**:
- From Keycloak: Go to Realm Settings → Keys → RS256 → Public Key
- From Auth0: Go to Applications → Settings → Advanced Settings → Certificates
- From custom OAuth server: Export public key from key pair

#### JWT_ALGORITHM (Default: RS256)

**Purpose**: Algorithm used for JWT signature verification.

**Supported Values**:
- `RS256` - RSA with SHA-256 (recommended for production)
- `RS384` - RSA with SHA-384
- `RS512` - RSA with SHA-512
- `HS256` - HMAC with SHA-256 (symmetric key, less secure)
- `HS384` - HMAC with SHA-384 (symmetric key, less secure)
- `HS512` - HMAC with SHA-512 (symmetric key, less secure)

**Recommendation**: Use `RS256` (asymmetric) for production environments.

**Example**:
```bash
JWT_ALGORITHM=RS256
```

### Optional Variables

#### JWT_ISSUER (Optional)

**Purpose**: Expected token issuer (iss claim). If set, tokens from other issuers will be rejected.

**Use Case**: Restrict tokens to specific authentication provider.

**Example**:
```bash
JWT_ISSUER=https://auth.example.com
JWT_ISSUER=https://your-tenant.auth0.com/
JWT_ISSUER=https://keycloak.example.com/realms/your-realm
```

**Behavior**:
- If set: Tokens must have matching `iss` claim
- If unset: Issuer is not validated (less secure)

#### JWT_AUDIENCE (Optional)

**Purpose**: Expected token audience (aud claim). If set, tokens without matching audience will be rejected.

**Use Case**: Ensure tokens are intended for your API.

**Example**:
```bash
JWT_AUDIENCE=https://api.example.com
JWT_AUDIENCE=requirements-api
```

**Behavior**:
- If set: Tokens must have matching `aud` claim
- If unset: Audience is not validated

### Configuration Summary

**Minimal Configuration** (Development):
```bash
JWT_PUBLIC_KEY_PATH=./certs/public_key.pem
JWT_ALGORITHM=RS256
```

**Recommended Configuration** (Production):
```bash
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----..."
JWT_ALGORITHM=RS256
JWT_ISSUER=https://your-auth-server.com
JWT_AUDIENCE=requirements-api
```

### Environment Variable Precedence

1. **JWT_PUBLIC_KEY** takes precedence over `JWT_PUBLIC_KEY_PATH`
2. If neither is set, application will fail to start
3. **JWT_ALGORITHM** defaults to `RS256` if not specified
4. **JWT_ISSUER** and **JWT_AUDIENCE** are optional

## Code Examples for Each Endpoint

All endpoints (except public ones) use the `get_creator_id_from_token` dependency to automatically extract and validate the user's identity from the JWT token.

### Requirements Endpoints

#### Create Requirement

```python
@app.post("/requirements", response_model=RequirementRead)
async def create_requirement(
    requirement: RequirementCreate,
    creator_id: str = Depends(get_creator_id_from_token),
    db: AsyncSession = Depends(get_db),
):
    """Create a new requirement for the authenticated user."""
    db_requirement = Requirement(
        **requirement.model_dump(),
        creator_id=creator_id,  # Automatically set from JWT token
    )
    db.add(db_requirement)
    await db.commit()
    await db.refresh(db_requirement)
    return db_requirement
```

**cURL Example**:
```bash
curl -X POST "http://localhost:8090/requirements" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "User Authentication",
    "description": "Implement JWT-based authentication",
    "project_id": "proj-123"
  }'
```

#### List Requirements

```python
@app.get("/requirements", response_model=list[RequirementRead])
async def get_requirements(
    creator_id: str = Depends(get_creator_id_from_token),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
):
    """Get all requirements for the authenticated user."""
    result = await db.execute(
        select(Requirement)
        .filter(Requirement.creator_id == creator_id)  # Only user's data
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()
```

**cURL Example**:
```bash
curl -X GET "http://localhost:8090/requirements" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

#### Get Specific Requirement

```python
@app.get("/requirements/{requirement_id}", response_model=RequirementRead)
async def get_requirement(
    requirement_id: int,
    creator_id: str = Depends(get_creator_id_from_token),
    db: AsyncSession = Depends(get_db),
):
    """Get a specific requirement (only if owned by user)."""
    result = await db.execute(
        select(Requirement).filter(
            Requirement.id == requirement_id,
            Requirement.creator_id == creator_id,  # Ownership check
        )
    )
    requirement = result.scalar_one_or_none()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return requirement
```

**cURL Example**:
```bash
curl -X GET "http://localhost:8090/requirements/123" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

#### Update Requirement

```python
@app.put("/requirements/{requirement_id}", response_model=RequirementRead)
async def update_requirement(
    requirement_id: int,
    requirement: RequirementUpdate,
    creator_id: str = Depends(get_creator_id_from_token),
    db: AsyncSession = Depends(get_db),
):
    """Update a requirement (only if owned by user)."""
    result = await db.execute(
        select(Requirement).filter(
            Requirement.id == requirement_id,
            Requirement.creator_id == creator_id,  # Ownership check
        )
    )
    db_requirement = result.scalar_one_or_none()
    if not db_requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    # Update fields
    for key, value in requirement.model_dump(exclude_unset=True).items():
        setattr(db_requirement, key, value)

    await db.commit()
    await db.refresh(db_requirement)
    return db_requirement
```

**cURL Example**:
```bash
curl -X PUT "http://localhost:8090/requirements/123" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Updated Title",
    "description": "Updated description"
  }'
```

#### Delete Requirement

```python
@app.delete("/requirements/{requirement_id}", status_code=204)
async def delete_requirement(
    requirement_id: int,
    creator_id: str = Depends(get_creator_id_from_token),
    db: AsyncSession = Depends(get_db),
):
    """Delete a requirement (only if owned by user)."""
    result = await db.execute(
        select(Requirement).filter(
            Requirement.id == requirement_id,
            Requirement.creator_id == creator_id,  # Ownership check
        )
    )
    db_requirement = result.scalar_one_or_none()
    if not db_requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    await db.delete(db_requirement)
    await db.commit()
    return None
```

**cURL Example**:
```bash
curl -X DELETE "http://localhost:8090/requirements/123" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### Principles Endpoints

The principles endpoints follow the same pattern as requirements:

```python
# All principles endpoints use the same dependency
creator_id: str = Depends(get_creator_id_from_token)
```

**Examples**:
```bash
# Create principle
curl -X POST "http://localhost:8090/principles" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "API First", "statement": "Design APIs before implementation"}'

# List principles
curl -X GET "http://localhost:8090/principles" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Update principle
curl -X PUT "http://localhost:8090/principles/456" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "Updated Principle"}'
```

### Decisions Endpoints

Decision endpoints also use creator-based isolation:

```python
# All decisions endpoints use the same dependency
creator_id: str = Depends(get_creator_id_from_token)
```

**Examples**:
```bash
# Create decision
curl -X POST "http://localhost:8090/decisions" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "Use PostgreSQL", "project_id": "proj-123", "status": "accepted"}'

# List decisions
curl -X GET "http://localhost:8090/decisions" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Delete decision
curl -X DELETE "http://localhost:8090/decisions/789" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### Search Endpoints

Search endpoints also filter results by creator_id:

```bash
# Search requirements
curl -X GET "http://localhost:8090/search?q=authentication" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Semantic search
curl -X GET "http://localhost:8090/semantic_search/?q=security" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### Public Endpoints (No Authentication Required)

The following endpoints do NOT require authentication:

```bash
# Health check
curl -X GET "http://localhost:8090/health"

# API documentation
curl -X GET "http://localhost:8090/docs"

# OpenAPI schema
curl -X GET "http://localhost:8090/openapi.json"
```

## Testing Guide

### Testing with Real JWT Tokens

#### 1. Obtain a JWT Token

**Option A: Using Keycloak**
```bash
curl -X POST "https://keycloak.example.com/realms/your-realm/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" \
  -d "client_id=your-client-id" \
  -d "client_secret=your-client-secret" \
  -d "username=testuser" \
  -d "password=testpass"
```

**Option B: Using Auth0**
```bash
curl -X POST "https://YOUR_DOMAIN.auth0.com/oauth/token" \
  -H "Content-Type: application/json" \
  -d '{
    "grant_type": "password",
    "username": "user@example.com",
    "password": "password",
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "audience": "YOUR_API_IDENTIFIER"
  }'
```

**Response**:
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

#### 2. Test API Endpoints

```bash
# Store token in variable
TOKEN="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."

# Test authentication
curl -X GET "http://localhost:8090/requirements" \
  -H "Authorization: Bearer $TOKEN"

# Should return only your requirements
```

### Testing with pytest

```python
import pytest
from fastapi.testclient import TestClient
from jose import jwt
from datetime import datetime, timedelta


@pytest.fixture
def test_token():
    """Generate a test JWT token."""
    payload = {
        "sub": "test-user-123",
        "email": "test@example.com",
        "exp": datetime.utcnow() + timedelta(hours=1),
        "iat": datetime.utcnow(),
    }
    # Note: In tests, you'd use a test private key
    token = jwt.encode(payload, test_private_key, algorithm="RS256")
    return token


def test_create_requirement(client: TestClient, test_token: str):
    """Test creating a requirement with JWT auth."""
    response = client.post(
        "/requirements",
        json={
            "title": "Test Requirement",
            "description": "Test description",
            "project_id": "test-project",
        },
        headers={"Authorization": f"Bearer {test_token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["creator_id"] == "test-user-123"


def test_list_requirements_filtered(client: TestClient, test_token: str):
    """Test that requirements are filtered by creator_id."""
    # Create requirement as test-user-123
    client.post(
        "/requirements",
        json={"title": "My Requirement", "project_id": "test"},
        headers={"Authorization": f"Bearer {test_token}"},
    )

    # List requirements
    response = client.get("/requirements", headers={"Authorization": f"Bearer {test_token}"})
    assert response.status_code == 200
    requirements = response.json()

    # All requirements should belong to test-user-123
    assert all(req["creator_id"] == "test-user-123" for req in requirements)


def test_unauthorized_access(client: TestClient):
    """Test that requests without token are rejected."""
    response = client.get("/requirements")
    assert response.status_code == 401
    assert "Authorization header required" in response.json()["detail"]


def test_invalid_token(client: TestClient):
    """Test that invalid tokens are rejected."""
    response = client.get("/requirements", headers={"Authorization": "Bearer invalid-token-here"})
    assert response.status_code == 401
    assert "Invalid token" in response.json()["detail"]
```

### Manual Testing Checklist

- [ ] Can create requirement with valid JWT token
- [ ] Can list only own requirements
- [ ] Cannot access other user's requirements
- [ ] Cannot create requirement without token (401 error)
- [ ] Cannot use expired token (401 error)
- [ ] Cannot use token with invalid signature (401 error)
- [ ] Can update own requirement
- [ ] Cannot update other user's requirement (404 error)
- [ ] Can delete own requirement
- [ ] Cannot delete other user's requirement (404 error)

## Troubleshooting Common Issues

### Issue 1: "Authorization header required"

**Symptom**:
```json
{
  "detail": "Authorization header required"
}
```

**Cause**: The `Authorization` header is missing from the request.

**Solution**:
```bash
# Wrong
curl -X GET "http://localhost:8090/requirements"

# Correct
curl -X GET "http://localhost:8090/requirements" \
  -H "Authorization: Bearer YOUR_TOKEN_HERE"
```

### Issue 2: "Invalid authorization header format"

**Symptom**:
```json
{
  "detail": "Invalid authorization header format. Expected: 'Bearer <token>'"
}
```

**Cause**: The Authorization header doesn't follow the Bearer token format.

**Solution**:
```bash
# Wrong formats
Authorization: YOUR_TOKEN
Authorization: Token YOUR_TOKEN
Authorization: BearerYOUR_TOKEN

# Correct format (note the space after Bearer)
Authorization: Bearer YOUR_TOKEN
```

### Issue 3: "Invalid token"

**Symptom**:
```json
{
  "detail": "Invalid token"
}
```

**Possible Causes**:
1. Token signature is invalid
2. Token is malformed
3. Wrong public key configured
4. Token signed with different algorithm

**Solutions**:

**Check token is well-formed**:
```bash
# Decode token to inspect (without verification)
echo "YOUR_TOKEN" | cut -d. -f2 | base64 -d | jq .
```

**Verify public key matches**:
```bash
# Check what algorithm and key ID token uses
echo "YOUR_TOKEN" | cut -d. -f1 | base64 -d | jq .
```

**Check environment configuration**:
```bash
# Verify public key is loaded
docker exec requirements-api env | grep JWT_PUBLIC_KEY

# Verify algorithm matches
docker exec requirements-api env | grep JWT_ALGORITHM
```

### Issue 4: "Token has expired"

**Symptom**:
```json
{
  "detail": "Token has expired"
}
```

**Cause**: The JWT token's `exp` (expiration) claim indicates the token is no longer valid.

**Solution**: Obtain a new token from your authentication provider.

```bash
# Request new token from Keycloak
curl -X POST "https://keycloak.example.com/realms/your-realm/protocol/openid-connect/token" \
  -d "grant_type=refresh_token" \
  -d "client_id=your-client" \
  -d "refresh_token=YOUR_REFRESH_TOKEN"
```

### Issue 5: "Token does not contain required creator identification"

**Symptom**:
```json
{
  "detail": "Token does not contain required creator identification"
}
```

**Cause**: The JWT token doesn't have any of the expected claims (`creator_id`, `sub`, `user_id`).

**Solution**:

**Inspect token payload**:
```bash
echo "YOUR_TOKEN" | cut -d. -f2 | base64 -d | jq .
```

**Ensure your auth provider includes one of these claims**:
- `sub` (standard OIDC subject claim - recommended)
- `creator_id` (custom claim)
- `user_id` (custom claim)

**Keycloak**: The `sub` claim is included by default in all tokens.

**Auth0**: The `sub` claim is included by default.

**Custom OAuth**: Add a mapper/claim to include user ID in tokens.

### Issue 6: Public Key Not Loading

**Symptom**: Application fails to start or logs indicate public key errors.

**Possible Causes**:
1. `JWT_PUBLIC_KEY` and `JWT_PUBLIC_KEY_PATH` both unset
2. File path incorrect
3. File permissions incorrect
4. Invalid PEM format

**Solutions**:

**Check environment variables**:
```bash
docker exec requirements-api env | grep JWT_PUBLIC_KEY
```

**Verify file exists and is readable**:
```bash
docker exec requirements-api cat /app/certs/public_key.pem
```

**Check PEM format**:
```bash
# Should start with -----BEGIN PUBLIC KEY-----
# and end with -----END PUBLIC KEY-----
cat /path/to/public_key.pem
```

**Fix file permissions**:
```bash
chmod 644 /path/to/public_key.pem
```

### Issue 7: Cannot Access Resource (404)

**Symptom**: GET/PUT/DELETE returns 404 even though resource exists.

**Cause**: The resource belongs to a different user (different `creator_id`).

**Explanation**: This is intentional security behavior. Users can only access their own resources.

**To Verify**:
```bash
# Check who owns the resource (requires direct DB access)
docker exec requirements-db psql -U app -d reqs -c \
  "SELECT id, title, creator_id FROM requirements WHERE id = 123;"
```

If the `creator_id` doesn't match your JWT token's subject, you cannot access it.

### Issue 8: Token Works in Browser but Not in cURL

**Cause**: Token may contain special characters that need escaping, or token was copied with extra whitespace.

**Solution**:
```bash
# Store token in variable (avoid shell interpretation)
TOKEN='eyJhbGciOiJSUzI1NiI...'

# Use variable in request
curl -X GET "http://localhost:8090/requirements" \
  -H "Authorization: Bearer $TOKEN"

# Or use single quotes to prevent shell expansion
curl -X GET "http://localhost:8090/requirements" \
  -H 'Authorization: Bearer eyJhbGciOiJSUzI1NiI...'
```

### Debug Mode

Enable debug logging to see detailed JWT validation information:

```python
# In main.py or config
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("app.auth.jwt_auth")
logger.setLevel(logging.DEBUG)
```

Logs will show:
- Token extraction
- Signature verification attempts
- Claim extraction
- Validation errors with details

### Getting Help

If you continue to experience issues:

1. Check application logs: `docker-compose logs -f api`
2. Verify environment configuration: `docker exec requirements-api env | grep JWT`
3. Test with a known-good token from jwt.io
4. Check that public key matches the private key that signed the token
5. Ensure token hasn't expired (check `exp` claim)
6. Verify your authentication provider is accessible

---

## Summary

JWT authentication in the Requirements API provides:

- **Secure authentication** via cryptographic token verification
- **User isolation** with automatic creator_id filtering
- **Stateless design** for scalability
- **Standards compliance** with OAuth 2.0 / OIDC
- **Production-ready** with comprehensive error handling

All endpoints (except public ones) automatically extract `creator_id` from validated JWT tokens, ensuring users can only access their own data. This eliminates the critical security vulnerability of header-based user spoofing.

For more information, see:
- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) - Implementation patterns
- [API_ENDPOINTS.md](API_ENDPOINTS.md) - API reference
- [SECURITY_AUDIT.md](SECURITY_AUDIT.md) - Security best practices
