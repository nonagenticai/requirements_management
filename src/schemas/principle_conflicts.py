"""Pydantic schemas for the principle-conflicts read/patch surface."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PrincipleConflictOut(BaseModel):
    """A persisted principle-conflict record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    principle_id: UUID
    counterparty_kind: str = Field(description="'principle' | 'procedure'")
    counterparty_id: str
    conflict_type: str
    severity: str | None = None
    rationale: str
    status: str = Field(description="'open' | 'resolved' | 'dismissed'")
    source: str
    detected_at: datetime | None = None
    updated_at: datetime | None = None


class PrincipleConflictPatch(BaseModel):
    """Manual lifecycle transition for a conflict.

    Only ``dismissed`` (suppress) and ``open`` (un-dismiss) are client-settable;
    ``resolved`` is owned by the reconciler.
    """

    status: str = Field(description="'dismissed' to suppress, 'open' to un-dismiss")
