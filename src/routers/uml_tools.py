"""UML profile reference data and stateless validation (``/uml``).

``GET /uml/metamodel`` serves the ``uml-2.5.1-p1`` profile with a JSON Schema
for every snapshot part; ``POST /uml/tools/validate`` runs the pure UML
validator (``tools.uml.rules``) over a caller-supplied snapshot and persists
nothing. Stored UML concepts and diagrams: ``routers/uml_models.py``.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import Field

from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.common import ErrorResponse
from schemas.modeling import ValidationReport
from schemas.uml import (
    UmlDiagramIn,
    UmlElementIn,
    UmlInteractionIn,
    UmlRelationshipIn,
    UmlSnapshot,
)
from schemas.uml_api import UmlProfileOut
from settings import get_settings
from tools.uml.profile import get_profile
from tools.uml.rules import UnknownRuleError, ruleset_info
from tools.uml.rules import validate as validate_uml

router = APIRouter(route_class=ErrorEnvelopeRoute)


@router.get(
    "/metamodel",
    operation_id="get_uml_profile",
    response_model=UmlProfileOut,
    responses=AUTH_RESPONSES,
)
async def get_uml_profile() -> UmlProfileOut:
    """The supported UML profile: kinds, operators, rules, and JSON Schemas.

    Anything outside this profile validates as ``UML-UNSUP-001`` (an error), so
    a model using it can never be reported clean — the profile is a stated
    subset of UML 2.5.1, not a claim of full UML support.
    """
    prof = get_profile()
    body = asdict(prof)
    for key, value in list(body.items()):
        if isinstance(value, frozenset):
            body[key] = sorted(value)
        elif isinstance(value, dict):
            body[key] = {k: sorted(v) if isinstance(v, frozenset) else v for k, v in value.items()}
    body["ruleset"] = ruleset_info().model_dump()
    body["schemas"] = {
        "element": UmlElementIn.model_json_schema(),
        "relationship": UmlRelationshipIn.model_json_schema(),
        "interaction": UmlInteractionIn.model_json_schema(),
        "diagram": UmlDiagramIn.model_json_schema(),
    }
    return UmlProfileOut.model_validate(body)


class ValidateUmlRequest(UmlSnapshot):
    """A UML model fragment to validate statelessly."""

    rules: list[str] | None = Field(None, description="Only these rule ids (default: all)")
    disable: list[str] | None = Field(None, description="Rule ids to skip")


@router.post(
    "/tools/validate",
    response_model=ValidationReport,
    operation_id="validate_uml_fragment",
    responses={
        **AUTH_RESPONSES,
        422: {"description": "Unknown rule id or input too large", "model": ErrorResponse},
    },
)
async def validate_uml_fragment(req: ValidateUmlRequest) -> ValidationReport:
    """Run the ``uml-2.5.1-p1`` ruleset over a snapshot. Nothing is persisted.

    A sequence diagram that validates is internally consistent; that does not
    show the code behaves the same way.
    """
    limit = get_settings().model_sync_max_concepts
    total = len(req.elements) + len(req.relationships) + len(req.interactions)
    if total > limit:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "input_too_large",
                "message": f"{total} elements + relationships + interactions exceeds the synchronous limit of {limit}",
            },
        )
    snapshot = UmlSnapshot.model_validate(req.model_dump(exclude={"rules", "disable"}))
    try:
        return validate_uml(snapshot, rules=req.rules, disable=req.disable)
    except UnknownRuleError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "unknown_rule", "message": str(exc)}
        ) from exc
