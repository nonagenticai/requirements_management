"""ArchiMate reference data: concepts, relationship matrix, viewpoints, rulesets.

Read-only views over the Archi-derived registry (``tools.architecture.registry``)
and the rule catalogue (``tools.architecture.archimate_rules``). Nothing here is
tenant data, so nothing reads the database; the routes sit behind the normal
auth middleware like every other ``/architecture`` route.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.common import ErrorResponse
from schemas.modeling import RulesetInfo
from tools.architecture.archimate_rules import ruleset_info
from tools.architecture.registry import ARCHI_VIEWPOINT_IDS, get_registry
from tools.uml.rules import ruleset_info as uml_ruleset_info

metamodel_router = APIRouter(route_class=ErrorEnvelopeRoute)
viewpoints_router = APIRouter(route_class=ErrorEnvelopeRoute)
rulesets_router = APIRouter(route_class=ErrorEnvelopeRoute)


class ArchimateConceptOut(BaseModel):
    type: str = Field(..., description="Service element type, e.g. business_actor")
    archi_class: str = Field(..., description="Archi EClass name, e.g. BusinessActor")
    layer: str
    aspect: str = Field(..., description="Descriptive only; legality comes from the matrix")


class ArchimateConceptsOut(BaseModel):
    language_version: str
    reference: dict
    license_notice: str
    concepts: list[ArchimateConceptOut]
    relationship_types: list[str]


class RelationshipMatrixCell(BaseModel):
    source_type: str
    target_type: str
    allowed: list[str]


class ArchimateViewpointOut(BaseModel):
    id: str
    name: str
    allowed_concepts: list[str]
    allowed_relationships: list[str] = Field(
        ..., description="Empty means every relationship is allowed (Archi semantics)"
    )
    source: str = Field(..., description="'archi' or 'extension' (service-defined)")


@metamodel_router.get(
    "/concepts",
    response_model=ArchimateConceptsOut,
    operation_id="list_archimate_concepts",
    responses=AUTH_RESPONSES,
)
async def list_archimate_concepts() -> ArchimateConceptsOut:
    """Every ArchiMate concept the service accepts, with its layer and Archi class."""
    reg = get_registry()
    return ArchimateConceptsOut(
        language_version=reg.language_version,
        reference=reg.reference,
        license_notice=reg.license_notice,
        concepts=[
            ArchimateConceptOut(
                type=c.type, archi_class=c.archi_class, layer=c.layer, aspect=c.aspect
            )
            for c in reg.concepts.values()
        ],
        relationship_types=list(reg.relationship_types),
    )


@metamodel_router.get(
    "/relationships",
    response_model=list[RelationshipMatrixCell],
    operation_id="query_archimate_relationship_matrix",
    responses={**AUTH_RESPONSES, 422: {"description": "Unknown type", "model": ErrorResponse}},
)
async def query_archimate_relationship_matrix(
    source_type: str | None = Query(None, max_length=50, description="Filter: source concept type"),
    target_type: str | None = Query(None, max_length=50, description="Filter: target concept type"),
) -> list[RelationshipMatrixCell]:
    """Permitted relationship types per (source, target) pair.

    ``relationship`` is a valid endpoint type: ArchiMate lets some
    relationships end on another relationship. Without filters the full matrix
    (62 × 62 cells) is returned; cells with nothing allowed are omitted.
    """
    reg = get_registry()
    known = set(reg.matrix)
    for name, value in (("source_type", source_type), ("target_type", target_type)):
        if value is not None and value not in known:
            raise HTTPException(
                status_code=422,
                detail={"code": "unknown_concept_type", "message": f"unknown {name} '{value}'"},
            )
    cells = []
    for src in sorted(known if source_type is None else {source_type}):
        for tgt in sorted(known if target_type is None else {target_type}):
            allowed = reg.allowed(src, tgt)
            if allowed or (source_type is not None and target_type is not None):
                cells.append(
                    RelationshipMatrixCell(
                        source_type=src, target_type=tgt, allowed=sorted(allowed)
                    )
                )
    return cells


def _viewpoint_out(vp_id: str) -> ArchimateViewpointOut:
    vp = get_registry().viewpoints[vp_id]
    return ArchimateViewpointOut(
        id=vp.id,
        name=vp.name,
        allowed_concepts=sorted(vp.allowed_concepts),
        allowed_relationships=sorted(vp.allowed_relationships),
        source="archi" if vp.id in ARCHI_VIEWPOINT_IDS else "extension",
    )


@viewpoints_router.get(
    "",
    response_model=list[ArchimateViewpointOut],
    operation_id="list_archimate_viewpoints",
    responses=AUTH_RESPONSES,
)
async def list_archimate_viewpoints() -> list[ArchimateViewpointOut]:
    """The 25 Archi 5.10.0 viewpoints plus the service's declared extensions."""
    return [_viewpoint_out(v) for v in get_registry().viewpoints]


@viewpoints_router.get(
    "/{viewpoint_id}",
    response_model=ArchimateViewpointOut,
    operation_id="get_archimate_viewpoint",
    responses={**AUTH_RESPONSES, 404: {"description": "Unknown viewpoint", "model": ErrorResponse}},
)
async def get_archimate_viewpoint(viewpoint_id: str) -> ArchimateViewpointOut:
    """One viewpoint and the concepts it allows."""
    if viewpoint_id not in get_registry().viewpoints:
        raise HTTPException(status_code=404, detail="viewpoint not found")
    return _viewpoint_out(viewpoint_id)


def _rulesets() -> dict[str, RulesetInfo]:
    """Every ruleset the service can run, keyed by id."""
    return {r.ruleset_id: r for r in (ruleset_info(), uml_ruleset_info())}


@rulesets_router.get(
    "",
    response_model=list[RulesetInfo],
    operation_id="list_rulesets",
    responses=AUTH_RESPONSES,
)
async def list_rulesets() -> list[RulesetInfo]:
    """Rulesets with their reference tool/version; rules omitted (see the single route)."""
    return [r.model_copy(update={"rules": []}) for r in _rulesets().values()]


@rulesets_router.get(
    "/{ruleset_ref:path}",
    response_model=RulesetInfo,
    operation_id="get_ruleset",
    responses={**AUTH_RESPONSES, 404: {"description": "Unknown ruleset", "model": ErrorResponse}},
)
async def get_ruleset(ruleset_ref: str) -> RulesetInfo:
    """A ruleset and every rule: id, default severity, required flag, source."""
    found = _rulesets().get(ruleset_ref)
    if found is None:
        raise HTTPException(status_code=404, detail="ruleset not found")
    return found
