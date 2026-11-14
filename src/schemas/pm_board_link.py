"""The project ↔ Planka board link (George 2026-10-13, v4 R73 / D20).

The conveyor runs on Planka webhooks, whose body names only Planka ids: ``data.item`` is the
card (``id``, ``boardId``, ``listId``) and the webhook fires for EVERY board on the
instance. fr builds the board from RM's board template right after it creates the project,
then writes the link here once (``PUT /pm/projects/{eid}/planka-board``); n8n resolves any
board id to the RM project (``GET /pm/projects/by-planka-board/{planka_board_id}``) and any
list id to its conveyor list (``lists_by_id``).

The fr project id is the project's ``origin_project_id`` (set at create, immutable).
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from schemas.pm_intake import ProjectRepository

# A Planka id: a 64-bit snowflake serialised as a string. Kept a little wider than digits so
# a Planka version that changes the encoding does not need a migration.
PlankaId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    ),
]

# The conveyor's lists, in flow order (v4 model.md; R7, R33, R68). The board template's list
# names are exactly these (plus the closed ``DROPPED`` list, which the conveyor never reads).
CONVEYOR_LISTS: tuple[str, ...] = ("TODO", "READY", "DOING", "QA", "DONE", "BLOCKED")
ConveyorList = Literal["TODO", "READY", "DOING", "QA", "DONE", "BLOCKED"]


class PlankaListIds(BaseModel):
    """The Planka list id of each of the six conveyor lists. All six, nothing else, distinct."""

    model_config = ConfigDict(extra="forbid")

    TODO: PlankaId
    READY: PlankaId
    DOING: PlankaId
    QA: PlankaId
    DONE: PlankaId
    BLOCKED: PlankaId

    @model_validator(mode="after")
    def _distinct(self) -> "PlankaListIds":
        ids = [getattr(self, name) for name in CONVEYOR_LISTS]
        if len(set(ids)) != len(ids):
            raise ValueError("each conveyor list needs its own Planka list id")
        return self


class ProjectBoardLinkIn(BaseModel):
    """``PUT /pm/projects/{eid}/planka-board``: what fr sends once it has built the board."""

    model_config = ConfigDict(extra="forbid")

    planka_board_id: PlankaId = Field(..., description="The Planka board built for the project")
    planka_project_id: PlankaId | None = Field(
        None, description="The Planka project that holds the board"
    )
    planka_list_ids: PlankaListIds = Field(
        ..., description="The Planka list id of TODO, READY, DOING, QA, DONE and BLOCKED"
    )


class ProjectBoardOut(BaseModel):
    """What n8n needs to go from a Planka event to the RM project."""

    project_id: UUID = Field(..., description="The RM project id")
    tenant_id: str = Field(..., description="The project's tenant")
    fr_project_id: UUID | None = Field(
        None, description="The fr project id (the project's ``origin_project_id``)"
    )
    title: str
    status: str = Field(..., description="The project's lifecycle status")
    project_type_id: UUID | None = None
    project_type: str | None = Field(None, description="The project type's machine name")
    current_phase_id: UUID | None = None
    current_phase: str | None = Field(None, description="The current phase's machine name")
    wip_limit: int | None = Field(None, description="The project's own WIP limit; null = not set")
    effective_wip_limit: int | None = Field(
        None, description="The project TOTAL in DOING that applies; null = no limit; 0 = paused"
    )
    wip_limit_source: Literal["project", "catalogue", "none"] = "none"
    repository_url: str | None = Field(
        None, description="The primary repository's url (else the first one); null = none"
    )
    repository_branch: str | None = Field(
        None,
        description="That same repository's branch; null = none set (the repo's default branch)",
    )
    repository_sha: str | None = Field(
        None,
        description=(
            "That same repository's pinned 40-hex commit, when the project was created with one "
            "(fr `repo.*.pin`); null = not pinned — take `repository_branch`. The conveyor's "
            "workspace import reads this first (George 2026-11-07: the exact version of a repo "
            "is the input)."
        ),
    )
    repositories: list[ProjectRepository] | None = Field(
        None, description="All of the project's repositories, as stored"
    )
    planka_board_id: str | None = None
    planka_project_id: str | None = None
    planka_list_ids: dict[ConveyorList, str] | None = Field(
        None, description="Conveyor list → Planka list id"
    )
    lists_by_id: dict[str, ConveyorList] | None = Field(
        None, description="Planka list id → conveyor list (the reverse of ``planka_list_ids``)"
    )
    planka_linked_at: datetime | None = Field(None, description="When the link last changed")


class ProjectBoardLinkOut(ProjectBoardOut):
    """Result of ``PUT /pm/projects/{eid}/planka-board``."""

    changed: bool = Field(..., description="False when the project already had exactly this link")
