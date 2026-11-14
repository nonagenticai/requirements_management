"""Principle request/response schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from schemas.common import (
    OriginFieldsInMixin,
    OriginFieldsOutMixin,
    SystemInheritanceOutMixin,
    TimestampMixin,
    VersionFieldsIn,
    VersionFieldsMixin,
)


class PrincipleIn(VersionFieldsIn, OriginFieldsInMixin):
    """Schema for creating or updating a principle."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Principle title")
    statement: str = Field(..., min_length=1, description="Statement")
    rationale: str | None = Field(None, min_length=1, description="Rationale")
    implications: list[str] = Field(default_factory=list, description="Implications")
    tags: list[str] = Field(default_factory=list, description="Tags")
    active: bool = Field(default=True, description="Active")

    @field_validator("active")
    @classmethod
    def validate_active(cls, v) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes", "on")
        return bool(v)


class PrincipleOut(
    SystemInheritanceOutMixin,
    VersionFieldsMixin,
    OriginFieldsOutMixin,
    TimestampMixin,
):
    """Schema for returning a principle from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Principle ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Principle title")
    statement: str = Field(..., min_length=1, description="Statement")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    rationale: str | None = Field(None, description="Rationale")
    implications: list[str] = Field(default_factory=list, description="Implications")
    tags: list[str] = Field(default_factory=list, description="Tags")
    active: bool = Field(default=True, description="Active")
    quality_score: float | None = Field(
        default=None, description="Latest persisted quality score (0-1)"
    )
    quality_issues: list[str] | None = Field(
        default=None, description="Issues from the latest quality evaluation"
    )
    quality_suggestions: list[str] | None = Field(
        default=None, description="Suggestions from the latest quality evaluation"
    )
    last_evaluated_at: datetime | None = Field(
        default=None, description="Timestamp of the latest quality evaluation"
    )
    last_evaluated_by_agent: str | None = Field(
        default=None, description="Name of the agent that produced the latest evaluation"
    )
