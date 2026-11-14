"""Architecture tool endpoints — analysis and validation."""

import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from auth import get_subject_id, get_tenant_id, get_visibility
from clients.coding_analysis import (
    CodingAnalysisContainerNotFoundError,
    CodingAnalysisUnreadableBodyError,
    fetch_code_units,
)
from db import get_session
from models.architecture import (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    Standard,
)
from models.decisions import Decision
from models.principles import Principle
from models.requirements import Requirement
from repositories.bounded import (
    DEFAULT_MAX_INPUT_ELEMENTS,
    DEFAULT_MAX_INPUT_RELATIONSHIPS,
    InputTooLarge,
    fetch_bounded,
)
from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.architecture import MetamodelValidationResult
from schemas.common import ErrorResponse
from schemas.modeling import ArchimateSnapshot, ValidationReport
from schemas.tools import (
    ADMCompletenessRequest,
    ArchitectureConformanceRequest,
    ArchitectureConformanceResult,
    CapabilityHeatmapRequest,
    DeriveRelationshipsRequest,
    DeriveRelationshipsResult,
    ImpactAnalysisRequest,
    ProjectScopeRequest,
    TraceabilityMatrixRequest,
    ValidateRelationshipRequest,
    ViewGenerationRequest,
)
from services import modeling_models as models_svc
from settings import get_settings
from tools.architecture.adm_completeness import check_adm_completeness
from tools.architecture.archimate_rules import UnknownRuleError
from tools.architecture.archimate_rules import validate as validate_archimate
from tools.architecture.capability_heatmap import build_capability_heatmap
from tools.architecture.compliance import check_compliance
from tools.architecture.conformance import ARTIFACT_TYPE, DEFAULT_LAYER_ORDER, analyze_conformance
from tools.architecture.derivation import DerivationTooLarge, Edge, derive_relationships
from tools.architecture.gap_analysis import analyze_gaps
from tools.architecture.impact import analyze_impact
from tools.architecture.metamodel import validate_relationship
from tools.architecture.traceability import build_traceability_matrix
from tools.architecture.view_generator import generate_view

logger = logging.getLogger(__name__)


class _BoundedInputRoute(ErrorEnvelopeRoute):
    """Render ``InputTooLarge`` from the loaders as HTTP 422 — ONE place, every route.

    Every tool endpoint below feeds ``_load_elements`` / ``_load_relationships``
    / ``_load_views`` / ``_load_standards``, so the mapping would otherwise be
    eight identical ``try/except`` blocks that a ninth endpoint silently forgets.
    A ``route_class`` on the router is the one hook FastAPI gives you that wraps
    every operation the router carries, present and future, without touching a
    single endpoint body.

    422, not 500 and not 200:

    - Not 200: a 200 carrying a truncated answer is the defect this bound
      exists to prevent (see the loaders' docstrings). A caller that branches
      on the status code would parse a coverage/gap/impact report computed from
      an arbitrary slice of the model and believe it.
    - Not 500: nothing is broken. The request was understood and the server is
      healthy; this tenant's architecture is simply too large to analyse in one
      pass at the configured bound.
    - 422 over 400: the JSON is syntactically fine and every field passes its
      own validator. What fails is a semantic precondition discovered from the
      referenced tenant/project's stored state — FastAPI's existing meaning for
      "well-formed but unprocessable".
    - Not 413 (Payload Too Large): the request body is a couple of hundred
      bytes. What exceeds the limit is server-side state the client never
      transmitted, so a payload-size status points the caller at the wrong thing.

    ``total`` and ``limit`` ride in BOTH the structured detail and the message
    text, because ``main._http_exception_to_response`` flattens a dict detail
    to its ``code`` + ``message`` and drops every sibling key — so the message
    is the only place an operator reading the wire actually sees the numbers.
    The message is built here from the exception's fields rather than from
    ``str(exc)``, so the wire contract does not depend on the helper's
    ``__str__``.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_route_handler = super().get_route_handler()

        async def bounded_input_route_handler(request: Request) -> Response:
            try:
                return await original_route_handler(request)
            except InputTooLarge as exc:
                logger.warning(
                    "architecture tool input over bound: label=%s total=%s limit=%s",
                    exc.label,
                    exc.total,
                    exc.limit,
                )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "input_too_large",
                        "message": (
                            f"{exc.label}: this project's effective view holds {exc.total} "
                            f"rows, above the {exc.limit}-row analysis limit. Narrow the "
                            f"scope (project_id) rather than accepting a partial answer."
                        ),
                        "label": exc.label,
                        "total": exc.total,
                        "limit": exc.limit,
                    },
                ) from exc

        return bounded_input_route_handler


# Exported so a sibling router that calls these same loaders maps the refusal the
# same way (architecture_elements, integration_tools). Without sharing this route_class an
# over-cap path would raise an unhandled ValueError and answer 500 — strictly worse
# than the unbounded load it replaced, because a 500 says "this service is broken"
# when it was merely refused.
BoundedInputRoute = _BoundedInputRoute

router = APIRouter(route_class=_BoundedInputRoute)


# ---------------------------------------------------------------------------
# Helpers to load a project's effective view (project-scope + tenant-scope)
# ---------------------------------------------------------------------------
#
# ``project_id IS NULL`` rows are tenant-scope assets — reusable across every
# project under the same tenant. Every loader below ORs them in alongside the
# explicit project rows so downstream tools (conformance, traceability, ADM, …)
# see the same effective architecture the operator does.
#
# ---------------------------------------------------------------------------
# Two properties every loader below holds, and why
# ---------------------------------------------------------------------------
#
# 1. COLUMN SELECTS, NOT ORM ENTITIES.
#    Each loader already discards the entity and returns a ``list[dict]``, so
#    selecting whole rows bought nothing and cost a great deal: SQLAlchemy
#    builds an instance per row, registers it in the session identity map, and
#    holds the buffered ``Result`` alongside — all of it live at the same
#    moment as the projected dicts. Measured shape of the problem: the largest
#    tenant carries 2,703 elements and 11,674 relationships, this pod is
#    ``memory: 512Mi`` with ``replicas: 1``, and an unbounded load on this
#    service has already OOM-killed it once. Selecting only the columns the
#    projection reads removes the dominant term.
#
#    ⚠️ The returned dict shape is part of the contract and is deliberately
#    byte-identical to the pre-change shape — ``tools/architecture/*`` and
#    ``_bound_containers`` here read these dicts. Adding a column to a select is cheap; dropping a key is
#    a silent behaviour change downstream. ``tests/test_arch_input_bounds.py``
#    pins each key set explicitly for exactly that reason.
#
# 2. THEY REFUSE OVER THE CAP; THEY DO NOT TRUNCATE.
#    Every consumer of these loaders (coverage, gap, impact, conformance, ADM
#    completeness, traceability) computes a whole-model verdict. Feeding such a
#    tool an arbitrary slice of the model does not yield a partial answer — it
#    yields a CONFIDENTLY WRONG one: elements whose realising relationship was
#    cut land in the report as gaps, and the caller cannot tell. That is the
#    same "200 carrying a failure" defect this repo has just fixed twice (B6,
#    B7), and it is why ``.raise_if_truncated()`` is called at every site.
#
#    This is the deliberate difference from #147, which bounded the RESPONSE.
#    Truncating an OUTPUT is honest — the caller asked for a view and gets a
#    labelled prefix of it. Truncating an INPUT is not, because the caller
#    never sees the input at all.
#
#    ``InputTooLarge`` is raised, not ``HTTPException``, so the loaders stay
#    framework-agnostic and the mapping lives in one place: ``_BoundedInputRoute``
#    turns it into a 422 for every endpoint on this router at once.
#
#    ⚠️ A ``route_class`` is per-ROUTER: any other router that calls these
#    loaders must reuse this class via the ``BoundedInputRoute`` alias below.


def _subject_visible(model: Any, subject_id: str | None) -> Any:
    """The subject half of ``repositories.visibility.scope`` (finding A04).

    The tool loaders filtered tenant + project only, so another subject's
    ``visibility="user"`` rows fed every analysis. ``None`` keeps the old
    tenant-wide read for callers outside this router that have not been moved
    yet; every route here passes the caller's subject.
    """
    if subject_id is None:
        return true()
    return or_(
        model.visibility == "tenant",
        and_(model.visibility == "user", model.subject_id == subject_id),
    )


async def _load_elements(
    session: AsyncSession, tenant_id: str, project_id: str, *, subject_id: str | None = None
) -> list[dict]:
    bounded = await fetch_bounded(
        session,
        select(
            # Exactly the columns the projection below reads — no more. Keep in
            # step with the dict literal at the bottom of this function.
            ArchitectureElement.id,
            ArchitectureElement.slug,
            ArchitectureElement.title,
            ArchitectureElement.description,
            ArchitectureElement.layer,
            ArchitectureElement.element_type,
            ArchitectureElement.is_synthetic,
            ArchitectureElement.properties,
            ArchitectureElement.status,
            ArchitectureElement.architecture_state,
            ArchitectureElement.parent_id,
        ).where(
            ArchitectureElement.tenant_id == tenant_id,
            or_(
                ArchitectureElement.project_id.is_(None),
                ArchitectureElement.project_id == project_id,
            ),
            _subject_visible(ArchitectureElement, subject_id),
        ),
        # Module-global, read at call time so a test (and a future settings
        # binding) can rebind the cap without reaching into the helper.
        cap=DEFAULT_MAX_INPUT_ELEMENTS,
        # ORDER BY the primary key: a LIMIT without an ORDER BY returns a
        # non-deterministic subset, so the same request could answer differently
        # on two consecutive calls — and the ``total`` in the refusal would name
        # a population the caller cannot reproduce.
        order_by=ArchitectureElement.id,
        label="architecture elements",
    )
    bounded.raise_if_truncated()
    rows = bounded.rows
    # Resolve each element's coding-binding via the structured
    # ``architecture_element_realisation_bindings`` table (kind=coding_component)
    # so downstream readers (_bound_containers, conformance, proposals) consume
    # a single shape — the structured table is the canonical store.
    element_ids = [r.id for r in rows]
    # An element may carry SEVERAL bindings (A09): keep every row, in a stable
    # (container, component_id) order, never one arbitrary survivor.
    binding_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    if element_ids:
        from models.architecture import ArchitectureElementRealisationBinding

        binding_rows = (
            await session.execute(
                select(
                    ArchitectureElementRealisationBinding.element_id,
                    ArchitectureElementRealisationBinding.container,
                    ArchitectureElementRealisationBinding.component_id,
                )
                .where(
                    ArchitectureElementRealisationBinding.tenant_id == tenant_id,
                    ArchitectureElementRealisationBinding.binding_kind == "coding_component",
                    ArchitectureElementRealisationBinding.element_id.in_(element_ids),
                    ArchitectureElementRealisationBinding.container.isnot(None),
                    ArchitectureElementRealisationBinding.component_id.isnot(None),
                )
                .order_by(
                    ArchitectureElementRealisationBinding.element_id,
                    ArchitectureElementRealisationBinding.container,
                    ArchitectureElementRealisationBinding.component_id,
                )
            )
        ).all()
        for element_id, container, component_id in binding_rows:
            binding_index[str(element_id)].append(
                {"container": str(container), "component_id": str(component_id)}
            )

    out: list[dict] = []
    for r in rows:
        eid = str(r.id)
        props = r.properties or {}
        # Canonical source: structured realisation-bindings table.
        code_bindings = binding_index.get(eid, [])
        out.append(
            {
                "id": eid,
                "slug": r.slug,
                "title": r.title,
                "description": r.description,
                "layer": r.layer,
                "element_type": r.element_type,
                "is_synthetic": r.is_synthetic,
                "properties": props,
                "code_bindings": code_bindings,
                "status": r.status,
                "architecture_state": r.architecture_state,
                "parent_id": str(r.parent_id) if r.parent_id else None,
            }
        )
    return out


async def _load_relationships(
    session: AsyncSession, tenant_id: str, project_id: str, *, subject_id: str | None = None
) -> list[dict]:
    bounded = await fetch_bounded(
        session,
        select(
            ArchitectureRelationship.id,
            ArchitectureRelationship.source_entity_type,
            ArchitectureRelationship.source_id,
            ArchitectureRelationship.target_entity_type,
            ArchitectureRelationship.target_id,
            ArchitectureRelationship.relationship_type,
            ArchitectureRelationship.properties,
        ).where(
            ArchitectureRelationship.tenant_id == tenant_id,
            or_(
                ArchitectureRelationship.project_id.is_(None),
                ArchitectureRelationship.project_id == project_id,
            ),
            _subject_visible(ArchitectureRelationship, subject_id),
        ),
        # Relationships outnumber elements ~4:1 in the live data (11,674 vs
        # 2,703 for the largest tenant), hence their own, larger cap.
        cap=DEFAULT_MAX_INPUT_RELATIONSHIPS,
        order_by=ArchitectureRelationship.id,
        label="architecture relationships",
    )
    bounded.raise_if_truncated()
    rows = bounded.rows
    return [
        {
            "id": str(r.id),
            "source_entity_type": r.source_entity_type,
            "source_id": str(r.source_id),
            "target_entity_type": r.target_entity_type,
            "target_id": str(r.target_id),
            "relationship_type": r.relationship_type,
            "properties": r.properties or {},
        }
        for r in rows
    ]


async def _load_views(
    session: AsyncSession, tenant_id: str, project_id: str, *, subject_id: str | None = None
) -> list[dict]:
    bounded = await fetch_bounded(
        session,
        select(
            ArchitectureView.id,
            ArchitectureView.title,
            ArchitectureView.viewpoint,
        ).where(
            ArchitectureView.tenant_id == tenant_id,
            or_(
                ArchitectureView.project_id.is_(None),
                ArchitectureView.project_id == project_id,
            ),
            _subject_visible(ArchitectureView, subject_id),
        ),
        # Views and standards reuse the ELEMENT cap deliberately: neither has a
        # cap of its own because neither has ever been the memory term (both are
        # low-hundreds at most, and the projection is 3-5 short columns). They
        # are bounded anyway because the reason for the bound is correctness,
        # not just memory — ``check_adm_completeness`` scores phase deliverable
        # COVERAGE from these lists, so a silently short list reads as a missing
        # deliverable. Giving them the element cap keeps one number to reason
        # about rather than four; if either ever needs its own, add it to
        # ``repositories.bounded`` rather than hardcoding it here.
        cap=DEFAULT_MAX_INPUT_ELEMENTS,
        order_by=ArchitectureView.id,
        label="architecture views",
    )
    bounded.raise_if_truncated()
    return [{"id": str(r.id), "title": r.title, "viewpoint": r.viewpoint} for r in bounded.rows]


async def _load_standards(
    session: AsyncSession, tenant_id: str, project_id: str, *, subject_id: str | None = None
) -> list[dict]:
    bounded = await fetch_bounded(
        session,
        select(
            Standard.id,
            Standard.title,
            Standard.standard_type,
            Standard.status,
            Standard.compliance_level,
        ).where(
            Standard.tenant_id == tenant_id,
            or_(
                Standard.project_id.is_(None),
                Standard.project_id == project_id,
            ),
            _subject_visible(Standard, subject_id),
        ),
        # Element cap reused — see ``_load_views`` for why.
        cap=DEFAULT_MAX_INPUT_ELEMENTS,
        order_by=Standard.id,
        label="architecture standards",
    )
    bounded.raise_if_truncated()
    rows = bounded.rows
    return [
        {
            "id": str(r.id),
            "title": r.title,
            "standard_type": r.standard_type,
            "status": r.status,
            "compliance_level": r.compliance_level,
        }
        for r in rows
    ]


def _bound_containers(elements: list[dict]) -> list[str]:
    """Distinct code-graph containers bound to this project's Artifact elements."""
    containers: set[str] = set()
    for elem in elements:
        if elem.get("element_type") != ARTIFACT_TYPE:
            continue
        for binding in elem.get("code_bindings") or []:
            container = binding.get("container")
            if container:
                containers.add(str(container))
    return sorted(containers)


async def _count_entities(
    session: AsyncSession, model_class, tenant_id: str, project_id: str
) -> int:
    """Count tenant-scope + project-scope rows together — see loader docstring."""
    result = await session.execute(
        select(func.count())
        .select_from(model_class)
        .where(
            model_class.tenant_id == tenant_id,
            or_(
                model_class.project_id.is_(None),
                model_class.project_id == project_id,
            ),
        )
    )
    return result.scalar() or 0


async def _count_tenant_entities(session: AsyncSession, model_class, tenant_id: str) -> int:
    """Count rows scoped to a tenant only (no project axis).

    For models like ``Principle`` whose scope is deliberately tenant-wide —
    no ``project_id`` column — and which are valid for every project under
    the same tenant. Calling ``_count_entities`` on such a model raises
    ``AttributeError`` at the ``model_class.project_id`` reference; this
    helper is the tenant-only counterpart.
    """
    result = await session.execute(
        select(func.count()).select_from(model_class).where(model_class.tenant_id == tenant_id)
    )
    return result.scalar() or 0


# ---------------------------------------------------------------------------
# Tool endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/validate-relationship",
    response_model=MetamodelValidationResult,
    operation_id="validate_architecture_relationship",
    responses=AUTH_RESPONSES,
)
async def validate_relationship_endpoint(
    req: ValidateRelationshipRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
) -> MetamodelValidationResult:
    """Validate a relationship against ArchiMate 3.2 metamodel rules."""
    return validate_relationship(
        source_layer=req.source_layer,
        source_element_type=req.source_element_type,
        target_layer=req.target_layer,
        target_element_type=req.target_element_type,
        relationship_type=req.relationship_type,
    )


class ValidateArchimateRequest(ArchimateSnapshot):
    """A model fragment to validate statelessly (nothing is persisted)."""

    rules: list[str] | None = Field(None, description="Only these rule ids (default: all)")
    disable: list[str] | None = Field(None, description="Rule ids to skip")


@router.post(
    "/validate",
    response_model=ValidationReport,
    operation_id="validate_archimate_fragment",
    responses={
        **AUTH_RESPONSES,
        422: {"description": "Unknown rule id or input too large", "model": ErrorResponse},
    },
)
async def validate_archimate_fragment(req: ValidateArchimateRequest) -> ValidationReport:
    """Run the full ArchiMate 3.2 ruleset over a caller-supplied snapshot.

    The same pure function backs write-time checks and validation runs, so a
    fragment that comes back ``clean`` here is one the write path would accept.
    ``views`` omitted ⇒ view rules are not run (and not listed as enabled);
    ``views: []`` ⇒ the model has no views and every element is unused.
    Above ``MODEL_SYNC_MAX_CONCEPTS`` concepts + relationships the request is
    refused with ``input_too_large`` rather than truncated.
    """
    limit = get_settings().model_sync_max_concepts
    total = len(req.concepts) + len(req.relationships)
    if total > limit:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "input_too_large",
                "message": f"{total} concepts + relationships exceeds the synchronous limit of {limit}",
            },
        )
    snapshot = ArchimateSnapshot.model_validate(req.model_dump(exclude={"rules", "disable"}))
    try:
        return validate_archimate(snapshot, rules=req.rules, disable=req.disable)
    except UnknownRuleError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "unknown_rule", "message": str(exc)}
        ) from exc


@router.post(
    "/traceability-matrix",
    operation_id="build_traceability_matrix",
    responses=AUTH_RESPONSES,
)
async def traceability_matrix_endpoint(
    req: TraceabilityMatrixRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Build cross-layer traceability matrix."""
    elements = await _load_elements(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    relationships = await _load_relationships(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    return build_traceability_matrix(elements, relationships, req.root_layer)


@router.post(
    "/gap-analysis",
    operation_id="analyze_architecture_gaps",
    responses=AUTH_RESPONSES,
)
async def gap_analysis_endpoint(
    req: ProjectScopeRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Compare baseline vs target architecture and identify gaps."""
    elements = await _load_elements(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    relationships = await _load_relationships(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    return analyze_gaps(elements, relationships)


@router.post(
    "/impact-analysis",
    operation_id="analyze_architecture_impact",
    responses=AUTH_RESPONSES,
)
async def impact_analysis_endpoint(
    req: ImpactAnalysisRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Propagate a change through the relationship graph."""
    elements = await _load_elements(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    relationships = await _load_relationships(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )

    # 404 THE UNKNOWN ELEMENT — do not let the tool's sentinel reach the wire.
    #
    # ``analyze_impact`` answers an id it cannot find with
    # ``{"source": {"title": "unknown", ...}, "affected": []}``. Over HTTP that
    # is a 200 whose body says "changing this element affects nothing" — the
    # single most dangerous answer this endpoint can give, because a caller
    # passing a stale, deleted or mistyped id is told the change is SAFE. Same
    # class as the input-bound refusal above: a confidently wrong 200.
    #
    # ⛔ THE CHECK LIVES HERE, NOT IN ``tools/architecture/impact.py``.
    # ``tests/test_architecture.py::test_unknown_element_returns_empty`` pins
    # that sentinel against the pure function directly, and the function is
    # deliberately framework-agnostic — it has no status codes to raise. The
    # HTTP contract is this layer's job.
    #
    # ⚠️ THE WORDING IS "in this project scope", NOT "does not exist".
    # ``_load_elements`` filters ``tenant_id == … AND (project_id IS NULL OR
    # project_id == requested)``, so an element belonging to a DIFFERENT project
    # of the same tenant is legitimately absent from this set while existing
    # perfectly well. Saying "not found" flat would send the caller hunting for a
    # deleted row that is sitting in the next project along. Pinned by
    # ``tests/integration/test_architecture_graph_tools.py``
    # ::test_an_element_in_another_project_of_the_same_tenant_is_out_of_scope.
    element_id = str(req.element_id)
    if element_id not in {str(e["id"]) for e in elements}:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Element {element_id} is not in this project scope "
                f"(project_id={req.project_id!r}). It may belong to another "
                f"project of this tenant, or no longer exist. Impact analysis "
                f"is refused rather than answered with an empty result, which "
                f"would read as 'this change affects nothing'."
            ),
        )

    return analyze_impact(element_id, elements, relationships, req.max_depth)


@router.post(
    "/adm-completeness",
    operation_id="check_adm_completeness",
    responses=AUTH_RESPONSES,
)
async def adm_completeness_endpoint(
    req: ADMCompletenessRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Check TOGAF ADM phase deliverable completeness."""
    elements = await _load_elements(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    relationships = await _load_relationships(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    views = await _load_views(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    standards = await _load_standards(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    req_count = await _count_entities(session, Requirement, tenant_id, req.project_id)
    dec_count = await _count_entities(session, Decision, tenant_id, req.project_id)
    # Principle is tenant-scope only — no ``project_id`` column. Route through
    # the tenant-only counter; calling ``_count_entities`` here would raise
    # ``AttributeError`` on ``Principle.project_id``.
    prin_count = await _count_tenant_entities(session, Principle, tenant_id)
    return check_adm_completeness(
        req.phase, elements, relationships, views, standards, req_count, dec_count, prin_count
    )


@router.post(
    "/capability-heatmap",
    operation_id="build_capability_heatmap",
    responses=AUTH_RESPONSES,
)
async def capability_heatmap_endpoint(
    req: CapabilityHeatmapRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Score capabilities across maturity, strategic importance, investment need."""
    elements = await _load_elements(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    relationships = await _load_relationships(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    # WIDEN Literal -> str AT THE BOUNDARY. ``req.dimensions`` is now
    # ``list[HeatmapDimension] | None`` (a closed vocabulary — see
    # ``schemas/tools.py``), while ``build_capability_heatmap`` takes
    # ``list[str] | None``. ``list`` is INVARIANT, so ``list[Literal[...]]`` is
    # not assignable to ``list[str]`` and ``ty`` rejects the direct pass-through.
    #
    # The widening belongs here, not in the tool: ``capability_heatmap.py`` is a
    # pure, framework-agnostic function whose ``str`` signature is what lets its
    # unit tests call it with arbitrary dimension names. Narrowing it to the
    # Literal would couple the tool to the request schema for no gain — the
    # validation that matters already happened at the request boundary, which is
    # the only place an untrusted value can enter.
    dimensions: list[str] | None = (
        [str(d) for d in req.dimensions] if req.dimensions is not None else None
    )
    return build_capability_heatmap(elements, relationships, dimensions)


@router.post(
    "/generate-view",
    operation_id="generate_architecture_view",
    responses=AUTH_RESPONSES,
)
async def generate_view_endpoint(
    req: ViewGenerationRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Auto-select elements and relationships for a given ArchiMate viewpoint."""
    elements = await _load_elements(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    relationships = await _load_relationships(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    return generate_view(req.viewpoint, elements, relationships)


@router.post(
    "/conformance",
    operation_id="analyze_architecture_conformance",
    response_model=ArchitectureConformanceResult,
    responses=AUTH_RESPONSES,
)
async def conformance_endpoint(
    req: ArchitectureConformanceRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureConformanceResult:
    """Compare the as-designed ArchiMate model against the as-built code-graph.

    Loads this project's Architecture elements + relationships, resolves the
    as-built code units (caller-supplied, else pulled from coding-analysis for
    the containers bound to the project's synthetic Artifacts), and runs the
    three deterministic conformance measures.
    """
    elements = await _load_elements(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    relationships = await _load_relationships(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )

    # ⛔ RETURN THE POOLED CONNECTION BEFORE THE UPSTREAM FETCH LOOP.
    #
    # The two reads above autobegin a transaction, SQLAlchemy holds the pooled connection
    # for its whole life, and the loop below awaits `fetch_code_units` once PER
    # BOUND CONTAINER, serially, against a client with a 30s timeout. A project
    # with N containers therefore pins one of 15 connections for up to 30s x N
    # while doing no database work at all.
    #
    # `commit()`, not `close()`/`rollback()`: `expire_on_commit=False` keeps the loaded `elements` and
    # `relationships` usable afterwards, which `rollback()` would not. Both
    # callees are reads, so this ends an empty transaction.
    #
    # This route did NOT appear in the log window of the
    # 2026-09-03 exhaustion incident, so it is not the holder that caused it. It
    # is the same defect, found while looking for that one, and fixed on its own
    # merits — a single 30s-per-container hold is enough to starve `/health/ready`
    # (5 + 10 pool, `timeoutSeconds: 5`, `failureThreshold: 3`, ONE replica).
    await session.commit()

    missing_containers: set[str] = set()
    if req.code_units is not None:
        code_units = req.code_units
    else:
        code_units = []
        try:
            for container in _bound_containers(elements):
                try:
                    code_units.extend(await fetch_code_units(container, tenant_id=tenant_id))
                except CodingAnalysisContainerNotFoundError:
                    # No graph for THIS container is missing evidence about its
                    # bindings, not "no code": its elements are reported
                    # `insufficient_evidence`, never unrealised/dead, and the
                    # other containers' evidence still counts.
                    missing_containers.add(container)
        except CodingAnalysisUnreadableBodyError as exc:
            # 502, not 500: the fault is coding-analysis's body, not this
            # service. A bare 500 (which is where this landed — nothing caught
            # it, so it reached the app-level handler in main.py) sends the
            # operator to the wrong service's logs and loses the "which
            # upstream" signal entirely. One contract for an unreadable
            # coding-* body.
            #
            # THE WHOLE REQUEST FAILS, and the container is deliberately NOT
            # skipped. `analyze_conformance` reads the accumulated
            # `code_units` as the complete as-built picture, so continuing with
            # the siblings' units would report every element realized only by
            # the unreadable container as an unrealized gap — a confident,
            # complete-looking wrong answer. That is the exact defect
            # tests/test_unreadable_body_is_not_zero_components.py
            # ::test_one_unreadable_container_does_not_silently_shrink_the_report
            # pins (it asserts the loop aborts with the partial list unused),
            # and the same reasoning the client's raise site states. Failing
            # loudly costs the operator one report; shrinking it silently costs
            # them the truth.
            logger.warning("coding-analysis body unreadable for conformance: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    layer_order = tuple(req.layer_order) if req.layer_order else DEFAULT_LAYER_ORDER
    return ArchitectureConformanceResult.model_validate(
        analyze_conformance(
            elements,
            relationships,
            code_units,
            layer_order,
            insufficient_containers=missing_containers,
        )
    )


@router.post(
    "/compliance-check",
    operation_id="check_standards_compliance",
    responses=AUTH_RESPONSES,
)
async def compliance_check_endpoint(
    req: ProjectScopeRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Check elements against approved standards and principles."""
    elements = await _load_elements(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    standards = await _load_standards(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    relationships = await _load_relationships(
        session, tenant_id, req.project_id, subject_id=subject_id or tenant_id
    )
    return check_compliance(elements, standards, relationships)


@router.post(
    "/derive-relationships",
    response_model=DeriveRelationshipsResult,
    operation_id="derive_architecture_relationships",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Model not found (or not visible)", "model": ErrorResponse},
        422: {
            "description": "Not an ArchiMate model, or input / search too large",
            "model": ErrorResponse,
        },
    },
)
async def derive_relationships_endpoint(
    req: DeriveRelationshipsRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> DeriveRelationshipsResult:
    """Relationships ArchiMate 3.2 §5.7 derives from the stored ones, with provenance.

    Read-only. Each result names its rule (``AM-DER-*`` valid, ``AM-DER-POT-*`` potential), the
    chain of stored relationship ids it was derived from, and whether a direct
    relationship of the same type already exists. Derivations the relationship
    matrix forbids come back in ``filtered``, never in ``derived``. Over the input
    bound, or when the chain search exceeds its bound, the request is refused
    (``422 input_too_large``) rather than answered with a partial set.
    """
    sub = subject_id or tenant_id
    concepts: dict[str, str]
    edges: list[Edge]
    if req.model_id is not None:
        model = await models_svc.get_model(session, req.model_id, tenant_id, sub)
        if model.language != "archimate":
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "not_archimate",
                    "message": f"model {model.id} is a {model.language} model, not ArchiMate",
                },
            )
        limit = get_settings().model_sync_max_concepts
        total = await models_svc.count_concepts(session, model.id)
        if total > limit:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": models_svc.INPUT_TOO_LARGE,
                    "message": (
                        f"the model has {total} concepts and relationships; derivation is "
                        f"synchronous up to MODEL_SYNC_MAX_CONCEPTS={limit}"
                    ),
                },
            )
        snapshot = models_svc.archimate_snapshot(await models_svc.build_manifest(session, model))
        concepts = {c.id: c.type for c in snapshot.concepts}
        edges = [Edge(r.id, r.type, r.source, r.target) for r in snapshot.relationships]
    else:
        project_id = req.project_id or ""
        elements = await _load_elements(session, tenant_id, project_id, subject_id=sub)
        relationships = await _load_relationships(session, tenant_id, project_id, subject_id=sub)
        concepts = {e["id"]: e["element_type"] for e in elements}
        edges = [
            Edge(r["id"], r["relationship_type"], r["source_id"], r["target_id"])
            for r in relationships
        ]
    try:
        return await run_in_threadpool(
            lambda: derive_relationships(
                concepts,
                edges,
                source_id=req.source_id,
                target_id=req.target_id,
                max_path_length=req.max_path_length,
                include_potential=req.include_potential,
            )
        )
    except DerivationTooLarge as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "input_too_large",
                "message": (
                    f"{exc}. Narrow the request (source_id / target_id / max_path_length) "
                    f"rather than accepting a partial derivation set."
                ),
            },
        ) from exc
