"""PM (Project Management) Pydantic schemas — In/Out for all PM entities."""

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import PydanticCustomError

from schemas.common import (
    OriginFieldsInMixin,
    OriginFieldsOutMixin,
    StableKey,
    SystemInheritanceOutMixin,
    TimestampMixin,
    VersionFieldsIn,
    VersionFieldsMixin,
)
from schemas.currency import Iso4217Code
from schemas.pm_intake import (
    MAX_LIST,
    BoundAssets,
    BriefAuthorKind,
    CoordinationMode,
    ExpectedSize,
    ItInputs,
    LifecycleClass,
    ProjectAttachments,
    ProjectBrief,
    ProjectRepository,
    ProjectShape,
    QuestionMode,
    ScopeLevel,
)
from schemas.registers import RegisterFieldsIn, RegisterFieldsOut

# ---------------------------------------------------------------------------
# Controlled vocabularies as types — the REQUEST side
# ---------------------------------------------------------------------------
# Each alias below mirrors, one-for-one, a ``frozenset`` in ``models.pm``. Those
# frozensets stay the runtime source of truth (the service layer and the DB
# CHECK-constraint story read them); these aliases are how the same vocabulary
# reaches the wire.
#
# Why a ``Literal`` and not the ``field_validator`` that used to live on each
# field: a validator returns a correct 422 but emits NO ``enum`` into the
# generated OpenAPI, so every generated client — and the n8n node UI — renders a
# free-text box for a field the API is guaranteed to reject. A ``Literal`` puts a
# top-level ``enum`` on the property, which is what turns the box into a
# dropdown.
#
# ⛔ Never spell one of these as ``XStatus | None`` on a non-nullable field.
# Pydantic emits an optional Literal as ``anyOf: [{enum: [...]}, {"type":
# "null"}]``; the downstream n8n generator reads ``enum`` only at the TOP level
# of a property (``generate-node.ts:843``), so a nested enum is invisible and the
# field silently degrades back to free text. Every field converted here is
# non-nullable, so every one is a bare ``Literal`` with a default or a required
# ``...``.
#
# ⚠️ Duplication is deliberate but must not drift: ``tests/test_pm_vocabularies.py``
# asserts set-equality between ``typing.get_args`` of each alias and its
# ``models.pm`` frozenset. Add a value in one place only and that test goes red.
#
# ⛔ These aliases are for ``*In`` models ONLY. The matching ``*Out`` fields stay
# plain ``str``: they carry no validator today, so narrowing them would be a NEW
# constraint on rows already in the database and one legacy value would 500 the
# read path. Same rule, and same reasoning, as ``VersionFieldsIn`` vs
# ``VersionFieldsMixin`` in ``schemas.common``.

ProgrammeStatus = Literal["closing", "defining", "identifying", "managing_tranches"]
ProjectStatus = Literal["closed", "closing", "executing", "initiating", "monitoring", "planning"]
ResourceType = Literal["equipment", "material", "person", "team"]
RiskStatus = Literal["assessed", "closed", "identified", "mitigated", "occurred"]
RiskCategory = Literal["cost", "external", "resource", "schedule", "scope", "technical"]
RiskResponseStrategy = Literal[
    "accept", "avoid", "enhance", "exploit", "mitigate", "share", "transfer"
]
BenefitStatus = Literal["emerging", "expected", "realized", "superseded"]
BenefitType = Literal["dis_benefit", "financial", "non_financial"]


def _normalise_vocab(v: object) -> object:
    """Lower-case and strip a vocabulary value before the ``Literal`` sees it.

    This is EXACTLY what the ``field_validator``s these ``Literal``s replaced
    did (``v.strip().lower()``), lifted into one helper so every call site is
    provably the same transform. Without it a bare ``Literal`` would newly
    reject ``"Active"`` / ``" active "`` — input the API accepts and coerces
    today — which is a real regression, not a tightening.

    Non-``str`` input passes through untouched so the ``Literal`` produces the
    type error rather than this helper raising ``AttributeError``.
    """
    if isinstance(v, str):
        return v.strip().lower()
    return v


# ---------------------------------------------------------------------------
# Programme
# ---------------------------------------------------------------------------


class ProgrammeIn(VersionFieldsIn):
    """Schema for creating or updating a programme."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Programme name")
    description: str | None = Field(None, description="Programme description")
    # Census: pm_programmes.status holds 2 distinct in-vocabulary values, zero
    # strays — freezing the set to the Literal cannot reject existing data.
    status: ProgrammeStatus = Field(default="identifying", description="Programme lifecycle status")
    start_date: datetime | None = Field(None, description="Planned start date")
    end_date: datetime | None = Field(None, description="Planned end date")
    blueprint_data: dict[str, Any] | None = Field(
        default_factory=dict, description="Programme blueprint data"
    )

    @field_validator("status", mode="before")
    @classmethod
    def normalise_vocab_fields(cls, v: object) -> object:
        """Preserve the ``.strip().lower()`` the replaced validator performed."""
        return _normalise_vocab(v)


class ProgrammeOut(SystemInheritanceOutMixin, VersionFieldsMixin, TimestampMixin):
    """Schema for returning a programme from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Programme ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Programme name")
    description: str | None = Field(None, description="Programme description")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    status: str = Field(
        default="identifying", max_length=20, description="Programme lifecycle status"
    )
    start_date: datetime | None = Field(None, description="Planned start date")
    end_date: datetime | None = Field(None, description="Planned end date")
    blueprint_data: dict[str, Any] | None = Field(None, description="Programme blueprint data")


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


# A project's rework limit when ``pm_projects.rework_limit`` is NULL (George 2026-10-13).
DEFAULT_REWORK_LIMIT = 2


class ProjectIn(VersionFieldsIn, OriginFieldsInMixin):
    """Schema for creating or updating a project."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Project name")
    description: str | None = Field(None, description="Project description")
    # The creation identity (POST's get-or-create key): set at create, IMMUTABLE afterwards.
    origin_kind: str = Field(
        default="tenant",
        pattern="^(tenant|project|workflow)$",
        description=(
            "Provenance: tenant | project | workflow. Set at create and IMMUTABLE afterwards: "
            "on PUT, omitted = kept; the stored value is accepted (no-op); any other "
            "value is refused (409 ``origin_immutable``). A get-or-create re-POST keeps "
            "the stored value."
        ),
    )
    origin_project_id: UUID | None = Field(
        default=None,
        description=(
            "Required when origin_kind='project'. Set at create and IMMUTABLE afterwards: "
            "on PUT, omitted = kept; the stored value is accepted (no-op); any other "
            "value, including null when one is stored, is refused (409 "
            "``origin_immutable``). A get-or-create re-POST keeps the stored value."
        ),
    )
    origin_workflow_run_id: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Required when origin_kind='workflow' (n8n run id). Set at create and IMMUTABLE "
            "afterwards: on PUT, omitted = kept; the stored value is accepted (no-op); "
            "any other value, including null when one is stored, is refused (409 "
            "``origin_immutable``). A get-or-create re-POST keeps the stored value (a "
            "differing run id is ignored)."
        ),
    )
    programme_id: UUID | None = Field(None, description="Parent programme ID (UUID FK)")
    tranche: str | None = Field(
        None, max_length=50, description="Tranche label within the programme"
    )
    # Census: pm_projects.status holds 2 distinct in-vocabulary values, zero strays.
    status: ProjectStatus = Field(default="initiating", description="Project lifecycle status")
    start_date: datetime | None = Field(None, description="Planned start date")
    end_date: datetime | None = Field(None, description="Planned end date")
    budget: float | None = Field(None, description="Total budget")
    currency: str | None = Field(None, max_length=10, description="Currency code (e.g. EUR, USD)")
    budget_currency: Iso4217Code | None = Field(
        None,
        description=(
            "ISO 4217 code of ``budget`` (e.g. EUR). Reporting only (R-CK3): nothing enforces "
            "the budget. null = not set; any case accepted, stored upper-case"
        ),
    )
    project_type_id: UUID | None = Field(
        None,
        description=(
            "The tenant's project type (``/pm/project-types``). REQUIRED on create (422 "
            "``project_type_required``): the project starts in the type's ``setup`` phase "
            "(409 ``project_type_missing_setup_phase`` if it has none). On update, omitted = "
            "unchanged; a change that would leave the current phase outside the new type "
            "is refused (409), never silently cleared."
        ),
    )
    wip_limit: int | None = Field(
        None,
        ge=0,
        description=(
            "The project's own WIP limit: how many of its cards may be in DOING at once (the "
            "project TOTAL). null = not set on the project, the tenant's catalogue policy "
            "applies (``/pm/wip-limits``). 0 = no card may be in DOING (the project is "
            "paused). On update, omitted = unchanged; an explicit null clears it."
        ),
    )
    rework_limit: int | None = Field(
        None,
        ge=0,
        description=(
            "How many times a card of this project may go back for rework. null = the "
            f"default ({DEFAULT_REWORK_LIMIT}). 0 = no rework. On update, omitted = "
            "unchanged; an explicit null clears it (back to the default)."
        ),
    )

    # ---- The fr project intake (migration 2026_10_13_0056; ``schemas.pm_intake``) ----
    # On update every intake field follows the same rule: omitted = the stored value is
    # kept; an explicit null clears it. Any change stamps ``intake_updated_at``.
    # fr's ``deadline`` / ``budget`` strings live in ``brief`` as typed; mapping them onto
    # ``end_date`` / ``budget`` above is fr's job (no duplicate columns here).
    repositories: Annotated[list[ProjectRepository], Field(max_length=MAX_LIST)] | None = Field(
        None,
        description=(
            "The project's repositories, primary first by convention; at most one has "
            "``primary: true``. fr maps ``repo.primary`` / ``repo.additional[]`` (or the "
            "legacy ``repository_url`` + ``branch``) onto this list."
        ),
    )
    repository_id: UUID | None = Field(None, description="fr's repositories row id, if any")
    coordination_mode: CoordinationMode | None = Field(
        None, description="How the project's repositories relate (fr ``coordination_mode``)"
    )
    scope_level: ScopeLevel | None = Field(None, description="Granularity (fr ``scope_level``)")
    shape: ProjectShape | None = Field(
        None,
        description=(
            "fr ``shape``. Immutable once set: an update that changes or clears it is 409."
        ),
    )
    priority_level_id: UUID | None = Field(
        None, description="One of the tenant's ``/pm/priority-levels`` (else 422)"
    )
    priority: str | None = Field(
        None,
        exclude=True,
        max_length=100,
        description=(
            "Write-only alternative to ``priority_level_id``: the NAME of one of the "
            "tenant's priority levels, case-insensitive (fr's ``P0``..``P3`` → ``p0``..). "
            "Unknown → 422. Giving both with different levels → 422."
        ),
    )
    expected_size: ExpectedSize | None = Field(None, description="Expected size: S, M or L")
    requested_by: str | None = Field(
        None, max_length=255, description="Who asked for the project (fr ``requestor``)"
    )
    github_token_secret_id: UUID | None = Field(
        None,
        description=(
            "The secrets-service id of the GitHub token. Never the token itself: a body "
            "carrying ``github_access_token`` is refused (422)."
        ),
    )
    question_mode: QuestionMode | None = Field(None, description="fr ``question_mode``")
    lifecycle_class: LifecycleClass | None = Field(None, description="fr ``lifecycle_class``")
    fixture: bool | None = Field(None, description="fr ``fixture`` (self-declared test fixture)")
    it_inputs: ItInputs | None = Field(
        None, description="fr's IT-project inputs; replaced whole when sent"
    )
    attachments: ProjectAttachments | None = Field(
        None, description="Secret and document ids the user attached (ids only)"
    )
    bound_assets: BoundAssets | None = Field(
        None, description="Library assets the user picked, per asset kind (fr ``bound_assets``)"
    )
    brief: ProjectBrief | None = Field(
        None,
        exclude=True,
        description=(
            "The FULL brief. REQUIRED when a project is created (422 ``brief_required``): it "
            "becomes version 1 and sets its ``constraints`` freely. v1 is written at create; it "
            "is ``user`` when fr creates the project on a user's behalf (delegated), "
            "otherwise ``conveyor``. On "
            "update it is appended as the next version — 409 ``brief_missing_v1`` if the "
            "project has no version 1 (a legacy row) — (no compare-and-set — use "
            "``POST /pm/projects/{eid}/brief/versions`` for that) and its ``constraints`` must "
            "equal the current version's (422 ``brief_constraints_immutable``). Omitted = "
            "unchanged. A brief cannot be cleared: an explicit null is 422. Re-sending an "
            "identical brief still appends a version."
        ),
    )
    # Set by the router from the caller's principal (``auth.author_kind``: ``user`` iff
    # fr-delegated, else ``conveyor``); never a body field. Private, so ``model_copy``
    # carries it through ``ProjectService``.
    _author_kind: Any = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def _refuse_raw_token(cls, data: object) -> object:
        if isinstance(data, dict) and "github_access_token" in data:
            # A fixed type tag: the 422 envelope shows ``loc:type`` only (never ``msg``),
            # so the tag itself tells the caller what to send instead.
            raise PydanticCustomError(
                "github_access_token_refused_send_github_token_secret_id",
                "github_access_token is refused: RM never stores a raw token. Store it in "
                "the secrets service and send its id as github_token_secret_id",
            )
        return data

    @field_validator("priority", mode="before")
    @classmethod
    def _priority_name(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("repositories")
    @classmethod
    def _one_primary(cls, v: list[ProjectRepository] | None) -> list[ProjectRepository] | None:
        if v is not None and sum(r.primary for r in v) > 1:
            raise PydanticCustomError(
                "at_most_one_primary_repository", "at most one repository may be primary"
            )
        return v

    @field_serializer("repositories", "it_inputs", "attachments", "bound_assets")
    def _stored_json(self, v: Any) -> Any:
        """The stored form drops null keys (absent and null mean the same thing)."""
        if v is None:
            return None
        if isinstance(v, list):
            return [r.model_dump(mode="json", exclude_none=True) for r in v]
        return v.model_dump(mode="json", exclude_none=True)

    @model_validator(mode="after")
    def _brief_not_null(self) -> "ProjectIn":
        if "brief" in self.model_fields_set and self.brief is None:
            raise PydanticCustomError(
                "brief_cannot_be_cleared",
                "brief cannot be cleared; its versions are append-only (omit it to keep it)",
            )
        return self

    @model_validator(mode="after")
    def _origin_project_only_for_project_kind(self) -> "ProjectIn":
        # Stricter than the shared mixin: a project's ``origin_project_id`` IS its fr project
        # (R345, CHECK ``ck_pm_projects_origin_project``), so only ``origin_kind='project'``
        # carries one — a ``workflow`` row naming one would be a 500 at the DB, not a 422.
        if self.origin_project_id is not None and self.origin_kind != "project":
            raise PydanticCustomError(
                "origin_project_id_requires_origin_kind_project",
                "origin_project_id is set only with origin_kind='project'",
            )
        return self

    @field_validator("status", mode="before")
    @classmethod
    def normalise_vocab_fields(cls, v: object) -> object:
        """Preserve the ``.strip().lower()`` the replaced validator performed."""
        return _normalise_vocab(v)


# The intake columns on ``pm_projects`` that ``ProjectIn`` writes directly (omitted on
# update = kept). ``brief`` and ``priority`` are handled by ``ProjectService``.
INTAKE_COLUMNS: tuple[str, ...] = (
    "repositories",
    "repository_id",
    "coordination_mode",
    "scope_level",
    "shape",
    "priority_level_id",
    "expected_size",
    "requested_by",
    "github_token_secret_id",
    "question_mode",
    "lifecycle_class",
    "fixture",
    "it_inputs",
    "attachments",
    "bound_assets",
)


class ProjectOut(
    SystemInheritanceOutMixin,
    VersionFieldsMixin,
    OriginFieldsOutMixin,
    TimestampMixin,
):
    """Schema for returning a project from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Project ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Project name")
    description: str | None = Field(None, description="Project description")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    programme_id: UUID | None = Field(None, description="Parent programme ID (UUID FK)")
    tranche: str | None = Field(None, max_length=50, description="Tranche label")
    status: str = Field(default="initiating", max_length=20, description="Project lifecycle status")
    start_date: datetime | None = Field(None, description="Planned start date")
    end_date: datetime | None = Field(None, description="Planned end date")
    budget: float | None = Field(None, description="Total budget")
    currency: str | None = Field(None, max_length=10, description="Currency code")
    budget_currency: str | None = Field(None, description="ISO 4217 code of ``budget``")
    project_type_id: UUID | None = Field(None, description="The project's type, if set")
    current_phase_id: UUID | None = Field(None, description="The project's current phase id")
    current_phase: str | None = Field(None, description="The current phase's machine name")
    phase_updated_at: datetime | None = Field(
        None, description="When the current phase last really changed"
    )
    wip_limit: int | None = Field(
        None,
        description=(
            "The project's own WIP limit as stored (project TOTAL in DOING); null = not set. "
            "0 = no card may be in DOING (paused)"
        ),
    )
    effective_wip_limit: int | None = Field(
        None,
        description=(
            "The limit that applies: ``wip_limit`` if set, else the tenant's catalogue "
            "project-total row for the project's type (else the tenant-global total); null "
            "= no limit"
        ),
    )
    wip_limit_source: Literal["project", "catalogue", "none"] = Field(
        "none", description="Where ``effective_wip_limit`` came from"
    )
    rework_limit: int | None = Field(
        None, description="The project's own rework limit as stored; null = not set"
    )
    effective_rework_limit: int = Field(
        DEFAULT_REWORK_LIMIT,
        description=(
            f"The rework limit that applies: ``rework_limit`` if set, else the default "
            f"({DEFAULT_REWORK_LIMIT}). 0 = no rework"
        ),
    )

    # ---- The fr project intake ----
    repositories: list[ProjectRepository] | None = None
    repository_id: UUID | None = None
    coordination_mode: str | None = None
    scope_level: str | None = None
    shape: str | None = None
    priority_level_id: UUID | None = None
    priority: str | None = Field(None, description="The priority level's name (``p0``..)")
    expected_size: str | None = None
    requested_by: str | None = None
    github_token_secret_id: UUID | None = None
    question_mode: str | None = None
    lifecycle_class: str | None = None
    fixture: bool | None = None
    it_inputs: ItInputs | None = None
    attachments: ProjectAttachments | None = None
    bound_assets: BoundAssets | None = None
    brief: ProjectBrief | None = Field(None, description="The current brief version's snapshot")
    brief_version: int | None = Field(
        None,
        validation_alias=AliasChoices("brief_version", "current_brief_version"),
        description="The current brief version (``/brief/versions``); null = no brief",
    )
    brief_author_kind: BriefAuthorKind | None = Field(
        None, description="Who wrote the current brief version"
    )
    brief_updated_at: datetime | None = Field(
        None, description="When the current brief version was created"
    )
    intake_updated_at: datetime | None = Field(
        None, description="When any intake field (or the brief) last changed"
    )

    # ---- The Planka board link (``PUT /pm/projects/{eid}/planka-board``; v4 R73 / D20) ----
    planka_board_id: str | None = Field(None, description="The project's Planka board")
    planka_project_id: str | None = Field(None, description="The Planka project of the board")
    planka_list_ids: dict[str, str] | None = Field(
        None, description="Conveyor list (TODO, READY, DOING, QA, DONE, BLOCKED) → Planka list id"
    )
    planka_linked_at: datetime | None = Field(None, description="When the board link last changed")

    @model_validator(mode="after")
    def _effective_rework(self) -> "ProjectOut":
        self.effective_rework_limit = (
            DEFAULT_REWORK_LIMIT if self.rework_limit is None else self.rework_limit
        )
        return self


class ScopedWipLimit(BaseModel):
    """A catalogue row more specific than the project total (phase, capability, card type)."""

    id: UUID = Field(..., description="The ``pm_wip_limits`` row")
    wip_limit: int = Field(..., description="Cards of that scope that may be in DOING")
    matched_on: list[str] = Field(..., description="The row's non-null dimensions")


class ProjectWipLimitOut(BaseModel):
    """``GET /pm/projects/{eid}/wip-limit``: the check made before pulling a card into DOING."""

    project_id: UUID
    wip_limit: int | None = Field(None, description="The project's own value; null = not set")
    effective_wip_limit: int | None = Field(
        None, description="The project TOTAL that applies; null = no limit; 0 = paused"
    )
    source: Literal["project", "catalogue", "none"]
    catalogue_limit_id: UUID | None = Field(
        None, description="The catalogue row behind the total when ``source`` is catalogue"
    )
    scoped: ScopedWipLimit | None = Field(
        None,
        description=(
            "When phase/capability/card type were given and a more specific catalogue row "
            "matches: that row. It caps that scope; the project total still applies"
        ),
    )


class ProjectPhaseSetIn(BaseModel):
    """Body of ``PUT /pm/projects/{eid}/phase``: exactly one of ``phase_id`` / ``phase``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    phase_id: UUID | None = Field(None, description="Target phase id")
    phase: str | None = Field(None, min_length=1, max_length=120, description="Target phase name")
    if_current_phase: UUID | str | None = Field(
        None,
        description=(
            "Compare-and-set: the phase (id or name) the caller expects the project to be "
            "in. Omitted = no check; explicit null = expect no phase. A mismatch is 409."
        ),
    )
    restart: bool = Field(
        False,
        description=(
            "Start a new visit even when the target is the current phase: bumps "
            "``phase_updated_at`` (per-visit coach caps start at zero) and writes a phase "
            "event with from = to. False = a same-phase re-stamp is a no-op."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "ProjectPhaseSetIn":
        if (self.phase_id is None) == (self.phase is None):
            raise ValueError("exactly one of phase_id or phase is required")
        return self


class PhaseRef(BaseModel):
    """A phase by id and machine name."""

    id: UUID
    name: str


class ProjectPhaseSetOut(BaseModel):
    """Result of ``PUT /pm/projects/{eid}/phase``."""

    project_id: UUID
    phase: PhaseRef
    previous_phase: PhaseRef | None = None
    phase_updated_at: datetime | None = None
    changed: bool = Field(
        ...,
        description="False when the project was already in the phase and ``restart`` was not set",
    )


class ProjectPhaseEventOut(BaseModel):
    """One real change of a project's phase, a restart of it (``kind='phase'``,
    from = to), or its closure."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    from_phase_id: UUID | None = None
    from_phase: str | None = Field(None, description="Name, if the phase still exists")
    to_phase_id: UUID
    to_phase: str | None = Field(None, description="Name, if the phase still exists")
    at: datetime
    subject_id: str
    kind: Literal["phase", "closure"] = Field(
        "phase",
        description=(
            "``phase`` = a phase change; ``closure`` = the project's status became ``closed`` "
            "(from = to = the phase it closed in)"
        ),
    )


# ---------------------------------------------------------------------------
# Project capabilities (R260 v1): the PM's per-project capability selection
# ---------------------------------------------------------------------------

# Every project has these two; they are never selected (422 ``implicit_capability``).
IMPLICIT_CAPABILITIES: tuple[str, ...] = ("project_management", "kanban_coach")


# R302 (George 2026-10-17): the PM's per-capability consult decision. Mirrors
# ``models.pm.CAPABILITY_INVOLVEMENTS`` / ``CAPABILITY_LEVELS``.
CapabilityInvolvement = Literal["none", "light", "standard", "heavy"]
CapabilityLevel = Literal["low", "medium", "high"]

CapabilityName = Annotated[str, Field(min_length=1, max_length=120)]

# Bounds on the R302 free-form fields (serialised ``consult`` JSON, in characters).
MAX_CAPABILITY_PURPOSE = 2000
MAX_CAPABILITY_CONSULT = 16_000
MAX_CAPABILITY_ORDER_AFTER = 50


class ProjectCapabilityIn(BaseModel):
    """One capability the PM selects for a project, why, and (R302) how it is involved.

    The R302 fields are optional: a name + reason body stays valid. ``PUT`` is a replace-set,
    so an omitted field is stored as null (``order_after`` as ``[]``).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=120, description="Capability machine name")
    reason: str = Field(..., min_length=1, description="Why the project needs it (non-empty)")
    # ``X | SkipJsonSchema[None]``: optional, yet the OpenAPI keeps ``enum`` at the top level
    # of the property (the n8n generator reads it only there; see the vocabulary note above).
    involvement: CapabilityInvolvement | SkipJsonSchema[None] = Field(
        None, description="How much the capability is involved (R302)"
    )
    purpose: str | SkipJsonSchema[None] = Field(
        None,
        min_length=1,
        max_length=MAX_CAPABILITY_PURPOSE,
        description="What the capability is involved for (R302)",
    )
    rigor: CapabilityLevel | SkipJsonSchema[None] = Field(
        None, description="How rigorous its work must be (R302)"
    )
    complexity: CapabilityLevel | SkipJsonSchema[None] = Field(
        None, description="How complex its part of the project is (R302)"
    )
    consult: dict[str, Any] | SkipJsonSchema[None] = Field(
        None,
        description=(
            "The raw ``cap.<c>.consult`` answer (likely_card_types, operation_order "
            f"{{needs, feeds}}, questions …); at most {MAX_CAPABILITY_CONSULT} characters "
            "as JSON (R302)"
        ),
    )
    order_after: list[CapabilityName] = Field(
        default_factory=list,
        max_length=MAX_CAPABILITY_ORDER_AFTER,
        description=(
            "Selected capability names whose work must precede this one: the PM's operation "
            "order (R302). Distinct, never the capability itself, each one selected in the "
            "same request."
        ),
    )

    @field_validator("involvement", "rigor", "complexity", mode="before")
    @classmethod
    def _normalise(cls, v: object) -> object:
        return _normalise_vocab(v)

    @field_validator("consult")
    @classmethod
    def _consult_bounded(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None and len(json.dumps(v, default=str)) > MAX_CAPABILITY_CONSULT:
            raise ValueError(f"consult exceeds {MAX_CAPABILITY_CONSULT} characters as JSON")
        return v

    @model_validator(mode="after")
    def _order_after_valid(self) -> "ProjectCapabilityIn":
        if len(set(self.order_after)) != len(self.order_after):
            raise ValueError("order_after names must be distinct")
        if self.name in self.order_after:
            raise ValueError("order_after must not name the capability itself")
        return self


class ProjectCapabilitiesSetIn(BaseModel):
    """Body of ``PUT /pm/projects/{eid}/capabilities``: the FULL selection (replace-set)."""

    model_config = ConfigDict(extra="forbid")

    capabilities: list[ProjectCapabilityIn] = Field(
        ..., min_length=1, description="The selected capabilities (non-empty, distinct names)"
    )
    approval_id: UUID | None = Field(
        None, description="The fr sign-off that approved this selection (stored on added rows)"
    )
    if_current: list[str] | None = Field(
        None,
        description=(
            "Compare-and-set: the capability names the caller expects to be selected now "
            "(order-insensitive; [] = none yet). Omitted/null = no check; a mismatch is 409 "
            "``if_current_mismatch``."
        ),
    )

    @model_validator(mode="after")
    def _distinct_names(self) -> "ProjectCapabilitiesSetIn":
        names = [c.name for c in self.capabilities]
        if len(set(names)) != len(names):
            raise ValueError("capability names must be distinct")
        stray = sorted({a for c in self.capabilities for a in c.order_after} - set(names))
        if stray:
            raise ValueError(f"order_after names {stray} are not selected in this request")
        return self


class SelectedCapabilityOut(BaseModel):
    """A capability selected for the project."""

    name: str
    capability_id: UUID
    reason: str
    # R302 — plain ``str`` on the read side (see the vocabulary note above).
    involvement: str | None = None
    purpose: str | None = None
    rigor: str | None = None
    complexity: str | None = None
    consult: dict[str, Any] | None = None
    order_after: list[str] = Field(default_factory=list)
    approval_id: UUID | None = None
    selected_at: datetime


class SelectableCapabilityOut(BaseModel):
    """A capability the PM may select: it has a card type in the project type's composition."""

    name: str
    purpose: str | None = Field(None, description="The capability's catalogue description")
    phases: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Phase name (in the type's order) → this capability's card type names there",
    )


class ProjectCapabilitiesOut(BaseModel):
    """``GET /pm/projects/{eid}/capabilities``."""

    project_id: UUID
    project_type: str | None = Field(None, description="The project type's machine name")
    selected: list[SelectedCapabilityOut] = Field(default_factory=list)
    implicit: list[str] = Field(
        default_factory=lambda: list(IMPLICIT_CAPABILITIES),
        description="Capabilities every project has; never selected",
    )
    selectable: list[SelectableCapabilityOut] = Field(default_factory=list)


class ProjectCapabilitiesSetOut(ProjectCapabilitiesOut):
    """Result of ``PUT /pm/projects/{eid}/capabilities``."""

    changed: bool = Field(..., description="False when the selected NAME set did not change")


class ProjectCapabilityEventOut(BaseModel):
    """One real change of a project's selected capability names."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    added: list[str]
    removed: list[str]
    approval_id: UUID | None = None
    subject_id: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------


class ResourceIn(VersionFieldsIn):
    """Schema for creating or updating a resource."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Resource name")
    description: str | None = Field(None, description="Resource description")
    # Census: pm_resources is EMPTY, so this vocabulary is unexercised rather
    # than proven. Freezing it cannot break existing rows because there are none.
    resource_type: ResourceType = Field(..., description="Resource type")
    capacity_per_day: float = Field(default=8.0, gt=0, description="Available hours per day")
    cost_per_unit: float | None = Field(None, ge=0, description="Cost per unit (hour/item)")
    calendar_data: dict[str, Any] | None = Field(
        default_factory=dict, description="Working calendar overrides"
    )
    skills: list[str] | None = Field(default_factory=list, description="Skill tags")
    # Deterministic AA-intake dedup key (cardId + kind + ordinal). A resource
    # title is legitimately non-unique (many "Senior Developer"), so title-keyed
    # dedup would false-collide; the stable_key is the only safe dedup handle.
    # Empty/whitespace coerces to null (see StableKey). Resources have no
    # project_id, so dedup keys on (tenant_id, stable_key).
    stable_key: StableKey = Field(default=None, max_length=200, description="AA dedup key")

    @field_validator("resource_type", mode="before")
    @classmethod
    def normalise_vocab_fields(cls, v: object) -> object:
        """Preserve the ``.strip().lower()`` the replaced validator performed."""
        return _normalise_vocab(v)


class ResourceOut(SystemInheritanceOutMixin, VersionFieldsMixin, TimestampMixin):
    """Schema for returning a resource from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Resource ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Resource name")
    description: str | None = Field(None, description="Resource description")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    resource_type: str = Field(..., max_length=20, description="Resource type")
    capacity_per_day: float = Field(default=8.0, gt=0, description="Available hours per day")
    cost_per_unit: float | None = Field(None, ge=0, description="Cost per unit")
    calendar_data: dict[str, Any] | None = Field(None, description="Working calendar overrides")
    skills: list[str] | None = Field(None, description="Skill tags")
    stable_key: str | None = Field(None, description="AA-intake dedup key")


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


class RiskIn(VersionFieldsIn, RegisterFieldsIn):
    """Schema for creating or updating a risk."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Risk title")
    description: str | None = Field(None, description="Risk description")
    project_id: str = Field(
        ..., min_length=1, max_length=100, description="Parent project slug or ID"
    )
    probability: float = Field(..., ge=0, le=1, description="Probability (0.0 to 1.0)")
    impact_score: float = Field(..., ge=1, le=5, description="Impact score (1 to 5)")
    risk_score: float | None = Field(
        None, description="Computed risk score (probability x impact_score)"
    )
    # Census: pm_risks is EMPTY — all three vocabularies below are unexercised
    # rather than proven, so freezing them cannot reject any existing row.
    category: RiskCategory = Field(..., description="Risk category")
    response_strategy: RiskResponseStrategy = Field(..., description="Risk response strategy")
    response_plan: str | None = Field(None, description="Detailed response plan")
    status: RiskStatus = Field(default="identified", description="Risk status")
    owner: str | None = Field(None, max_length=100, description="Risk owner")
    due_at: datetime | None = Field(None, description="When the risk response is due")
    last_reviewed_at: datetime | None = Field(None, description="When the risk was last reviewed")

    @field_validator("status", "category", "response_strategy", mode="before")
    @classmethod
    def normalise_vocab_fields(cls, v: object) -> object:
        """Preserve the ``.strip().lower()`` all three replaced validators performed."""
        return _normalise_vocab(v)

    @model_validator(mode="after")
    def compute_risk_score(self) -> "RiskIn":
        if self.risk_score is None:
            self.risk_score = self.probability * self.impact_score
        return self


class RiskOut(SystemInheritanceOutMixin, VersionFieldsMixin, RegisterFieldsOut, TimestampMixin):
    """Schema for returning a risk from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Risk ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Risk title")
    description: str | None = Field(None, description="Risk description")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    project_id: str = Field(
        ..., min_length=1, max_length=100, description="Parent project slug or ID"
    )
    probability: float = Field(..., ge=0, le=1, description="Probability (0.0 to 1.0)")
    impact_score: float = Field(..., ge=1, le=5, description="Impact score (1 to 5)")
    risk_score: float = Field(..., description="Computed risk score")
    category: str = Field(..., max_length=30, description="Risk category")
    response_strategy: str = Field(..., max_length=20, description="Risk response strategy")
    response_plan: str | None = Field(None, description="Detailed response plan")
    status: str = Field(default="identified", max_length=20, description="Risk status")
    owner: str | None = Field(None, max_length=100, description="Risk owner")
    due_at: datetime | None = Field(None, description="When the risk response is due")
    last_reviewed_at: datetime | None = Field(None, description="When the risk was last reviewed")


# ---------------------------------------------------------------------------
# Benefit
# ---------------------------------------------------------------------------


class BenefitIn(VersionFieldsIn):
    """Schema for creating or updating a benefit."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300, description="Benefit title")
    description: str | None = Field(None, description="Benefit description")
    programme_id: UUID | None = Field(None, description="Parent programme ID (UUID FK)")
    # Census: pm_benefits is EMPTY — both vocabularies unexercised, not proven.
    benefit_type: BenefitType = Field(..., description="Benefit type")
    measurement: str = Field(..., description="How the benefit is measured")
    baseline_value: float | None = Field(None, description="Baseline measurement value")
    target_value: float | None = Field(None, description="Target measurement value")
    actual_value: float | None = Field(None, description="Actual measurement value")
    realization_date: datetime | None = Field(None, description="Expected realization date")
    owner: str | None = Field(None, max_length=100, description="Benefit owner")
    status: BenefitStatus = Field(default="expected", description="Benefit status")

    @field_validator("status", "benefit_type", mode="before")
    @classmethod
    def normalise_vocab_fields(cls, v: object) -> object:
        """Preserve the ``.strip().lower()`` both replaced validators performed."""
        return _normalise_vocab(v)


class BenefitOut(SystemInheritanceOutMixin, VersionFieldsMixin, TimestampMixin):
    """Schema for returning a benefit from the API."""

    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    id: UUID = Field(..., description="Benefit ID")
    slug: str = Field(..., min_length=1, max_length=120, description="URL slug")
    title: str = Field(..., min_length=1, max_length=300, description="Benefit title")
    description: str | None = Field(None, description="Benefit description")
    tenant_id: str = Field(..., min_length=1, max_length=100, description="Tenant ID")
    programme_id: UUID | None = Field(None, description="Parent programme ID (UUID FK)")
    benefit_type: str = Field(..., max_length=20, description="Benefit type")
    measurement: str = Field(..., description="How the benefit is measured")
    baseline_value: float | None = Field(None, description="Baseline measurement value")
    target_value: float | None = Field(None, description="Target measurement value")
    actual_value: float | None = Field(None, description="Actual measurement value")
    realization_date: datetime | None = Field(None, description="Expected realization date")
    owner: str | None = Field(None, max_length=100, description="Benefit owner")
    status: str = Field(default="expected", max_length=20, description="Benefit status")
