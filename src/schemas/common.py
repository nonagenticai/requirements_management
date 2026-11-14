"""Shared mixins, error schemas, and search schemas."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)


def _empty_scope_to_none(v: str | None) -> str | None:
    """Normalise an empty/whitespace project scope to ``None`` (= no project).

    The AA get-or-create gate is ``if payload.project_id:`` (falsy for ``""``),
    which routes an empty scope to a plain create — but the partial unique
    index is ``WHERE project_id IS NOT NULL`` and ``'' IS NOT NULL`` is TRUE, so
    a duplicate ``""``-scoped create would raise an uncaught ``IntegrityError``
    → HTTP 500. Coercing ``""`` → ``None`` keeps the gate and the index in
    agreement: an empty scope means "no project" everywhere. (str_strip_whitespace
    runs first, so a whitespace-only value is already ``""`` by the time this runs.)
    """
    return v or None


# A nullable project-scope column that treats an empty string as "no scope".
ProjectScope = Annotated[str | None, AfterValidator(_empty_scope_to_none)]

# A nullable AA stable dedup key that treats an empty/whitespace string as "no
# key". Same rationale as ProjectScope above: the get-or-create gate is
# ``if payload.stable_key:`` (falsy for ``""``) while the partial unique index is
# ``WHERE stable_key IS NOT NULL``, and ``'' IS NOT NULL`` is TRUE — so coercing
# ``""`` → ``None`` keeps the Python gate and the SQL predicate in agreement. The
# n8n AA-intake node derives this deterministically (cardId + kind + ordinal) so
# it survives re-runs, unlike the LLM-authored title.
StableKey = Annotated[str | None, AfterValidator(_empty_scope_to_none)]


def lower_strip(v: Any) -> Any:
    """Reproduce the pre-``Literal`` ``v.strip().lower()`` coercion.

    Every membership validator that a ``Literal`` replaced normalised BEFORE
    checking, so ``layer="Business"`` has always been accepted and coerced. A
    bare ``Literal`` would newly reject it — a real regression for existing
    callers — so the normalisation is preserved as a ``mode="before"`` validator
    and the ``Literal`` does only the membership check. Non-``str`` input is
    passed through untouched so pydantic reports the usual type error, and
    ``None`` survives for optional fields.

    NOTE: ``str_strip_whitespace=True`` in ``model_config`` does NOT reach a
    ``Literal`` field (it is a property of the *str* core schema), which is the
    other reason the strip has to happen here.

    Lives here, not in ``schemas.architecture``, because ``schemas.tools``
    needs the identical coercion and the two modules must not import each
    other. ⛔ The two halves of one vocabulary disagreeing about case is a real
    defect: ``viewpoint="Layered"`` was accepted by ``POST /architecture/views``
    and rejected by ``POST /architecture/tools/generate-view``.
    """
    return v.strip().lower() if isinstance(v, str) else v


# ---------------------------------------------------------------------------
# Version-status vocabulary — ONE source of truth
# ---------------------------------------------------------------------------
# The closed set used to live as an unnamed literal set inside
# ``VersionFieldsMixin.validate_version_status``'s body, so nothing outside that
# function could reference it. Naming it lets the request-side ``Literal`` and
# the response-side runtime check share a single definition, and lets a test
# assert the two never drift apart.

# ``SUPERSEDED`` (G8 / R313, 2026-10-19): a project copy whose content was promoted into the
# tenant original (``POST /{kind}/{copy_id}/promote``). It is a merged state: the copy stays
# readable (history) but no longer shadows the original in a project list.
#
# ⛔ It is SERVER-ONLY: readable on every ``*Out`` (so a promoted copy never 500s a read) and
# NOT in the request ``VersionStatus`` Literal, so no client can mark a row SUPERSEDED (which
# would hide a copy from its project and freeze it). Only ``promote`` writes it. That is the
# SAFE direction of an In/Out difference — accepted on read, refused on write — the reverse
# of the drift ``test_version_status_vocabulary.py`` exists to stop.
SERVER_ONLY_VERSION_STATUSES: frozenset[str] = frozenset({"SUPERSEDED"})
VERSION_STATUSES: frozenset[str] = frozenset(
    {"DRAFT", "PUBLISHED", "DEPRECATED"} | SERVER_ONLY_VERSION_STATUSES
)

# The same vocabulary as a type. A ``Literal`` is what puts a TOP-LEVEL ``enum``
# into the generated OpenAPI property, which is the only place the downstream
# n8n node generator looks (``generate-node.ts:843``). A ``field_validator``
# alone returns a correct 422 but emits no ``enum`` at all, so every generated
# client renders the field as a free-text box.
#
# ⛔ Never spell this as ``VersionStatus | None``. Pydantic emits an optional
# Literal as ``anyOf: [{enum: [...]}, {"type": "null"}]`` — the enum is then
# NESTED, the generator cannot see it, and the field silently degrades back to
# free text. Non-optional with a default is the shape that works.
VersionStatus = Literal["DRAFT", "PUBLISHED", "DEPRECATED"]


# ---------------------------------------------------------------------------
# Shared mixins
# ---------------------------------------------------------------------------


class VersionFieldsMixin(BaseModel):
    """Version fields shared by all entity schemas — the RESPONSE-side base.

    ``version_status`` stays a plain ``str`` here — but be precise about WHY,
    because the obvious reason is the wrong one for THIS field.

    ⛔ It is NOT "a ``Literal`` here would newly reject legacy rows". It would
    not. ``validate_version_status`` below lives on THIS class, so every ``*Out``
    model that inherits the mixin already enforces ``VERSION_STATUSES`` on the
    read path today: ``VersionFieldsMixin.model_validate({"version_status":
    "created"})`` raises right now, ``Literal`` or no ``Literal``. The
    out-of-vocabulary rows in ``entity_version_history.version_status`` —
    ``created`` (34,828) and ``updated`` (5,011) — are already unparseable by
    this base, and no schema serves that table today. Narrowing to
    ``VersionStatus`` would change the ERROR SHAPE (``ValueError`` message vs
    pydantic's ``literal_error``), not the accept/reject set.

    ✅ What the In/Out split actually buys for ``version_status`` is the **enum
    in the published spec**: ``VersionFieldsIn`` narrows the field to the
    ``Literal``, which is the only thing that emits a top-level ``enum`` into the
    OpenAPI property, so generated clients render a dropdown instead of a
    free-text box. The response side gains nothing from that and keeps the
    plainer annotation.

    ⚠️ The "a ``Literal`` on a response model is a NEW constraint that could 500
    a read" argument is real, but it belongs to the fields declared DIRECTLY on
    ``*Out`` classes with no validator behind them — ``layer``,
    ``element_type``, ``status`` and their peers. Those genuinely are tolerant
    today: ``ArchitectureElementOut(layer="NOT_A_LAYER")`` constructs fine.
    Tightening one of THOSE is a data-migration decision, not a typing tidy-up.
    Do not transplant that reasoning onto ``version_status``.
    """

    version: str = Field(default="1.0.0", max_length=20, description="Version")
    version_status: str = Field(
        default="DRAFT",
        max_length=20,
        description="Status: DRAFT, PUBLISHED, DEPRECATED, or SUPERSEDED (server-only)",
    )
    supersedes: str | None = Field(None, max_length=120, description="Superseded entity slug")
    superseded_by: str | None = Field(None, max_length=120, description="Superseding entity slug")
    version_notes: str | None = Field(None, description="Version notes")
    approved_by: str | None = Field(None, max_length=100, description="Approver")
    approved_at: datetime | None = Field(None, description="Approval timestamp")
    published_at: datetime | None = Field(None, description="Publication timestamp")

    @field_validator("version_status")
    @classmethod
    def validate_version_status(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.strip().upper()
        if v not in VERSION_STATUSES:
            raise ValueError(
                f"Version status must be one of: {', '.join(sorted(VERSION_STATUSES))}"
            )
        return v


class SystemInheritanceOutMixin(BaseModel):
    """RESPONSE-only ``is_system`` flag for tables that inherit system rows.

    ``True`` when the row is owned by the SYSTEM+IMPROVEMENT tenant
    (``settings.system_tenant_id``) — i.e. it reached a non-system caller
    through ``scope(include_system=True)`` and is READ-ONLY to them. Computed
    from the source object's ``tenant_id`` (ORM row or dict), so it needs no
    column and cannot be spoofed by a client.

    ⛔ Never mix this into an ``*In`` schema (nor into ``VersionFieldsMixin``,
    which ``VersionFieldsIn`` inherits): ``EntityService.create`` passes every
    ``*In`` field to the ORM constructor, and ``is_system`` is not a column.
    """

    is_system: bool = Field(
        default=False,
        description="True if this row is inherited from the system tenant (read-only to you)",
    )

    @model_validator(mode="wrap")
    @classmethod
    def _stamp_is_system(cls, data: Any, handler: Any) -> Any:
        out = handler(data)
        if isinstance(data, dict):
            tenant = data.get("tenant_id")
        else:
            tenant = getattr(data, "tenant_id", None)
        if tenant is not None:
            # Lazy import: schemas must not pull settings at module import.
            from repositories.visibility import same_tenant
            from settings import get_settings

            out.is_system = same_tenant(tenant, get_settings().system_tenant_id)
        return out


class VersionFieldsIn(VersionFieldsMixin):
    """Version fields for REQUEST (``*In``) schemas — enum-publishing variant.

    Inheritance rule for this repo:

    * ``*In``  (request)  models inherit **this** class → OpenAPI property gets a
      top-level ``enum``, so generated clients render a dropdown.
    * ``*Out`` (response) models inherit :class:`VersionFieldsMixin` → the field
      stays ``str`` at the TYPE level.

    ⛔ Do NOT justify that second bullet as "so a legacy out-of-set row can never
    500 a read". For **this** field that is false, and it was written here once
    already: ``validate_version_status`` lives on :class:`VersionFieldsMixin`
    itself, so response models inherit it and ALREADY reject an out-of-set value
    — measured, ``VersionFieldsMixin.model_validate({"version_status": "created"})``
    raises today. The split here buys the **enum in the spec**, nothing more.

    The legacy-tolerance argument is real, but it belongs to the fields declared
    DIRECTLY on ``*In`` classes — ``layer``, ``element_type``, ``status`` and
    their peers — which carry no validator on the ``*Out`` side. Measured:
    ``ArchitectureElementOut(layer="NOT_A_LAYER", ...)`` constructs fine.

    Normalisation is preserved: ``" draft "`` still round-trips to ``"DRAFT"``.
    The ``mode="before"`` validator does the ``.strip().upper()`` (guarded by
    ``isinstance``, exactly as the base validator is) and the ``Literal`` does
    the membership check — so a bare ``Literal`` never gets the chance to reject
    input the old validator accepted.
    """

    # The narrowing str -> VersionStatus IS the request/response split described
    # above (tests/test_version_status_vocabulary.py pins both halves). ty flags
    # the narrowed override as unsound for writes through a base-typed reference;
    # nothing assigns ``version_status`` on a request model after validation.
    version_status: VersionStatus = Field(  # ty: ignore[invalid-mutable-override]
        default="DRAFT",
        description="Status: DRAFT, PUBLISHED, or DEPRECATED",
    )

    @field_validator("version_status", mode="before")
    @classmethod
    def normalise_version_status(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().upper()
        return v


class TimestampMixin(BaseModel):
    """Read-only timestamp fields for Out schemas."""

    created_at: datetime | None = Field(None, description="Created")
    updated_at: datetime | None = Field(None, description="Updated")


# ---------------------------------------------------------------------------
# Asset-bindings provenance
# ---------------------------------------------------------------------------
# Plan ref: ~/.claude/plans/fr-asset-bindings-and-reuse.md (Phase B / G).
# Origin captures immutable provenance of an asset (where it was
# created); bindings (cross-asset table in fr-backend) capture the
# mutable many-to-many to projects.
#
# OriginFieldsInMixin is for *In* schemas — adds the model_validator
# that enforces origin_kind/identifier consistency, mirroring the
# CHECK constraint at the DB.
# OriginFieldsOutMixin is for *Out* schemas — just exposes the fields.


class OriginFieldsOutMixin(BaseModel):
    """Read-only origin fields for Out schemas."""

    origin_kind: str = Field(
        default="tenant",
        description="Provenance: tenant | project | workflow",
    )
    origin_project_id: UUID | None = Field(
        default=None, description="Set when origin_kind='project'"
    )
    origin_workflow_run_id: str | None = Field(
        default=None, max_length=200, description="Set when origin_kind='workflow'"
    )
    created_by_user_id: str = Field(
        default="", max_length=255, description="Subject id of the creator"
    )


class OriginFieldsInMixin(BaseModel):
    """Origin fields for In schemas — with consistency model_validator."""

    origin_kind: str = Field(
        default="tenant",
        pattern="^(tenant|project|workflow)$",
        description="Provenance: tenant | project | workflow",
    )
    origin_project_id: UUID | None = Field(
        default=None, description="Required when origin_kind='project'"
    )
    origin_workflow_run_id: str | None = Field(
        default=None,
        max_length=200,
        description="Required when origin_kind='workflow' (n8n run id)",
    )
    created_by_user_id: str = Field(
        default="",
        max_length=255,
        description="Subject id of the creator (empty for legacy rows)",
    )

    @model_validator(mode="after")
    def check_origin_consistency(self) -> "OriginFieldsInMixin":
        if self.origin_kind == "tenant":
            if self.origin_project_id is not None or self.origin_workflow_run_id is not None:
                raise ValueError(
                    "origin_kind='tenant' requires origin_project_id and origin_workflow_run_id to be null"
                )
        elif self.origin_kind == "project":
            if self.origin_project_id is None:
                raise ValueError("origin_kind='project' requires origin_project_id")
            if self.origin_workflow_run_id is not None:
                raise ValueError("origin_kind='project' must not carry origin_workflow_run_id")
        elif self.origin_kind == "workflow":
            if self.origin_workflow_run_id is None:
                raise ValueError("origin_kind='workflow' requires origin_workflow_run_id")
        return self


# ---------------------------------------------------------------------------
# Error / utility schemas
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    """Inner error payload — canonical fleet envelope."""

    code: str = Field(..., description="Stable machine-readable error code")
    message: str = Field(..., description="Human-readable error description")
    status: int = Field(..., description="HTTP status code")
    request_id: str | None = Field(default=None, description="Request UUID7 for log correlation")
    timestamp: str | None = Field(
        default=None, description="ISO-8601 UTC timestamp the error was emitted"
    )
    details: str | None = Field(
        default=None,
        description=(
            "Diagnostic detail. On request-validation failures (422) this is always "
            "populated: a bounded list of redacted `loc:type` pairs naming which fields "
            "failed and how — never the values submitted — truncated with a `(+N more)` "
            "marker when there are more errors than the cap. On every other error path it "
            "carries internal exception text and is populated only in development "
            "(LEAK_EXCEPTION_DETAILS=true, or ENVIRONMENT=development/dev, which "
            "enables the same flag)."
        ),
    )
    findings: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Present only when code is `model_integrity`: the error-severity findings "
            "(rule_id, ruleset_id, severity, targets, path, message, evidence) that "
            "refused the write. Same shape as a validation run's findings."
        ),
    )
    head: int | None = Field(
        default=None,
        description="Present only when code is `stale_head` (412): the model's current head.",
    )


class ErrorResponse(BaseModel):
    """Standard error response envelope."""

    error: ErrorDetail


# ---------------------------------------------------------------------------
# Delete acknowledgements
# ---------------------------------------------------------------------------


class DeleteResult(BaseModel):
    """Body of a successful single-row DELETE: ``{"status": "deleted", "id": <uuid>}``.

    Documents what the handlers already return (``EntityService.delete``,
    ``RelationshipService.delete``, ``modeling_models.delete_model``,
    ``trace_links.delete_link`` and the hand-built acknowledgements of the view
    object/connection and TOC node routes), so a client generated from
    ``/openapi.json`` gets a typed body instead of ``unknown``.
    """

    status: Literal["deleted"] = Field(..., description="Always 'deleted'")
    id: UUID = Field(..., description="ID of the deleted row")


class TocTreeDeleteResult(DeleteResult):
    """``DELETE /toc/{tree_id}``: the tree went, and with it this many nodes."""

    nodes_deleted: int = Field(..., ge=0, description="Nodes deleted with the tree")


# ---------------------------------------------------------------------------
# Search schemas
# ---------------------------------------------------------------------------


class SearchHit(BaseModel):
    """A single search result."""

    model_config = ConfigDict(str_strip_whitespace=True)

    id: UUID = Field(..., description="Entity ID")
    slug: str = Field(..., min_length=1, description="Slug")
    title: str = Field(..., min_length=1, description="Title")
    snippet: str = Field(default="", description="Text snippet")
    similarity: float | None = Field(None, ge=0, le=1, description="Similarity (0-1)")


class UnifiedSearchHit(BaseModel):
    """A single search result from unified search across all entity types."""

    model_config = ConfigDict(str_strip_whitespace=True)

    entity_type: str = Field(..., description="Entity type")
    id: UUID = Field(..., description="Entity ID")
    slug: str = Field(..., min_length=1, description="Slug")
    title: str = Field(..., min_length=1, description="Title")
    snippet: str = Field(default="", description="Text snippet")
    similarity: float | None = Field(None, ge=0, le=1, description="Similarity score (0-1)")


class EntityInfo(BaseModel):
    """Named entity information."""

    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(..., min_length=1, description="Entity text")
    label: str = Field(..., min_length=1, description="Entity type")
    description: str = Field(default="", description="Description")


class SemanticSearchResult(BaseModel):
    """Single semantic search result from knowledge base."""

    model_config = ConfigDict(str_strip_whitespace=True)

    topic: str = Field(..., min_length=1, description="Topic")
    title: str = Field(..., min_length=1, description="Title")
    url: str = Field(..., min_length=1, description="URL")
    note: str = Field(default="", description="Notes")
    similarity_score: float = Field(..., ge=0, le=1, description="Similarity (0-1)")


class SemanticSearchResponse(BaseModel):
    """Complete semantic search response with NER."""

    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(default="", description="Query")
    enhanced_query: str = Field(default="", description="Enhanced query")
    results: list[SemanticSearchResult] = Field(default_factory=list, description="Results")
    entities: list[EntityInfo] = Field(default_factory=list, description="Named entities")
    total_results: int = Field(default=0, ge=0, description="Total results")


# ---------------------------------------------------------------------------
# Version endpoints
# ---------------------------------------------------------------------------


class VersionInfo(BaseModel):
    """Version information for a principle or decision."""

    model_config = ConfigDict(str_strip_whitespace=True)

    version: str = Field(..., description="Version")
    version_status: str = Field(..., description="Status")
    created_at: datetime | None = Field(None, description="Created")
    updated_at: datetime | None = Field(None, description="Updated")
    approved_by: str | None = Field(None, description="Approver")
    approved_at: datetime | None = Field(None, description="Approval timestamp")
    published_at: datetime | None = Field(None, description="Publication timestamp")


class VersionComparison(BaseModel):
    """Result of comparing two versions."""

    model_config = ConfigDict(str_strip_whitespace=True)

    old_version: str = Field(..., description="Old version")
    new_version: str = Field(..., description="New version")
    is_breaking_change: bool = Field(..., description="Breaking change")
    comparison_result: int = Field(
        ..., description="Comparison: -1 (old < new), 0 (equal), 1 (old > new)"
    )
    version_diff: dict[str, str] = Field(default_factory=dict, description="Differences")


def partial_model(
    model: type[BaseModel], *, exclude: frozenset[str] = frozenset()
) -> type[BaseModel]:
    """An all-optional, extra-forbidding copy of ``model`` for PATCH bodies.

    Used only to DOCUMENT and shape-check a partial update: the handler reads
    ``model_dump(exclude_unset=True)`` to learn which fields were sent, and the
    service re-validates the merged record through ``model`` itself, so every
    validator and constraint of the full schema still applies.

    ``exclude`` drops server-managed fields from the PATCH surface entirely, so
    ``extra="forbid"`` answers a client that sends one with a 422 and the published
    spec (and every node generated from it) no longer offers it.
    """

    def _optional(annotation: Any) -> Any:
        # ``FieldInfo.annotation`` is a runtime type VALUE; ``| None`` builds the union.
        return annotation | None

    fields: dict[str, Any] = {
        name: (_optional(f.annotation), Field(default=None, description=f.description))
        for name, f in model.model_fields.items()
        if name not in exclude
    }
    return create_model(  # type: ignore[call-overload]
        f"{model.__name__.removesuffix('In')}Patch",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
