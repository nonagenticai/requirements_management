"""Integration tool endpoints — cross-domain architecture + PM analysis."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from models.architecture import ArchitectureElement, ArchitectureRelationship
from models.requirements import Requirement
from repositories.bounded import (
    DEFAULT_MAX_INPUT_ELEMENTS,
    DEFAULT_MAX_INPUT_RELATIONSHIPS,
    InputTooLarge,
    fetch_bounded,
)
from routers.responses import AUTH_RESPONSES
from schemas.tools import GapToWorkpackagesRequest, RequirementTraceRequest
from tools.integration.gap_to_wp import generate_workpackage_proposals
from tools.integration.req_arch_trace import trace_requirements

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_elements(session: AsyncSession, tenant_id: str, project_id: str) -> list[dict]:
    """Project-scoped architecture elements, as the plain dicts the tools read.

    Column select, not ``select(ArchitectureElement)``: every ORM object built
    here was discarded on the next line anyway — the projection below is the
    only thing that survives — so hydrating full entities bought nothing and
    cost the identity map plus ~30 unread columns per row (the whole
    ``QualityMixin``, the version/approval block, both timestamps).
    """
    stmt = select(
        ArchitectureElement.id,
        ArchitectureElement.slug,
        ArchitectureElement.title,
        ArchitectureElement.description,
        ArchitectureElement.layer,
        ArchitectureElement.element_type,
        ArchitectureElement.properties,
        ArchitectureElement.status,
        ArchitectureElement.architecture_state,
        ArchitectureElement.parent_id,
    ).where(
        ArchitectureElement.tenant_id == tenant_id,
        ArchitectureElement.project_id == project_id,
    )
    bounded = await fetch_bounded(
        session,
        stmt,
        cap=DEFAULT_MAX_INPUT_ELEMENTS,
        order_by=ArchitectureElement.id,
        label="architecture_elements",
    )
    # Refuse, do not truncate. `generate_workpackage_proposals` and
    # `trace_requirements` both reason over the WHOLE graph: a gap is "a target
    # element with no realising work package anywhere", and a trace is "a
    # requirement with no path to any element". Silently dropping the tail
    # turns both into confident fabrications — every dropped work package
    # becomes a gap that does not exist, every dropped link becomes an
    # untraced requirement — and neither answer carries a mark saying so.
    bounded.raise_if_truncated()
    return [
        {
            "id": str(r.id),
            "slug": r.slug,
            "title": r.title,
            "description": r.description,
            "layer": r.layer,
            "element_type": r.element_type,
            "properties": r.properties or {},
            "status": r.status,
            "architecture_state": r.architecture_state,
            "parent_id": str(r.parent_id) if r.parent_id else None,
        }
        for r in bounded.rows
    ]


async def _load_relationships(session: AsyncSession, tenant_id: str, project_id: str) -> list[dict]:
    """Project-scoped relationships, as the plain dicts the tools read.

    Column select for the same reason as ``_load_elements``: ``name``,
    ``description``, ``slug``, ``visibility`` and both timestamps are fetched
    by ``select(ArchitectureRelationship)`` and read by nobody.
    """
    stmt = select(
        ArchitectureRelationship.id,
        ArchitectureRelationship.source_entity_type,
        ArchitectureRelationship.source_id,
        ArchitectureRelationship.target_entity_type,
        ArchitectureRelationship.target_id,
        ArchitectureRelationship.relationship_type,
        ArchitectureRelationship.properties,
    ).where(
        ArchitectureRelationship.tenant_id == tenant_id,
        ArchitectureRelationship.project_id == project_id,
    )
    bounded = await fetch_bounded(
        session,
        stmt,
        cap=DEFAULT_MAX_INPUT_RELATIONSHIPS,
        order_by=ArchitectureRelationship.id,
        label="architecture_relationships",
    )
    # Relationships are the edge set — a truncated edge set is the most
    # dangerous input of the three, because a missing edge reads as a real
    # absence (an unrealised gap, an untraced requirement) rather than as
    # missing data. Refuse.
    bounded.raise_if_truncated()
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
        for r in bounded.rows
    ]


async def _load_requirements(session: AsyncSession, tenant_id: str, project_id: str) -> list[dict]:
    """Project-scoped requirements, as the plain dicts ``trace_requirements`` reads.

    Only three columns are projected, so the column select here is the widest
    saving of the three loaders: ``select(Requirement)`` hydrates the full
    entity (description, quality scores, embeddings, links, timestamps) to read
    ``id``/``slug``/``title``.
    """
    stmt = select(
        Requirement.id,
        Requirement.slug,
        Requirement.title,
    ).where(
        Requirement.tenant_id == tenant_id,
        Requirement.project_id == project_id,
    )
    bounded = await fetch_bounded(
        session,
        stmt,
        # Deliberately `DEFAULT_MAX_INPUT_ELEMENTS`, not a third constant.
        # This is a NODE population, not an edge population: requirements enter
        # `trace_requirements` alongside architecture elements as the two sides
        # of the same coverage matrix, so they should be bounded on the same
        # scale. A separate `DEFAULT_MAX_INPUT_REQUIREMENTS` would be a knob
        # with no independent meaning — and one that could be tuned out of step
        # with the element cap it is multiplied against.
        cap=DEFAULT_MAX_INPUT_ELEMENTS,
        order_by=Requirement.id,
        label="requirements",
    )
    bounded.raise_if_truncated()
    return [
        {
            "id": str(r.id),
            "slug": r.slug,
            "title": r.title,
        }
        for r in bounded.rows
    ]


def _input_too_large(exc: InputTooLarge) -> HTTPException:
    """Map an over-cap input population to HTTP 422.

    The same mapping as ``architecture_tools``, and for the same reasons:

    - Not 200: returning a body with an ``"error"`` key inside a 2xx is how the
      Monte Carlo endpoint used to report this, and every caller that branches
      on the status code read it as success.
    - Not 500: nothing is broken. The request was understood and the server is
      healthy; this project is simply too large to analyse in one call.
    - 422 over 400: the JSON is syntactically fine and every field passes its
      own validator. What fails is a semantic precondition discovered from the
      referenced project's server-side state — FastAPI's existing use of 422.
    - Not 413: the request payload is a couple of hundred bytes. The thing that
      exceeds a limit is state the client never transmitted.

    ``label``, ``total`` and ``limit`` are carried in BOTH the structured detail
    and the message text, because ``main._http_exception_to_response`` flattens
    a dict detail to its ``code`` + ``message`` and drops every sibling key —
    so a caller that only ever sees the rendered envelope would otherwise be
    told "too large" without being told how large, or how large is allowed.

    The message is composed here rather than taken from ``str(exc)``: the three
    numbers the caller needs to act on are this router's contract, and
    ``InputTooLarge`` is a plain ``ValueError`` in ``repositories/bounded`` whose
    ``__str__`` is free to change.
    """
    message = (
        f"{exc.label}: {exc.total} rows exceeds the maximum of {exc.limit} "
        "this analysis accepts. Narrow the request to a smaller project scope, "
        "or raise the limit."
    )
    return HTTPException(
        status_code=422,
        detail={
            "code": "input_too_large",
            "message": message,
            "label": exc.label,
            "total": exc.total,
            "limit": exc.limit,
        },
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/gap-to-workpackages",
    operation_id="generate_workpackage_proposals",
    responses=AUTH_RESPONSES,
)
async def gap_to_workpackages_endpoint(
    req: GapToWorkpackagesRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Generate work package proposals from architecture gaps."""
    try:
        elements = await _load_elements(session, tenant_id, req.project_id)
        relationships = await _load_relationships(session, tenant_id, req.project_id)
    except InputTooLarge as exc:
        raise _input_too_large(exc) from exc
    return generate_workpackage_proposals(elements, relationships)


@router.post(
    "/requirement-trace",
    operation_id="trace_requirement_architecture",
    responses=AUTH_RESPONSES,
)
async def requirement_trace_endpoint(
    req: RequirementTraceRequest,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Compute requirement ↔ architecture element traceability coverage."""
    try:
        requirements = await _load_requirements(session, tenant_id, req.project_id)
        elements = await _load_elements(session, tenant_id, req.project_id)
        relationships = await _load_relationships(session, tenant_id, req.project_id)
    except InputTooLarge as exc:
        raise _input_too_large(exc) from exc
    return trace_requirements(requirements, elements, relationships)
