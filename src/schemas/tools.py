"""Request/response schemas for architecture tool endpoints."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from schemas.common import lower_strip

# ⚠️ This module had ZERO validators before the vocabularies were published, so
# every ``Literal`` below needs its own ``mode="before"`` normaliser. Without
# one, the two halves of a single vocabulary disagree about case:
# ``viewpoint="Layered"`` is accepted by ``POST /architecture/views`` (whose
# schema normalises in ``schemas.architecture``) and 422s on
# ``POST /architecture/tools/generate-view``. ``str_strip_whitespace=True`` does
# NOT reach a ``Literal`` field. The dropdown protects hand-authored values, but
# the AA conveyor forwards ``$json.step.parameters.*`` verbatim, so a mis-cased
# LLM-authored value would fail at runtime rather than at author time.

# --- Closed request vocabularies -------------------------------------------
#
# Each alias below MUST stay set-equal to the runtime constant it mirrors;
# ``tests/test_tool_param_vocabularies.py`` fails the build on any drift.
#
# ⛔ Never write these as ``Literal[...] | None``. Pydantic renders an optional
# Literal as ``anyOf: [{enum: [...]}, {type: "null"}]``, and the downstream n8n
# node generator reads ``enum`` only at the TOP level of a property schema — a
# nested enum is invisible and the field degrades to a free-text box. A
# non-optional Literal with a default keeps ``enum`` at the top level.

# Mirrors ``models.architecture.ARCHITECTURE_LAYERS`` (8 values); member order
# follows ``tools.architecture.traceability.LAYER_ORDER`` (display order).
ArchitectureLayer = Literal[
    "strategy",
    "motivation",
    "business",
    "application",
    "technology",
    "physical",
    "implementation_migration",
    "other",
]

# Mirrors ``models.architecture.ARCHITECTURE_VIEWPOINTS`` == the keys of
# ``tools.architecture.view_generator.VIEWPOINT_FILTERS`` (27 values: 25 Archi + 2 extensions), in the
# order the filter table declares them.
ArchitectureViewpoint = Literal[
    "organization",
    "business_process_cooperation",
    "product",
    "application_cooperation",
    "application_usage",
    "technology_usage",
    "technology",
    "information_structure",
    "service_realization",
    "implementation_deployment",
    "physical",
    "layered",
    "landscape",
    "capability_map",
    "value_stream",
    "motivation",
    "strategy",
    "migration",
    "project",
    "stakeholder",
    "goal_realization",
    "requirements_realization",
    "application_structure",
    "capability",
    "implementation_migration",
    "outcome_realization",
    "resource",
]

# Mirrors the keys of ``tools.architecture.adm_completeness.ADM_DELIVERABLES``
# (8 values), in the TOGAF phase order that table declares them.
#
# ⚠️ This is the ONE vocabulary in this module whose canonical spelling is
# UPPER-case, so its ``mode="before"`` normaliser is ``.strip().upper()`` and
# NOT the shared ``lower_strip``. It reproduces exactly the ``phase.upper()
# .strip()`` coercion ``check_adm_completeness`` applied before the Literal.
AdmPhase = Literal["A", "B", "C", "D", "E", "F", "G", "H"]

# Mirrors ``tools.architecture.capability_heatmap.DEFAULT_DIMENSIONS``, which is
# not merely the default set — it is the COMPLETE set. ``build_capability_heatmap``
# has exactly three ``if "<dim>" in dims`` branches and no else, so any other
# string scores nothing: ``dimensions=["bogus"]`` used to answer **200** with an
# empty ``scores`` dict per capability and an ``averages`` map keyed on the
# caller's typo. Empty is not "no capabilities scored badly" — it is "this
# dimension does not exist", and a 200 cannot say so.
#
# ⚠️ Unlike the single-value vocabularies above, this one is used as
# ``list[HeatmapDimension] | None``, so its ``enum`` necessarily sits under
# ``items`` (and, because the field is nullable, under ``anyOf``) rather than at
# the top level of the property schema. That is inherent to a LIST field — a
# multi-select was never a top-level ``enum`` dropdown in the n8n generator — so
# the ⛔ rule above does not apply here and closing the vocabulary costs nothing
# that was ever available. ``None`` and ``[]`` both still mean "use the defaults".
HeatmapDimension = Literal["maturity", "strategic_importance", "investment_need"]


def upper_strip(v: Any) -> Any:
    """``lower_strip``'s twin for the one UPPER-case vocabulary in this module.

    Non-``str`` input is passed through untouched so pydantic reports the real
    type error instead of a silently-stringified value.
    """
    return v.strip().upper() if isinstance(v, str) else v


class ProjectScopeRequest(BaseModel):
    """Base request scoped to a project."""

    model_config = ConfigDict(str_strip_whitespace=True)

    project_id: str = Field(..., min_length=1, max_length=100, description="Project ID/slug")


class TraceabilityMatrixRequest(ProjectScopeRequest):
    root_layer: ArchitectureLayer = Field(
        default="strategy",
        description="Root layer for tracing",
        examples=["business"],
    )

    @field_validator("root_layer", mode="before")
    @classmethod
    def _normalise_root_layer(cls, v: Any) -> Any:
        """Preserve the case/whitespace coercion its ``architecture.py`` twin applies."""
        return lower_strip(v)


class ImpactAnalysisRequest(ProjectScopeRequest):
    element_id: UUID = Field(..., description="Element to analyze impact from")
    max_depth: int = Field(default=3, ge=1, le=10, description="Max traversal depth")


class ADMCompletenessRequest(ProjectScopeRequest):
    # ``min_length=1, max_length=1`` was dropped with the ``str`` -> ``Literal``
    # conversion: every member of ``AdmPhase`` is exactly one character, so the
    # pair can neither reject a value the Literal accepts nor accept one it
    # rejects. Kept, they would render ``minLength``/``maxLength`` beside the
    # ``enum`` and imply a free-text length rule where the real rule is
    # membership. Rejection of ``""`` and ``"AB"`` now comes from the Literal.
    phase: AdmPhase = Field(
        ...,
        description="TOGAF ADM phase (A-H)",
        examples=["A"],
    )

    @field_validator("phase", mode="before")
    @classmethod
    def _normalise_phase(cls, v: Any) -> Any:
        """Preserve the ``phase.upper().strip()`` coercion the tool applied."""
        return upper_strip(v)


class CapabilityHeatmapRequest(ProjectScopeRequest):
    dimensions: list[HeatmapDimension] | None = Field(
        None,
        description="Dimensions to score (default: maturity, strategic_importance, investment_need)",
    )

    @field_validator("dimensions", mode="before")
    @classmethod
    def _normalise_dimensions(cls, v: Any) -> Any:
        """Case/whitespace-coerce each member, mirroring ``_normalise_viewpoint``.

        Element-wise because the field is a LIST of Literals: ``lower_strip``
        applied to the list itself would pass a ``list`` through untouched (it
        only coerces ``str``), so ``["Maturity"]`` would 422 while
        ``viewpoint="Layered"`` is accepted twelve lines below. Non-list input is
        handed on unchanged so pydantic reports the real type error.
        """
        if isinstance(v, list):
            return [lower_strip(item) for item in v]
        return v


class ViewGenerationRequest(ProjectScopeRequest):
    viewpoint: ArchitectureViewpoint = Field(
        ...,
        description="ArchiMate viewpoint",
        examples=["application_cooperation"],
    )

    @field_validator("viewpoint", mode="before")
    @classmethod
    def _normalise_viewpoint(cls, v: Any) -> Any:
        """Preserve the case/whitespace coercion its ``architecture.py`` twin applies."""
        return lower_strip(v)


class ValidateRelationshipRequest(BaseModel):
    """Request to validate a relationship against ArchiMate metamodel."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_layer: str = Field(..., min_length=1, max_length=30, description="Source element layer")
    source_element_type: str = Field(
        ..., min_length=1, max_length=50, description="Source element type"
    )
    target_layer: str = Field(..., min_length=1, max_length=30, description="Target element layer")
    target_element_type: str = Field(
        ..., min_length=1, max_length=50, description="Target element type"
    )
    relationship_type: str = Field(
        ..., min_length=1, max_length=30, description="Relationship type"
    )


# ---------------------------------------------------------------------------
# Integration tool schemas
# ---------------------------------------------------------------------------


class GapToWorkpackagesRequest(ProjectScopeRequest):
    """Request to generate work package proposals from architecture gaps."""


class RequirementTraceRequest(ProjectScopeRequest):
    """Request to compute requirement ↔ architecture traceability."""


class ArchitectureConformanceRequest(ProjectScopeRequest):
    """Request to compute as-designed (ArchiMate) vs as-built (code-graph) conformance."""

    code_units: list[dict] | None = Field(
        None,
        description=(
            "Optional caller-supplied as-built code units. When omitted, units are "
            "fetched from coding-analysis for the containers bound to this project's "
            "synthetic Artifact elements via architecture_element_realisation_bindings."
        ),
    )
    layer_order: list[str] | None = Field(
        None,
        description="Optional layer ranking (high→low) for the illegal-dependency policy",
    )


# ---------------------------------------------------------------------------
# Architecture conformance result (tools.architecture.conformance.analyze_conformance)
# ---------------------------------------------------------------------------
# Code-unit fields other than ``id`` are copied verbatim from the as-built units, which a
# caller may supply as free-form dicts, so they are typed ``Any`` rather than narrowed.


class ConformanceElementSummary(BaseModel):
    id: str
    title: str
    layer: str
    element_type: str


class ConformanceBinding(BaseModel):
    """An Artifact's (container, component_id) realisation binding."""

    id: str = Field(..., description="Artifact element ID")
    name: str
    container: str
    component_id: str


class ConformanceInsufficientBinding(ConformanceBinding):
    # ``str``, not a Literal: this module's Literals are request vocabularies that must
    # carry a case normaliser (test_tool_param_case_normalisation); this is output.
    status: str = Field(..., description="Always 'insufficient_evidence'")


class ConformanceCodeUnitRef(BaseModel):
    id: str
    name: Any


class ConformanceUnboundCodeUnit(ConformanceCodeUnitRef):
    container: Any


class RealizationCoverage(BaseModel):
    realization_coverage: float | None = Field(
        ..., description="None when no Application Component is designed (no denominator)"
    )
    total_application_components: int
    realized_count: int
    realized: list[ConformanceElementSummary]
    unrealized: list[ConformanceElementSummary]
    orphan_artifacts: list[ConformanceElementSummary]
    unbound_code_units: list[ConformanceUnboundCodeUnit]
    dead_bindings: list[ConformanceBinding] | None = Field(
        ..., description="None = not measured (no code units to compare against)"
    )
    insufficient_evidence: list[ConformanceInsufficientBinding]


class ConformanceDependencyPair(BaseModel):
    source: ConformanceElementSummary
    target: ConformanceElementSummary


class DependencyConformance(BaseModel):
    conformance_score: float | None = Field(
        ..., description="None when there is nothing designed or realized to compare"
    )
    designed_count: int
    realized_count: int
    matched: list[ConformanceDependencyPair]
    missing: list[ConformanceDependencyPair]
    extra: list[ConformanceDependencyPair]
    insufficient_evidence: list[ConformanceDependencyPair]
    unmappable_realized_edges: int


class IllegalDependency(BaseModel):
    source: ConformanceCodeUnitRef
    target: ConformanceCodeUnitRef
    source_layer: str
    target_layer: str
    reason: str


class IllegalDependencies(BaseModel):
    illegal_count: int
    illegal_dependencies: list[IllegalDependency]
    unranked_edges: int
    layer_order: list[str]


class ConformanceSummary(BaseModel):
    realization_coverage: float | None
    conformance_score: float | None
    illegal_count: int
    unrealized_components: int
    orphan_artifacts: int
    dead_binding_count: int | None = Field(..., description="None = could not measure")
    insufficient_evidence_count: int
    insufficient_evidence_containers: list[str]


class ArchitectureConformanceResult(BaseModel):
    """``POST /architecture/tools/conformance``: the three measures plus a summary."""

    realization_coverage: RealizationCoverage
    dependency_conformance: DependencyConformance
    illegal_dependencies: IllegalDependencies
    summary: ConformanceSummary


# --- ArchiMate relationship derivation (POST /architecture/tools/derive-relationships) ---

DerivationCertainty = Literal["valid", "potential"]


class DeriveRelationshipsRequest(BaseModel):
    """Derive the relationships ArchiMate 3.2 §5.7 implies from a model's stored ones.

    Exactly one scope: ``model_id`` (one modeling model) or ``project_id`` (the
    project's effective view — project rows plus tenant-scope rows). Read-only.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    model_id: UUID | None = Field(None, description="Modeling model to derive within")
    project_id: str | None = Field(
        None, min_length=1, max_length=100, description="Project ID/slug (effective view)"
    )
    source_id: str | None = Field(
        None, min_length=1, max_length=100, description="Only derivations from this concept"
    )
    target_id: str | None = Field(
        None, min_length=1, max_length=100, description="Only derivations to this concept"
    )
    max_path_length: int = Field(
        4, ge=2, le=6, description="Longest chain of stored relationships considered"
    )
    include_potential: bool = Field(
        True, description="Also return potential derivations (AM-DER-POT-* rules)"
    )

    @model_validator(mode="after")
    def _one_scope(self) -> "DeriveRelationshipsRequest":
        if (self.model_id is None) == (self.project_id is None):
            raise ValueError("give exactly one of model_id or project_id")
        return self


class DerivedRelationship(BaseModel):
    """One relationship derived from a chain of stored relationships."""

    type: str = Field(..., description="ArchiMate relationship type of the derived relationship")
    source_id: str
    target_id: str
    certainty: DerivationCertainty = Field(
        ..., description="valid = always holds; potential = may hold, needs review"
    )

    @field_validator("certainty", mode="before")
    @classmethod
    def _normalise_certainty(cls, v: Any) -> Any:
        """Same case/whitespace coercion as every other ``Literal`` in this module."""
        return lower_strip(v)

    rule_id: str = Field(
        ...,
        description="Rule applied at the last step, e.g. AM-DER-STRUCT-DEPENDENCY or AM-DER-POT-FLOW-CHAIN",
    )
    rule_chain: list[str] = Field(..., description="Rule applied at each composition step")
    path: list[str] = Field(..., description="Stored relationship ids, in chain order")
    direct_exists: bool = Field(
        ..., description="A stored relationship of the same type already joins source to target"
    )


class FilteredDerivation(DerivedRelationship):
    """A derivation the rules produce but the ArchiMate relationship matrix forbids."""

    reason: str


class DerivationExamined(BaseModel):
    concepts: int
    relationships: int
    states: int = Field(..., description="Chain states explored")


class DeriveRelationshipsResult(BaseModel):
    ruleset_id: str
    language_version: str
    max_path_length: int
    include_potential: bool
    derived: list[DerivedRelationship]
    filtered: list[FilteredDerivation]
    examined: DerivationExamined
