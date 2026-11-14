"""Requirement request/response schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.common import (
    OriginFieldsInMixin,
    OriginFieldsOutMixin,
    ProjectScope,
    StableKey,
    SystemInheritanceOutMixin,
    TimestampMixin,
    VersionFieldsIn,
    VersionFieldsMixin,
)
from schemas.registers import (
    OwnerText,
    RegisterFieldsIn,
    RegisterFieldsOut,
    RequirementStatusText,
)
from validation import validate_float_bounds

REQUIREMENT_FLOAT_BOUNDS: dict[str, tuple[float, float]] = {
    "effort": (0.001, 999999),
    "reach": (0, 999999999),
    "impact": (0, 10),
    "confidence": (0.001, 1),
}


class RequirementIn(VersionFieldsIn, OriginFieldsInMixin, RegisterFieldsIn):
    """Schema for creating or updating a requirement."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Requirement title")
    description: str = Field(..., min_length=1, description="Description")
    project_id: ProjectScope = Field(None, max_length=100, description="Project ID")
    stable_key: StableKey = Field(
        default=None,
        max_length=200,
        description=(
            "Deterministic AA-intake dedup key (cardId + kind + ordinal). When set "
            "with a project_id, get-or-create keys on it instead of the drift-prone "
            "title. Empty/whitespace coerces to null."
        ),
    )
    links: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Trace links (business_objectives, capabilities, services)",
    )
    acceptance: list[str] = Field(default_factory=list, description="Acceptance criteria")
    effort: float = Field(..., gt=0.001, le=999999, description="Effort estimate")
    reach: float = Field(default=0.0, ge=0, le=999999999, description="Users/items affected")
    impact: float = Field(default=0.0, ge=0, le=10, description="Impact score (0-10)")
    confidence: float = Field(default=0.5, ge=0.001, le=1, description="Confidence level (0-1)")
    status: RequirementStatusText = Field(
        None, description="Register status (free text, lowercased, max 30), e.g. proposed, agreed"
    )
    owner: OwnerText = Field(None, description="Who owns the requirement (max 100)")
    lens_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "CLR reasoning lenses (Logical Thinking Process), authored by the AA "
            "author subroutine. Shape: {clr_assessment: {clarity, entity_existence, "
            "causality_existence, cause_sufficiency, no_additional_cause, "
            "no_cause_effect_reversal, predicted_effect_exists, no_tautology, notes}, "
            "goal_tree_relevance: {relevant, traces_to}}. Free-form."
        ),
    )

    @field_validator("effort", "reach", "impact", "confidence")
    @classmethod
    def validate_bounds(cls, v: float, info) -> float:
        lo, hi = REQUIREMENT_FLOAT_BOUNDS[info.field_name]
        validate_float_bounds(v, info.field_name, lo, hi)
        return v

    @field_validator("links")
    @classmethod
    def need_min_trace(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        if v and not any(
            key in v and v[key] for key in ("business_objectives", "capabilities", "services")
        ):
            if v != {}:
                raise ValueError(
                    "Provide at least one trace link (business_objectives, capabilities, or services)."
                )
        return v


class RequirementOut(
    SystemInheritanceOutMixin,
    VersionFieldsMixin,
    OriginFieldsOutMixin,
    RegisterFieldsOut,
    TimestampMixin,
):
    """Schema used when returning a requirement from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Requirement ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Requirement title")
    description: str = Field(..., min_length=1, description="Description")
    project_id: str | None = Field(None, max_length=100, description="Project ID")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    stable_key: str | None = Field(None, description="AA-intake dedup key")
    links: dict[str, list[str]] = Field(default_factory=dict, description="Trace links")
    acceptance: list[str] = Field(default_factory=list, description="Acceptance criteria")
    effort: float = Field(..., ge=0, description="Effort estimate")
    reach: float = Field(default=0.0, ge=0, description="Users/items affected")
    impact: float = Field(default=0.0, ge=0, description="Impact score")
    confidence: float = Field(default=0.5, ge=0.001, le=1, description="Confidence (0-1)")
    status: str | None = Field(None, description="Register status")
    owner: str | None = Field(None, description="Requirement owner")
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
    lens_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "CLR reasoning lenses (clr_assessment 8-category booleans + notes, "
            "goal_tree_relevance) authored by the AA author subroutine. Empty until authored."
        ),
    )


# ---------------------------------------------------------------------------
# Quality & Priority
# ---------------------------------------------------------------------------


class QualityReport(BaseModel):
    """Report returned by the quality check endpoint."""

    model_config = ConfigDict(str_strip_whitespace=True)

    score: float = Field(..., ge=0, le=1, description="Quality score (0-1)")
    issues: list[str] = Field(default_factory=list, description="Issues")
    passes: list[str] = Field(default_factory=list, description="Passed checks")


class PriorityOut(BaseModel):
    """WSJF/RICE prioritisation results."""

    model_config = ConfigDict(str_strip_whitespace=True)

    wsjf: float | None = Field(None, ge=0, description="WSJF score")
    rice: float | None = Field(None, ge=0, description="RICE score")


class QualityEvaluationItem(BaseModel):
    """One persisted quality evaluation (``quality_evaluations`` row)."""

    id: str
    score: float = Field(..., ge=0, le=1, description="Score (0-1) as the evaluator returned it")
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    evaluator_kind: str = Field(..., description="'llm' (a judge) or 'heuristic' (rule-based)")
    evaluator: str = Field(..., description="Agent name, or 'heuristic'")
    model_id: str | None = Field(
        None, description="Model WM reported as served; null = not captured"
    )
    served_by: str | None = Field(None, description="Provider WM reported; null = not captured")
    prompt_version: str | None = None
    prompt_sha: str | None = None
    correlation_id: str | None = None
    created_at: str | None = Field(None, description="ISO-8601 UTC")


class QualityHistory(BaseModel):
    """Append-only evaluation history for one entity, newest first.

    ``n`` counts the items returned (bounded by ``limit``). An entity that was
    never evaluated returns ``n: 0`` and ``items: []`` — never a fabricated score.
    """

    entity_type: str
    entity_id: str
    n: int
    items: list[QualityEvaluationItem]
