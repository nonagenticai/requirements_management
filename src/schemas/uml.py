"""UML snapshot — the input of the pure UML validator (profile ``uml-2.5.1-p1``).

Revision manifests and the stateless ``POST /uml/tools/validate`` body share
this shape (target spec §8.2). Kinds, operators, aggregation and visibility are
plain strings on purpose: a value outside the profile must reach the validator
and come back as ``UML-UNSUP-001``, not die as a schema 422 that names no rule.

Multiplicities are strings in UML notation: ``"1"``, ``"0..1"``, ``"*"``,
``"1..*"``. ``None`` means "unspecified" (UML default 1).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from schemas.modeling import Bounds, Point, Style

Id = Annotated[str, Field(min_length=1, max_length=100)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UmlProperty(_Strict):
    id: Id
    name: str = Field("", max_length=300)
    type_ref: str | None = Field(None, description="Element id of the property's type")
    multiplicity: str | None = None
    visibility: str | None = None
    is_static: bool = False
    default: str | None = None


class UmlParameter(_Strict):
    id: Id
    name: str = Field("", max_length=300)
    type_ref: str | None = None
    direction: str | None = Field(None, description="in | out | inout | return")
    multiplicity: str | None = None


class UmlOperation(_Strict):
    id: Id
    name: str = Field("", max_length=300)
    visibility: str | None = None
    is_abstract: bool = False
    is_static: bool = False
    parameters: list[UmlParameter] = Field(default_factory=list)
    return_type_ref: str | None = None


class UmlElementAttributes(_Strict):
    properties: list[UmlProperty] = Field(default_factory=list)
    operations: list[UmlOperation] = Field(default_factory=list)
    literals: list[str] = Field(default_factory=list, description="enumeration only")
    is_abstract: bool = False
    provided: list[str] = Field(default_factory=list, description="Interface ids (component/port)")
    required: list[str] = Field(default_factory=list, description="Interface ids (component/port)")


class UmlElementIn(_Strict):
    id: Id
    kind: str = Field(..., description="package|class|interface|data_type|enumeration|...")
    name: str = Field("", max_length=300)
    owner_id: str | None = Field(
        None, description="Owning element (package; class/component for a port)"
    )
    attributes: UmlElementAttributes = Field(default_factory=UmlElementAttributes)


class UmlAssociationEnd(_Strict):
    element_ref: str
    role: str | None = None
    multiplicity: str | None = None
    aggregation: str = Field("none", description="none | shared | composite")
    navigable: bool = False


class UmlRelationshipIn(_Strict):
    id: Id
    kind: str = Field(..., description="association|generalization|interface_realization|...")
    name: str = Field("", max_length=300)
    source: str | None = Field(None, description="Element id; for generalization, the specific")
    target: str | None = Field(None, description="Element id; for generalization, the general")
    ends: list[UmlAssociationEnd] | None = Field(None, description="association only, exactly two")
    connector_kind: str | None = Field(None, description="assembly | delegation — connector only")


class UmlLifeline(_Strict):
    id: Id
    name: str = Field("", max_length=300)
    represents_ref: str | None = Field(None, description="Class, component, interface or actor id")


class UmlOccurrence(_Strict):
    id: Id
    lifeline: str


class UmlMessage(_Strict):
    id: Id
    kind: str = Field(..., description="synch_call|asynch_call|asynch_signal|reply|create|delete")
    name: str = Field("", max_length=300)
    send_occurrence: str
    receive_occurrence: str
    operation_ref: str | None = None
    arguments: list[str] = Field(default_factory=list)


class UmlExecution(_Strict):
    id: Id
    lifeline: str
    start: str
    finish: str


class UmlOperand(_Strict):
    guard: str | None = None
    occurrences: list[str] = Field(default_factory=list)


class UmlFragment(_Strict):
    id: Id
    operator: str = Field(..., description="alt | opt | loop | par | break")
    covered: list[str] = Field(default_factory=list, description="Lifeline ids")
    operands: list[UmlOperand] = Field(default_factory=list)


class UmlInteractionIn(_Strict):
    id: Id
    name: str = Field("", max_length=300)
    owner_id: str | None = None
    lifelines: list[UmlLifeline] = Field(default_factory=list)
    occurrences: list[UmlOccurrence] = Field(
        default_factory=list, description="Ordered: list position is the time order"
    )
    messages: list[UmlMessage] = Field(default_factory=list)
    executions: list[UmlExecution] = Field(default_factory=list)
    fragments: list[UmlFragment] = Field(default_factory=list)


class UmlDiagramObjectIn(_Strict):
    id: Id
    element_ref: str | None = None
    lifeline_id: str | None = None
    bounds: Bounds | None = None
    style: Style | None = None


class UmlDiagramConnectionIn(_Strict):
    id: Id
    relationship_ref: str | None = None
    message_id: str | None = None
    source_object_id: str
    target_object_id: str
    bendpoints: list[Point] | None = None
    style: Style | None = None


class UmlDiagramIn(_Strict):
    id: Id
    kind: str = Field(..., description="class | component | sequence")
    name: str = Field("", max_length=300)
    objects: list[UmlDiagramObjectIn] = Field(default_factory=list)
    connections: list[UmlDiagramConnectionIn] = Field(default_factory=list)


class UmlSnapshot(_Strict):
    """A whole UML model, or the part of it a caller wants checked.

    ``diagrams=None`` means "diagrams not supplied": the diagram rules
    (``UML-DIA-*``) report nothing and are not listed as enabled.
    """

    elements: list[UmlElementIn] = Field(default_factory=list)
    relationships: list[UmlRelationshipIn] = Field(default_factory=list)
    interactions: list[UmlInteractionIn] = Field(default_factory=list)
    diagrams: list[UmlDiagramIn] | None = None
