"""Logical Thinking Process (TOC) request/response schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.architecture import ArchitectureRelationshipOut
from schemas.common import (
    OriginFieldsInMixin,
    OriginFieldsOutMixin,
    ProjectScope,
    SystemInheritanceOutMixin,
    TimestampMixin,
    VersionFieldsIn,
    VersionFieldsMixin,
)

# Edge relationship types valid between toc_nodes (subset of RELATIONSHIP_TYPES).
TOC_EDGE_TYPES = frozenset({"toc_necessity", "toc_sufficiency", "toc_conflict", "toc_assumption"})

# ---------------------------------------------------------------------------
# Request-side vocabularies as ``Literal`` unions.
#
# A ``field_validator`` returns a correct 422 but emits NO ``enum`` into the
# OpenAPI schema, so every generated client renders a free-text box. These
# aliases put the membership check in the type, which pydantic serialises as a
# top-level ``enum`` on the property.
#
# Deliberately NOT ``Literal[...] | None``: pydantic emits that as
# ``anyOf: [{enum: [...]}, {"type": "null"}]`` and the downstream n8n generator
# reads ``enum`` only at the TOP level of a property, so a nested enum is
# invisible.
#
# Deliberately spelled out rather than derived from the frozensets: a
# ``frozenset`` has no stable order and ``Literal[*sorted(...)]`` is not
# statically checkable. ``tests/test_toc_edge_scoping.py`` pins each alias to
# its runtime constant by set equality, so drift fails CI.
#
# ⛔ REQUEST models only. The ``*Out`` response models carry no validator today;
# a ``Literal`` there would be a NEW constraint on rows already in the database
# and one legacy value would 500 the read path.

TocTreeType = Literal["io_map", "evaporating_cloud", "crt", "frt", "prt", "trt"]

# The FULL cross-tree union (``models.toc.ALL_NODE_KINDS``). The per-tree-type
# subset is enforced by the router (``_validate_node_kind``) because it needs the
# tree row; narrowing this Literal to one tree's kinds would reject valid input.
TocNodeKind = Literal[
    "goal",
    "csf",
    "necessary_condition",
    "objective",
    "requirement",
    "prerequisite",
    "assumption",
    "injection",
    "ude",
    "intermediate_cause",
    "root_cause",
    "intermediate_effect",
    "desired_effect",
    "obstacle",
    "intermediate_objective",
    "need",
    "action",
    "expected_effect",
]

TocEdgeType = Literal["toc_necessity", "toc_sufficiency", "toc_conflict", "toc_assumption"]


def _normalise_vocab(v: Any) -> Any:
    """Strip + lowercase a vocabulary value before the ``Literal`` membership check.

    The replaced ``field_validator``s all did ``v.strip().lower()``, so
    ``"DRAFT"`` / ``" draft "`` were accepted and coerced. A bare ``Literal``
    would newly reject them; this ``mode="before"`` hook keeps the old
    coercion and leaves membership to the type. Non-``str`` input is passed
    through untouched so pydantic emits its own type error.
    """
    return v.strip().lower() if isinstance(v, str) else v


# ---------------------------------------------------------------------------
# Trees
# ---------------------------------------------------------------------------


class TocTreeIn(VersionFieldsIn, OriginFieldsInMixin):
    """Schema for creating or updating a TOC tree."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Tree title")
    description: str | None = Field(None, description="What the tree analyses")
    project_id: ProjectScope = Field(None, max_length=100, description="Project scope")
    tree_type: TocTreeType = Field(
        ...,
        description="One of: io_map, evaporating_cloud, crt, frt, prt, trt",
    )

    @field_validator("tree_type", mode="before")
    @classmethod
    def normalise_tree_type(cls, v: Any) -> Any:
        """Keep the pre-Literal ``.strip().lower()`` coercion; membership is the type's job."""
        return _normalise_vocab(v)


class TocTreeOut(
    SystemInheritanceOutMixin,
    VersionFieldsMixin,
    OriginFieldsOutMixin,
    TimestampMixin,
):
    """Schema returned for a TOC tree."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Tree ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Tree title")
    description: str | None = Field(None, description="Description")
    project_id: str | None = Field(None, max_length=100, description="Project ID")
    tenant_id: str = Field(..., min_length=1, max_length=255, description="Tenant ID")
    tree_type: str = Field(..., description="Tree type")
    quality_score: float | None = Field(default=None, description="Latest CLR score (0-1)")
    quality_issues: list[str] | None = Field(default=None, description="CLR reservations raised")
    quality_suggestions: list[str] | None = Field(default=None, description="CLR suggestions")
    last_evaluated_at: datetime | None = Field(default=None, description="Last CLR scrutiny")
    last_evaluated_by_agent: str | None = Field(default=None, description="Evaluator")


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


class TocNodeIn(VersionFieldsIn):
    """Schema for creating or updating a node within a tree.

    ``tree_id`` is set from the path by the router; any value supplied in the
    body is overwritten so a node cannot be reparented to a foreign tree. It is
    therefore OPTIONAL in the body — requiring it made the documented payload
    422 on its first call.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    tree_id: UUID | None = Field(
        default=None,
        description="Owning tree ID — set from the path; any value sent here is overwritten",
    )
    title: str = Field(..., min_length=1, max_length=300, description="Short node label")
    statement: str = Field(..., min_length=1, description="The full entity statement")
    node_kind: TocNodeKind = Field(
        ..., description="Node kind (further narrowed against the tree's type by the router)"
    )
    layout: dict[str, Any] = Field(default_factory=dict, description="Optional layout/position")
    project_id: str | None = Field(None, max_length=100, description="Project scope")

    @field_validator("node_kind", mode="before")
    @classmethod
    def normalise_node_kind(cls, v: Any) -> Any:
        """Keep the pre-Literal ``.strip().lower()`` coercion; membership is the type's job."""
        return _normalise_vocab(v)


class TocNodeOut(SystemInheritanceOutMixin, VersionFieldsMixin, TimestampMixin):
    """Schema returned for a node."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Node ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    tree_id: UUID = Field(..., description="Owning tree ID")
    title: str = Field(..., min_length=1, max_length=300, description="Short node label")
    statement: str = Field(..., min_length=1, description="Entity statement")
    node_kind: str = Field(..., description="Node kind")
    layout: dict[str, Any] = Field(default_factory=dict, description="Layout/position")
    project_id: str | None = Field(None, max_length=100, description="Project ID")
    tenant_id: str = Field(..., min_length=1, max_length=255, description="Tenant ID")


# ---------------------------------------------------------------------------
# Edges (thin wrapper over architecture_relationships, forced to toc_nodes)
# ---------------------------------------------------------------------------


class TocEdgeIn(BaseModel):
    """Schema for creating an edge between two nodes of the same tree."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_id: UUID = Field(..., description="Source node ID (the cause / condition)")
    target_id: UUID = Field(..., description="Target node ID (the effect / objective)")
    relationship_type: TocEdgeType = Field(
        ..., description="toc_necessity | toc_sufficiency | toc_conflict | toc_assumption"
    )
    name: str | None = Field(None, max_length=300, description="Edge label")
    description: str | None = Field(None, description="Description")
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Edge metadata, e.g. {'and_group': 'g1', 'assumptions': [...]}",
    )

    @field_validator("relationship_type", mode="before")
    @classmethod
    def normalise_edge_type(cls, v: Any) -> Any:
        """Keep the pre-Literal ``.strip().lower()`` coercion; membership is the type's job."""
        return _normalise_vocab(v)


# ---------------------------------------------------------------------------
# Graph + CLR + config
# ---------------------------------------------------------------------------


class TocGraph(BaseModel):
    """A whole tree: container + nodes + edges."""

    model_config = ConfigDict(from_attributes=True)

    tree: TocTreeOut
    nodes: list[TocNodeOut] = Field(default_factory=list)
    edges: list[ArchitectureRelationshipOut] = Field(default_factory=list)


class ClrReport(BaseModel):
    """Structural Categories-of-Legitimate-Reservation scrutiny of a tree.

    These are well-formedness checks (structure the eight CLR can be tested
    against without subject knowledge). Semantic CLR scrutiny is the job of
    the LLM reasoning lenses in the n8n QA/authoring subflows.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    score: float = Field(..., ge=0, le=1, description="Fraction of CLR checks passed (0-1)")
    passes: list[str] = Field(default_factory=list, description="CLR checks satisfied")
    issues: list[str] = Field(default_factory=list, description="CLR reservations raised")
    categories: dict[str, bool] = Field(
        default_factory=dict, description="Per-category pass/fail across the 8 CLR"
    )


class TocSettingsIn(BaseModel):
    """Upsert payload for the 3-level config."""

    model_config = ConfigDict(str_strip_whitespace=True)

    project_id: str = Field(default="", max_length=100, description="'' for the tenant-level row")
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="e.g. {'ec_auto_severity_threshold': 'high', 'enabled_lenses': [...]}",
    )


class TocSettingsOut(BaseModel):
    """Resolved (merged) effective config for a (tenant, project)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    tenant_id: str = Field(..., description="Tenant the config resolved for")
    project_id: str = Field(default="", description="Project the config resolved for")
    settings: dict[str, Any] = Field(default_factory=dict, description="Effective merged settings")
