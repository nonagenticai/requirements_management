"""Schemas for the PM catalogue: project types, project phases, capabilities, card types."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.pm_catalogue import CATALOGUE_NAME_PATTERN

DefinitionOfDone = Literal["pr_merged", "pr_opened", "asset_promoted", "asset_persisted", "none"]
RecipeSize = Literal["S", "M", "L"]
ApproverRole = Literal["project_owner", "tenant_admin", "member", "super_admin"]

# ---------------------------------------------------------------------------
# Shared catalogue fields
# ---------------------------------------------------------------------------


class _CatalogueIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=120,
        pattern=CATALOGUE_NAME_PATTERN,
        description="Machine name, lower snake_case, unique per tenant (e.g. `bug_analysis`)",
    )
    title: str | None = Field(
        None, max_length=300, description="Display label; defaults to the name"
    )
    description: str | None = Field(None, description="What this entry means")

    @model_validator(mode="after")
    def _default_title(self):
        given = "title" in self.model_fields_set
        if not self.title:
            self.title = self.name
        if not given:
            # Defaulting must not count as "sent": on update an omitted title is unchanged.
            self.model_fields_set.discard("title")
        return self


class _CatalogueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="ID")
    name: str = Field(..., description="Machine name, unique per tenant")
    title: str = Field(..., description="Display label")
    description: str | None = Field(None, description="What this entry means")
    tenant_id: str = Field(..., description="Owning tenant")
    created_at: datetime | None = Field(None, description="Created")
    updated_at: datetime | None = Field(None, description="Updated")


# ---------------------------------------------------------------------------
# Project phases
# ---------------------------------------------------------------------------


_UNSET_NOTE = (
    " On create, null (or omitted) = not set: the default import fills it. On update"
    " (PUT), omitted = unchanged; an explicit null = not set (the import fills it); an"
    " explicit empty value = deliberately cleared: the import keeps it."
)
_OUT_NOTE = " null = not set; import-defaults will fill it."


class ProjectPhaseIn(_CatalogueIn):
    """Create or update a project phase."""

    output: str | None = Field(None, description="What the phase produces." + _UNSET_NOTE)
    exit_rule: str | None = Field(None, description="When the phase is done." + _UNSET_NOTE)


class ProjectPhaseOut(_CatalogueOut):
    """A project phase."""

    output: str | None = Field(None, description="What the phase produces." + _OUT_NOTE)
    exit_rule: str | None = Field(None, description="When the phase is done." + _OUT_NOTE)
    has_gate: bool = Field(
        False, description="Derived: a gate (`/pm/gates`) guards this phase's exit"
    )


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class CapabilityIn(_CatalogueIn):
    """Create or update a capability."""


class CapabilityOut(_CatalogueOut):
    """A capability."""


# ---------------------------------------------------------------------------
# Card types
# ---------------------------------------------------------------------------


class _CardTypeAttributesIn(BaseModel):
    # NULL (omitted) = not set, and the default import fills it; [] = deliberately cleared,
    # stored as-is and kept by the import. No default_factory here: that would turn
    # "omitted" into "cleared".
    inputs: list[str] | None = Field(None, description="Assets the card needs." + _UNSET_NOTE)
    outputs: list[str] | None = Field(None, description="Assets the card produces." + _UNSET_NOTE)
    acceptance: list[str] | None = Field(
        None, description="Acceptance statements for the card's result." + _UNSET_NOTE
    )
    rules: list[str] | None = Field(
        None, description="Free-form rules (e.g. R33/R34)." + _UNSET_NOTE
    )
    human_task: bool | None = Field(
        None,
        description="A human performs or decides this card; an explicit false is kept."
        + _UNSET_NOTE,
    )
    definition_of_done: DefinitionOfDone | None = Field(
        None, description="How the card's work counts as landed." + _UNSET_NOTE
    )


class _CardTypeAttributes(BaseModel):
    # NULL renders as null, never as [] / false: a client that GETs a row and PUTs it back
    # must send "not set", not "deliberately cleared".
    inputs: list[str] | None = Field(None, description="Assets the card needs." + _OUT_NOTE)
    outputs: list[str] | None = Field(None, description="Assets the card produces." + _OUT_NOTE)
    acceptance: list[str] | None = Field(
        None, description="Acceptance statements for the card's result." + _OUT_NOTE
    )
    rules: list[str] | None = Field(None, description="Free-form rules (e.g. R33/R34)." + _OUT_NOTE)
    human_task: bool | None = Field(
        None, description="A human performs or decides this card." + _OUT_NOTE
    )
    definition_of_done: DefinitionOfDone | None = Field(
        None, description="How the card's work counts as landed." + _OUT_NOTE
    )


class CardTypeIn(_CatalogueIn, _CardTypeAttributesIn):
    """Create or update a card type."""

    capability_id: UUID = Field(..., description="The capability this card type belongs to")


class CardTypeOut(_CatalogueOut, _CardTypeAttributes):
    """A card type. A not-set (NULL) attribute renders as null, so GET → PUT keeps it not set."""

    capability_id: UUID = Field(..., description="The capability this card type belongs to")


# ---------------------------------------------------------------------------
# Project types
# ---------------------------------------------------------------------------


class ProjectTypeIn(_CatalogueIn):
    """Create or update a project type. ``phase_ids`` is the ordered phase selection.

    On update a sent selection REPLACES the previous one; an omitted one is unchanged. Card
    types placed in a phase that is dropped from the selection are removed with it; phases that
    stay keep their card types.
    """

    phase_ids: list[UUID] = Field(
        default_factory=list,
        description="Selected project phases, in order. On update, omitted = unchanged",
    )

    @model_validator(mode="after")
    def _distinct_phases(self):
        if len(set(self.phase_ids)) != len(self.phase_ids):
            raise ValueError("phase_ids must not repeat a phase")
        return self


class ProjectTypePhaseOut(BaseModel):
    """A phase selected by a project type."""

    phase_id: UUID = Field(..., description="Project phase ID")
    name: str = Field(..., description="Phase machine name")
    title: str = Field(..., description="Phase display label")
    position: int = Field(..., description="0-based position in the project type")
    has_gate: bool = Field(False, description="Derived: a gate guards this phase's exit")


class ProjectTypeOut(_CatalogueOut):
    """A project type with its ordered phases."""

    phases: list[ProjectTypePhaseOut] = Field(
        default_factory=list, description="Selected phases, in order"
    )
    # Read-only (never on ProjectTypeIn): retired types are still listed, for history.
    retired_at: datetime | None = Field(
        None,
        description="When the type was retired. A retired type starts no new project; it is "
        "still listed and its projects keep reading",
    )


# ---------------------------------------------------------------------------
# Project type composition (card types per phase, grouped by capability)
# ---------------------------------------------------------------------------


class PhaseCardTypesIn(BaseModel):
    """The card types a project type uses in one of its phases."""

    model_config = ConfigDict(extra="forbid")

    phase_id: UUID = Field(..., description="A phase the project type has selected")
    card_type_ids: list[UUID] = Field(
        default_factory=list, description="Card types used in this phase"
    )
    entry_card_type_ids: list[UUID] = Field(
        default_factory=list,
        description="The card types (a subset of card_type_ids) the phase starts with. "
        "Omitted = unchanged: card types that stay keep their entry flag, new ones are not "
        "entries",
    )
    output: str | None = Field(
        None,
        description="Override of the phase's output for this project type (GET: "
        "`output_override`). Omitted = unchanged; null = no override",
    )
    exit_rule: str | None = Field(
        None,
        description="Override of the phase's exit rule for this project type (GET: "
        "`exit_rule_override`). Omitted = unchanged; null = no override",
    )


class ProjectTypeCardTypesIn(BaseModel):
    """Full replacement of a project type's card types per phase.

    Phases not listed end up with no card types; their overrides are unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    phases: list[PhaseCardTypesIn] = Field(
        default_factory=list, description="Card types per selected phase"
    )

    @model_validator(mode="after")
    def _distinct(self):
        ids = [p.phase_id for p in self.phases]
        if len(set(ids)) != len(ids):
            raise ValueError("a phase may appear only once")
        for p in self.phases:
            if len(set(p.card_type_ids)) != len(p.card_type_ids):
                raise ValueError(f"phase {p.phase_id}: card_type_ids must not repeat")
            if not set(p.entry_card_type_ids) <= set(p.card_type_ids):
                raise ValueError(
                    f"phase {p.phase_id}: entry_card_type_ids must be among card_type_ids"
                )
        return self


class CompositionCardTypeOut(BaseModel):
    """A card type within a capability within a phase."""

    card_type_id: UUID = Field(..., description="Card type ID")
    name: str = Field(..., description="Card type machine name")
    title: str = Field(..., description="Card type display label")
    is_entry: bool = Field(False, description="The phase starts with this card type")


class CompositionCapabilityOut(BaseModel):
    """A capability's card types within a phase."""

    capability_id: UUID = Field(..., description="Capability ID")
    name: str = Field(..., description="Capability machine name")
    title: str = Field(..., description="Capability display label")
    card_types: list[CompositionCardTypeOut] = Field(default_factory=list)


class CompositionPhaseOut(BaseModel):
    """One selected phase of a project type, with the capabilities and card types used in it."""

    phase_id: UUID = Field(..., description="Project phase ID")
    name: str = Field(..., description="Phase machine name")
    title: str = Field(..., description="Phase display label")
    position: int = Field(..., description="0-based position")
    output: str | None = Field(
        None, description="Effective output: the project type's override, else the phase's"
    )
    output_overridden: bool = Field(False, description="output comes from the override")
    output_override: str | None = Field(
        None,
        description="The project type's own output override (what PUT `output` sets); "
        "null = no override",
    )
    exit_rule: str | None = Field(
        None, description="Effective exit rule: the project type's override, else the phase's"
    )
    exit_rule_overridden: bool = Field(False, description="exit_rule comes from the override")
    exit_rule_override: str | None = Field(
        None,
        description="The project type's own exit-rule override (what PUT `exit_rule` sets); "
        "null = no override",
    )
    has_gate: bool = Field(False, description="Derived: a gate guards this phase's exit")
    capabilities: list[CompositionCapabilityOut] = Field(default_factory=list)


class ProjectTypeCardTypesOut(BaseModel):
    """The high-level picture of a project type: phase → capability → card types."""

    project_type_id: UUID = Field(..., description="Project type ID")
    name: str = Field(..., description="Project type machine name")
    title: str = Field(..., description="Project type display label")
    phases: list[CompositionPhaseOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Default catalogue import
# ---------------------------------------------------------------------------


class CatalogueImportCounts(BaseModel):
    """Per-catalogue counts of an import."""

    created: int = Field(..., description="Rows created by this import")
    existing: int = Field(..., description="Rows already present")
    filled: int = Field(
        0,
        description=(
            "Existing rows whose unset (NULL/empty) attribute columns this import filled; "
            "a set value is never overwritten"
        ),
    )


class CatalogueImportOut(BaseModel):
    """Result of importing the default catalogue into the caller's tenant."""

    project_phases: CatalogueImportCounts
    capabilities: CatalogueImportCounts
    card_types: CatalogueImportCounts
    project_types: CatalogueImportCounts
    recipes: CatalogueImportCounts
    wip_limits: CatalogueImportCounts
    priority_levels: CatalogueImportCounts
    deliverable_kinds: CatalogueImportCounts
    milestone_kinds: CatalogueImportCounts
    gates: CatalogueImportCounts
    board_templates: CatalogueImportCounts
    skipped_card_types: list[str] = Field(
        default_factory=list,
        description=(
            "Default card types a NEW project type would use but which the tenant no longer "
            "has (deleted after an earlier import); they were left out of its composition"
        ),
    )
    skipped_recipes: list[str] = Field(
        default_factory=list,
        description=(
            "Default recipes not created because the tenant's project type does not fit them "
            "(an item's card type is missing or not in its matrix, or the slot is taken)"
        ),
    )
    skipped_priority_levels: list[str] = Field(
        default_factory=list,
        description=(
            "Default priority levels not created because the tenant already uses their rank"
        ),
    )


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


class RecipeItemIn(BaseModel):
    """One line of a recipe."""

    model_config = ConfigDict(extra="forbid")

    card_type_id: UUID = Field(..., description="Card type to create")
    count_mode: Literal["fixed", "per"] = Field(
        "fixed", description="`fixed`: `count` cards; `per`: one card per item of `per_field`"
    )
    count: int | None = Field(None, ge=1, description="Number of cards (fixed mode)")
    per_field: str | None = Field(
        None,
        min_length=1,
        description="The list to create one card per item of, e.g. `bug_report.confirmed_bugs`",
    )
    cap: int | None = Field(None, ge=1, description="At most this many cards (per mode)")
    inputs: list[str] = Field(default_factory=list, description="Assets the card needs here")
    outputs: list[str] = Field(default_factory=list, description="Assets the card produces here")

    @model_validator(mode="after")
    def _mode_fields(self):
        if self.count_mode == "fixed":
            if self.count is None:
                self.count = 1
            if self.per_field is not None or self.cap is not None:
                raise ValueError("fixed items take count only (no per_field, no cap)")
        else:
            if self.per_field is None or self.count is not None:
                raise ValueError("per items take per_field (and optionally cap), not count")
        return self


class RecipeIn(_CatalogueIn):
    """Create or update a recipe. ``items`` is the full, ordered item list (PUT replaces it)."""

    project_type_id: UUID = Field(..., description="Project type the recipe belongs to")
    phase_id: UUID = Field(..., description="Phase the recipe creates cards in")
    size: RecipeSize | None = Field(None, description="S/M/L, or null for any size")
    items: list[RecipeItemIn] = Field(default_factory=list, description="Ordered recipe lines")


class RecipeItemOut(BaseModel):
    """One line of a recipe."""

    position: int = Field(..., description="0-based position")
    card_type_id: UUID = Field(..., description="Card type to create")
    card_type: str = Field(..., description="Card type machine name")
    count_mode: str = Field(..., description="`fixed` or `per`")
    count: int | None = Field(None, description="Number of cards (fixed mode)")
    per_field: str | None = Field(None, description="List to create one card per item of")
    cap: int | None = Field(None, description="At most this many cards (per mode)")
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)


class RecipeOut(_CatalogueOut):
    """A recipe with its ordered items."""

    project_type_id: UUID = Field(..., description="Project type ID")
    project_type: str = Field(..., description="Project type machine name")
    phase_id: UUID = Field(..., description="Phase ID")
    phase: str = Field(..., description="Phase machine name")
    size: str | None = Field(None, description="S/M/L, or null for any size")
    items: list[RecipeItemOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# WIP limits
# ---------------------------------------------------------------------------


class _WipScope(BaseModel):
    project_type_id: UUID | None = Field(None, description="Project type; null = any")
    phase_id: UUID | None = Field(None, description="Phase; null = any")
    capability_id: UUID | None = Field(None, description="Capability; null = any")
    card_type_id: UUID | None = Field(None, description="Card type; null = any")


class WipLimitIn(_WipScope):
    """Create or update a WIP limit. One per scope; every dimension null = any."""

    model_config = ConfigDict(extra="forbid")

    wip_limit: int = Field(..., ge=0, description="Cards that may be in progress at once")


class WipLimitOut(_WipScope):
    """A WIP limit."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="ID")
    wip_limit: int = Field(..., description="Cards that may be in progress at once")
    tenant_id: str = Field(..., description="Owning tenant")
    created_at: datetime | None = Field(None, description="Created")
    updated_at: datetime | None = Field(None, description="Updated")


class WipLimitResolveOut(WipLimitOut):
    """The WIP limit that applies to a card: the most specific matching row."""

    matched_on: list[str] = Field(
        default_factory=list,
        description="The row's non-null dimensions, i.e. what the query matched it on",
    )


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------


class _PriorityAttributes(BaseModel):
    rank: int = Field(..., ge=0, description="0 is the highest priority; unique per tenant")
    is_default: bool = Field(False, description="The priority a new card gets (one per tenant)")
    conveyor_may_set: bool = Field(
        True, description="The conveyor may set this level (false: humans only)"
    )


class PriorityLevelIn(_CatalogueIn, _PriorityAttributes):
    """Create or update a priority level."""


class PriorityLevelOut(_CatalogueOut, _PriorityAttributes):
    """A priority level."""


# ---------------------------------------------------------------------------
# Deliverable kinds
# ---------------------------------------------------------------------------


class _DeliverableAttributes(BaseModel):
    counts_for_phase_exit: bool = Field(
        True, description="A deliverable of this kind counts towards its phase's exit"
    )


class DeliverableKindIn(_CatalogueIn, _DeliverableAttributes):
    """Create or update a deliverable kind."""


class DeliverableKindOut(_CatalogueOut, _DeliverableAttributes):
    """A deliverable kind."""


# ---------------------------------------------------------------------------
# Milestone kinds
# ---------------------------------------------------------------------------


class _MilestoneAttributes(BaseModel):
    phase_id: UUID | None = Field(
        None, description="The phase whose exit this milestone marks; null = none"
    )


class MilestoneKindIn(_CatalogueIn, _MilestoneAttributes):
    """Create or update a milestone kind."""


class MilestoneKindOut(_CatalogueOut, _MilestoneAttributes):
    """A milestone kind."""


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class _GateAttributes(BaseModel):
    phase_id: UUID = Field(..., description="The phase whose exit the gate guards")
    approver_role: ApproverRole = Field(
        "project_owner", description="Who approves: project_owner, tenant_admin, member, ..."
    )
    quorum: int = Field(1, ge=1, description="Approvals needed")
    expires_after_hours: int | None = Field(
        None, gt=0, description="The request expires after this many hours; null = never"
    )


class GateIn(_CatalogueIn, _GateAttributes):
    """Create or update a gate."""


class GateOut(_CatalogueOut, _GateAttributes):
    """A gate."""


# ---------------------------------------------------------------------------
# Board templates
# ---------------------------------------------------------------------------


class BoardListSpec(BaseModel):
    """A list a board starts with."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=120, description="List name, e.g. `TODO`")
    position: int = Field(..., ge=0, description="0-based position on the board")
    closed: bool = Field(False, description="A terminal list (cards here are finished)")


class BoardLabelSpec(BaseModel):
    """A label a board starts with."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=120, description="Label name")
    color: str | None = Field(None, max_length=64, description="Label colour; null = any")


class CustomFieldGroupSpec(BaseModel):
    """A custom field group a board's cards start with."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=120, description="Group name")
    fields: list[str] = Field(default_factory=list, description="Field names, in order")

    @model_validator(mode="after")
    def _distinct_fields(self):
        if len(set(self.fields)) != len(self.fields):
            raise ValueError(f"group {self.name!r}: fields must not repeat")
        return self


def _distinct(items: list, attr: str, what: str) -> None:
    values = [getattr(i, attr) for i in items]
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must not repeat a {attr}")


class BoardTemplateIn(_CatalogueIn):
    """Create or update a board template."""

    project_type_id: UUID | None = Field(
        None, description="The project type this template is for; null = any"
    )
    lists: list[BoardListSpec] | None = Field(None, description="Lists, in order." + _UNSET_NOTE)
    labels: list[BoardLabelSpec] | None = Field(None, description="Labels." + _UNSET_NOTE)
    custom_field_groups: list[CustomFieldGroupSpec] | None = Field(
        None, description="Custom field groups, in order." + _UNSET_NOTE
    )

    @model_validator(mode="after")
    def _distinct_names(self):
        _distinct(self.lists or [], "name", "lists")
        _distinct(self.lists or [], "position", "lists")
        _distinct(self.labels or [], "name", "labels")
        _distinct(self.custom_field_groups or [], "name", "custom_field_groups")
        return self


class BoardTemplateOut(_CatalogueOut):
    """A board template. A not-set (NULL) list renders as null, so GET → PUT keeps it not set."""

    project_type_id: UUID | None = Field(
        None, description="The project type this template is for; null = any"
    )
    lists: list[BoardListSpec] | None = Field(None, description="Lists, in order." + _OUT_NOTE)
    labels: list[BoardLabelSpec] | None = Field(None, description="Labels." + _OUT_NOTE)
    custom_field_groups: list[CustomFieldGroupSpec] | None = Field(
        None, description="Custom field groups, in order." + _OUT_NOTE
    )
