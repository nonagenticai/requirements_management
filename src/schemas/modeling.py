"""Language-neutral validation contract shared by ArchiMate and UML.

One finding shape serves write-time 422 bodies, persisted validation runs and
the stateless ``…/tools/validate`` routes (target spec §7.3). The validators
that produce it are pure functions; nothing here touches the database.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

Severity = Literal["error", "warning", "advice"]

TargetKind = Literal[
    "element",
    "relationship",
    "view",
    "view_object",
    "view_connection",
    "uml_element",
    "uml_relationship",
    "interaction",
    "diagram",
    "diagram_object",
    "diagram_connection",
]

RunStatus = Literal["complete", "incomplete", "failed"]


class FindingTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TargetKind
    id: str


class Finding(BaseModel):
    """One diagnostic. ``message`` is deterministic: same input, same text."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(..., description="Stable rule identifier, e.g. AM-REL-001")
    ruleset_id: str = Field(..., description="e.g. archimate-3.2/archi-5.10.0")
    severity: Severity
    targets: list[FindingTarget] = Field(default_factory=list)
    path: str | None = Field(None, description="JSON pointer into the target's attributes")
    message: str
    evidence: dict[str, Any] | None = None


class SeverityCounts(BaseModel):
    error: int = 0
    warning: int = 0
    advice: int = 0


class RuleInfo(BaseModel):
    rule_id: str
    title: str
    default_severity: Severity
    required: bool = Field(..., description="A run with this rule disabled is never clean")
    description: str
    source: str = Field(..., description="Where the rule comes from (Archi checker, spec clause)")


class RulesetInfo(BaseModel):
    ruleset_id: str
    language: Literal["archimate", "uml"]
    language_version: str
    reference: dict[str, Any]
    rule_count: int
    rules: list[RuleInfo] = Field(default_factory=list)


class ValidationReport(BaseModel):
    """Result of one pure validation pass over a snapshot."""

    ruleset_id: str
    enabled_rules: list[str]
    status: RunStatus
    examined: dict[str, int]
    counts: SeverityCounts
    clean: bool = Field(
        ...,
        description="complete AND zero errors AND every required rule enabled",
    )
    findings: list[Finding]


SEVERITY_RANK = {"error": 0, "warning": 1, "advice": 2}

# Error ``type`` the request-validation handler recognises (main.py): a schema
# validator that raises it yields a ``model_integrity`` 422 whose findings come
# from the error's ``ctx`` instead of a generic VALIDATION_ERROR.
INTEGRITY_ERROR_TYPE = "model_integrity"


def integrity_value_error(
    rule_id: str,
    message: str,
    *,
    path: str | None = None,
    evidence: dict[str, Any] | None = None,
    ruleset_id: str = "archimate-3.2/archi-5.10.0",
) -> PydanticCustomError:
    finding = Finding(
        rule_id=rule_id,
        ruleset_id=ruleset_id,
        severity="error",
        targets=[],
        path=path,
        message=message,
        evidence=evidence,
    )
    return PydanticCustomError(
        INTEGRITY_ERROR_TYPE,
        "{rule_id}: {message}",
        {"rule_id": rule_id, "message": message, "finding": finding.model_dump(mode="json")},
    )


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Order by (severity desc, rule_id, first target id) — spec §7.3."""
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_RANK[f.severity],
            f.rule_id,
            f.targets[0].id if f.targets else "",
            f.message,
        ),
    )


def build_report(
    *,
    ruleset_id: str,
    enabled: list[str],
    required: set[str],
    examined: dict[str, int],
    findings: list[Finding],
    status: RunStatus = "complete",
) -> ValidationReport:
    counts = SeverityCounts()
    for f in findings:
        setattr(counts, f.severity, getattr(counts, f.severity) + 1)
    enabled_sorted = sorted(enabled)
    # Disabling a required rule makes the run incomplete, never clean (§4.1).
    if status == "complete" and not required <= set(enabled):
        status = "incomplete"
    clean = status == "complete" and counts.error == 0
    return ValidationReport(
        ruleset_id=ruleset_id,
        enabled_rules=enabled_sorted,
        status=status,
        examined=examined,
        counts=counts,
        clean=clean,
        findings=sort_findings(findings),
    )


# ---------------------------------------------------------------------------
# ArchiMate snapshot — the input of the pure validator. Revision manifests and
# the stateless ``POST /architecture/tools/validate`` body share this shape.
# Types are plain strings on purpose: an unknown type must reach the validator
# and come back as a finding (AM-TYP-*), not die as a schema 422 that names no
# rule.
# ---------------------------------------------------------------------------


class Bounds(BaseModel):
    """Diagram geometry in Archi's coordinate space (pixels, origin top-left).

    ``x``/``y`` are relative to the parent occurrence when there is one, as in
    Archi and the Open Group exchange format. ``w``/``h`` of -1 mean "default
    size", which is how Archi stores an unsized figure.
    """

    model_config = ConfigDict(extra="forbid")

    x: int = 0
    y: int = 0
    w: int = -1
    h: int = -1


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int
    y: int


# Free-form presentation: fill/line/font colour, font, alignment, notation
# variant ("box" | "icon"). Keys are presentation only — never semantics.
Style = dict[str, Any]
# User-defined properties: ordered key/value pairs; keys may repeat, as in Archi.
Properties = list[tuple[str, str]]


class ArchimateConceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., description="ArchiMate element type, e.g. business_actor")
    name: str = Field("", max_length=300)
    layer: str | None = Field(None, description="If given, must match the registry's layer")
    parent_id: str | None = Field(None, description="Organisational parent (decomposition)")
    junction_type: str | None = Field(None, description="and | or — junctions only")
    documentation: str | None = None
    properties: Properties | None = None


class ArchimateRelationshipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., description="One of the 11 ArchiMate relationship types")
    source: str = Field(..., description="Concept id or relationship id")
    target: str = Field(..., description="Concept id or relationship id")
    name: str = Field("", max_length=300)
    access_type: str | None = Field(None, description="write|read|access|readwrite — access only")
    strength: str | None = Field(None, max_length=50, description="influence only")
    directed: bool | None = Field(None, description="association only")
    documentation: str | None = None
    properties: Properties | None = None


ViewObjectKind = Literal["element", "note", "group"]
ViewConnectionKind = Literal["relationship", "line"]


class ArchimateViewObjectIn(BaseModel):
    """One occurrence on a view.

    ``kind="element"`` shows a concept (``concept_id`` required). ``note`` and
    ``group`` are Archi's purely visual diagram objects: they carry no concept
    (``concept_id`` forbidden) and no semantics — a note's body is ``text``, a
    group's title is ``name``. ``label`` is an Archi label expression shown in
    place of an element's name (e.g. ``"${name} (${type})"``).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=100)
    kind: ViewObjectKind = "element"
    concept_id: str | None = None
    parent_object_id: str | None = None
    bounds: Bounds | None = None
    style: Style | None = None
    name: str | None = Field(None, description="group title")
    text: str | None = Field(None, description="note body")
    label: str | None = Field(None, description="Archi label expression on an element occurrence")
    border_type: str | None = Field(
        None, description='note: "dogear"|"rectangle"|"none"; group: "tabbed"|"rectangle"'
    )
    documentation: str | None = Field(
        None, description="note/group only; an element occurrence uses its concept's"
    )
    view_ref: str | None = Field(
        None, description="drill-down: id of a view of the same model this object refers to"
    )

    @model_validator(mode="after")
    def _concept_iff_element(self) -> ArchimateViewObjectIn:
        if self.kind == "element" and not self.concept_id:
            raise ValueError("concept_id is required for a view object of kind 'element'")
        if self.kind != "element" and self.concept_id is not None:
            raise ValueError(f"concept_id is not allowed on a view object of kind '{self.kind}'")
        if self.kind == "element" and self.documentation is not None:
            raise ValueError(
                "documentation is not allowed on an element occurrence; the concept carries it"
            )
        return self


class ArchimateViewConnectionIn(BaseModel):
    """A line on a view. ``relationship`` shows a relationship; ``line`` is Archi's
    plain visual connection (no semantics) between any two objects/connections."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=100)
    kind: ViewConnectionKind = "relationship"
    relationship_id: str | None = None
    source_object_id: str = Field(..., description="A view object or another connection")
    target_object_id: str = Field(..., description="A view object or another connection")
    label: str | None = Field(None, description="Label text / expression shown on the connection")
    bendpoints: list[Point] | None = None
    style: Style | None = None

    @model_validator(mode="after")
    def _relationship_iff_relationship(self) -> ArchimateViewConnectionIn:
        if self.kind == "relationship" and not self.relationship_id:
            raise ValueError("relationship_id is required for a connection of kind 'relationship'")
        if self.kind == "line" and self.relationship_id is not None:
            raise ValueError("relationship_id is not allowed on a connection of kind 'line'")
        return self


class ArchimateViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=100)
    name: str = Field("", max_length=300)
    viewpoint: str | None = Field(None, description="None = unfiltered view")
    viewpoint_ref: str | None = Field(
        None, max_length=100, description="id of one of the model's custom viewpoints"
    )
    objects: list[ArchimateViewObjectIn] = Field(default_factory=list)
    connections: list[ArchimateViewConnectionIn] = Field(default_factory=list)
    documentation: str | None = None
    properties: Properties | None = None


class ArchimateOrganization(BaseModel):
    """One folder of the model tree (Open Group ``organizations``; Archi folders).

    Presentation only: no validator reads it. ``items`` are concept, relationship
    or view ids in folder order; ``children`` are sub-folders.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(None, min_length=1, max_length=100)
    name: str | None = None
    documentation: str | None = None
    items: list[str] = Field(default_factory=list)
    children: list[ArchimateOrganization] = Field(default_factory=list)


class ArchimateViewpointDefinition(BaseModel):
    """A custom viewpoint (Open Group ``views/viewpoints/viewpoint``).

    ``xml`` is the definition as exchange XML (exclusive C14N, insignificant whitespace
    removed) without its ``identifier`` and ``properties``, which are ``id`` and
    ``properties`` here: name, documentation, concerns, purpose, content, allowed types
    and modeling notes are kept verbatim.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=100)
    xml: str = Field(..., min_length=1)
    properties: Properties | None = None


class ArchimateExchangeContent(BaseModel):
    """Model-level exchange content with no other snapshot field. Validators ignore it.

    ``metadata`` is the model's ``<metadata>`` element as exchange XML (exclusive C14N).
    """

    model_config = ConfigDict(extra="forbid")

    properties: Properties | None = None
    metadata: str | None = None
    viewpoints: list[ArchimateViewpointDefinition] = Field(default_factory=list)


class ArchimateSnapshot(BaseModel):
    """A whole model, or the part of it a caller wants checked.

    ``views=None`` means "views not supplied": the view-dependent checks
    (unused, viewpoint, nesting, empty views) report nothing and are not
    listed as enabled. ``views=[]`` means "this model has no views", and every
    element is then reported unused, exactly as Archi would.

    ``organizations`` is the folder tree; ``None`` means "not supplied" (an
    exchange export then writes Archi's default folders). ``exchange`` is model-level
    exchange content (properties, metadata, custom viewpoints). Validators ignore both.
    """

    model_config = ConfigDict(extra="forbid")

    concepts: list[ArchimateConceptIn] = Field(default_factory=list)
    relationships: list[ArchimateRelationshipIn] = Field(default_factory=list)
    views: list[ArchimateViewIn] | None = None
    organizations: list[ArchimateOrganization] | None = None
    exchange: ArchimateExchangeContent | None = None
