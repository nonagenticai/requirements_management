"""
Tenant Context Module for Multi-Tenant Isolation

Provides tenant ID extraction from X-Tenant-Id header (Bearer license token format).
Supports BOLA (Broken Object Level Authorization) protection for resource access.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from fastapi import Header, HTTPException, Request, status

logger = logging.getLogger(__name__)

T = TypeVar("T")

VISIBILITY_VALUES = frozenset({"tenant", "user"})


async def get_tenant_id(request: Request) -> str:
    """
    Extract tenant ID from request context.

    The tenant ID is extracted from the user context set by AuthMiddleware
    after validating the license token from X-Tenant-Id header.

    Args:
        request: FastAPI request object

    Returns:
        Tenant ID string

    Raises:
        HTTPException: 401 if no tenant context available
    """
    # Check request.state for user set by middleware
    if hasattr(request.state, "user") and request.state.user:
        user = request.state.user
        if hasattr(user, "tenant_id") and user.tenant_id:
            return str(user.tenant_id)
        if hasattr(user, "sub") and user.sub:
            return str(user.sub)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing tenant context",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_tenant_name(request: Request) -> str:
    """Extract human-readable tenant name from request context.

    Populated by AuthMiddleware from the X-Tenant-Name header (injected by
    license-authz post Phase 2 cutover). Returns "" when the header is absent
    (trusted-network callers, pre-cutover traffic) — callers that need a
    non-empty value fall back to tenant_id, which since Phase 4
    (``2026_08_24_0042``) is a UUID and therefore a poor display label. Prefer
    showing nothing over showing a UUID that looks like a name. Never raises.
    """
    explicit = getattr(request.state, "tenant_name", None)
    if explicit:
        return str(explicit)
    return ""


async def get_subject_id(request: Request) -> str:
    """
    Extract per-user subject_id (licenses Subject UUID) from request context.

    Set by AuthMiddleware after license introspection. Returns an empty
    string when no subject is available (trusted-network bypass without
    X-Subject-Id, missing introspect field, or public endpoint that ran
    middleware before any auth).

    Empty string is treated as "tenant-only caller" downstream:
    - list queries return only visibility='tenant' rows (matches the
      backfilled state — every existing row has visibility='tenant').
    - mutations land with subject_id := tenant_id, preserving legacy
      "any tenant member can edit" behavior bit-for-bit.

    Returning a string (never raising / never None) keeps this dependency
    optional from the caller's perspective and matches the pilot
    (procedure_management) pattern.
    """
    # Prefer explicit request.state.subject_id (set by middleware)
    explicit = getattr(request.state, "subject_id", None)
    if explicit:
        return str(explicit)

    # Fall back to user.subject_id for callers that only stash the User
    user = getattr(request.state, "user", None)
    if user is not None:
        sub = getattr(user, "subject_id", None)
        if sub:
            return str(sub)

    return ""


def get_visibility(
    x_visibility: Annotated[str, Header(alias="X-Visibility")] = "tenant",
) -> str:
    """Caller-supplied visibility for newly-created rows.

    Valid values: 'tenant' (default — every tenant member sees + may mutate)
    or 'user' (only the creating subject sees + may mutate). Invalid values
    silently coerce to 'tenant' — the safer default (exposes rather than
    over-restricts; rejecting would break legacy clients).

    Surfaces in OpenAPI via Header() so Orval / n8n consumers can discover it.
    Read paths (list/get/update/delete) ignore this header.
    """
    raw = x_visibility.strip().lower()
    return raw if raw in VISIBILITY_VALUES else "tenant"


async def get_license_info(request: Request) -> dict[str, Any]:
    """
    Get full license introspection data from request.

    Args:
        request: FastAPI request object

    Returns:
        License introspection dict

    Raises:
        HTTPException: 401 if no license info available
    """
    if hasattr(request.state, "license") and request.state.license:
        return dict(request.state.license)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing license context",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_resource_with_tenant_check(
    resource_id: int,
    tenant_id: str,
    db_func: Callable[[int], Awaitable[T | None]],
    resource_name: str = "Resource",
) -> T:
    """
    Retrieve a resource with BOLA (Broken Object Level Authorization) protection.

    Returns 404 for resources that don't exist or belong to a different tenant,
    preventing information disclosure about resource existence.

    Args:
        resource_id: The ID of the resource to retrieve
        tenant_id: The tenant ID from the request context
        db_func: Async callable that retrieves the resource by ID
        resource_name: Human-readable resource name for error messages

    Returns:
        The resource if it exists and belongs to the tenant

    Raises:
        HTTPException: 404 if resource doesn't exist or belongs to different tenant
    """
    resource = await db_func(resource_id)

    if resource is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_name} not found",
        )

    # Check tenant ownership - return 404 for wrong tenant (BOLA protection)
    resource_tenant = getattr(resource, "tenant_id", None)

    if resource_tenant != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_name} not found",
        )

    return resource
