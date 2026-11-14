"""Architecture models — Elements, Relationships, Views, and Standards."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from db import Base, TenantSubjectMixin
from models._quality_mixin import QualityMixin

# Valid realisation-binding kinds (used by ``ArchitectureElementRealisationBinding``)
REALISATION_BINDING_KINDS = frozenset(
    {
        "coding_component",  # coding-analysis Component — container + component_id
        "coding_container",  # coding-containers container — container only
        "external_repo",  # non-coding-onboarded git URL — repository_url only
    }
)

# Valid ArchiMate layers
ARCHITECTURE_LAYERS = frozenset(
    {
        "strategy",
        "business",
        "application",
        "technology",
        "physical",
        "implementation_migration",
        "motivation",
        # Location, Grouping and Junction (Archi "Other" / "Connector").
        "other",
    }
)

# Valid architecture states for gap analysis
ARCHITECTURE_STATES = frozenset({"baseline", "target", "transitional"})

# Valid element statuses
ELEMENT_STATUSES = frozenset({"draft", "baseline", "target", "retired"})

# Valid relationship types (ArchiMate + cross-domain + TOC)
RELATIONSHIP_TYPES = frozenset(
    {
        # ArchiMate structural
        "composition",
        "aggregation",
        "assignment",
        "realization",
        # ArchiMate dependency
        "serving",
        "access",
        "influence",
        # ArchiMate dynamic
        "triggering",
        "flow",
        # ArchiMate other
        "specialization",
        "association",
        # Cross-domain
        "traceability",
        # Logical Thinking Process (TOC) edges between toc_nodes
        "toc_necessity",  # "in order to A, we must B" (IO-Map, EC, Prerequisite Tree)
        "toc_sufficiency",  # "if cause then effect" (CRT, FRT, Transition Tree)
        "toc_conflict",  # the wedge of an Evaporating Cloud (D ↔ D')
        "toc_assumption",  # an assumption surfaced beneath a necessity/sufficiency arrow
    }
)


class ArchitectureElement(QualityMixin, TenantSubjectMixin, Base):
    """Universal ArchiMate element covering all layers via layer + element_type discriminator."""

    __tablename__ = "architecture_elements"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    project_id: Mapped[str | None] = mapped_column(String(100), index=True)
    # The model this concept belongs to (target spec §7.1, migration 0069). A write that
    # names none gets its scope's default model (``models/modeling.py`` flush hook).
    model_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("modeling_models.id"), index=True)
    layer: Mapped[str] = mapped_column(String(30))
    element_type: Mapped[str] = mapped_column(String(50))
    properties: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    architecture_state: Mapped[str | None] = mapped_column(String(20))
    parent_id: Mapped[UUID | None] = mapped_column(Uuid)
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Synthetic-identity key: the (container, component_id) code unit a synthetic
    # Artifact was minted from. Denormalised onto the element (its OWN row, no
    # cross-table sync) so the partial unique indexes below can DB-enforce
    # one-synthetic-per-(tenant, project, code-unit). NULL for non-synthetic rows
    # and synthetics minted without a code_binding. See migration
    # 2026_07_18_0036 and find_synthetic_by_code_binding.
    synthetic_binding_container: Mapped[str | None] = mapped_column(String(255))
    synthetic_binding_component_id: Mapped[str | None] = mapped_column(String(255))
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
        UniqueConstraint("tenant_id", "slug", name="uq_arch_elements_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_arch_elements_visibility"),
        Index("ix_arch_elements_tsv", "tsv", postgresql_using="gin"),
        Index("ix_arch_elements_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_arch_elements_tenant_visibility", "tenant_id", "visibility"),
        Index("ix_arch_elements_synthetic", "tenant_id", "is_synthetic"),
        # Synthetic-identity dedup. DB-enforces one tenant-visible synthetic per
        # (tenant, project, code-unit) so the concurrent double-mint race cannot
        # create two synthetics for the same code binding. COALESCE(project_id, '')
        # so null-project synthetics dedup too (a synthetic's identity is its code
        # unit, tenant-wide included). Non-synthetic elements have NO identity index:
        # they are identified by id only and may share a title (A12, migration 0068
        # dropped ``uq_arch_elements_tenant_project_title_type``). text()/postgresql_where required for the
        # COALESCE expression; declared on the model so create_all() builds it for
        # the Postgres test suite (tests do not run alembic).
        Index(
            "uq_arch_elements_synthetic_binding_tenant",
            text("tenant_id"),
            text("COALESCE(project_id, '')"),
            text("synthetic_binding_container"),
            text("synthetic_binding_component_id"),
            unique=True,
            postgresql_where=text(
                "is_synthetic = true AND visibility = 'tenant' "
                "AND synthetic_binding_container IS NOT NULL"
            ),
        ),
        # User-visibility variant: two distinct subjects may each own a private
        # synthetic on the same code unit, so subject_id is part of the key.
        Index(
            "uq_arch_elements_synthetic_binding_user",
            text("tenant_id"),
            text("subject_id"),
            text("COALESCE(project_id, '')"),
            text("synthetic_binding_container"),
            text("synthetic_binding_component_id"),
            unique=True,
            postgresql_where=text(
                "is_synthetic = true AND visibility = 'user' "
                "AND synthetic_binding_container IS NOT NULL"
            ),
        ),
    )


class ArchitectureRelationship(TenantSubjectMixin, Base):
    """Universal edge table for ArchiMate relationships and PM dependencies.

    Polymorphic source/target via entity type discriminators. Source and target
    IDs are application-validated, not DB FK constraints.
    """

    __tablename__ = "architecture_relationships"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    project_id: Mapped[str | None] = mapped_column(String(100), index=True)
    # Set for edges whose both ends are ArchiMate concepts (inherited from the source
    # end); NULL for the PM / TOC / governance links this table also carries.
    model_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("modeling_models.id"), index=True
    )
    source_entity_type: Mapped[str] = mapped_column(String(50))
    source_id: Mapped[UUID] = mapped_column(Uuid)
    target_entity_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[UUID] = mapped_column(Uuid)
    relationship_type: Mapped[str] = mapped_column(String(30))
    name: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    properties: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        UniqueConstraint("tenant_id", "slug", name="uq_arch_rels_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_arch_rels_visibility"),
        Index("ix_arch_rels_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_arch_rels_tenant_visibility", "tenant_id", "visibility"),
        # AA-intake get-or-create backstop (WS1). A relationship has no
        # meaningful title; its identity is the edge tuple. Partial on
        # ``project_id IS NOT NULL`` so non-project edges keep pure-create
        # semantics. Declared on the model so create_all() builds it for tests.
        Index(
            "uq_arch_rels_tenant_edge",
            "tenant_id",
            "project_id",
            "source_entity_type",
            "source_id",
            "target_entity_type",
            "target_id",
            "relationship_type",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL AND visibility = 'tenant'"),
        ),
        # FIX 6 (WS1 hardening): TOC-node edges (``create_toc_edge``) carry NO
        # ``project_id``, so ``uq_arch_rels_tenant_edge`` (partial on
        # ``project_id IS NOT NULL``) can't cover them — a re-POST silently
        # duplicated. This partial index is the identity backstop for the
        # null-project toc edge; the router routes through ``get_or_create``.
        # Declared on the model so create_all() builds it for tests.
        Index(
            "uq_arch_rels_toc_edge",
            "tenant_id",
            "source_entity_type",
            "source_id",
            "target_entity_type",
            "target_id",
            "relationship_type",
            unique=True,
            postgresql_where=text("source_entity_type = 'toc_nodes' AND visibility = 'tenant'"),
        ),
    )


# Valid ArchiMate viewpoints (22 standard)
ARCHITECTURE_VIEWPOINTS = frozenset(
    {
        "organization",
        "business_process_cooperation",
        "product",
        "application_cooperation",
        "application_usage",
        "technology_usage",
        "technology",
        "information_structure",
        "service_realization",
        "implementation_deployment",
        "physical",
        "layered",
        "landscape",
        "capability_map",
        "value_stream",
        "motivation",
        "strategy",
        "migration",
        "project",
        "stakeholder",
        "goal_realization",
        "requirements_realization",
        # Archi 5.10.0 viewpoints added 2026-10-26 (finding A11).
        "application_structure",
        "capability",
        "implementation_migration",
        "outcome_realization",
        "resource",
    }
)

# Valid standard types
STANDARD_TYPES = frozenset(
    {
        "technology_standard",
        "application_standard",
        "data_standard",
        "business_standard",
        "guideline",
    }
)

# Valid standard statuses (tech radar model)
STANDARD_STATUSES = frozenset({"approved", "trial", "assess", "hold", "retired"})

# Valid compliance levels
COMPLIANCE_LEVELS = frozenset({"mandatory", "recommended", "optional"})


class ArchitectureView(QualityMixin, TenantSubjectMixin, Base):
    """Viewpoint-based element/relationship selection for ArchiMate views."""

    __tablename__ = "architecture_views"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    project_id: Mapped[str | None] = mapped_column(String(100), index=True)
    model_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("modeling_models.id"), index=True)
    # NULL = no viewpoint (Archi "None"): unfiltered (migration 0072).
    viewpoint: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # A custom viewpoint of the model (``modeling_models.exchange``), by id (migration 0074).
    viewpoint_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    element_ids: Mapped[list[Any] | None] = mapped_column(JSONB, default=list)
    relationship_ids: Mapped[list[Any] | None] = mapped_column(JSONB, default=list)
    layout_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=dict)
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
        UniqueConstraint("tenant_id", "slug", name="uq_arch_views_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_arch_views_visibility"),
        Index("ix_arch_views_tsv", "tsv", postgresql_using="gin"),
        Index("ix_arch_views_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_arch_views_tenant_visibility", "tenant_id", "visibility"),
    )


class Standard(TenantSubjectMixin, Base):
    """Architecture standard / technology radar entry."""

    __tablename__ = "architecture_standards"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    project_id: Mapped[str | None] = mapped_column(String(100), index=True)
    standard_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="assess")
    compliance_level: Mapped[str] = mapped_column(String(20), default="recommended")
    related_principle_ids: Mapped[list[Any] | None] = mapped_column(JSONB, default=list)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        UniqueConstraint("tenant_id", "slug", name="uq_arch_standards_tenant_slug"),
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_arch_standards_visibility"),
        Index("ix_arch_standards_tsv", "tsv", postgresql_using="gin"),
        Index("ix_arch_standards_tenant_subject", "tenant_id", "subject_id"),
        Index("ix_arch_standards_tenant_visibility", "tenant_id", "visibility"),
    )


class ArchitectureElementRealisationBinding(Base):
    """N-ary realisation binding from an ArchitectureElement to a coding-side
    artifact or external repository.

    Replaces the 1:1 ``ArchitectureElement.properties.code_binding`` JSONB
    with a structured table supporting multiple bindings per element across
    three kinds:

    * ``coding_component`` — coding-analysis Component, keyed by
      ``(container, component_id)``.
    * ``coding_container`` — coding-containers container (repo-level link
      without component-level precision), keyed by ``container`` alone.
    * ``external_repo`` — a non-coding-onboarded git URL (vendor repo,
      future-onboard repo), keyed by ``repository_url``.

    Canonical store: post-T3.2 the legacy ``properties.code_binding`` JSONB
    block is stripped from the element row on write (router-level) and is
    not read by any reader; the alembic migration ``2026_05_27_0027`` clears
    it from historical rows.

    The dedup unique index (declared in alembic only, since it uses
    ``COALESCE(...)`` over nullable columns) prevents two rows of the same
    binding kind from binding the same logical target to an element.
    """

    __tablename__ = "architecture_element_realisation_bindings"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    element_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("architecture_elements.id", ondelete="CASCADE"),
        index=True,
    )
    # Denormalised from the parent element so the structured-table query
    # plan stays cheap when filtering by tenant + container without joining.
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    binding_kind: Mapped[str] = mapped_column(String(32))
    container: Mapped[str | None] = mapped_column(String(255))
    component_id: Mapped[str | None] = mapped_column(String(255))
    repository_url: Mapped[str | None] = mapped_column(Text)
    repository_branch: Mapped[str | None] = mapped_column(String(255))
    properties: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        CheckConstraint(
            (
                "(binding_kind = 'coding_component' AND container IS NOT NULL "
                "AND component_id IS NOT NULL) "
                "OR (binding_kind = 'coding_container' AND container IS NOT NULL "
                "AND component_id IS NULL) "
                "OR (binding_kind = 'external_repo' AND repository_url IS NOT NULL)"
            ),
            name="ck_realisation_binding_kind_shape",
        ),
        Index(
            "ix_realisation_bindings_tenant_container",
            "tenant_id",
            "container",
        ),
        # Dedup unique index keyed on the per-kind shape — partial uniqueness
        # via ``COALESCE(...)`` so nulls in container/component_id/url all
        # collapse to the same key. Declared here (not only in alembic) so
        # ``Base.metadata.create_all()`` in tests creates it too; otherwise
        # the application-level idempotency path in ``attach_binding`` never
        # fires its ``IntegrityError`` branch.
        Index(
            "uq_realisation_binding_dedup",
            text("element_id"),
            text("binding_kind"),
            text("COALESCE(container, '')"),
            text("COALESCE(component_id, '')"),
            text("COALESCE(repository_url, '')"),
            unique=True,
        ),
    )
