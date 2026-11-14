"""Architecture element, relationship, view, and standard request/response schemas."""

import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from schemas.common import (
    ProjectScope,
    SystemInheritanceOutMixin,
    TimestampMixin,
    VersionFieldsIn,
    VersionFieldsMixin,
    lower_strip,
)
from schemas.modeling import integrity_value_error
from tools.architecture.registry import ELEMENT_TYPE_TO_LAYER, JUNCTION_TYPES, get_registry

_log = logging.getLogger(__name__)
_ARCHIMATE = get_registry()

# Element types by layer — the RUNTIME source of truth for the element-type
# vocabulary, derived from the Archi-generated ArchiMate registry
# (``src/data/archimate/3.2/registry.json``) so it cannot drift from the
# relationship matrix. ``ElementType`` (below) mirrors the union of these sets
# as a ``Literal`` so the vocabulary reaches OpenAPI; the two are pinned equal
# by ``tests/test_architecture_vocabularies.py``.
ELEMENT_TYPES_BY_LAYER: dict[str, frozenset[str]] = {
    layer: _ARCHIMATE.types_in_layer(layer)
    for layer in sorted({c.layer for c in _ARCHIMATE.concepts.values()})
}

ALL_ELEMENT_TYPES: frozenset[str] = frozenset().union(*ELEMENT_TYPES_BY_LAYER.values())

# Valid entity types that can appear in relationship source/target — runtime
# source of truth, mirrored as the ``EntityType`` Literal below.
VALID_ENTITY_TYPES = frozenset(
    {
        "architecture_elements",
        "requirements",
        "principles",
        "decisions",
        "pm_resources",
        "toc_trees",
        "toc_nodes",
        # ArchiMate permits a relationship as the end of another relationship
        # (e.g. an association or aggregation onto a relationship) — A10.
        "architecture_relationships",
    }
)


# ---------------------------------------------------------------------------
# Controlled vocabularies as TYPES
# ---------------------------------------------------------------------------
# The frozensets above (and their twins in ``models.architecture``) are the
# runtime source of truth. The ``Literal`` aliases below are the same closed
# sets expressed as a TYPE, which is the only thing that puts a **top-level**
# ``enum`` into the generated OpenAPI property. A ``@field_validator`` returns a
# correct 422 but emits no ``enum`` at all, so every generated client — and the
# n8n node UI — renders a free-text box for a field the API will reject.
#
# ⛔ Never spell a request field as ``SomeLiteral | None`` unless the field is
# GENUINELY nullable: pydantic emits an optional Literal as
# ``anyOf: [{enum: [...]}, {"type": "null"}]``, the enum is then NESTED, and the
# downstream n8n generator (``generate-node.ts:843``) reads ``enum`` only at the
# top level of a property — so the dropdown silently degrades back to free text.
#
# Duplication between the frozenset and the Literal is deliberate (a Literal
# cannot be built from a runtime frozenset without losing static analysis).
# ``tests/test_architecture_vocabularies.py`` asserts set-equality between every
# pair below, so drift fails the suite rather than shipping.

ArchitectureLayer = Literal[
    "strategy",
    "business",
    "application",
    "technology",
    "physical",
    "implementation_migration",
    "motivation",
    # Location, Grouping (Archi "Other") and Junction (Archi "Connector").
    "other",
]

ElementType = Literal[
    # strategy
    "resource",
    "capability",
    "value_stream",
    "course_of_action",
    # business
    "business_actor",
    "business_role",
    "business_collaboration",
    "business_interface",
    "business_process",
    "business_function",
    "business_interaction",
    "business_event",
    "business_service",
    "business_object",
    "contract",
    "representation",
    "product",
    # application
    "application_component",
    "application_collaboration",
    "application_interface",
    "application_function",
    "application_interaction",
    "application_process",
    "application_event",
    "application_service",
    "data_object",
    # technology
    "node",
    "device",
    "system_software",
    "technology_collaboration",
    "technology_interface",
    "technology_function",
    "technology_process",
    "technology_interaction",
    "technology_event",
    "technology_service",
    "artifact",
    "communication_network",
    "path",
    # physical
    "equipment",
    "facility",
    "distribution_network",
    "material",
    # implementation_migration
    "work_package",
    "deliverable",
    "implementation_event",
    "plateau",
    "gap",
    # motivation
    "stakeholder",
    "driver",
    "assessment",
    "goal",
    "outcome",
    "principle",
    "constraint",
    "meaning",
    "value",
    "requirement",
    # other
    "location",
    "grouping",
    "junction",
]

ElementStatus = Literal["draft", "baseline", "target", "retired"]

ArchitectureState = Literal["baseline", "target", "transitional"]

RelationshipType = Literal[
    # ArchiMate structural
    "composition",
    "aggregation",
    "assignment",
    "realization",
    # ArchiMate dependency
    "serving",
    "access",
    "influence",
    # ArchiMate dynamic
    "triggering",
    "flow",
    # ArchiMate other
    "specialization",
    "association",
    # Cross-domain
    "traceability",
    # Logical Thinking Process (TOC)
    "toc_necessity",
    "toc_sufficiency",
    "toc_conflict",
    "toc_assumption",
]

EntityType = Literal[
    "architecture_elements",
    "requirements",
    "principles",
    "decisions",
    "pm_resources",
    "toc_trees",
    "toc_nodes",
    "architecture_relationships",
]

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
    # Archi 5.10.0 viewpoints the service lacked before 2026-10-26 (A11).
    "application_structure",
    "capability",
    "implementation_migration",
    "outcome_realization",
    "resource",
]

StandardType = Literal[
    "technology_standard",
    "application_standard",
    "data_standard",
    "business_standard",
    "guideline",
]

StandardStatus = Literal["approved", "trial", "assess", "hold", "retired"]

ComplianceLevel = Literal["mandatory", "recommended", "optional"]

RealisationBindingKind = Literal["coding_component", "coding_container", "external_repo"]


# The coercion now lives in ``schemas.common`` because ``schemas.tools`` needs
# the identical normalisation and the two modules must not import each other.
# Kept under the old private name so every call site below is unchanged.
_lower_strip = lower_strip


class ArchitectureElementIn(VersionFieldsIn):
    """Schema for creating or updating an architecture element."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Element name")
    description: str | None = Field(None, description="Full description")
    project_id: ProjectScope = Field(None, max_length=100, description="Project scope")
    model_id: UUID | None = Field(
        None,
        exclude=True,
        description=(
            "Create only: the modeling model to add this to (must be an ArchiMate model the "
            "caller may write, with the row's visibility). Omitted: the default model of "
            "the scope (tenant, project_id, visibility). Ignored on update."
        ),
    )
    # Derived from ``element_type`` when omitted (the registry knows every
    # type's layer). A supplied layer that disagrees is refused as
    # ``model_integrity`` / AM-TYP-001 rather than stored (finding A01): it used
    # to be validated independently, so ``business`` + ``application_component``
    # was accepted. Typed as the bare Literal (not ``| None``) so the enum stays
    # top-level in OpenAPI — see the ⛔ note above the vocabularies.
    layer: ArchitectureLayer = Field(  # ty: ignore[invalid-assignment]
        None,
        description="ArchiMate layer; derived from element_type when omitted",
    )
    element_type: ElementType = Field(..., description="Element type")
    properties: dict[str, Any] | None = Field(
        default_factory=dict, description="Arbitrary key-value properties"
    )
    status: ElementStatus = Field(default="draft", description="Element status")
    # ⚠️ GENUINELY NULLABLE — 100% NULL in production, and "no state recorded" is
    # a real, distinct value from any of the three states. It therefore keeps
    # ``| None`` and its enum is anyOf-NESTED, i.e. invisible to the n8n
    # generator. That is accepted: correctness beats a dropdown, and forcing the
    # field non-nullable to win one would invent a state for every element.
    architecture_state: ArchitectureState | None = Field(
        None, description="Baseline, target, or transitional"
    )
    parent_id: UUID | None = Field(None, description="Parent element ID for decomposition")
    is_synthetic: bool = Field(
        default=False,
        description="True for tool-generated scaffolding rows hidden from default lists",
    )

    @field_validator("layer", "element_type", "status", "architecture_state", mode="before")
    @classmethod
    def _normalise_vocabulary(cls, v: Any) -> Any:
        """Preserve the pre-Literal ``strip().lower()`` coercion (see _lower_strip)."""
        return _lower_strip(v)

    @model_validator(mode="after")
    def _layer_follows_type(self) -> "ArchitectureElementIn":
        expected = ELEMENT_TYPE_TO_LAYER[self.element_type]
        if self.layer is None:
            self.layer = expected
        elif self.layer != expected:
            raise integrity_value_error(
                "AM-TYP-001",
                f"element type '{self.element_type}' belongs to layer '{expected}', "
                f"not '{self.layer}'",
                path="/layer",
                evidence={
                    "element_type": self.element_type,
                    "layer": self.layer,
                    "expected_layer": expected,
                },
            )
        if self.element_type == "junction":
            props = dict(self.properties or {})
            jt = props.setdefault("junction_type", "and")
            if jt not in JUNCTION_TYPES:
                raise integrity_value_error(
                    "AM-REL-003",
                    f"junction_type must be one of {list(JUNCTION_TYPES)}, not {jt!r}",
                    path="/properties/junction_type",
                )
            self.properties = props
        return self


class ArchitectureElementOut(SystemInheritanceOutMixin, VersionFieldsMixin, TimestampMixin):
    """Schema for returning an architecture element from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Element ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Element name")
    description: str | None = Field(None, description="Full description")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    project_id: str | None = Field(None, max_length=100, description="Project scope")
    model_id: UUID | None = Field(None, description="Modeling model this row belongs to")
    layer: str = Field(..., max_length=30, description="ArchiMate layer")
    element_type: str = Field(..., max_length=50, description="Element type")
    properties: dict[str, Any] | None = Field(None, description="Properties")
    status: str = Field(default="draft", max_length=20, description="Element status")
    architecture_state: str | None = Field(None, description="Architecture state")
    parent_id: UUID | None = Field(None, description="Parent element ID")
    is_synthetic: bool = Field(
        default=False,
        description="Tool-generated scaffolding marker",
    )
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


# ---------------------------------------------------------------------------
# Architecture Element Realisation Bindings
# ---------------------------------------------------------------------------


class ArchitectureElementRealisationBindingIn(BaseModel):
    """Request body for ``POST /architecture/elements/{eid}/bindings``.

    Discriminated by ``binding_kind``:

    * ``coding_component`` requires ``container`` + ``component_id``.
    * ``coding_container`` requires ``container`` only.
    * ``external_repo``    requires ``repository_url``.

    Field-level validators enforce the discriminator-shape rules so the API
    rejects mismatched shapes with HTTP 422 before they reach the DB-level
    ``ck_realisation_binding_kind_shape`` check constraint.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    binding_kind: RealisationBindingKind = Field(
        ..., description="One of coding_component / coding_container / external_repo"
    )
    container: str | None = Field(
        None, max_length=255, description="coding-containers container_name"
    )
    component_id: str | None = Field(
        None, max_length=255, description="coding-analysis Component.id"
    )
    repository_url: str | None = Field(
        None, description="External git URL (only for external_repo)"
    )
    repository_branch: str | None = Field(None, max_length=255, description="Optional branch ref")
    properties: dict[str, Any] | None = Field(
        default_factory=dict,
        description="Optional metadata (e.g. path, kind, node_id)",
    )

    @field_validator("binding_kind", mode="before")
    @classmethod
    def _normalise_kind(cls, v: Any) -> Any:
        """Preserve the pre-Literal ``strip().lower()`` coercion (see _lower_strip)."""
        return _lower_strip(v)

    def model_post_init(self, __context: Any) -> None:  # noqa: D401
        """Enforce the per-kind required-fields shape (matches DB check)."""
        kind = self.binding_kind
        if kind == "coding_component":
            if not self.container or not self.component_id:
                raise ValueError(
                    "coding_component binding requires both container and component_id"
                )
        elif kind == "coding_container":
            if not self.container:
                raise ValueError("coding_container binding requires container")
            if self.component_id:
                raise ValueError(
                    "coding_container binding must NOT set component_id "
                    "(use binding_kind=coding_component for component-level links)"
                )
        elif kind == "external_repo":
            if not self.repository_url:
                raise ValueError("external_repo binding requires repository_url")


class ArchitectureElementRealisationBindingOut(BaseModel):
    """Response body for one realisation binding row."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Binding ID")
    element_id: UUID = Field(..., description="Architecture element ID")
    tenant_id: str = Field(..., max_length=255, description="Tenant ID (denormalised from element)")
    binding_kind: str = Field(..., max_length=32, description="Binding kind discriminator")
    container: str | None = Field(
        None, max_length=255, description="coding-containers container_name"
    )
    component_id: str | None = Field(
        None, max_length=255, description="coding-analysis Component.id"
    )
    repository_url: str | None = Field(None, description="External git URL")
    repository_branch: str | None = Field(None, max_length=255, description="Branch ref")
    properties: dict[str, Any] | None = Field(None, description="Metadata")
    created_at: datetime | None = Field(None, description="Created timestamp")
    updated_at: datetime | None = Field(None, description="Updated timestamp")


class ArchitectureElementRealisationBindingList(BaseModel):
    """Response body for ``GET /architecture/elements/{eid}/bindings``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    bindings: list[ArchitectureElementRealisationBindingOut] = Field(default_factory=list)
    total: int = Field(..., ge=0)


class ArchitectureContainerDetail(BaseModel):
    """Response body for ``GET /architecture/containers/{container_name}``.

    Used by fr's project↔repo provisioning flow to validate that a container
    is reachable from a project's effective architecture before promoting
    the repo to primary.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., max_length=255, description="Container name")
    scope: str = Field(..., max_length=16, description="tenant | project | mixed")
    element_count: int = Field(..., ge=0, description="Number of bound elements")
    valid: bool = Field(..., description="Always true when 200; absent when 404")


class ArchitectureRelationshipIn(BaseModel):
    """Schema for creating an architecture relationship."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_entity_type: EntityType = Field(..., description="Source entity table name")
    source_id: UUID = Field(..., description="Source entity ID")
    target_entity_type: EntityType = Field(..., description="Target entity table name")
    target_id: UUID = Field(..., description="Target entity ID")
    relationship_type: RelationshipType = Field(..., description="Relationship type")
    project_id: ProjectScope = Field(None, max_length=100, description="Project scope")
    name: str | None = Field(None, max_length=300, description="Relationship label")
    description: str | None = Field(None, description="Description")
    properties: dict[str, Any] | None = Field(
        default_factory=dict, description="Metadata (e.g. lag_days)"
    )

    @field_validator("relationship_type", "source_entity_type", "target_entity_type", mode="before")
    @classmethod
    def _normalise_vocabulary(cls, v: Any) -> Any:
        """Preserve the pre-Literal ``strip().lower()`` coercion (see _lower_strip)."""
        return _lower_strip(v)


class ArchitectureRelationshipOut(BaseModel):
    """Schema for returning an architecture relationship from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Relationship ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    project_id: str | None = Field(None, max_length=100, description="Project scope")
    model_id: UUID | None = Field(None, description="Modeling model this row belongs to")
    source_entity_type: str = Field(..., description="Source entity type")
    source_id: UUID = Field(..., description="Source entity ID")
    target_entity_type: str = Field(..., description="Target entity type")
    target_id: UUID = Field(..., description="Target entity ID")
    relationship_type: str = Field(..., description="Relationship type")
    name: str | None = Field(None, description="Relationship label")
    description: str | None = Field(None, description="Description")
    properties: dict[str, Any] | None = Field(None, description="Metadata")
    created_at: Any | None = Field(None, description="Created")
    updated_at: Any | None = Field(None, description="Updated")

    @field_validator("properties", mode="before")
    @classmethod
    def _non_object_properties_read_as_none(cls, v: Any, info: ValidationInfo) -> Any:
        """A stored JSONB list/string must not 500 a read. Writes already
        refuse non-object properties with 422; legacy rows read as ``None``."""
        if v is None or isinstance(v, dict):
            return v
        _log.warning(
            "architecture_relationship.non_object_properties",
            extra={"relationship_id": str((info.data or {}).get("id")), "kind": type(v).__name__},
        )
        return None


class ArchitectureRelationshipUpdate(BaseModel):
    """Partial-update payload for an existing architecture relationship.

    Every field is optional. ``relationship_type`` and entity-type fields
    re-trigger metamodel validation in the service layer when supplied.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    # ⚠️ These three keep ``| None`` because this is a PARTIAL-update body: None
    # means "field not supplied", not "set it to null". Making them non-optional
    # with a default would turn every PATCH into a full overwrite of the edge —
    # data corruption traded for a dropdown. Their enums are therefore
    # anyOf-nested and invisible to the n8n generator, same as
    # ``architecture_state``; the create body (``ArchitectureRelationshipIn``)
    # is the one that publishes a top-level enum.
    source_entity_type: EntityType | None = Field(None, description="Source entity table name")
    source_id: UUID | None = Field(None, description="Source entity ID")
    target_entity_type: EntityType | None = Field(None, description="Target entity table name")
    target_id: UUID | None = Field(None, description="Target entity ID")
    relationship_type: RelationshipType | None = Field(None, description="Relationship type")
    project_id: str | None = Field(None, max_length=100, description="Project scope")
    name: str | None = Field(None, max_length=300, description="Relationship label")
    description: str | None = Field(None, description="Description")
    properties: dict[str, Any] | None = Field(None, description="Metadata (e.g. lag_days)")

    @field_validator("relationship_type", "source_entity_type", "target_entity_type", mode="before")
    @classmethod
    def _normalise_vocabulary(cls, v: Any) -> Any:
        """Preserve the pre-Literal ``strip().lower()`` coercion (see _lower_strip)."""
        return _lower_strip(v)


class MetamodelValidationResult(BaseModel):
    """Result of metamodel relationship validation."""

    model_config = ConfigDict(str_strip_whitespace=True)

    valid: bool = Field(..., description="Whether the relationship is valid per ArchiMate rules")
    violations: list[str] = Field(default_factory=list, description="Violation messages")
    suggestions: list[str] = Field(default_factory=list, description="Suggestions")


# ---------------------------------------------------------------------------
# Architecture Views
# ---------------------------------------------------------------------------


class ArchitectureViewIn(VersionFieldsIn):
    """Schema for creating or updating an architecture view."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="View name")
    description: str | None = Field(None, description="Purpose and audience")
    project_id: ProjectScope = Field(None, max_length=100, description="Project scope")
    model_id: UUID | None = Field(
        None,
        exclude=True,
        description=(
            "Create only: the modeling model to add this to (must be an ArchiMate model the "
            "caller may write, with the row's visibility). Omitted: the default model of "
            "the scope (tenant, project_id, visibility). Ignored on update."
        ),
    )
    viewpoint: ArchitectureViewpoint | None = Field(
        None, description="ArchiMate viewpoint; null = none (unfiltered, as in Archi)"
    )
    element_ids: list[UUID] | None = Field(default_factory=list, description="Element IDs")
    relationship_ids: list[UUID] | None = Field(
        default_factory=list, description="Relationship IDs"
    )
    layout_data: dict[str, Any] | None = Field(
        default_factory=dict, description="Visualization hints"
    )

    @field_validator("viewpoint", mode="before")
    @classmethod
    def _normalise_vocabulary(cls, v: Any) -> Any:
        """Preserve the pre-Literal ``strip().lower()`` coercion (see _lower_strip)."""
        return _lower_strip(v)


class ArchitectureViewOut(SystemInheritanceOutMixin, VersionFieldsMixin, TimestampMixin):
    """Schema for returning an architecture view from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="View ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="View name")
    description: str | None = Field(None, description="Purpose and audience")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    project_id: str | None = Field(None, max_length=100, description="Project scope")
    model_id: UUID | None = Field(None, description="Modeling model this row belongs to")
    viewpoint: str | None = Field(
        None, max_length=50, description="ArchiMate viewpoint; null = none (unfiltered)"
    )
    element_ids: list[UUID] | None = Field(None, description="Element IDs")
    relationship_ids: list[UUID] | None = Field(None, description="Relationship IDs")
    layout_data: dict[str, Any] | None = Field(None, description="Visualization hints")
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


# ---------------------------------------------------------------------------
# Architecture Standards
# ---------------------------------------------------------------------------


class StandardIn(VersionFieldsIn):
    """Schema for creating or updating an architecture standard."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Standard name")
    description: str | None = Field(None, description="Full description")
    project_id: ProjectScope = Field(None, max_length=100, description="Project scope")
    standard_type: StandardType = Field(..., description="Standard type")
    status: StandardStatus = Field(default="assess", description="Tech radar status")
    compliance_level: ComplianceLevel = Field(default="recommended", description="Compliance level")
    related_principle_ids: list[UUID] | None = Field(
        default_factory=list, description="Related principle IDs"
    )
    valid_from: datetime | None = Field(None, description="Effective date")
    valid_until: datetime | None = Field(None, description="Expiry date")

    @field_validator("standard_type", "status", "compliance_level", mode="before")
    @classmethod
    def _normalise_vocabulary(cls, v: Any) -> Any:
        """Preserve the pre-Literal ``strip().lower()`` coercion (see _lower_strip)."""
        return _lower_strip(v)


class StandardOut(SystemInheritanceOutMixin, VersionFieldsMixin, TimestampMixin):
    """Schema for returning an architecture standard from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Standard ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Standard name")
    description: str | None = Field(None, description="Full description")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    project_id: str | None = Field(None, max_length=100, description="Project scope")
    standard_type: str = Field(..., max_length=30, description="Standard type")
    status: str = Field(default="assess", max_length=20, description="Tech radar status")
    compliance_level: str = Field(
        default="recommended", max_length=20, description="Compliance level"
    )
    related_principle_ids: list[UUID] | None = Field(None, description="Related principle IDs")
    valid_from: datetime | None = Field(None, description="Effective date")
    valid_until: datetime | None = Field(None, description="Expiry date")
