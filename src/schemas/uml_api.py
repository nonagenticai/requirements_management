"""API shapes of the stored UML routes (``/uml/*``, target spec §8.2).

Bodies reuse the snapshot parts of ``schemas.uml`` (attributes, ends, interaction parts)
so a stored row, a revision manifest and the stateless validator all speak one shape.
References to rows (``owner_id``, ``source``, ``element_ref``, …) are plain strings: a
value that is not a row of the same model — malformed, missing, another model's, another
tenant's or another subject's private row — is refused uniformly as ``422 model_integrity``
with ``UML-REF-001`` / ``UML-DIA-*``, so existence never leaks.

``model_id`` is fixed at create: update bodies do not carry it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.modeling import Bounds, Point, RulesetInfo, Style
from schemas.uml import (
    UmlAssociationEnd,
    UmlElementAttributes,
    UmlExecution,
    UmlFragment,
    UmlLifeline,
    UmlMessage,
    UmlOccurrence,
)

# Upper bounds on every list a body carries (the snapshot schemas reused here are
# unbounded on purpose — the stateless validator states its own limits — so they are
# enforced here). A larger model is refused at the model size cap anyway; these keep a
# single body from being arbitrarily large before that check can run.
MAX_PARTS = 5000  # one interaction document part; one feature group of an element
MAX_NESTED = 500  # parameters, ends, fragment operands / covered / occurrences, arguments,
#                   bendpoints, interface lists, style keys
MAX_TEXT = 100_000  # documentation


def _bounded(label: str, items: Any, limit: int) -> None:
    if items is not None and len(items) > limit:
        raise ValueError(f"{label}: at most {limit} items (got {len(items)})")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ModelScope(_Strict):
    model_id: UUID | None = Field(
        None,
        description=(
            "UML model to join. Omitted: the default UML model of the scope "
            "(tenant, project_id, X-Visibility, subject if private), created on first use"
        ),
    )
    project_id: str | None = Field(
        None,
        max_length=100,
        description="Scope of the default model; with model_id it must equal the model's",
    )


# -- elements ---------------------------------------------------------------------


class UmlElementUpdate(_Strict):
    kind: str = Field(..., max_length=30, description="One of the profile's element kinds")
    name: str = Field("", max_length=300)
    owner_id: str | None = Field(
        None, max_length=100, description="Owning element (package; class/component for a port)"
    )
    attributes: UmlElementAttributes = Field(default_factory=UmlElementAttributes)

    @model_validator(mode="after")
    def _limits(self) -> UmlElementUpdate:
        a = self.attributes
        for group in ("properties", "operations", "literals"):
            _bounded(f"attributes.{group}", getattr(a, group), MAX_PARTS)
        for group in ("provided", "required"):
            _bounded(f"attributes.{group}", getattr(a, group), MAX_NESTED)
        for n, op in enumerate(a.operations):
            _bounded(f"attributes.operations[{n}].parameters", op.parameters, MAX_NESTED)
        return self


class UmlElementCreate(UmlElementUpdate, _ModelScope):
    pass


class _RowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    model_id: UUID
    project_id: str | None
    visibility: str
    created_at: datetime | None
    updated_at: datetime | None


class UmlElementOut(_RowOut):
    kind: str
    name: str
    owner_id: UUID | None
    attributes: dict[str, Any]


# -- relationships ----------------------------------------------------------------


class UmlRelationshipUpdate(_Strict):
    kind: str = Field(..., max_length=30, description="One of the profile's relationship kinds")
    name: str = Field("", max_length=300)
    source: str | None = Field(
        None, max_length=100, description="Element id; for generalization, the specific"
    )
    target: str | None = Field(
        None, max_length=100, description="Element id; for generalization, the general"
    )
    ends: list[UmlAssociationEnd] | None = Field(
        None, max_length=MAX_NESTED, description="association only: two ends"
    )
    connector_kind: str | None = Field(None, max_length=20, description="assembly | delegation")


class UmlRelationshipCreate(UmlRelationshipUpdate):
    model_id: UUID | None = Field(
        None, description="UML model to join. Omitted: the model of the source (or first) end"
    )


class UmlRelationshipOut(_RowOut):
    kind: str
    name: str
    source: UUID | None
    target: UUID | None
    ends: list[dict[str, Any]] | None
    connector_kind: str | None


# -- interactions -----------------------------------------------------------------


class UmlInteractionUpdate(_Strict):
    """The whole interaction document; ``PUT`` replaces it."""

    name: str = Field("", max_length=300)
    owner_id: str | None = Field(None, max_length=100)
    lifelines: list[UmlLifeline] = Field(default_factory=list, max_length=MAX_PARTS)
    occurrences: list[UmlOccurrence] = Field(
        default_factory=list,
        max_length=MAX_PARTS,
        description="Ordered: list position is the time order",
    )
    messages: list[UmlMessage] = Field(default_factory=list, max_length=MAX_PARTS)
    executions: list[UmlExecution] = Field(default_factory=list, max_length=MAX_PARTS)
    fragments: list[UmlFragment] = Field(default_factory=list, max_length=MAX_PARTS)

    @model_validator(mode="after")
    def _limits(self) -> UmlInteractionUpdate:
        for n, m in enumerate(self.messages):
            _bounded(f"messages[{n}].arguments", m.arguments, MAX_NESTED)
        for n, f in enumerate(self.fragments):
            _bounded(f"fragments[{n}].covered", f.covered, MAX_NESTED)
            _bounded(f"fragments[{n}].operands", f.operands, MAX_NESTED)
            for k, operand in enumerate(f.operands):
                _bounded(
                    f"fragments[{n}].operands[{k}].occurrences", operand.occurrences, MAX_PARTS
                )
        return self


class UmlInteractionCreate(UmlInteractionUpdate, _ModelScope):
    pass


class UmlInteractionOut(_RowOut):
    name: str
    owner_id: UUID | None
    lifelines: list[dict[str, Any]]
    occurrences: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    executions: list[dict[str, Any]]
    fragments: list[dict[str, Any]]


# -- diagrams ---------------------------------------------------------------------


class UmlDiagramUpdate(_Strict):
    kind: str = Field(..., max_length=20, description="class | component | sequence")
    name: str = Field("", max_length=300)
    documentation: str | None = Field(None, max_length=MAX_TEXT)


class UmlDiagramCreate(UmlDiagramUpdate, _ModelScope):
    pass


class UmlDiagramOut(_RowOut):
    kind: str
    name: str
    documentation: str | None


class UmlDiagramObjectIn(_Strict):
    element_ref: str | None = Field(
        None, max_length=100, description="Element shown (class / component diagrams)"
    )
    lifeline_id: str | None = Field(
        None, max_length=100, description="Lifeline shown (sequence diagrams)"
    )
    bounds: Bounds | None = None
    style: Style | None = None
    position: int | None = Field(
        None,
        ge=0,
        le=100000,
        description=(
            "z-order: the object's index in the diagram's object list (0 = bottom). "
            "Omitted: on create the object goes on top (appended), on update it keeps its "
            "place."
        ),
    )

    @model_validator(mode="after")
    def _limits(self) -> UmlDiagramObjectIn:
        _bounded("style", self.style, MAX_NESTED)
        return self


class UmlDiagramObjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    diagram_id: UUID
    model_id: UUID
    element_ref: UUID | None
    lifeline_id: str | None
    bounds: dict[str, Any] | None
    style: dict[str, Any] | None
    position: int = Field(..., description="z-order: lists are ordered by it (then id)")
    created_at: datetime | None
    updated_at: datetime | None


class UmlDiagramConnectionIn(_Strict):
    relationship_ref: str | None = Field(
        None, max_length=100, description="Relationship shown (class / component diagrams)"
    )
    message_id: str | None = Field(
        None, max_length=100, description="Message shown (sequence diagrams)"
    )
    source_object_id: str = Field(..., max_length=100)
    target_object_id: str = Field(..., max_length=100)
    bendpoints: list[Point] | None = Field(None, max_length=MAX_NESTED)
    style: Style | None = None

    @model_validator(mode="after")
    def _limits(self) -> UmlDiagramConnectionIn:
        _bounded("style", self.style, MAX_NESTED)
        return self


class UmlDiagramConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    diagram_id: UUID
    model_id: UUID
    relationship_ref: UUID | None
    message_id: str | None
    source_object_id: UUID
    target_object_id: UUID
    bendpoints: list[dict[str, Any]] | None
    style: dict[str, Any] | None
    created_at: datetime | None
    updated_at: datetime | None


class RenderReport(BaseModel):
    """``?report=true``: the sanitized SVG and the renderer's diagnostics."""

    svg: str
    diagnostics: list[dict[str, Any]]
    renderer_version: str


class UmlDeleteResult(BaseModel):
    """Body of a successful UML DELETE (``services.uml_store.delete``).

    ``cascaded`` lists, per UML kind, the ids of the OTHER rows the cascade removed
    (sorted); a kind with nothing removed is absent, so ``{}`` means nothing else went.
    """

    status: Literal["deleted"] = Field(..., description="Always 'deleted'")
    id: UUID = Field(..., description="ID of the deleted row")
    cascaded: dict[str, list[str]] = Field(
        ...,
        description=(
            "Rows removed by the cascade, keyed by kind (uml_element, uml_relationship, "
            "interaction, diagram, diagram_object, diagram_connection); only non-empty kinds"
        ),
    )


class UmlDiagramKindOut(BaseModel):
    """One supported diagram kind and what its objects / connections show."""

    kind: str
    object_ref: str = Field(..., description="'element' or 'lifeline'")
    connection_ref: str = Field(..., description="'relationship' or 'message'")


class UmlProfileSchemas(BaseModel):
    """JSON Schemas of the snapshot parts a client sends."""

    element: dict[str, Any]
    relationship: dict[str, Any]
    interaction: dict[str, Any]
    diagram: dict[str, Any]


class UmlProfileOut(BaseModel):
    """``GET /uml/metamodel``: the ``tools.uml.profile.Profile`` (sets as sorted lists),
    plus the ruleset and the JSON Schemas of the snapshot parts."""

    profile_id: str
    ruleset_id: str
    language_version: str
    reference: dict[str, Any]
    element_kinds: list[str]
    element_features: dict[str, list[str]]
    owner_kinds: dict[str, list[str]]
    owner_required: list[str]
    interaction_owner_kinds: list[str]
    relationship_kinds: list[str]
    aggregation_kinds: list[str]
    connector_kinds: list[str]
    visibilities: list[str]
    parameter_directions: list[str]
    generalization_compatible: dict[str, list[str]]
    lifeline_type_kinds: list[str]
    message_kinds: list[str]
    synchronous_message_kinds: list[str]
    fragment_operators: list[str]
    guarded_operators: list[str]
    single_operand_operators: list[str]
    diagram_kinds: dict[str, UmlDiagramKindOut]
    ruleset: RulesetInfo
    schemas: UmlProfileSchemas
