"""PM models — Programmes, Projects, Resources, Risks, Benefits."""

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid7

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Double,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, column_property, mapped_column

from db import Base, TenantSubjectMixin
from models._quality_mixin import QualityMixin
from models.pm_catalogue import PriorityLevel, ProjectPhase

# Valid programme statuses (MSP lifecycle)
PROGRAMME_STATUSES = frozenset({"identifying", "defining", "managing_tranches", "closing"})

# Valid project statuses (PMBOK process groups)
# "closed" (2026-10-08, fr#1470): terminal state fr writes when the fr project a
# row was minted for (``origin_project_id``) is archived. Before it existed every
# intake-minted row stayed "executing" forever.
PROJECT_STATUSES = frozenset(
    {"initiating", "planning", "executing", "monitoring", "closing", "closed"}
)

# Valid resource types
RESOURCE_TYPES = frozenset({"person", "team", "equipment", "material"})


class Programme(TenantSubjectMixin, Base):
    """MSP programme — top-level container grouping projects into tranches."""

    __tablename__ = "pm_programmes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    status: Mapped[str] = mapped_column(String(20), default="identifying")
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blueprint_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    version_status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    supersedes: Mapped[str | None] = mapped_column(String(120))
    superseded_by: Mapped[str | None] = mapped_column(String(120))
    version_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))",
            persisted=True,
        ),
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_pm_programmes_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_pm_programmes_visibility"),
        Index("ix_pm_programmes_tsv", "tsv", postgresql_using="gin"),
        Index("ix_pm_programmes_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_pm_programmes_tenant_visibility", "tenant_id", "visibility"),
    )


class Project(TenantSubjectMixin, Base):
    """PM project — belongs to a programme via programme_id slug reference."""

    __tablename__ = "pm_projects"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    programme_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_programmes.id", ondelete="SET NULL"), nullable=True
    )
    tranche: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="initiating")
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(10))
    # ISO 4217 code of ``budget`` (migration 2026_10_25_0064; fr cockpit R-CK3, reporting
    # only). The request edge checks the code list (``schemas.currency``); the CHECK pins the
    # shape.
    budget_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    version_status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    supersedes: Mapped[str | None] = mapped_column(String(120))
    superseded_by: Mapped[str | None] = mapped_column(String(120))
    version_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Origin provenance (WS2 groundwork) — mirrors Requirement.origin_*. Captures
    # immutable provenance for AA-intake / workflow-minted projects.
    origin_kind: Mapped[str] = mapped_column(String(20), default="tenant", server_default="tenant")
    origin_project_id: Mapped[UUID | None] = mapped_column(Uuid)
    origin_workflow_run_id: Mapped[str | None] = mapped_column(String(200))
    created_by_user_id: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # Project TYPE and current PHASE (migration 2026_10_13_0053). RM owns them; n8n is the
    # only writer of the phase (``PUT /pm/projects/{eid}/phase``); fr reads them. NULL on
    # legacy rows. The phase must be one the type selected: composite FK onto
    # ``pm_project_type_phases``, NO ACTION, DEFERRABLE INITIALLY DEFERRED (``set_phases``
    # deletes and re-inserts a type's selection in one transaction), MATCH SIMPLE so a
    # NULL phase passes; the CHECK forbids a phase without a type.
    project_type_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_project_types.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    current_phase_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    phase_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The project's own WIP limit: the project TOTAL of cards in DOING (migration
    # 2026_10_13_0054). NULL = not set here, the tenant's catalogue policy applies
    # (``pm_wip_limits``); 0 = no card may be in DOING (a pause).
    wip_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How many times a card may go back for rework (migration 2026_10_13_0054). NULL = the
    # default (``schemas.pm.DEFAULT_REWORK_LIMIT``).
    rework_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The fr project INTAKE — everything the user entered when creating the project in fr
    # (migration 2026_10_13_0056; shapes in ``schemas.pm_intake``). NULL on legacy rows.
    # The brief itself is versioned in ``pm_project_brief_versions``; this row only points
    # at the current version.
    repositories: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    repository_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    coordination_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shape: Mapped[str | None] = mapped_column(String(32), nullable=True)
    priority_level_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_priority_levels.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    expected_size: Mapped[str | None] = mapped_column(String(1), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    github_token_secret_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    question_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lifecycle_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fixture: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    it_inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    attachments: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    bound_assets: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    current_brief_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intake_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The project's Planka board (migration 2026_10_13_0058; George 10-13 v4 R73 / D20): fr
    # writes it once after it builds the board (``PUT /pm/projects/{eid}/planka-board``); n8n
    # resolves a Planka webhook's board id to this project (``GET
    # /pm/projects/by-planka-board/{planka_board_id}``). ``planka_list_ids`` maps the six
    # conveyor lists (TODO, READY, DOING, QA, DONE, BLOCKED) to Planka list ids.
    planka_board_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    planka_project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    planka_list_ids: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    planka_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))",
            persisted=True,
        ),
    )

    if TYPE_CHECKING:
        # Derived: the current phase's ``name`` (mapped below the class).
        current_phase: Mapped[str | None]
        # Derived: the priority level's ``name`` and the current brief version.
        priority: Mapped[str | None]
        brief: Mapped[dict[str, Any] | None]
        brief_author_kind: Mapped[str | None]
        brief_updated_at: Mapped[datetime | None]

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_pm_projects_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_pm_projects_visibility"),
        CheckConstraint(
            "current_phase_id IS NULL OR project_type_id IS NOT NULL",
            name="ck_pm_projects_phase_needs_type",
        ),
        CheckConstraint("wip_limit >= 0", name="ck_pm_projects_wip_limit_nonneg"),
        CheckConstraint("rework_limit >= 0", name="ck_pm_projects_rework_limit_nonneg"),
        CheckConstraint("expected_size IN ('S', 'M', 'L')", name="ck_pm_projects_expected_size"),
        CheckConstraint("current_brief_version >= 1", name="ck_pm_projects_current_brief_version"),
        CheckConstraint(
            "budget_currency ~ '^[A-Z]{3}$'", name="ck_pm_projects_budget_currency_iso4217"
        ),
        # Per tenant: Planka is per tenant, so board ids from different tenants are independent.
        UniqueConstraint(
            "tenant_id", "planka_board_id", name="uq_pm_projects_tenant_planka_board_id"
        ),
        CheckConstraint(
            "(planka_board_id IS NULL) = (planka_list_ids IS NULL)",
            name="ck_pm_projects_planka_link_complete",
        ),
        ForeignKeyConstraint(
            ["project_type_id", "current_phase_id"],
            ["pm_project_type_phases.project_type_id", "pm_project_type_phases.phase_id"],
            name="fk_pm_projects_selected_phase",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_pm_projects_type_phase", "project_type_id", "current_phase_id"),
        Index("ix_pm_projects_tsv", "tsv", postgresql_using="gin"),
        Index("ix_pm_projects_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_pm_projects_tenant_visibility", "tenant_id", "visibility"),
        # R345 (migration 2026_11_03_0078): ONE PM project per fr project, for life — closed
        # rows and every visibility included (``origin_project_id`` is immutable; no unlink).
        # It is also POST's get-or-create key (``routers/pm_projects.create_project``).
        CheckConstraint(
            "origin_kind IN ('tenant', 'project', 'workflow')",
            name="ck_pm_projects_origin_kind",
        ),
        CheckConstraint(
            "(origin_kind = 'project') = (origin_project_id IS NOT NULL)",
            name="ck_pm_projects_origin_project",
        ),
        Index(
            "uq_pm_projects_tenant_origin_project",
            "tenant_id",
            "origin_project_id",
            unique=True,
            postgresql_where=text("origin_kind = 'project'"),
        ),
    )


# Derived, read with every project: the current phase's machine name.
Project.current_phase = column_property(
    select(ProjectPhase.name)
    .where(ProjectPhase.id == Project.current_phase_id)
    .correlate_except(ProjectPhase)
    .scalar_subquery()
)


# Derived, read with every project: the priority level's machine name (``p0``..).
Project.priority = column_property(
    select(PriorityLevel.name)
    .where(PriorityLevel.id == Project.priority_level_id)
    .correlate_except(PriorityLevel)
    .scalar_subquery()
)


class ProjectBriefVersion(Base):
    """One version of a project's brief: the FULL snapshot (``schemas.pm_intake.ProjectBrief``).

    Append-only — there is no update or delete route. ``version`` is assigned under a row
    lock on the project (``services.pm_project_brief_service``); ``uq_pm_project_brief_
    versions_project_version`` is the backstop. ``pm_projects.current_brief_version`` names
    the current one. ``author_subject_id`` is the caller's principal, never a body field.
    """

    __tablename__ = "pm_project_brief_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("pm_projects.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    brief: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # ``user`` iff the write was fr-delegated, else ``conveyor`` (``auth.author_kind``);
    # derived from the principal, never a body field.
    author_kind: Mapped[str] = mapped_column(String(16))
    author_subject_id: Mapped[str] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "project_id", "version", name="uq_pm_project_brief_versions_project_version"
        ),
        CheckConstraint("version >= 1", name="ck_pm_project_brief_versions_version"),
        CheckConstraint(
            "author_kind IN ('user', 'conveyor')", name="ck_pm_project_brief_versions_author_kind"
        ),
        CheckConstraint(
            "note IS NULL OR char_length(note) <= 2000",
            name="ck_pm_project_brief_versions_note_len",
        ),
    )


def _current_brief(column: Any) -> Any:
    # The explicit tenant predicate is the APPLICATION-level filter: RLS is only the backstop
    # (a role that bypasses row security, or a future policy change, must not pair a project
    # with a version row of another tenant). Whether the CALLER may see the brief at all is decided by
    # ``pm_project_intake_service.redact_foreign`` (a system-shared project read by another
    # tenant gets no intake).
    return (
        select(column)
        .where(
            ProjectBriefVersion.project_id == Project.id,
            ProjectBriefVersion.tenant_id == Project.tenant_id,
            ProjectBriefVersion.version == Project.current_brief_version,
        )
        .correlate_except(ProjectBriefVersion)
        .scalar_subquery()
    )


# Derived, read with every project: the current brief version's snapshot and metadata.
Project.brief = column_property(_current_brief(ProjectBriefVersion.brief))
Project.brief_author_kind = column_property(_current_brief(ProjectBriefVersion.author_kind))
Project.brief_updated_at = column_property(_current_brief(ProjectBriefVersion.created_at))


class ProjectPhaseEvent(Base):
    """One real change of a project's current phase, or its closure (history; newest first
    on read).

    Written by ``PUT /pm/projects/{eid}/phase`` in the same transaction as the change; an
    idempotent re-stamp writes none, a ``restart: true`` re-stamp writes one with
    ``kind='phase'`` and ``from_phase_id == to_phase_id`` (a new visit of the same phase).
    ``kind='closure'`` is written by the project update that moves ``status`` to ``closed``
    (once per transition; re-sending ``closed`` writes none). Phase ids are kept as plain
    uuids (no FK): history outlives a phase being removed from the catalogue.
    """

    __tablename__ = "pm_project_phase_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("pm_projects.id", ondelete="CASCADE"))
    from_phase_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    to_phase_id: Mapped[UUID] = mapped_column(Uuid)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    subject_id: Mapped[str] = mapped_column(String(255))
    # ``phase`` = a real phase change; ``closure`` = the project's status became ``closed``
    # (migration 2026_10_25_0064; fr cockpit A4): from = to = the phase it closed in.
    kind: Mapped[str] = mapped_column(String(16), default="phase", server_default="phase")

    __table_args__ = (
        Index("ix_pm_project_phase_events_project_at", "project_id", "at"),
        CheckConstraint("kind IN ('phase', 'closure')", name="ck_pm_project_phase_events_kind"),
    )


# R302 (George 2026-10-17): the PM's per-capability setup decision. Mirrored by the
# ``CapabilityInvolvement`` / ``CapabilityLevel`` Literals in ``schemas.pm`` (pinned by
# set-equality in ``tests/test_pm_project_capabilities.py``) and the CHECKs below.
CAPABILITY_INVOLVEMENTS = frozenset({"none", "light", "standard", "heavy"})
CAPABILITY_LEVELS = frozenset({"low", "medium", "high"})


class ProjectCapability(Base):
    """A capability the PM selected for a project (R260 v1, migration 2026_10_15_0059).

    Only PM / n8n writes the selection (``PUT /pm/projects/{eid}/capabilities``, v4 ruling 1);
    fr reads it. A selectable capability has at least one card type in the project type's
    composition. ``approval_id`` is the fr sign-off that approved it (no FK: sign-offs live
    in fr).
    """

    __tablename__ = "pm_project_capabilities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("pm_projects.id", ondelete="CASCADE"))
    capability_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("pm_capabilities.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    # R302 (migration 2026_10_17_0060): the PM's decision from the capability consult.
    # All nullable / empty: a selection written before R302 (or without a consult) has none.
    involvement: Mapped[str | None] = mapped_column(String(16), nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    rigor: Mapped[str | None] = mapped_column(String(16), nullable=True)
    complexity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    consult: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    order_after: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]"), default=list
    )
    approval_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    subject_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "project_id", "capability_id", name="uq_pm_project_capabilities_project_capability"
        ),
        CheckConstraint(
            "involvement IN ('none', 'light', 'standard', 'heavy')",
            name="ck_pm_project_capabilities_involvement",
        ),
        CheckConstraint(
            "rigor IN ('low', 'medium', 'high')", name="ck_pm_project_capabilities_rigor"
        ),
        CheckConstraint(
            "complexity IN ('low', 'medium', 'high')",
            name="ck_pm_project_capabilities_complexity",
        ),
    )


class ProjectCapabilityEvent(Base):
    """One real change of a project's selected capability NAME set (history; newest first).

    Written by ``PUT /pm/projects/{eid}/capabilities`` in the same transaction as the change;
    a reason-only change or an identical re-send writes none. Names, not ids: history outlives
    a capability being removed from the catalogue.
    """

    __tablename__ = "pm_project_capability_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("pm_projects.id", ondelete="CASCADE"))
    added: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'::text[]")
    )
    removed: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'::text[]")
    )
    approval_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    subject_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_pm_project_capability_events_project_created", "project_id", "created_at"),
    )


# R311 (kanban-coach-errors.md §3, §4.4): the coach's remedies, in their order. RM's caps only
# ever move a remedy UP this order. ``note`` never reaches RM at admission; it is only an
# ``applied_remedy`` (the post-Admit downgrade).
COACH_REMEDY_ORDER: tuple[str, ...] = ("note", "rerun", "move", "repull", "ask", "block")
COACH_AUTOMATIC_REMEDIES: frozenset[str] = frozenset({"rerun", "move", "repull"})
COACH_EXIT_GROUPS: tuple[str, ...] = ("rerun", "pull", "own")


def _in_sql(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in sorted(values))


class CoachAction(Base):
    """One kanban-coach ADMISSION (R311, migration 2026_10_19_0062; append-only).

    ``POST /pm/projects/{eid}/coach-actions`` writes it under the project row lock: one row
    per coach run (UNIQUE ``(tenant_id, coach_execution_id)``) and at most one per incident
    (UNIQUE incident key ``(tenant_id, project_id, target_card_id, entry, root_execution_id,
    exit_group)``). ``remedy`` is what RM admitted after the caps; ``applied_remedy`` is what
    the coach recorded after its post-Admit re-read (``PATCH``): null until then, and every
    cap counts ``coalesce(applied_remedy, remedy)`` (fail closed). ``phase_visit_at`` is the
    project's ``phase_updated_at`` read under the lock at admission: the per-project incident
    cap and the per-card cap count the rows of the current phase VISIT by equality with it
    (no clock comparison).
    ``phase`` is the phase name at admission, trace only, never counted.
    """

    __tablename__ = "pm_coach_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("pm_projects.id", ondelete="CASCADE"))
    card_id: Mapped[str] = mapped_column(String(64))
    target_card_id: Mapped[str] = mapped_column(String(64))
    exit_group: Mapped[str] = mapped_column(String(16))
    entry: Mapped[str] = mapped_column(String(64))
    phase: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phase_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_class: Mapped[str] = mapped_column(String(64))
    requested_remedy: Mapped[str] = mapped_column(String(16))
    remedy: Mapped[str] = mapped_column(String(16))
    applied_remedy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admitted: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_execution_id: Mapped[str] = mapped_column(String(64))
    root_execution_id: Mapped[str] = mapped_column(String(64))
    coach_execution_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "uq_pm_coach_actions_coach_execution",
            "tenant_id",
            "coach_execution_id",
            unique=True,
        ),
        Index(
            "uq_pm_coach_actions_incident",
            "tenant_id",
            "project_id",
            "target_card_id",
            "entry",
            "root_execution_id",
            "exit_group",
            unique=True,
        ),
        Index("ix_pm_coach_actions_project_visit", "tenant_id", "project_id", "phase_visit_at"),
        Index(
            "ix_pm_coach_actions_target_entry",
            "tenant_id",
            "project_id",
            "target_card_id",
            "entry",
        ),
        CheckConstraint(
            f"exit_group IN ({_in_sql(COACH_EXIT_GROUPS)})", name="ck_pm_coach_actions_exit_group"
        ),
        CheckConstraint(
            f"requested_remedy IN ({_in_sql(set(COACH_REMEDY_ORDER) - {'note'})})",
            name="ck_pm_coach_actions_requested_remedy",
        ),
        CheckConstraint(
            f"remedy IN ({_in_sql(set(COACH_REMEDY_ORDER) - {'note'})})",
            name="ck_pm_coach_actions_remedy",
        ),
        CheckConstraint(
            f"applied_remedy IN ({_in_sql(COACH_REMEDY_ORDER)})",
            name="ck_pm_coach_actions_applied_remedy",
        ),
        CheckConstraint(
            "(applied_remedy IS NULL) = (applied_at IS NULL)",
            name="ck_pm_coach_actions_applied_at",
        ),
    )


class CardLedgerEntry(Base):
    """One fact or claim recorded against a Planka card (migration 2026_11_04_0080; append-only).

    ``POST /pm/cards/{planka_card_id}/ledger`` writes it (PM / n8n only); there is no update or
    delete. Keyed by ``(tenant_id, planka_card_id)`` only; it references no other table.
    ``laya_score`` / ``accepted`` / ``reason`` are a claim's verdict and are NULL on every fact.
    """

    __tablename__ = "pm_card_ledger"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    planka_card_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    value: Mapped[Any] = mapped_column(JSONB)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB)
    laya_score: Mapped[float | None] = mapped_column(Double, nullable=True)
    accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "ix_pm_card_ledger_tenant_card_created",
            "tenant_id",
            "planka_card_id",
            "created_at",
            "id",
        ),
        CheckConstraint("kind IN ('fact', 'claim')", name="ck_pm_card_ledger_kind"),
        CheckConstraint(
            "kind = 'claim' OR (laya_score IS NULL AND accepted IS NULL AND reason IS NULL)",
            name="ck_pm_card_ledger_laya_claim_only",
        ),
    )


class Resource(TenantSubjectMixin, Base):
    """PM resource — person, team, equipment, or material. Lightweight, no tsv."""

    __tablename__ = "pm_resources"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    resource_type: Mapped[str] = mapped_column(String(20))
    # Deterministic AA-intake dedup key (cardId + kind + ordinal). A resource
    # title is legitimately non-unique, so title dedup is unsafe; the stable_key
    # is the only durable dedup handle. Resources have no project_id → dedup on
    # (tenant_id, stable_key). NULL for non-AA callers (pure create).
    stable_key: Mapped[str | None] = mapped_column(String(200))
    capacity_per_day: Mapped[float] = mapped_column(Float, default=8.0)
    cost_per_unit: Mapped[float | None] = mapped_column(Float)
    calendar_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    skills: Mapped[list[Any] | None] = mapped_column(JSONB)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    version_status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    supersedes: Mapped[str | None] = mapped_column(String(120))
    superseded_by: Mapped[str | None] = mapped_column(String(120))
    version_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_pm_resources_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_pm_resources_visibility"),
        Index("ix_pm_resources_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_pm_resources_tenant_visibility", "tenant_id", "visibility"),
        # Deterministic AA-intake dedup key (cardId + kind + ordinal). Resources
        # have no project_id, so the key is tenant-scoped; the cardId embedded in
        # stable_key keeps it card/project-specific. Partial on IS NOT NULL so
        # non-AA rows (pure create) are unconstrained.
        Index(
            "uq_pm_resources_tenant_stable_key",
            "tenant_id",
            "stable_key",
            unique=True,
            postgresql_where=text("stable_key IS NOT NULL AND visibility = 'tenant'"),
        ),
    )


# Valid risk categories
RISK_CATEGORIES = frozenset({"technical", "schedule", "cost", "scope", "resource", "external"})
# Valid risk response strategies
RISK_RESPONSE_STRATEGIES = frozenset(
    {"avoid", "mitigate", "transfer", "accept", "exploit", "enhance", "share"}
)
# Valid risk statuses
RISK_STATUSES = frozenset({"identified", "assessed", "mitigated", "closed", "occurred"})


class Risk(QualityMixin, TenantSubjectMixin, Base):
    """PM risk — identified risk with probability/impact scoring and response plan."""

    __tablename__ = "pm_risks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    project_id: Mapped[str] = mapped_column(String(100))
    probability: Mapped[float] = mapped_column(Float)
    impact_score: Mapped[float] = mapped_column(Float)
    risk_score: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(30))
    response_strategy: Mapped[str] = mapped_column(String(20))
    response_plan: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="identified")
    owner: Mapped[str | None] = mapped_column(String(100))
    # Register fields (REG-1, migration 2026_10_27_0067). ``phase`` is a phase NAME from the
    # tenant's ``pm_project_phases`` catalogue (setup | preliminary | execution by default);
    # ``card_id`` is the Planka card that raised the risk.
    phase: Mapped[str | None] = mapped_column(String(64))
    card_id: Mapped[str | None] = mapped_column(String(64))
    raised_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    version_status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    supersedes: Mapped[str | None] = mapped_column(String(120))
    superseded_by: Mapped[str | None] = mapped_column(String(120))
    version_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))",
            persisted=True,
        ),
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_pm_risks_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_pm_risks_visibility"),
        Index("ix_pm_risks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_pm_risks_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_pm_risks_tenant_visibility", "tenant_id", "visibility"),
        # AA-intake get-or-create backstop (WS1, Group A). Partial unique index
        # behind the best-effort router gate; declared here so
        # ``Base.metadata.create_all()`` builds it for the test suite.
        Index(
            "uq_pm_risks_tenant_project_title",
            "tenant_id",
            "project_id",
            "title",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL AND visibility = 'tenant'"),
        ),
    )


# Valid benefit types
BENEFIT_TYPES = frozenset({"financial", "non_financial", "dis_benefit"})
# Valid benefit statuses
BENEFIT_STATUSES = frozenset({"expected", "emerging", "realized", "superseded"})


class Benefit(QualityMixin, TenantSubjectMixin, Base):
    """PM benefit — expected or realized benefit tracked against a programme."""

    __tablename__ = "pm_benefits"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    programme_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_programmes.id", ondelete="SET NULL"), nullable=True
    )
    benefit_type: Mapped[str] = mapped_column(String(20))
    measurement: Mapped[str] = mapped_column(Text)
    baseline_value: Mapped[float | None] = mapped_column(Float)
    target_value: Mapped[float | None] = mapped_column(Float)
    actual_value: Mapped[float | None] = mapped_column(Float)
    realization_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="expected")
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    version_status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    supersedes: Mapped[str | None] = mapped_column(String(120))
    superseded_by: Mapped[str | None] = mapped_column(String(120))
    version_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))",
            persisted=True,
        ),
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_pm_benefits_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_pm_benefits_visibility"),
        Index("ix_pm_benefits_tsv", "tsv", postgresql_using="gin"),
        Index("ix_pm_benefits_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_pm_benefits_tenant_visibility", "tenant_id", "visibility"),
    )
