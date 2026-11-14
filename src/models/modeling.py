"""Modeling lifecycle tables (target spec §7.1–7.2, §8.1) and the draft-head flush hooks.

* ``modeling_models`` — one model per language; ``head`` is the draft head counter.
* ``modeling_model_revisions`` — sealed, immutable manifests (canonical JSON + sha256).
* ``architecture_view_objects`` / ``architecture_view_connections`` — diagram
  occurrences of ArchiMate concepts and relationships in a view.

Every ArchiMate concept row (``architecture_elements``, ``architecture_views``, and the
``architecture_relationships`` whose both ends are ArchiMate concepts) belongs to a model.
Two ORM flush hooks keep that true on EVERY write path — routers, the synthetic upsert,
batch deletes, PM/TOC code, tests that add rows directly — instead of each call site
remembering it:

* ``before_flush`` gives a new element/view without ``model_id`` the DEFAULT ArchiMate
  model of its scope ``(tenant, project_id, visibility, subject-if-private)`` — created on
  first use, race-safe through ``uq_modeling_models_default_scope`` — and a new ArchiMate
  relationship the model of its source end. It then bumps ``head`` of every model whose
  content the flush changes, ONCE per transaction: ``UPDATE … SET head = head + 1``
  takes the model row lock, so the write and a concurrent seal (which reads under
  ``SELECT … FOR UPDATE``) never interleave. The new head is kept in
  ``session.info`` for the route's ``ETag``.
* ``after_flush`` removes view connections left dangling by the flush's deletes (an
  endpoint object or connection that no longer exists). Objects of a deleted element,
  view or parent object and connections of a deleted relationship or view go by FK
  ``ON DELETE CASCADE``; a connection end may be another connection, which no FK can
  express.

Migration ``2026_10_27_0069``; RLS ``tenant_isolation`` on all four tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from db import Base
from models.architecture import (
    ArchitectureElement,
    ArchitectureElementRealisationBinding,
    ArchitectureRelationship,
    ArchitectureView,
)
from models.trace_kinds import TRACE_KIND_BY_TABLE

MODEL_LANGUAGES = ("archimate", "uml")
DEFAULT_LANGUAGE_VERSION = {"archimate": "3.2", "uml": "2.5.1-p1"}
# Entity types whose relationship rows are ArchiMate concepts' edges (mirrors
# ``services.archimate_integrity.ARCHIMATE_ENDPOINT_TYPES``; kept here so the model
# layer does not import a service).
ARCHIMATE_ENDPOINT_TYPES = frozenset({"architecture_elements", "architecture_relationships"})


class ModelingModel(Base):
    __tablename__ = "modeling_models"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    project_id: Mapped[str | None] = mapped_column(String(100))
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    subject_id: Mapped[str] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(20))
    language_version: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    head: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # The scope's implicit model, used by writes that name no model (backfill + writes
    # predating the lifecycle). At most one per scope and language.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Set by the review/publish slice; no FK (a revision already references its model).
    published_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    # The model's folder tree (snapshot ``organizations``), or NULL = none supplied.
    # Model content: in the manifest, and every write bumps ``head`` (migration 0072).
    organizations: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    # Exchange-format model content (snapshot ``exchange``: properties, metadata, custom
    # viewpoints), or NULL = none (migration 0074). Model content: in the manifest.
    exchange: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        CheckConstraint("visibility IN ('tenant', 'user')", name="ck_modeling_models_visibility"),
        CheckConstraint("language IN ('archimate', 'uml')", name="ck_modeling_models_language"),
        Index("ix_modeling_models_tenant_created", "tenant_id", "created_at"),
        # One default model per scope; a private scope is per subject. Declared on the
        # model so create_all() builds it for the test suite (tests do not run alembic).
        Index(
            "uq_modeling_models_default_scope",
            text("tenant_id"),
            text("COALESCE(project_id, '')"),
            text("visibility"),
            text("(CASE WHEN visibility = 'user' THEN subject_id ELSE '' END)"),
            text("language"),
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )


class ModelingModelRevision(Base):
    __tablename__ = "modeling_model_revisions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    model_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("modeling_models.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(200))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    head: Mapped[int] = mapped_column(Integer)
    sealed_by: Mapped[str] = mapped_column(String(255))
    sealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__: tuple = (
        UniqueConstraint("model_id", "seq", name="uq_modeling_model_revisions_model_seq"),
    )


class ArchitectureViewObject(Base):
    """One diagram object of a view: an element occurrence (Archi
    ``DiagramModelArchimateObject``), a note or a group."""

    __tablename__ = "architecture_view_objects"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    model_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("modeling_models.id"), index=True)
    view_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("architecture_views.id", ondelete="CASCADE"), index=True
    )
    # element | note | group (migration 0071). Notes and groups are visual only.
    kind: Mapped[str] = mapped_column(String(10), default="element", server_default="element")
    # The element shown; set iff kind = 'element'.
    concept_ref: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("architecture_elements.id", ondelete="CASCADE"), index=True
    )
    parent_object_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("architecture_view_objects.id", ondelete="CASCADE"), index=True
    )
    bounds: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    style: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    name: Mapped[str | None] = mapped_column(Text)  # group title
    text: Mapped[str | None] = mapped_column(Text)  # note body
    label: Mapped[str | None] = mapped_column(Text)  # Archi label expression
    border_type: Mapped[str | None] = mapped_column(String(20))
    # Note / group documentation; never on an element occurrence (the element has its own).
    documentation: Mapped[str | None] = mapped_column(Text)
    # Drill-down: a view of the same model this object refers to (migration 0074).
    view_ref: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("architecture_views.id", ondelete="SET NULL"), index=True
    )
    # z-order (migration 0071): the order of the view's object list (position, then id);
    # among siblings a later object is drawn above an earlier one.
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        CheckConstraint(
            "kind IN ('element', 'note', 'group')", name="ck_architecture_view_objects_kind"
        ),
        CheckConstraint(
            "(kind = 'element') = (concept_ref IS NOT NULL)",
            name="ck_architecture_view_objects_concept_iff_element",
        ),
        CheckConstraint(
            "kind <> 'element' OR documentation IS NULL",
            name="ck_architecture_view_objects_documentation_not_element",
        ),
    )


class ArchitectureViewConnection(Base):
    """One occurrence of a relationship in a view (Archi ``DiagramModelArchimateConnection``).

    ``source_object_id`` / ``target_object_id`` name a view object OR another connection
    of the same view (a relationship may end on a relationship), so they carry no FK;
    the ``after_flush`` hook below removes a connection whose end is gone.
    """

    __tablename__ = "architecture_view_connections"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    model_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("modeling_models.id"), index=True)
    view_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("architecture_views.id", ondelete="CASCADE"), index=True
    )
    # relationship | line (migration 0071). A line is a plain visual connection.
    kind: Mapped[str] = mapped_column(
        String(15), default="relationship", server_default="relationship"
    )
    # The relationship shown; set iff kind = 'relationship'.
    relationship_ref: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("architecture_relationships.id", ondelete="CASCADE"), index=True
    )
    source_object_id: Mapped[UUID] = mapped_column(Uuid)
    target_object_id: Mapped[UUID] = mapped_column(Uuid)
    bendpoints: Mapped[list[Any] | None] = mapped_column(JSONB)
    style: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__: tuple = (
        CheckConstraint(
            "kind IN ('relationship', 'line')", name="ck_architecture_view_connections_kind"
        ),
        CheckConstraint(
            "(kind = 'relationship') = (relationship_ref IS NOT NULL)",
            name="ck_architecture_view_connections_relationship_iff_kind",
        ),
    )


# ---------------------------------------------------------------------------
# Part 2 (migration 0070): validation runs, trace links, change sets, review/publish.
# ---------------------------------------------------------------------------


def _ts(**kw: Any) -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


class ModelingValidationRun(Base):
    """One persisted validation of a revision or of the draft head (spec §7.3)."""

    __tablename__ = "modeling_validation_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    model_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("modeling_models.id", ondelete="CASCADE"), index=True
    )
    target: Mapped[str] = mapped_column(String(20))
    revision_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("modeling_model_revisions.id", ondelete="CASCADE"), index=True
    )
    head: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    ruleset_id: Mapped[str] = mapped_column(String(100))
    ruleset_version: Mapped[str] = mapped_column(String(50))
    enabled_rules: Mapped[list[Any]] = mapped_column(JSONB)
    required_rules: Mapped[list[Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20))
    examined: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    clean: Mapped[bool] = mapped_column(Boolean)
    requested_by: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why a run failed (NULL otherwise).
    error: Mapped[str | None] = mapped_column(Text)
    # The process boot that owns a queued/running background run; NULL for sync runs.
    owner_boot_id: Mapped[UUID | None] = mapped_column(Uuid)

    __table_args__: tuple = (
        CheckConstraint(
            "target IN ('revision', 'draft_head')", name="ck_modeling_validation_runs_target"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'complete', 'incomplete', 'failed')",
            name="ck_modeling_validation_runs_status",
        ),
    )


class ModelingValidationFinding(Base):
    __tablename__ = "modeling_validation_findings"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("modeling_validation_runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    rule_id: Mapped[str] = mapped_column(String(50))
    ruleset_id: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(10))
    # 0 error, 1 warning, 2 advice — the spec's "severity desc" order as a column.
    severity_rank: Mapped[int] = mapped_column(Integer)
    targets: Mapped[list[Any]] = mapped_column(JSONB)
    target_ids: Mapped[list[Any]] = mapped_column(JSONB)
    first_target_id: Mapped[str] = mapped_column(String(100))
    path: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__: tuple = (
        Index(
            "ix_modeling_validation_findings_order",
            "run_id",
            "severity_rank",
            "rule_id",
            "first_target_id",
            "seq",
        ),
    )


TRACE_LINK_TYPES = ("realizes", "refines", "traces_to", "satisfies", "implements", "depends_on")


class ModelingTraceLink(Base):
    """An immutable typed link (spec §7.4). ``model_id`` = the source concept's model."""

    __tablename__ = "modeling_trace_links"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    subject_id: Mapped[str] = mapped_column(String(255))
    visibility: Mapped[str] = mapped_column(String(16), default="tenant", server_default="tenant")
    model_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("modeling_models.id", ondelete="CASCADE"), index=True
    )
    source_kind: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    target_kind: Mapped[str | None] = mapped_column(String(30))
    target_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    target_code: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    link_type: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime | None] = _ts()

    __table_args__: tuple = (
        CheckConstraint(
            "visibility IN ('tenant', 'user')", name="ck_modeling_trace_links_visibility"
        ),
        CheckConstraint(
            "(target_id IS NOT NULL AND target_kind IS NOT NULL AND target_code IS NULL) "
            "OR (target_id IS NULL AND target_kind IS NULL AND target_code IS NOT NULL)",
            name="ck_modeling_trace_links_one_target",
        ),
        CheckConstraint(
            "link_type IN ('realizes', 'refines', 'traces_to', 'satisfies', 'implements', "
            "'depends_on')",
            name="ck_modeling_trace_links_type",
        ),
    )


class ModelingChangeSet(Base):
    """A proposed set of model changes, validated in memory, applied atomically (§9.5)."""

    __tablename__ = "modeling_change_sets"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    model_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("modeling_models.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    ops: Mapped[list[Any]] = mapped_column(JSONB)
    rationale: Mapped[str] = mapped_column(Text)
    evaluator_ref: Mapped[str | None] = mapped_column(String(300))
    base_head: Mapped[int] = mapped_column(Integer)
    findings: Mapped[list[Any]] = mapped_column(JSONB)
    blocking_findings: Mapped[list[Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime | None] = _ts()
    applied_by: Mapped[str | None] = mapped_column(String(255))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_head: Mapped[int | None] = mapped_column(Integer)
    id_map: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rejected_by: Mapped[str | None] = mapped_column(String(255))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__: tuple = (
        CheckConstraint(
            "status IN ('proposed', 'invalid', 'applied', 'rejected')",
            name="ck_modeling_change_sets_status",
        ),
    )


class ModelingReview(Base):
    """A review decision on one sealed revision; actor from the principal (plan §7.3)."""

    __tablename__ = "modeling_reviews"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    model_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("modeling_models.id", ondelete="CASCADE"), index=True
    )
    revision_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("modeling_model_revisions.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime | None] = _ts()

    __table_args__: tuple = (
        CheckConstraint("decision IN ('approve', 'reject')", name="ck_modeling_reviews_decision"),
    )


class ModelingPublication(Base):
    """Publication history: which revision was published, by whom, superseding which."""

    __tablename__ = "modeling_publications"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    model_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("modeling_models.id", ondelete="CASCADE"), index=True
    )
    revision_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("modeling_model_revisions.id", ondelete="CASCADE")
    )
    supersedes_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    review_id: Mapped[UUID] = mapped_column(Uuid)
    validation_run_id: Mapped[UUID] = mapped_column(Uuid)
    published_by: Mapped[str] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = _ts()


# ---------------------------------------------------------------------------
# Draft head: default-model assignment and the once-per-transaction head bump.
# ---------------------------------------------------------------------------

# session.info keys. BUMPED is per transaction (cleared when the outermost transaction
# ends, and on ANY rollback — a savepoint's included — so a rolled-back bump can never
# hide a needed one; the cost is at most an extra increment). HEADS keeps the last head
# this session wrote per model; ETAG_MODEL names the model the request writes (the one
# its If-Match is checked against), so ``ETag`` reports THAT model's head even when one
# write bumps several models (a re-pointed edge, a pruned cross-model trace link).
BUMPED = "modeling_bumped"
HEADS = "modeling_heads"
ETAG_MODEL = "modeling_etag_model"


def note_etag_model(session: Any, model_id: Any) -> None:
    """Record the request's target model for ``ETag`` (the first one named wins)."""
    if model_id is not None:
        session.info.setdefault(ETAG_MODEL, UUID(str(model_id)))


def etag_head(session: Any) -> int | None:
    """The head ``ETag`` reports: the target model's, else the only model bumped."""
    heads: dict[UUID, int] = session.info.get(HEADS) or {}
    target = session.info.get(ETAG_MODEL)
    if target is not None:
        return heads.get(target)
    if len(heads) == 1:
        return next(iter(heads.values()))
    return None


# Columns whose change is not model content: heuristic quality scoring and bookkeeping.
_NON_CONTENT = frozenset(
    {
        "quality_score",
        "quality_issues",
        "quality_suggestions",
        "last_evaluated_at",
        "last_evaluated_by_agent",
        "updated_at",
        "tsv",
    }
)
_CONTENT_CLASSES = (
    ArchitectureElement,
    ArchitectureView,
    ArchitectureRelationship,
    ArchitectureViewObject,
    ArchitectureViewConnection,
    ModelingTraceLink,
)


def subject_key(visibility: str | None, subject_id: str | None, tenant_id: str) -> str:
    """The subject part of a default-model scope: '' for tenant-visible rows."""
    if (visibility or "tenant") != "user":
        return ""
    return subject_id or str(tenant_id)


def default_model_id(
    session: Session,
    *,
    tenant_id: str,
    project_id: str | None,
    visibility: str | None,
    subject_id: str | None,
    language: str = "archimate",
) -> UUID:
    """The scope's default model id, created on first use (sync; runs inside flush hooks).

    ``ON CONFLICT DO NOTHING`` then a re-SELECT: a concurrent creator of the same default
    blocks this INSERT on the unique index until it commits, after which the SELECT (a
    new READ COMMITTED snapshot) sees its row.
    """
    vis = visibility or "tenant"
    skey = subject_key(vis, subject_id, tenant_id)
    owner = skey or (subject_id or str(tenant_id))
    project = project_id or None
    conn = session.connection()
    params = {
        "tenant_id": str(tenant_id),
        "project_id": project,
        "visibility": vis,
        "subject_id": owner,
        "skey": skey,
        "language": language,
    }
    created = conn.execute(
        text(
            "INSERT INTO modeling_models (id, tenant_id, project_id, visibility, subject_id, "
            "language, language_version, title, description, head, is_default) "
            "VALUES (:id, CAST(:tenant_id AS uuid), :project_id, :visibility, :subject_id, "
            ":language, :language_version, :title, :description, 0, true) "
            "ON CONFLICT DO NOTHING RETURNING id"
        ),
        {
            **params,
            "id": uuid7(),
            "language_version": DEFAULT_LANGUAGE_VERSION[language],
            "title": f"Default {language} model" + (f" ({project})" if project else ""),
            "description": "Implicit model of writes that name no model_id",
        },
    ).scalar_one_or_none()
    if created is not None:
        return created
    return conn.execute(
        text(
            "SELECT id FROM modeling_models WHERE is_default AND tenant_id = CAST(:tenant_id AS uuid) "
            "AND COALESCE(project_id, '') = COALESCE(CAST(:project_id AS varchar), '') "
            "AND visibility = :visibility "
            "AND (CASE WHEN visibility = 'user' THEN subject_id ELSE '' END) = :skey "
            "AND language = :language"
        ),
        params,
    ).scalar_one()


def _end_model_id(session: Session, entity_type: str, ref: Any) -> UUID | None:
    """``model_id`` of an ArchiMate relationship end (a pending row in this flush first)."""
    cls = (
        ArchitectureElement if entity_type == "architecture_elements" else ArchitectureRelationship
    )
    for obj in session.new:
        if isinstance(obj, cls) and str(obj.id) == str(ref):
            return obj.model_id
    table = cls.__tablename__
    return (
        session.connection()
        .execute(text(f"SELECT model_id FROM {table} WHERE id = :id"), {"id": UUID(str(ref))})
        .scalar_one_or_none()
    )


def _is_archimate_edge(rel: ArchitectureRelationship) -> bool:
    return (
        rel.source_entity_type in ARCHIMATE_ENDPOINT_TYPES
        and rel.target_entity_type in ARCHIMATE_ENDPOINT_TYPES
    )


def _assign_default(session: Session, obj: Any) -> None:
    obj.model_id = default_model_id(
        session,
        tenant_id=obj.tenant_id,
        project_id=obj.project_id,
        visibility=obj.visibility,
        subject_id=obj.subject_id,
    )


def _derive_relationship_model(session: Session, obj: ArchitectureRelationship) -> None:
    obj.model_id = _end_model_id(session, obj.source_entity_type, obj.source_id)
    if obj.model_id is None:
        _assign_default(session, obj)


_END_ATTRS = ("source_entity_type", "source_id", "target_entity_type", "target_id")


def _ends_changed(obj: ArchitectureRelationship) -> bool:
    state = inspect(obj)
    return any(state.attrs[k].history.has_changes() for k in _END_ATTRS)


def _content_changed(obj: Any) -> bool:
    state = inspect(obj)
    return any(attr.key not in _NON_CONTENT and attr.history.has_changes() for attr in state.attrs)


@event.listens_for(Session, "before_flush")
def _modeling_before_flush(session: Session, _flush_context: Any, _instances: Any) -> None:
    touched: set[UUID] = set()
    # Elements and views first: a new relationship may end on a new element.
    for obj in list(session.new):
        if isinstance(obj, (ArchitectureElement, ArchitectureView)) and obj.model_id is None:
            _assign_default(session, obj)
    for obj in list(session.new):
        if (
            isinstance(obj, ArchitectureRelationship)
            and obj.model_id is None
            and _is_archimate_edge(obj)
        ):
            _derive_relationship_model(session, obj)
    # Re-pointed ends re-derive the model (the write gate has already refused ends in
    # two different models, AM-REF-003); the model it leaves is bumped too.
    for obj in list(session.dirty):
        if isinstance(obj, ArchitectureRelationship) and _ends_changed(obj):
            if obj.model_id is not None:
                touched.add(obj.model_id)
            obj.model_id = None
            if _is_archimate_edge(obj):
                _derive_relationship_model(session, obj)
    # Realisation bindings are manifest content of their element's model.
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(obj, ArchitectureElementRealisationBinding):
            model_id = _end_model_id(session, "architecture_elements", obj.element_id)
            if model_id is not None:
                touched.add(model_id)
    for obj in session.new:
        if isinstance(obj, _CONTENT_CLASSES) and obj.model_id is not None:
            touched.add(obj.model_id)
    for obj in session.dirty:
        if isinstance(obj, _CONTENT_CLASSES) and obj.model_id is not None and _content_changed(obj):
            touched.add(obj.model_id)
    for obj in session.deleted:
        if isinstance(obj, _CONTENT_CLASSES) and obj.model_id is not None:
            touched.add(obj.model_id)
    bump_heads(session, touched)


def bump_heads(session: Session, model_ids: set[UUID]) -> None:
    """Increment ``head`` of ``model_ids`` not yet bumped in this transaction (sync)."""
    bumped: set[UUID] = session.info.setdefault(BUMPED, set())
    todo = sorted({UUID(str(m)) for m in model_ids} - bumped)
    if not todo:
        return
    rows = (
        session.connection()
        .execute(
            text(
                "UPDATE modeling_models SET head = head + 1, updated_at = now() "
                "WHERE id = ANY(:ids) RETURNING id, head"
            ),
            {"ids": todo},
        )
        .all()
    )
    heads: dict[UUID, int] = session.info.setdefault(HEADS, {})
    for model_id, head in rows:
        heads[UUID(str(model_id))] = head
    bumped.update(todo)


async def bump_models(session: Any, model_ids: Any) -> None:
    """Async entry point of :func:`bump_heads` for Core/raw-SQL write paths."""
    ids = {m for m in model_ids if m is not None}
    if ids:
        await session.run_sync(lambda s: bump_heads(s, ids))


async def bump_element_models(session: Any, element_ids: Any) -> None:
    """Bump the models of ``element_ids`` (a Core write touched their manifest content)."""
    ids = [UUID(str(e)) for e in element_ids]
    if not ids:
        return
    rows = await session.execute(
        text("SELECT DISTINCT model_id FROM architecture_elements WHERE id = ANY(:ids)"),
        {"ids": ids},
    )
    await bump_models(session, {r[0] for r in rows})


_OCCURRENCE_OWNERS = (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    ArchitectureViewObject,
    ArchitectureViewConnection,
)
# Connection chains are short in practice; the bound only guards a malformed graph.
_PRUNE_ROUNDS = 50


@event.listens_for(Session, "after_flush")
def _modeling_after_flush(session: Session, _flush_context: Any) -> None:
    models = {
        obj.model_id
        for obj in session.deleted
        if isinstance(obj, _OCCURRENCE_OWNERS) and obj.model_id is not None
    }
    if models:
        prune_dangling_connections(session, models)
    ends = [obj.id for obj in session.deleted if _trace_kind(obj) is not None]
    if ends:
        prune_trace_links(session, ends)


def _trace_kind(obj: Any) -> str | None:
    return TRACE_KIND_BY_TABLE.get(getattr(obj, "__tablename__", ""))


def prune_trace_links(session: Session, ids: list[Any]) -> int:
    """Delete trace links whose source or target was deleted (sync; ends never dangle).

    A pruned link is manifest content of ITS model — which may be another model than the
    deleted end's (a link from model B to an element of model A) — so every such model's
    head is bumped.
    """
    uuids = sorted({UUID(str(i)) for i in ids})
    rows = (
        session.connection()
        .execute(
            text(
                "DELETE FROM modeling_trace_links WHERE source_id = ANY(:ids) "
                "OR target_id = ANY(:ids) RETURNING id, model_id"
            ),
            {"ids": uuids},
        )
        .all()
    )
    bump_heads(session, {r.model_id for r in rows if r.model_id is not None})
    return len(rows)


async def prune_trace_links_async(session: Any, ids: list[Any]) -> None:
    if ids:
        await session.run_sync(lambda s: prune_trace_links(s, ids))


def prune_dangling_connections(session: Session, model_ids: set[UUID]) -> int:
    """Delete connections of ``model_ids`` whose source or target occurrence is gone (sync)."""
    ids = sorted({UUID(str(m)) for m in model_ids})
    total = 0
    for _ in range(_PRUNE_ROUNDS):
        deleted = (
            session.connection()
            .execute(
                text(
                    "DELETE FROM architecture_view_connections c WHERE c.model_id = ANY(:ids) "
                    "AND (NOT EXISTS (SELECT 1 FROM architecture_view_objects o "
                    "  WHERE o.id = c.source_object_id AND o.view_id = c.view_id) "
                    " AND NOT EXISTS (SELECT 1 FROM architecture_view_connections x "
                    "  WHERE x.id = c.source_object_id AND x.view_id = c.view_id) "
                    " OR NOT EXISTS (SELECT 1 FROM architecture_view_objects o "
                    "  WHERE o.id = c.target_object_id AND o.view_id = c.view_id) "
                    " AND NOT EXISTS (SELECT 1 FROM architecture_view_connections x "
                    "  WHERE x.id = c.target_object_id AND x.view_id = c.view_id)) "
                    "RETURNING c.id"
                ),
                {"ids": ids},
            )
            .all()
        )
        if not deleted:
            break
        total += len(deleted)
    return total


@event.listens_for(Session, "after_soft_rollback")
def _modeling_rollback(session: Session, _previous_transaction: Any) -> None:
    # Any rollback — a savepoint's included — may have undone a bump: forget it, so the
    # next flush bumps again, and never report it as an ETag.
    session.info.pop(BUMPED, None)
    session.info.pop(HEADS, None)


@event.listens_for(Session, "after_transaction_end")
def _modeling_txn_end(session: Session, transaction: Any) -> None:
    # Only the outermost transaction: a released savepoint (EntityService's slug-retry
    # SAVEPOINT) is still inside the transaction that holds the bump and the row lock.
    if transaction.parent is None:
        session.info.pop(BUMPED, None)
