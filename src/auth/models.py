"""User models for authentication."""

from pydantic import BaseModel, Field


class User(BaseModel):
    """User model extracted from license token introspection."""

    sub: str = Field(..., description="User/Tenant ID")
    preferred_username: str = Field(..., description="Username or subject ID")
    tenant_id: str | None = None
    subject_id: str | None = Field(
        default=None,
        description="Stable per-user subject UUID from the licenses Subject table; "
        "propagated as X-Subject-Id to downstream services for per-user metering.",
    )

    @property
    def effective_tenant_id(self) -> str:
        """Get effective tenant ID (explicit tenant_id or fall back to sub)."""
        return self.tenant_id or self.sub

    @property
    def effective_subject_id(self) -> str:
        """Subject (user) ID within the tenant.

        Falls back to effective_tenant_id when no X-Subject-Id was provided
        (per Phase 1 contract: legacy single-user-per-tenant callers see their
        OWN backfilled rows because subject_id == tenant_id from the migration).
        Returning "" here would break the read path: scope() would query with
        subject_id="" and miss every backfilled row whose subject_id == tenant_id.
        """
        return self.subject_id or self.effective_tenant_id
