"""API shapes of the modeling lifecycle: models, revisions, view occurrences (spec §7.1–7.2, §8.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.modeling import Bounds, Point, Style, ViewConnectionKind, ViewObjectKind

ModelLanguage = Literal["archimate", "uml"]
ModelVisibility = Literal["tenant", "user"]


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    language: ModelLanguage
    language_version: str = Field(..., min_length=1, max_length=30, examples=["3.2", "2.5.1-p1"])
    title: str = Field(..., min_length=1, max_length=300)
    description: str | None = None
    project_id: str | None = Field(None, max_length=100)
    visibility: ModelVisibility | None = Field(
        None, description="Defaults to the X-Visibility header (tenant)"
    )


class ModelPatch(BaseModel):
    """Title, description and visibility only; language and its version are immutable."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = None
    visibility: ModelVisibility | None = None


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: str
    project_id: str | None
    visibility: str
    subject_id: str
    language: str
    language_version: str
    title: str
    description: str | None
    head: int
    is_default: bool
    published_revision_id: UUID | None
    created_at: datetime | None
    updated_at: datetime | None


class ModelDetail(ModelOut):
    latest_revision_id: UUID | None = None
    # Validation runs are persisted by a later slice; until then always null.
    latest_validation_run: dict[str, Any] | None = None


class RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str | None = Field(None, max_length=200)
    head: int | None = Field(
        None, ge=0, description="Expected draft head; same effect as the If-Match header"
    )


class RevisionOut(BaseModel):
    revision_id: UUID
    model_id: UUID
    seq: int
    label: str | None
    head: int
    content_hash: str
    sealed_by: str
    sealed_at: datetime | None


class RevisionDetail(RevisionOut):
    manifest: dict[str, Any]


class ViewObjectIn(BaseModel):
    """An element occurrence, a note or a group (spec §8.1; Archi's diagram objects)."""

    model_config = ConfigDict(extra="forbid")

    kind: ViewObjectKind = Field("element", description="element | note | group")
    concept_ref: UUID | None = Field(
        None, description="Element shown by this occurrence; required iff kind = element"
    )
    parent_object_id: UUID | None = Field(
        None, description="Visual parent: another object of the same view"
    )
    bounds: Bounds | None = None
    style: Style | None = None
    name: str | None = Field(None, max_length=1000, description="group title")
    text: str | None = Field(None, max_length=100000, description="note body")
    label: str | None = Field(None, max_length=1000, description="Archi label expression")
    border_type: str | None = Field(
        None,
        max_length=20,
        description='note: "dogear"|"rectangle"|"none"; group: "tabbed"|"rectangle"',
    )
    documentation: str | None = Field(
        None, description="note / group documentation; not allowed on an element occurrence"
    )
    view_ref: UUID | None = Field(
        None, description="Drill-down: a view of the same model this object refers to"
    )
    position: int | None = Field(
        None,
        ge=0,
        le=100000,
        description=(
            "z-order: the object's index in the view's object list (0 = bottom; among "
            "siblings a later object is drawn above an earlier one). Omitted: on create the "
            "object goes on top (appended), on update it keeps its place."
        ),
    )

    @model_validator(mode="after")
    def _concept_iff_element(self) -> ViewObjectIn:
        if (self.kind == "element") != (self.concept_ref is not None):
            raise ValueError("concept_ref is required for kind 'element' and not allowed otherwise")
        if self.kind == "element" and self.documentation is not None:
            raise ValueError(
                "documentation is not allowed on an element occurrence (document the element)"
            )
        return self


class ViewObjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    view_id: UUID
    model_id: UUID
    kind: str
    concept_ref: UUID | None
    parent_object_id: UUID | None
    bounds: Bounds | None
    style: Style | None
    name: str | None
    text: str | None
    label: str | None
    border_type: str | None
    documentation: str | None
    view_ref: UUID | None = None
    position: int = Field(..., description="z-order: lists are ordered by it (then id)")
    created_at: datetime | None
    updated_at: datetime | None


class ViewConnectionIn(BaseModel):
    """A relationship occurrence, or a plain ``line`` between any two objects/connections."""

    model_config = ConfigDict(extra="forbid")

    kind: ViewConnectionKind = Field("relationship", description="relationship | line")
    relationship_ref: UUID | None = Field(
        None, description="Relationship shown; required iff kind = relationship"
    )
    source_object_id: UUID = Field(..., description="A view object or another connection")
    target_object_id: UUID = Field(..., description="A view object or another connection")
    bendpoints: list[Point] | None = None
    style: Style | None = None
    label: str | None = Field(None, max_length=1000, description="Label text / expression")

    @model_validator(mode="after")
    def _relationship_iff_kind(self) -> ViewConnectionIn:
        if (self.kind == "relationship") != (self.relationship_ref is not None):
            raise ValueError(
                "relationship_ref is required for kind 'relationship' and not allowed for a line"
            )
        return self


class ViewConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    view_id: UUID
    model_id: UUID
    kind: str
    relationship_ref: UUID | None
    source_object_id: UUID
    target_object_id: UUID
    bendpoints: list[Point] | None
    style: Style | None
    label: str | None
    created_at: datetime | None
    updated_at: datetime | None


# ---------------------------------------------------------------------------
# Validation runs (spec §7.3)
# ---------------------------------------------------------------------------


class ValidationRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: UUID | None = Field(
        None, description="Validate this revision; default the draft head"
    )
    ruleset_id: str | None = Field(None, description="Default: the model language's ruleset")
    rules: list[str] | None = Field(None, description="Only these rules (default: all)")
    disable: list[str] | None = Field(None, description="Rules to switch off")


class ValidationRunOut(BaseModel):
    run_id: UUID
    model_id: UUID
    target: Literal["revision", "draft_head"]
    revision_id: UUID | None
    head: int | None
    content_hash: str
    ruleset_id: str
    ruleset_version: str
    enabled_rules: list[str]
    status: Literal["queued", "running", "complete", "incomplete", "failed"] = Field(
        description=(
            "`queued`/`running` only for a background run (a sealed revision above "
            "MODEL_SYNC_MAX_CONCEPTS); it ends `complete`, `incomplete` or `failed`"
        )
    )
    examined: dict[str, int]
    expected: dict[str, int]
    counts: dict[str, int]
    clean: bool
    started_at: datetime | None
    finished_at: datetime | None
    requested_by: str
    error: str | None = Field(None, description="Why the run failed (NULL otherwise)")


class ValidationFindingOut(BaseModel):
    rule_id: str
    ruleset_id: str
    severity: Literal["error", "warning", "advice"]
    targets: list[dict[str, Any]]
    path: str | None
    message: str
    evidence: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Trace links (spec §7.4)
# ---------------------------------------------------------------------------

TraceKind = Literal[
    "requirement",
    "principle",
    "decision",
    "element",
    "relationship",
    "view",
    "uml_element",
    "uml_relationship",
    "interaction",
    "diagram",
]
TraceLinkType = Literal["realizes", "refines", "traces_to", "satisfies", "implements", "depends_on"]


class TraceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TraceKind
    id: UUID


class CodeRef(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo: str = Field(..., min_length=1, max_length=300)
    commit: str = Field(..., min_length=1, max_length=100)
    symbol: str = Field(..., min_length=1, max_length=1000)


class TraceLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: TraceRef
    target: TraceRef | None = None
    target_code: CodeRef | None = None
    link_type: TraceLinkType
    note: str | None = Field(None, max_length=4000)

    @model_validator(mode="after")
    def _one_target(self) -> TraceLinkIn:
        if (self.target is None) == (self.target_code is None):
            raise ValueError("exactly one of target and target_code is required")
        return self


class TraceLinkOut(BaseModel):
    id: UUID
    model_id: UUID | None
    source: dict[str, Any]
    target: dict[str, Any] | None
    target_code: dict[str, Any] | None
    link_type: str
    note: str | None
    visibility: str
    created_by: str
    created_at: datetime | None


# ---------------------------------------------------------------------------
# Change sets (spec §9.5)
# ---------------------------------------------------------------------------

# ArchiMate kinds (an ArchiMate model) and UML kinds (a UML model).
ChangeKind = Literal[
    "element",
    "relationship",
    "view",
    "view_object",
    "view_connection",
    "organization",
    "model_exchange",
    "uml_element",
    "uml_relationship",
    "interaction",
    "diagram",
    "diagram_object",
    "diagram_connection",
]


class ChangeOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["create", "update", "delete"]
    kind: ChangeKind
    id: str | None = Field(
        None,
        min_length=1,
        max_length=100,
        description=(
            "update/delete: the row id. create: a local reference later ops may use in "
            "place of the new row's id (the real id is returned in id_map on apply). "
            "kind=organization: an update without id whose attributes.organizations "
            "replaces the model's folder tree. kind=model_exchange: an update without id "
            "whose attributes.exchange replaces the model's exchange content (properties, "
            "metadata, custom viewpoints)"
        ),
    )
    attributes: dict[str, Any] | None = None


class ChangeSetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ops: list[ChangeOp] = Field(..., min_length=1, max_length=500)
    rationale: str = Field(..., min_length=1, max_length=10000)
    evaluator_ref: str | None = Field(None, max_length=300)
    base_head: int = Field(..., ge=0)


class ChangeSetReject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=4000)


class ChangeSetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    model_id: UUID
    status: Literal["proposed", "invalid", "applied", "rejected"]
    ops: list[dict[str, Any]]
    rationale: str
    evaluator_ref: str | None
    base_head: int
    findings: list[dict[str, Any]]
    blocking_findings: list[dict[str, Any]]
    created_by: str
    created_at: datetime | None
    applied_by: str | None
    applied_at: datetime | None
    applied_head: int | None
    id_map: dict[str, Any] | None
    rejected_by: str | None
    rejected_at: datetime | None
    reject_reason: str | None


# ---------------------------------------------------------------------------
# Review and publication (plan §7.3)
# ---------------------------------------------------------------------------


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approve", "reject"]
    reason: str | None = Field(None, max_length=4000)


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    model_id: UUID
    revision_id: UUID
    decision: str
    reason: str | None
    actor: str
    created_at: datetime | None


class PublicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    model_id: UUID
    revision_id: UUID
    supersedes_revision_id: UUID | None
    review_id: UUID
    validation_run_id: UUID
    published_by: str
    published_at: datetime | None
