"""
Authentication Module

Uses license token authentication only. No Keycloak/JWT.

Main exports:
    - get_tenant_id: FastAPI dependency for tenant context from license token
    - get_license_info: FastAPI dependency for full license introspection data
    - get_resource_with_tenant_check: BOLA protection for resource access
    - User: User model with tenant info
    - AuthMiddleware: Middleware for license token validation

Usage Example:
    from fastapi import Depends
    from auth import get_tenant_id

    @app.get("/api/requirements")
    async def get_requirements(tenant_id: str = Depends(get_tenant_id)):
        return await fetch_requirements(tenant_id)
"""

# Middleware
from .middleware import AuthMiddleware

# Models
from .models import User

# Tenant context
from .tenant import (
    get_license_info,
    get_resource_with_tenant_check,
    get_subject_id,
    get_tenant_id,
    get_visibility,
)

__all__ = [
    # FastAPI Dependencies (Primary Interface)
    "get_tenant_id",
    "get_subject_id",
    "get_visibility",
    "get_license_info",
    "get_resource_with_tenant_check",
    # Models
    "User",
    # Middleware
    "AuthMiddleware",
]
