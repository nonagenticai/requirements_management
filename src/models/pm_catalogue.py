"""PM catalogue — the vocabulary a tenant's projects are composed from.

Four tenant-owned catalogues and the two tables that compose them:

* ``pm_project_phases`` — the phases a project moves through (e.g. the conveyor v3
  spine request → preliminary → contract → execution → validation → release → closed).
* ``pm_capabilities`` — the capabilities involved in projects (project management,
  analysis, development, …).
* ``pm_card_types`` — the kinds of work card a capability performs. Every card type
  has exactly ONE home capability (``capability_id``).
* ``pm_project_types`` — the kinds of project a tenant runs.
* ``pm_project_type_phases`` — the ORDERED phases a project type selects.
* ``pm_project_type_card_types`` — which card types a project type uses in each of its
  phases. The capability is derived through ``card_type.capability_id`` and is never
  stored twice. The composite FK onto ``pm_project_type_phases`` makes it impossible to
  place a card type in a phase the project type has not selected. ``is_entry`` marks the
  card types a phase starts with.
* ``pm_recipes`` / ``pm_recipe_items`` — per (project type, phase[, size]) the card types
  to create and how many; every item must be in the matrix for that phase.
* ``pm_wip_limits`` — how many cards may be in progress, per scope (project type, phase,
  capability, card type; NULL = any). The most specific matching row applies.
* ``pm_priority_levels`` — the priorities a card may carry, ordered by ``rank`` (0 first).
* ``pm_deliverable_kinds`` / ``pm_milestone_kinds`` — what a project delivers and the
  milestones it passes (a milestone kind may mark a phase's exit).
* ``pm_gates`` — a human approval a phase needs before it exits (``has_gate`` on a phase).
* ``pm_board_templates`` — the lists, labels and custom field groups a board starts with.

``name`` is the machine identifier (``bug_analysis``, ``preliminary``) and is unique per
tenant; it is what boards, workflows and fr refer to. ``title`` is the display label.

References between catalogue rows are RESTRICT: deleting a phase, capability or card type
that is still in use is refused (the API answers 409 naming the users) rather than
silently cascading away a project type's composition. Deleting a project type cascades
only its own composition rows.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid7

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    exists,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, column_property, mapped_column

from db import Base, TenantSubjectMixin

# Machine names: lower snake_case, as card types are written on boards and in workflows.
CATALOGUE_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

# How a card type's work counts as landed (``landing_predicates.json`` in ncn).
# landing_predicates.json predicate names a card type's default may carry (migration 0055 added
# asset_persisted). Keep in step with schemas.pm_catalogue.DefinitionOfDone.
DEFINITION_OF_DONE = ("pr_merged", "pr_opened", "asset_promoted", "asset_persisted", "none")
RECIPE_SIZES = ("S", "M", "L")
COUNT_MODES = ("fixed", "per")
# Who approves a gate (the portal role taxonomy plus the project's owner).
APPROVER_ROLES = ("project_owner", "tenant_admin", "member", "super_admin")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class _OwnedColumns(TenantSubjectMixin):
    """Identity, ownership and timestamps: every tenant-owned catalogue row."""

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255))
    subject_id: Mapped[str] = mapped_column(String(255))
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class _CatalogueColumns(_OwnedColumns):
    """Columns shared by the named catalogue tables."""

    name: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)


def _catalogue_args(table: str) -> tuple:
    return (
        UniqueConstraint("tenant_id", "name", name=f"uq_{table}_tenant_name"),
        CheckConstraint("visibility IN ('tenant', 'user')", name=f"ck_{table}_visibility"),
    )


class ProjectPhase(_CatalogueColumns, Base):
    """A phase a project moves through.

    ``output`` is what the phase produces; ``exit_rule`` is when it is done. A project type
    may override both for its own use of the phase (``ProjectTypePhase``).
    """

    __tablename__ = "pm_project_phases"

    output: Mapped[str | None] = mapped_column(Text)
    exit_rule: Mapped[str | None] = mapped_column(Text)
    __table_args__ = _catalogue_args("pm_project_phases")

    if TYPE_CHECKING:
        # Derived: a ``pm_gates`` row exists for this phase (mapped below ``Gate``).
        has_gate: Mapped[bool]


class Capability(_CatalogueColumns, Base):
    """A capability involved in projects; owns card types."""

    __tablename__ = "pm_capabilities"
    __table_args__ = _catalogue_args("pm_capabilities")


class CardType(_CatalogueColumns, Base):
    """A kind of work card. Belongs to exactly one capability."""

    __tablename__ = "pm_card_types"

    capability_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("pm_capabilities.id", ondelete="RESTRICT"), index=True
    )
    # Lists of strings. NULL = never set (the default import fills it); [] = deliberately
    # cleared (the import keeps it). ``none_as_null``: a Python None is SQL NULL, not 'null'.
    inputs: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    outputs: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    acceptance: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    rules: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    # NULL = never set; the API reads it as false.
    human_task: Mapped[bool | None] = mapped_column(Boolean)
    definition_of_done: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        *_catalogue_args("pm_card_types"),
        CheckConstraint(
            f"definition_of_done IS NULL OR {_in('definition_of_done', DEFINITION_OF_DONE)}",
            name="ck_pm_card_types_definition_of_done",
        ),
    )


class ProjectType(_CatalogueColumns, Base):
    """A kind of project; composed of ordered phases and per-phase card types.

    ``retired_at`` (George 2026-11-07): the type may no longer START a project, but every row
    stays — projects, phases and placements keep reading, so history is never deleted.
    """

    __tablename__ = "pm_project_types"

    # NULL = live; set once, by migration 2026_11_07_0083 for ``py2rust``.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = _catalogue_args("pm_project_types")


class ProjectTypePhase(Base):
    """A phase selected by a project type, at a position (0-based, dense)."""

    __tablename__ = "pm_project_type_phases"

    project_type_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("pm_project_types.id", ondelete="CASCADE"), primary_key=True
    )
    phase_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("pm_project_phases.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    # Per-project-type overrides of the phase's output / exit rule (NULL = use the phase's).
    output: Mapped[str | None] = mapped_column(Text)
    exit_rule: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "project_type_id", "position", name="uq_pm_project_type_phases_type_position"
        ),
        Index("ix_pm_project_type_phases_phase", "phase_id"),
    )


class ProjectTypeCardType(Base):
    """A card type a project type uses in one of its selected phases."""

    __tablename__ = "pm_project_type_card_types"

    project_type_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    phase_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    card_type_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("pm_card_types.id", ondelete="RESTRICT"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    # The phase starts with this card type (phases.yaml ``entry_cards``).
    is_entry: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_type_id", "phase_id"],
            ["pm_project_type_phases.project_type_id", "pm_project_type_phases.phase_id"],
            ondelete="CASCADE",
            name="fk_pm_project_type_card_types_selected_phase",
        ),
        Index("ix_pm_project_type_card_types_card_type", "card_type_id"),
    )


class Recipe(_CatalogueColumns, Base):
    """The cards a project type creates in one phase, optionally per size (S/M/L).

    One recipe per (project type, phase, size); ``size`` NULL is the size-independent
    recipe. Every item's card type must be in the project type's matrix for that phase.
    The phase must be one the project type selected — a composite FK onto
    ``pm_project_type_phases``, NO ACTION and DEFERRABLE INITIALLY DEFERRED: ``set_phases``
    deletes and re-inserts the selection inside one transaction, and the API refuses a
    selection that would orphan a recipe with its own 409 before the FK is ever checked.
    """

    __tablename__ = "pm_recipes"

    project_type_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("pm_project_types.id", ondelete="CASCADE"), index=True
    )
    phase_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("pm_project_phases.id", ondelete="RESTRICT"), index=True
    )
    size: Mapped[str | None] = mapped_column(String(1))

    __table_args__ = (
        *_catalogue_args("pm_recipes"),
        UniqueConstraint(
            "tenant_id",
            "project_type_id",
            "phase_id",
            "size",
            name="uq_pm_recipes_type_phase_size",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(f"size IS NULL OR {_in('size', RECIPE_SIZES)}", name="ck_pm_recipes_size"),
        ForeignKeyConstraint(
            ["project_type_id", "phase_id"],
            ["pm_project_type_phases.project_type_id", "pm_project_type_phases.phase_id"],
            name="fk_pm_recipes_selected_phase",
            deferrable=True,
            initially="DEFERRED",
        ),
    )


class RecipeItem(Base):
    """One line of a recipe: a card type and how many of it.

    ``count_mode`` ``fixed`` → ``count`` cards; ``per`` → one card per item of the list
    ``per_field`` names (e.g. ``bug_report.confirmed_bugs``), at most ``cap``.
    """

    __tablename__ = "pm_recipe_items"

    recipe_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("pm_recipes.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_type_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("pm_card_types.id", ondelete="RESTRICT")
    )
    count_mode: Mapped[str] = mapped_column(String(8))
    count: Mapped[int | None] = mapped_column(Integer)
    per_field: Mapped[str | None] = mapped_column(Text)
    cap: Mapped[int | None] = mapped_column(Integer)
    inputs: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    outputs: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)

    __table_args__ = (
        CheckConstraint(
            "(count_mode = 'fixed' AND count IS NOT NULL AND count >= 1 AND per_field IS NULL"
            " AND cap IS NULL)"
            " OR (count_mode = 'per' AND count IS NULL AND per_field IS NOT NULL"
            " AND (cap IS NULL OR cap >= 1))",
            name="ck_pm_recipe_items_count_mode",
        ),
        Index("ix_pm_recipe_items_card_type", "card_type_id"),
    )


# ---------------------------------------------------------------------------
# Tier 2: WIP limits, priority levels, deliverable / milestone kinds, gates, board templates
# ---------------------------------------------------------------------------


class WipLimit(_OwnedColumns, Base):
    """How many cards may be in progress within a scope.

    Each dimension is NULL for "any". One row per scope (NULLS NOT DISTINCT). The limit
    for a card is the most specific matching row: the one with the most non-NULL
    dimensions, ties broken card type > capability > phase > project type.
    """

    __tablename__ = "pm_wip_limits"

    project_type_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_project_types.id", ondelete="CASCADE"), index=True
    )
    phase_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_project_phases.id", ondelete="RESTRICT"), index=True
    )
    capability_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_capabilities.id", ondelete="RESTRICT"), index=True
    )
    card_type_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_card_types.id", ondelete="RESTRICT"), index=True
    )
    wip_limit: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_pm_wip_limits_visibility"),
        CheckConstraint("wip_limit >= 0", name="ck_pm_wip_limits_wip_limit"),
        UniqueConstraint(
            "tenant_id",
            "project_type_id",
            "phase_id",
            "capability_id",
            "card_type_id",
            name="uq_pm_wip_limits_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )


class PriorityLevel(_CatalogueColumns, Base):
    """A priority a card may carry. ``rank`` 0 is the highest; lists sort by rank ascending.

    At most one level per tenant is the default; ``conveyor_may_set`` false reserves the
    level for humans (fr refuses the conveyor setting P0).
    """

    __tablename__ = "pm_priority_levels"

    rank: Mapped[int] = mapped_column(Integer)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    conveyor_may_set: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    __table_args__ = (
        *_catalogue_args("pm_priority_levels"),
        UniqueConstraint("tenant_id", "rank", name="uq_pm_priority_levels_tenant_rank"),
        CheckConstraint("rank >= 0", name="ck_pm_priority_levels_rank"),
        Index(
            "uq_pm_priority_levels_one_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )


class DeliverableKind(_CatalogueColumns, Base):
    """A kind of thing a project delivers."""

    __tablename__ = "pm_deliverable_kinds"

    counts_for_phase_exit: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    __table_args__ = _catalogue_args("pm_deliverable_kinds")


class MilestoneKind(_CatalogueColumns, Base):
    """A milestone a project passes; with ``phase_id`` it is that phase's exit."""

    __tablename__ = "pm_milestone_kinds"

    phase_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_project_phases.id", ondelete="RESTRICT"), index=True
    )
    __table_args__ = _catalogue_args("pm_milestone_kinds")


class Gate(_CatalogueColumns, Base):
    """A human approval a phase needs before it exits."""

    __tablename__ = "pm_gates"

    phase_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("pm_project_phases.id", ondelete="RESTRICT"), index=True
    )
    approver_role: Mapped[str] = mapped_column(String(32))
    quorum: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # NULL = the approval never expires.
    expires_after_hours: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        *_catalogue_args("pm_gates"),
        CheckConstraint(_in("approver_role", APPROVER_ROLES), name="ck_pm_gates_approver_role"),
        CheckConstraint("quorum >= 1", name="ck_pm_gates_quorum"),
        CheckConstraint(
            "expires_after_hours IS NULL OR expires_after_hours > 0",
            name="ck_pm_gates_expires_after_hours",
        ),
    )


class BoardTemplate(_CatalogueColumns, Base):
    """The lists, labels and custom field groups a board starts with.

    ``project_type_id`` NULL = for any project type; deleting the project type sets it NULL.
    """

    __tablename__ = "pm_board_templates"

    project_type_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("pm_project_types.id", ondelete="SET NULL"), index=True
    )
    # [{name, position, closed}] / [{name, color}] / [{name, fields: [str]}].
    # NULL = never set (the default import fills it); [] = deliberately cleared (kept).
    lists: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    labels: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    custom_field_groups: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    __table_args__ = _catalogue_args("pm_board_templates")


# Derived, read with every phase: whether a gate guards the phase's exit.
ProjectPhase.has_gate = column_property(exists().where(Gate.phase_id == ProjectPhase.id))
