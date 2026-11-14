"""Pins the closed request vocabularies ``root_layer`` and ``viewpoint`` in ``src/schemas/tools.py``.

Before this file existed, both were plain ``str``. A typo returned HTTP 200 with
a wrong or empty answer (confirmed live in production), and the OpenAPI document
carried no ``enum``, so the downstream n8n node rendered a free-text box instead of
a dropdown.

Three independent guards live here:

* ``test_*_literal_matches_runtime_constant`` — anti-drift. The ``Literal`` must
  stay set-equal to the constant the runtime code actually consults.
* ``test_*_accepts_canonical_and_rejects_garbage`` — the behavioural 422. The
  ACCEPT leg is mandatory: without it, "reject everything" would pass.
* ``test_json_schema_carries_a_top_level_enum`` — protects the whole
  downstream chain: the enum must sit at the TOP level of the property schema,
  never nested under ``anyOf``.
"""

from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

from models.architecture import ARCHITECTURE_LAYERS, ARCHITECTURE_VIEWPOINTS
from schemas.tools import (
    TraceabilityMatrixRequest,
    ViewGenerationRequest,
)
from tools.architecture.traceability import LAYER_ORDER
from tools.architecture.view_generator import VIEWPOINT_FILTERS

# (model, field name, extra kwargs needed to build a valid instance)
_PROJECT: dict[str, Any] = {"project_id": "proj-vocab-test"}

VOCABS: list[tuple[type, str]] = [
    (TraceabilityMatrixRequest, "root_layer"),
    (ViewGenerationRequest, "viewpoint"),
]


def _members(model: type[BaseModel], field: str) -> tuple[str, ...]:
    """The declared ``Literal`` members of ``model.field``, in declaration order."""
    return get_args(model.model_fields[field].annotation)


# ---------------------------------------------------------------------------
# (a) anti-drift: Literal vs the constant the runtime actually reads
# ---------------------------------------------------------------------------


def test_root_layer_literal_matches_runtime_constant() -> None:
    """KILLING MUTATION: add a layer to ARCHITECTURE_LAYERS (or LAYER_ORDER) and
    not to the Literal -> RED."""
    members = _members(TraceabilityMatrixRequest, "root_layer")
    assert set(members) == set(ARCHITECTURE_LAYERS)
    assert set(members) == set(LAYER_ORDER)
    # Member order is the display order, which is what the dropdown shows.
    assert list(members) == LAYER_ORDER


def test_viewpoint_literal_matches_runtime_constant() -> None:
    """KILLING MUTATION: add a key to VIEWPOINT_FILTERS (or a value to
    ARCHITECTURE_VIEWPOINTS) and not to the Literal -> RED."""
    members = _members(ViewGenerationRequest, "viewpoint")
    assert set(members) == set(ARCHITECTURE_VIEWPOINTS)
    assert set(members) == set(VIEWPOINT_FILTERS)
    # Archi 5.10.0's 25 ArchiMate 3.2 viewpoints + 2 RM extensions.
    assert len(members) == 27


# ---------------------------------------------------------------------------
# (b) behaviour: every canonical value is ACCEPTED, garbage is REJECTED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("model", "field"), VOCABS, ids=[f for _m, f in VOCABS])
def test_every_canonical_member_is_accepted(model: type[BaseModel], field: str) -> None:
    """KILLING MUTATION: drop a member from the Literal (or mistype one, e.g.
    'application-cooperation' with a hyphen) -> that value 422s -> RED.

    This is the leg that stops "reject everything" from passing vacuously.
    """
    members = _members(model, field)
    assert members, f"{model.__name__}.{field} declares no Literal members"
    for value in members:
        instance = model(**_PROJECT, **{field: value})
        assert getattr(instance, field) == value


@pytest.mark.parametrize(("model", "field"), VOCABS, ids=[f for _m, f in VOCABS])
def test_garbage_value_is_rejected(model: type[BaseModel], field: str) -> None:
    """KILLING MUTATION: relax the field back to ``str`` -> the typo is accepted,
    the endpoint answers 200 with an empty/wrong result -> RED.

    'stratergy' is the real production shape of this bug: a near-miss spelling.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(**_PROJECT, **{field: "stratergy-not-a-real-value"})
    assert field in str(excinfo.value)


@pytest.mark.parametrize(("model", "field"), VOCABS, ids=[f for _m, f in VOCABS])
def test_empty_string_is_rejected(model: type[BaseModel], field: str) -> None:
    """KILLING MUTATION: relax the field back to ``str`` without ``min_length``
    -> '' is accepted and the tool silently returns nothing -> RED."""
    with pytest.raises(ValidationError):
        model(**_PROJECT, **{field: ""})


def test_defaults_are_unchanged() -> None:
    """KILLING MUTATION: change any default while converting to ``Literal`` ->
    RED. The conversion must be behaviour-preserving for existing callers."""
    assert TraceabilityMatrixRequest(**_PROJECT).root_layer == "strategy"
    # viewpoint is required and has no default.
    assert ViewGenerationRequest.model_fields["viewpoint"].is_required()


# ---------------------------------------------------------------------------
# (c) the downstream contract: a TOP-LEVEL enum in the JSON schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("model", "field"), VOCABS, ids=[f for _m, f in VOCABS])
def test_json_schema_carries_a_top_level_enum(model: type[BaseModel], field: str) -> None:
    """KILLING MUTATION: make the field optional (``Literal[...] | None``) ->
    pydantic emits ``anyOf: [{enum: [...]}, {type: 'null'}]`` -> RED.

    The n8n node generator reads ``ps.enum`` only at the top level of a property
    schema, so an enum nested under ``anyOf`` is invisible and the field renders
    as a free-text box. Thirteen production fields are already lost this way.
    """
    prop = model.model_json_schema()["properties"][field]
    assert "anyOf" not in prop, f"{model.__name__}.{field} nested its enum under anyOf"
    assert "allOf" not in prop, f"{model.__name__}.{field} nested its enum under allOf"
    assert prop["enum"] == list(_members(model, field))


@pytest.mark.parametrize(("model", "field"), VOCABS, ids=[f for _m, f in VOCABS])
def test_json_schema_keeps_description_and_gains_an_example(
    model: type[BaseModel], field: str
) -> None:
    """KILLING MUTATION: drop ``description=`` or ``examples=[...]`` from the
    ``Field(...)`` -> RED. The dropdown label and the sample value both come
    from here."""
    prop = model.model_json_schema()["properties"][field]
    assert prop["description"].strip()
    assert prop["examples"], f"{model.__name__}.{field} has no examples"
    for example in prop["examples"]:
        assert example in _members(model, field)
