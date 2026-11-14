"""Project registers (REG-1): the register fields on risks, decisions and requirements, and
the read model of ``GET /pm/projects/{eid}/registers``.

Spec: ``~/.claude/plans/program/20-designs/FR-PROJECT-HOME-REDESIGN-2026-10-26.md`` §7.3-7.5.
fr's Registers page and Overview strip read the endpoint; the n8n requirements node writes the
fields through the existing create / update routes of the three entities.

* ``phase`` is a phase NAME from the tenant's ``pm_project_phases`` catalogue (``setup`` |
  ``preliminary`` | ``execution`` by default). The catalogue is per tenant and extensible, so
  the column has no CHECK; the write edge only normalises (strip, lowercase) and checks the
  name's shape.
* ``card_id`` is the Planka card the row was raised on (same shape as ``PlankaId``).
* ``raised_at`` defaults to the insert time (server default ``now()``). Sent as ``null`` or
  omitted, it is left out of the write entirely (``exclude_if``), so a create keeps the server
  default and a PUT keeps the stored value; only an explicit timestamp changes it.
"""

from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints


def _blank_to_none(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip() or None
    return v


def _lower_blank_to_none(v: Any) -> Any:
    v = _blank_to_none(v)
    return v.lower() if isinstance(v, str) else v


def _is_none(v: Any) -> bool:
    return v is None


RegisterPhase = Annotated[
    Annotated[str, StringConstraints(max_length=64, pattern=r"^[a-z][a-z0-9_]*$")] | None,
    BeforeValidator(_lower_blank_to_none),
]
RegisterCardId = Annotated[
    Annotated[str, StringConstraints(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] | None,
    BeforeValidator(_blank_to_none),
]
RequirementStatusText = Annotated[
    Annotated[str, StringConstraints(max_length=30)] | None,
    BeforeValidator(_lower_blank_to_none),
]
OwnerText = Annotated[
    Annotated[str, StringConstraints(max_length=100)] | None,
    BeforeValidator(_blank_to_none),
]

DECIDED_BY_KINDS: frozenset[str] = frozenset({"human", "agent", "rule"})
DecidedByKind = Literal["human", "agent", "rule"]

_PHASE_DESC = (
    "Phase the row was raised in: a phase name from the tenant's project-phase catalogue "
    "(setup, preliminary, execution by default). Normalised to lowercase."
)
_CARD_DESC = "Planka card id the row was raised on"
_RAISED_DESC = (
    "When the row was raised. Defaults to the creation time; null/omitted keeps the stored "
    "value (create: now)."
)


class RegisterFieldsIn(BaseModel):
    """Register fields shared by the risk, decision and requirement write schemas.

    ``phase`` / ``card_id`` OMITTED from an update keep the stored value; an explicit
    ``null`` clears them (``EntityService.update`` reads ``KEEP_IF_OMITTED``).
    """

    KEEP_IF_OMITTED: ClassVar[frozenset[str]] = frozenset({"phase", "card_id"})

    phase: RegisterPhase = Field(None, description=_PHASE_DESC)
    card_id: RegisterCardId = Field(None, description=_CARD_DESC)
    raised_at: datetime | None = Field(None, description=_RAISED_DESC, exclude_if=_is_none)


class RegisterFieldsOut(BaseModel):
    """Register fields shared by the risk, decision and requirement read schemas."""

    phase: str | None = Field(None, description="Phase the row was raised in")
    card_id: str | None = Field(None, description=_CARD_DESC)
    raised_at: datetime | None = Field(None, description="When the row was raised")


# ---------------------------------------------------------------------------
# GET /pm/projects/{eid}/registers
# ---------------------------------------------------------------------------


class RegisterRisk(BaseModel):
    """One row of the risk register."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    title: str
    description: str | None = None
    category: str
    probability: float
    impact: float = Field(..., description="Impact score (1-5); pm_risks.impact_score")
    score: float = Field(..., description="probability x impact, rounded to 2 decimals")
    status: str
    owner: str | None = None
    response_strategy: str
    response_plan: str | None = Field(None, description="Response / mitigation plan")
    phase: str | None = None
    card_id: str | None = None
    raised_at: datetime | None = None
    due_at: datetime | None = None
    last_reviewed_at: datetime | None = None


class RegisterDecision(BaseModel):
    """One row of the decision register (RM ``decisions``: design / scope decisions)."""

    id: UUID
    slug: str
    title: str
    status: str
    context: str
    decision: str
    consequences: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    alternatives_count: int
    decided_by_kind: str | None = None
    decided_at: datetime | None = None
    approved_by: str | None = None
    phase: str | None = None
    card_id: str | None = None
    raised_at: datetime | None = None


class RegisterRequirement(BaseModel):
    """One row of the requirement register."""

    id: UUID
    slug: str
    title: str
    description: str
    status: str | None = None
    owner: str | None = None
    acceptance: list[Any] = Field(default_factory=list)
    version_status: str
    phase: str | None = None
    card_id: str | None = None
    raised_at: datetime | None = None


CriterionKind = Literal["success", "acceptance"]


class RegisterCriterion(BaseModel):
    """One criterion of the project's CURRENT brief.

    ``success`` criteria are the brief's ``success_criteria`` strings: they carry no id and
    no check (``id`` / ``status`` null). ``acceptance`` criteria are the brief's
    ``acceptance_criteria`` items with their server-owned check (``PATCH
    /pm/projects/{eid}/criteria/{criterion_id}``).
    """

    kind: CriterionKind
    id: str | None = None
    text: str
    status: str | None = Field(None, description="met | not_met | unchecked; null for success")
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    checked_at: datetime | None = None
    checked_by: str | None = None


class RegisterCounts(BaseModel):
    risks_open: int = Field(..., description="Risks whose status is not 'closed'")
    risks_total: int
    decisions: int
    requirements: int
    criteria_met: int = Field(..., description="Criteria with status 'met'")
    criteria_total: int = Field(..., description="All criteria (success + acceptance)")


class ProjectRegistersOut(BaseModel):
    """Every register of one project, plus the counts fr's Overview strip shows.

    Lists are ordered for the strip: risks by score (highest first), decisions by decision
    time (newest first), requirements by raise time (newest first). ``?phase=`` filters the
    risks, decisions and requirements (and their counts); criteria belong to the brief, which
    has no phase, and are never filtered.
    """

    project_id: UUID
    phase: str | None = Field(None, description="The ?phase= filter applied, if any")
    risks: list[RegisterRisk]
    decisions: list[RegisterDecision]
    requirements: list[RegisterRequirement]
    criteria: list[RegisterCriterion]
    counts: RegisterCounts
