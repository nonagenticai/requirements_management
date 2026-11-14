"""The fr project intake stored on ``pm_projects`` (George 2026-10-13).

George's flow: the user creates a project in fr → fr stores it in RM ``pm_projects`` →
fr calls the conveyor webhook with ONLY the RM project id → n8n reads everything the
user provided from RM in one ``GET /pm/projects/{eid}``.

So every shape below mirrors fr's zod contract field for field (fr
``backend/src/types/api.ts`` ``ProjectCreateFields`` :282-528 on
``feat/fr-rm-pm-catalogue``, ``types/project-constraints.ts``, ``types/scope-level.ts``,
``types/question-mode.ts``, ``services/improvement-cards/request-schema.ts``
``CARD_PRIORITIES`` and the ``asset_type`` pgEnum in ``db/schema.ts``). fr's names are
kept; where fr's item shape is an object it is an object here, with ``extra=forbid``.

The intake is what the USER said. It is not requirements, risks or dependencies in RM's
own PM tables: the phases derive those later.

Size limits (each one a 422): every string ≤ 10 000 characters, every list ≤ 200 items,
a brief ≤ 128 KiB and ``it_inputs`` ≤ 64 KiB as compact UTF-8 JSON.
"""

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid7

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

MAX_STR = 10_000
MAX_LIST = 200
MAX_BRIEF_BYTES = 128 * 1024
MAX_IT_INPUTS_BYTES = 64 * 1024

Str = Annotated[str, StringConstraints(max_length=MAX_STR)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=MAX_STR)]
StrList = Annotated[list[Str], Field(max_length=MAX_LIST)]

# fr vocabularies, verbatim.
ScopeLevel = Literal[
    "multi_repo", "repo", "app", "module", "component", "file", "class", "function"
]  # fr types/scope-level.ts SCOPE_LEVELS
CoordinationMode = Literal["coordinated", "independent"]  # api.ts ProjectCreateFields
ProjectShape = Literal["deliverable", "continuous_flow"]  # api.ts ProjectCreateFields
QuestionMode = Literal["structured", "free_text"]  # fr types/question-mode.ts
LifecycleClass = Literal["system", "workflow-improvement", "execution", "prompt-improvement"]
ExpectedSize = Literal["S", "M", "L"]  # v3; fr does not send it yet
ItFlow = Literal["dependency", "security", "source-security", "iac", "bug", "refactor", "rewrite"]
RepositoryRole = Literal["source", "target"]  # rewrite projects (py2rust RS-D1)
TargetLanguage = Literal["python", "rust"]  # rewrite projects (py2rust RS-D1)
# Which conveyor sign-offs the user is asked for (George 2026-10-27). ``all`` = today's
# behaviour (every approval asked); ``auto_drafts`` = start-draft confirmations are
# auto-accepted; ``brief_and_closure`` = only the brief sign-off and the project-closure
# sign-off are asked; ``autonomous`` = nothing is asked. RM only stores it; n8n reads it.
ApprovalMode = Literal["all", "auto_drafts", "brief_and_closure", "autonomous"]
MinSeverity = Literal["LOW", "MODERATE", "HIGH", "CRITICAL"]
BriefAuthorKind = Literal["user", "conveyor"]

# fr db/schema.ts ``asset_type`` pgEnum — the keys of ``BoundAssetsSchema``.
BOUND_ASSET_KINDS = (
    "requirement",
    "principle",
    "decision",
    "procedure",
    "kag_document",
    "workflow",
    "trigger",
    "artifact",
    "channel",
)


def compact_json_bytes(value: object) -> int:
    """Length of ``value`` as compact UTF-8 JSON (the unit of every KiB limit)."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _is_none(v: object) -> bool:
    """``exclude_if`` for fields added after the contract shipped (George R3, additive).

    Such a field is OMITTED from every response while null, so a pre-existing payload
    serialises byte-identically. ``exclude_if`` leaves the JSON schema untouched (a wrap
    ``model_serializer`` would split it into ``-Input``/``-Output`` and blank the output).
    """
    return v is None


# ---------------------------------------------------------------------------
# Constraints (fr types/project-constraints.ts ProjectConstraintSchema)
# ---------------------------------------------------------------------------


class NoLanguageVersionChange(_Strict):
    kind: Literal["no_language_version_change"]


class PinLanguageVersion(_Strict):
    kind: Literal["pin_language_version"]
    language: NonEmptyStr
    version: NonEmptyStr


class NoDependencyUpgrades(_Strict):
    kind: Literal["no_dependency_upgrades"]


class NoNewDependencies(_Strict):
    kind: Literal["no_new_dependencies"]


class NoFilesTouched(_Strict):
    kind: Literal["no_files_touched"]
    globs: Annotated[list[NonEmptyStr], Field(min_length=1, max_length=MAX_LIST)]


class MustKeepApiCompatible(_Strict):
    kind: Literal["must_keep_api_compatible"]


class CustomConstraint(_Strict):
    kind: Literal["custom"]
    text: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


ProjectConstraint = Annotated[
    NoLanguageVersionChange
    | PinLanguageVersion
    | NoDependencyUpgrades
    | NoNewDependencies
    | NoFilesTouched
    | MustKeepApiCompatible
    | CustomConstraint,
    Field(discriminator="kind"),
]


class ProjectDependency(_Strict):
    """fr ``ProjectDependencySchema``."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    kind: Literal["service", "library", "api"]
    note: Annotated[str, StringConstraints(max_length=2000)] | None = None


class ProjectRisk(_Strict):
    """fr ``RiskSchema``. fr declares likelihood/impact as free strings (no enum)."""

    description: Str
    likelihood: Str | None = None
    impact: Str | None = None
    mitigation: Str | None = None


# ---------------------------------------------------------------------------
# Acceptance criteria (fr cockpit A4, R-CK7)
# ---------------------------------------------------------------------------

CriterionStatus = Literal["met", "not_met", "unchecked"]
EvidenceKind = Literal["test_run", "pr", "report"]
CriterionId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
MAX_EVIDENCE = 50


class CriterionEvidence(_Strict):
    """One piece of evidence for a criterion's status: a test run, a PR or a report."""

    kind: EvidenceKind
    ref: Annotated[str, StringConstraints(min_length=1, max_length=2000)] = Field(
        ..., description="What the evidence is (a run id, a PR url, a report id)"
    )


class AcceptanceCriterion(_Strict):
    """One acceptance criterion of the brief, with its check status.

    ``status`` / ``evidence`` / ``checked_at`` / ``checked_by`` are SERVER-OWNED: only
    ``PATCH /pm/projects/{eid}/criteria/{criterion_id}`` (PM / n8n, never fr) sets them
    (R-CK7: the conveyor checks, fr displays). A brief write carries them over by id (same
    text) or by text; anything else it sends for them is replaced by the stored value or by
    ``unchecked``.
    """

    id: CriterionId = Field(..., description="Stable id of the criterion within the project")
    text: Str
    status: CriterionStatus = "unchecked"
    evidence: Annotated[list[CriterionEvidence], Field(max_length=MAX_EVIDENCE)] = Field(
        default_factory=list
    )
    checked_at: datetime | None = None
    checked_by: Annotated[str, StringConstraints(max_length=255)] | None = None

    def unchecked(self) -> "AcceptanceCriterion":
        """This criterion with its check cleared."""
        return self.model_copy(
            update={"status": "unchecked", "evidence": [], "checked_at": None, "checked_by": None}
        )


def _criterion_items(value: Any) -> Any:
    """Input-edge normalisation: a plain string becomes an ``unchecked`` item with a new id;
    an object without an ``id`` gets one. Stored briefs hold items only."""
    if not isinstance(value, list):
        return value
    out: list[Any] = []
    for item in value:
        if isinstance(item, str):
            out.append({"id": str(uuid7()), "text": item})
        elif isinstance(item, dict) and not item.get("id"):
            out.append({**item, "id": str(uuid7())})
        else:
            out.append(item)
    return out


AcceptanceCriteria = Annotated[
    list[AcceptanceCriterion],
    Field(max_length=MAX_LIST),
    BeforeValidator(_criterion_items),
]


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------


class ProjectBrief(_Strict):
    """What the user wrote about the project, field for field as fr sends it.

    ``deadline`` and ``budget`` are kept as the strings the user typed; they are NOT
    mapped onto ``pm_projects.end_date`` / ``budget`` (fr may set those separately).
    An absent key and ``null`` are the same thing; an empty list is kept as given.
    """

    # Problem & Objectives
    problem_statement: Str | None = None
    desired_outcome: Str | None = None
    business_case: Str | None = None
    # Scope & Requirements
    success_criteria: StrList | None = None
    acceptance_criteria: AcceptanceCriteria | None = Field(
        None,
        description=(
            "Items ``{id, text, status, evidence, checked_at, checked_by}``. A plain string "
            "is accepted on input and stored as an ``unchecked`` item with a new id. The "
            "check fields are server-owned (``PATCH /pm/projects/{eid}/criteria/{id}``, "
            "PM/n8n only): a brief write keeps a criterion's check when it sends the same id "
            "and text (or the same text), otherwise the criterion is ``unchecked``."
        ),
    )
    in_scope: StrList | None = None
    out_of_scope: StrList | None = None
    high_level_requirements: StrList | None = None
    target_language: TargetLanguage | None = Field(
        None,
        description=(
            "The language a rewrite ports INTO (``it_inputs.it_flow: rewrite``). fr reads it "
            "out of the project request at create and sends it here (George 2026-11-07); the "
            "rewrite flows read it from the brief."
        ),
    )
    assumptions: StrList | None = None
    access_requirements: StrList | None = None
    # People
    stakeholders: StrList | None = None
    # Strings, not UUIDs: fr keeps non-UUID approvers (a name or an email) on purpose
    # (fr ``routes/projects.ts:1953-1962``).
    approvers: (
        Annotated[
            list[Annotated[str, StringConstraints(min_length=1, max_length=255)]],
            Field(max_length=MAX_LIST),
        ]
        | None
    ) = None
    # Timeline, resources, notes
    deadline: Str | None = None
    budget: Str | None = None
    notes: Str | None = None
    # Rules, dependencies, risks — legacy plain strings and the typed shapes, as fr.
    constraints: Annotated[list[Str | ProjectConstraint], Field(max_length=MAX_LIST)] | None = (
        Field(
            None,
            description=(
                "The rules the conveyor's work is checked against. IMMUTABLE after version 1, "
                "for every author: a later version whose constraints differ from the current "
                "version's (same list, same order, deep-equal; absent = null = []) is 422 "
                "``brief_constraints_immutable``. fr's ProjectBriefUpdateSchema leaves them "
                "out for the same reason: the conveyor must never be able to loosen the gate "
                "that checks its work."
            ),
        )
    )
    dependencies: Annotated[list[Str | ProjectDependency], Field(max_length=MAX_LIST)] | None = None
    risks: Annotated[list[ProjectRisk], Field(max_length=MAX_LIST)] | None = None

    @field_validator("acceptance_criteria")
    @classmethod
    def _unique_criterion_ids(
        cls, v: list[AcceptanceCriterion] | None
    ) -> list[AcceptanceCriterion] | None:
        if v is not None and len({c.id for c in v}) != len(v):
            raise PydanticCustomError(
                "acceptance_criterion_ids_not_unique", "acceptance criterion ids must be unique"
            )
        return v

    @model_validator(mode="after")
    def _size(self) -> "ProjectBrief":
        size = compact_json_bytes(self.snapshot())
        if size > MAX_BRIEF_BYTES:
            # A fixed type tag: the 422 envelope shows ``loc:type`` only (never ``msg``).
            raise PydanticCustomError(
                "brief_over_128_kib",
                "brief is {size} bytes; the limit is {limit}",
                {"size": size, "limit": MAX_BRIEF_BYTES},
            )
        return self

    def snapshot(self) -> dict:
        """The stored form: JSON, absent and null keys dropped."""
        return self.model_dump(mode="json", exclude_none=True)


# ---------------------------------------------------------------------------
# The other intake groups
# ---------------------------------------------------------------------------


class ProjectRepository(_Strict):
    """One repository. fr sends ``repo.primary`` (url, branch?, path?) and
    ``repo.additional[]`` (url, branch?, name?), or the legacy ``repository_url`` +
    ``branch``; fr maps them to this list and marks the primary entry."""

    url: NonEmptyStr
    branch: NonEmptyStr | None = None
    path: Str | None = None
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | (
        None
    ) = None
    primary: bool = False
    role: RepositoryRole | None = Field(
        None,
        exclude_if=_is_none,
        description=(
            "A rewrite project's role for this repository: ``source`` (read, pinned to "
            "``sha``) or ``target`` (written; the primary repository). null = no role"
        ),
    )
    sha: str | None = Field(
        None,
        exclude_if=_is_none,
        description="The full 40-hex commit this repository is pinned to (stored lowercase)",
    )

    @field_validator("sha")
    @classmethod
    def _sha_is_40_hex(cls, v: str | None) -> str | None:
        # A fixed type tag: the 422 envelope shows ``loc:type`` only (never ``msg``).
        if v is None:
            return v
        if re.fullmatch(r"[0-9a-fA-F]{40}", v) is None:
            raise PydanticCustomError("sha_must_be_40_hex", "sha must be a full 40-hex commit")
        return v.lower()


class TargetPackage(_Strict):
    name: NonEmptyStr
    target_version: NonEmptyStr


class ItInputs(_Strict):
    """fr's IT-project inputs (EXEC ``it`` flows 1/2/3/4, CVE sweep, bug hunt).

    ``test_command`` / ``bug_test_command`` are argv arrays, as fr sends them.
    """

    it_flow: ItFlow | None = None
    min_severity: MinSeverity | None = None
    cve_sweep: bool | None = None
    bug_hunt: bool | None = None
    target_packages: Annotated[list[TargetPackage], Field(max_length=MAX_LIST)] | None = None
    test_command: StrList | None = None
    test_workdir: Str | None = None
    bug_test_command: StrList | None = None
    failing_test_ref: Str | None = None
    target_files: StrList | None = None
    finding_description: Str | None = None
    bug_description: Str | None = None
    # A rewrite's target language is NOT here: it is a fact about the request, so it lives on
    # the brief (``ProjectBrief.target_language``, George 2026-11-07).
    approval_mode: ApprovalMode | None = Field(
        None,
        exclude_if=_is_none,
        description=(
            "Which conveyor sign-offs the user is asked for: ``all`` (every one), "
            "``auto_drafts`` (start-draft confirmations auto-accepted), "
            "``brief_and_closure`` (only the brief and the project-closure sign-off), "
            "``autonomous`` (none). null = ``all``"
        ),
    )

    @model_validator(mode="after")
    def _size(self) -> "ItInputs":
        size = compact_json_bytes(self.model_dump(mode="json", exclude_none=True))
        if size > MAX_IT_INPUTS_BYTES:
            raise PydanticCustomError(
                "it_inputs_over_64_kib",
                "it_inputs is {size} bytes; the limit is {limit}",
                {"size": size, "limit": MAX_IT_INPUTS_BYTES},
            )
        return self


def rewrite_violation(
    repositories: list[ProjectRepository] | None,
    it_inputs: ItInputs | None,
    brief: ProjectBrief | None = None,
) -> tuple[str, str] | None:
    """``(code, message)`` when an ``it_flow: rewrite`` project is malformed, else None.

    A rewrite needs ≥1 ``role: source`` repository, each pinned to a ``sha``; exactly one
    ``role: target`` repository, and it is the primary; and ``brief.target_language`` — the
    fact is read out of the request by fr and recorded on the brief, never in ``it_inputs``
    (George 2026-11-07). Any other ``it_flow`` (or none) enforces nothing here — the fields
    stay optional.
    """
    if it_inputs is None or it_inputs.it_flow != "rewrite":
        return None
    repos = repositories or []
    sources = [r for r in repos if r.role == "source"]
    targets = [r for r in repos if r.role == "target"]
    if not sources:
        return (
            "rewrite_needs_source_repository",
            "a rewrite needs at least one role=source repository",
        )
    if any(r.sha is None for r in sources):
        return "rewrite_source_needs_sha", "every role=source repository needs a 40-hex sha"
    if len(targets) != 1:
        return (
            "rewrite_needs_exactly_one_target_repository",
            f"a rewrite needs exactly one role=target repository (got {len(targets)})",
        )
    if not targets[0].primary:
        return "rewrite_target_must_be_primary", "the role=target repository must be primary"
    if brief is None or brief.target_language is None:
        return (
            "rewrite_needs_language",
            "a rewrite needs brief.target_language (the language it ports into)",
        )
    return None


UuidList = Annotated[list[UUID], Field(max_length=MAX_LIST)]


class ProjectAttachments(_Strict):
    """Opaque references only (secrets-service secret ids, document ids) — never values."""

    secret_ids: UuidList = Field(default_factory=list)
    document_ids: UuidList = Field(default_factory=list)


AssetIds = Annotated[
    list[Annotated[str, StringConstraints(min_length=1, max_length=255)]],
    Field(max_length=MAX_LIST),
]


class BoundAssets(_Strict):
    """fr ``BoundAssetsSchema``: per asset kind, the upstream asset ids the user picked."""

    requirement: AssetIds | None = None
    principle: AssetIds | None = None
    decision: AssetIds | None = None
    procedure: AssetIds | None = None
    kag_document: AssetIds | None = None
    workflow: AssetIds | None = None
    trigger: AssetIds | None = None
    artifact: AssetIds | None = None
    channel: AssetIds | None = None


# ---------------------------------------------------------------------------
# Brief versions
# ---------------------------------------------------------------------------


class BriefVersionIn(_Strict):
    """``POST /pm/projects/{eid}/brief/versions`` — append the next version.

    The author (``author_kind`` and ``author_subject_id``) comes from the caller's
    credentials, never from the body: ``user`` iff fr-delegated, else ``conveyor``.
    """

    brief: ProjectBrief = Field(
        ...,
        description=(
            "The FULL brief (a snapshot, not a patch). Its ``constraints`` must equal the "
            "current version's (422 ``brief_constraints_immutable``)."
        ),
    )
    note: Annotated[str, StringConstraints(max_length=2000)] | None = Field(
        None, description="Why this version exists"
    )
    if_current_version: int | None = Field(
        None,
        ge=1,
        description=(
            "Compare-and-set: when present, the append is refused (409 "
            "``brief_version_mismatch``) unless it equals the project's current brief "
            "version. Omitted = no check."
        ),
    )


class BriefVersionSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    author_kind: BriefAuthorKind = Field(
        ...,
        description=(
            "Who wrote this version, derived from the caller: ``user`` iff the write was "
            "fr-delegated, otherwise ``conveyor``"
        ),
    )
    author_subject_id: str
    note: str | None = None
    created_at: datetime


class BriefVersionOut(BriefVersionSummaryOut):
    project_id: UUID
    brief: ProjectBrief


# ---------------------------------------------------------------------------
# Criterion check (``PATCH /pm/projects/{eid}/criteria/{criterion_id}``)
# ---------------------------------------------------------------------------


class CriterionCheckIn(_Strict):
    """Set one acceptance criterion's check. PM / n8n only (R-CK7).

    ``checked_at`` and ``checked_by`` are stamped by RM (now, the caller's principal). ``met``
    needs at least one piece of evidence; ``unchecked`` clears the check and takes none.
    """

    status: CriterionStatus
    evidence: Annotated[list[CriterionEvidence], Field(max_length=MAX_EVIDENCE)] = Field(
        default_factory=list, description="Replaces the criterion's evidence whole"
    )

    @model_validator(mode="after")
    def _evidence_fits_status(self) -> "CriterionCheckIn":
        if self.status == "met" and not self.evidence:
            raise PydanticCustomError(
                "criterion_met_needs_evidence", "status met needs at least one evidence item"
            )
        if self.status == "unchecked" and self.evidence:
            raise PydanticCustomError(
                "criterion_unchecked_takes_no_evidence", "status unchecked takes no evidence"
            )
        return self


class CriterionCheckOut(BaseModel):
    project_id: UUID
    brief_version: int = Field(..., description="The brief version that now holds the check")
    changed: bool = Field(
        ..., description="false = the criterion already had this status and evidence (no write)"
    )
    criterion: AcceptanceCriterion
