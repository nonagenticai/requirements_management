"""Decision request/response schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

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
from schemas.registers import DecidedByKind, RegisterFieldsIn, RegisterFieldsOut

# ---------------------------------------------------------------------------
# Decision-status vocabulary — ONE source of truth
# ---------------------------------------------------------------------------
# The closed set used to live as an unnamed literal set inside
# ``DecisionIn.validate_status``'s body, so nothing outside that function could
# reference it. Naming it lets a test assert the runtime set and the request-side
# ``Literal`` never drift apart.

DECISION_STATUSES: frozenset[str] = frozenset({"proposed", "accepted", "deprecated", "superseded"})

# The same vocabulary as a type. A ``Literal`` is what puts a TOP-LEVEL ``enum``
# into the generated OpenAPI property, which is the only place the downstream
# n8n node generator looks (``generate-node.ts:843``). A ``field_validator``
# alone returns a correct 422 but emits no ``enum`` at all, so every generated
# client renders the field as a free-text box.
#
# ⛔ Never spell this as ``DecisionStatus | None``. Pydantic emits an optional
# Literal as ``anyOf: [{enum: [...]}, {"type": "null"}]`` — the enum is then
# NESTED, the generator cannot see it, and the field silently degrades back to
# free text. Non-optional with a default is the shape that works.
#
# ⛔ The spelling here is deliberately independent of ``DECISION_STATUSES``
# above (rather than derived via ``get_args``) so that drift between the two is
# a state a test can actually reach and fail on.
DecisionStatus = Literal["proposed", "accepted", "deprecated", "superseded"]


class DecisionIn(VersionFieldsIn, OriginFieldsInMixin, RegisterFieldsIn):
    """Schema for creating or updating a decision."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Decision title")
    project_id: ProjectScope = Field(None, max_length=100, description="Project ID")
    stable_key: StableKey = Field(
        default=None,
        max_length=200,
        description=(
            "Deterministic AA-intake dedup key (cardId + kind + ordinal). When set "
            "with a project_id, get-or-create keys on it instead of the title. "
            "Empty/whitespace coerces to null."
        ),
    )
    status: DecisionStatus = Field(
        default="proposed",
        description="Status: proposed, accepted, deprecated, or superseded",
    )
    context: str = Field(..., min_length=1, description="Context")
    decision: str = Field(..., min_length=1, description="Decision")
    consequences: str | None = Field(None, min_length=1, description="Consequences")
    alternatives: list[str] = Field(default_factory=list, description="Alternatives")
    related_principles: list[UUID] = Field(
        default_factory=list, description="Related principle IDs"
    )
    lens_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "CLR reasoning lenses (Logical Thinking Process) authored by the AA "
            "author subroutine. Shape: {clr_assessment: {<8 CLR booleans>, notes}, "
            "goal_tree_relevance: {relevant, traces_to}}. Free-form."
        ),
    )
    decided_by_kind: DecidedByKind | None = Field(
        None,
        description="Who decided: human, agent (an approval agent) or rule",
        # An optional Literal nests its enum under ``anyOf`` where the n8n node generator
        # cannot see it (see ``DecisionStatus`` above); repeat it at the top level.
        json_schema_extra={"enum": ["human", "agent", "rule", None]},
    )
    decided_at: datetime | None = Field(None, description="When the decision was taken")

    @field_validator("related_principles", mode="before")
    @classmethod
    def validate_related_principles(cls, v) -> list[Any]:
        if not v:
            return []
        result = []
        for item in v:
            try:
                if isinstance(item, UUID):
                    result.append(item)
                else:
                    result.append(UUID(str(item)))
            except ValueError, TypeError:
                continue
        return result

    @field_validator("status", mode="before")
    @classmethod
    def normalise_status(cls, v: object) -> object:
        """Reproduce the old validator's normalisation EXACTLY: strip, nothing else.

        ``model_config``'s ``str_strip_whitespace`` does not reach a ``Literal``
        field (that config applies to ``str`` schemas), so this ``mode="before"``
        hook is what keeps ``" proposed "`` accepted, as it was before the
        conversion.

        ⛔ Do NOT add ``.lower()``. The canonical values are lowercase and the
        pre-conversion validator stripped ONLY, so ``"Proposed"`` is rejected
        today; lowercasing here would make the API start accepting input it
        currently 422s on. Pinned by ``tests/test_governance_vocabularies.py``.
        """
        if isinstance(v, str):
            return v.strip()
        return v


class DecisionOut(
    SystemInheritanceOutMixin,
    VersionFieldsMixin,
    OriginFieldsOutMixin,
    RegisterFieldsOut,
    TimestampMixin,
):
    """Schema for returning a decision from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Decision ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Decision title")
    project_id: str | None = Field(None, max_length=100, description="Project ID")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    status: str = Field(default="proposed", min_length=1, max_length=20, description="Status")
    stable_key: str | None = Field(None, description="AA-intake dedup key")
    context: str = Field(..., min_length=1, description="Context")
    decision: str = Field(..., min_length=1, description="Decision")
    consequences: str | None = Field(None, description="Consequences")
    alternatives: list[str] = Field(default_factory=list, description="Alternatives")
    related_principles: list[UUID] = Field(
        default_factory=list, description="Related principle IDs"
    )
    decided_by_kind: str | None = Field(None, description="human, agent or rule")
    decided_at: datetime | None = Field(None, description="When the decision was taken")
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
