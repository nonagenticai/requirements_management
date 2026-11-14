"""Kanban-coach ADMISSION schemas (R311, kanban-coach-errors.md §3, §4.3, §4.4).

``POST /pm/projects/{eid}/coach-actions`` admits a coach under the project row lock (the only
arbiter between two coaches of one incident, and the only place the caps are counted);
``PATCH /pm/projects/{eid}/coach-actions/{id}`` records the remedy the admitted coach applies
after its post-Admit re-read; ``GET`` lists the rows.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# The order RM's caps move a remedy along: only ever UP (more conservative), §3.
CoachRemedy = Literal["rerun", "move", "repull", "ask", "block"]
AppliedCoachRemedy = Literal["note", "rerun", "move", "repull", "ask", "block"]
CoachExitGroup = Literal["rerun", "pull", "own"]
# The one reason a PATCH may carry: the post-Admit upgrade to ask / block (§3 choice 3).
PostAdmitReason = Literal["post_admit_unverified"]

ExecutionId = Annotated[str, Field(min_length=1, max_length=64)]
PlankaCardRef = Annotated[str, Field(max_length=64)]

# Start values George D3 (2026-10-19); the coach sends the values it runs with (Claude tunes
# them from run data, R241).
DEFAULT_ENTRY_CAP = 1
DEFAULT_CARD_CAP = 3
DEFAULT_PROJECT_CAP = 10


class CoachCaps(BaseModel):
    """The three caps of §4.4 (fail closed: a reached cap moves the remedy up)."""

    model_config = ConfigDict(extra="forbid")

    entry: int = Field(
        DEFAULT_ENTRY_CAP,
        ge=0,
        le=100,
        description="Automatic remedies (rerun/move/repull) per card per list entry",
    )
    card: int = Field(
        DEFAULT_CARD_CAP, ge=0, le=1000, description="Coach actions per card per phase visit"
    )
    project: int = Field(
        DEFAULT_PROJECT_CAP,
        ge=0,
        le=10_000,
        description="Incidents (root execution, exit group) per project per phase visit",
    )


class CoachActionIn(BaseModel):
    """``POST /pm/projects/{eid}/coach-actions``: one coach asks to be admitted."""

    model_config = ConfigDict(extra="forbid")

    card_id: PlankaCardRef = Field("", description="The SOURCE card (the failed execution's)")
    target_card_id: str = Field(
        ..., min_length=1, max_length=64, description="The card the remedy acts on"
    )
    exit_group: CoachExitGroup = Field(..., description="The source branch of the exit (§2.1)")
    entry: PlankaCardRef = Field(
        "", description="The target's list entry (Planka listChangedAt), an opaque token"
    )
    error_class: str = Field(..., min_length=1, max_length=64)
    requested_remedy: CoachRemedy = Field(
        ..., description="The coach's diagnosis; ``note`` never reaches RM"
    )
    source_execution_id: ExecutionId
    root_execution_id: ExecutionId = Field(..., description="The last id of the contract's chain")
    coach_execution_id: ExecutionId = Field(..., description="This coach run (idempotency key)")
    ask_allowed: bool = Field(
        ..., description="The coach's §4.6 reading: may this entry still get an ask"
    )
    caps: CoachCaps = Field(default_factory=CoachCaps)


class CoachAdmissionOut(BaseModel):
    """The admission answer. ``admitted: false`` (``reason: same_incident``) ⇒ the coach
    posts nothing; ``winner`` is the admitted coach's execution id. The counts include the
    admitted row and are computed at answer time."""

    id: UUID | None = None
    admitted: bool
    remedy: str | None = Field(
        None, description="The remedy to apply after the caps (rerun/move/repull/ask/block)"
    )
    reason: str | None = Field(
        None,
        description=(
            "Why the remedy moved up (``project_cap`` / ``card_cap`` / ``entry_cap`` / "
            "``ask_in_entry`` / ``ask_not_allowed``), or ``same_incident`` for a loser"
        ),
    )
    winner: str | None = Field(None, description="The admitted coach of the incident (losers)")
    replayed: bool = Field(False, description="True when this coach's row already existed")
    project_incidents: int | None = None
    card_count: int | None = None
    entry_count: int | None = None


class CoachActionAppliedIn(BaseModel):
    """``PATCH /pm/projects/{eid}/coach-actions/{id}``: record the applied remedy."""

    model_config = ConfigDict(extra="forbid")

    coach_execution_id: ExecutionId
    applied_remedy: AppliedCoachRemedy = Field(
        ...,
        description=(
            "The admitted remedy, ``note``, or (admitted remedy automatic) ``ask`` / ``block`` "
            "with ``reason: post_admit_unverified``"
        ),
    )
    reason: PostAdmitReason | None = None


class CoachActionOut(BaseModel):
    """One admission row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    card_id: str
    target_card_id: str
    exit_group: str
    entry: str
    phase: str | None = None
    phase_visit_at: datetime | None = None
    error_class: str
    requested_remedy: str
    remedy: str
    applied_remedy: str | None = None
    applied_at: datetime | None = None
    admitted: bool
    reason: str | None = None
    source_execution_id: str
    root_execution_id: str
    coach_execution_id: str
    created_at: datetime
    effective_remedy: str = Field(
        ...,
        description="What every cap counts: ``applied_remedy`` if recorded, else ``remedy``",
    )
