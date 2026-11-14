"""Pin the architecture request-side controlled vocabularies to real ``enum``s.

Why this file exists
--------------------
``src/schemas/architecture.py`` used to enforce every controlled vocabulary with
a ``@field_validator`` membership check. That is correct at runtime and
**invisible** to OpenAPI: a validator emits no ``enum``, so every generated
client — and the n8n node UI, which reads ``enum`` only at the TOP level of a
property (``generate-node.ts:843``) — rendered a free-text box for a field the
API would reject.

The fields are now typed ``Literal[...]``. That buys a real ``enum`` but creates
new failure modes, and each test below names the mutation that kills it:

``drift-the-table``
    Add/remove/rename a member of a ``Literal`` (or of the runtime frozenset)
    without touching its twin. The set-equality tests go RED. This is the most
    valuable assertion here: the duplication between frozenset and ``Literal``
    is deliberate, so it needs a guard. Verified: renaming one ``Literal``
    member killed 3 tests.

``drop-normalisation``
    Delete a ``mode="before"`` ``strip().lower()`` validator. Every old
    validator normalised BEFORE checking, so ``layer="Business"`` has always
    been accepted and coerced; a bare ``Literal`` would newly reject it — a real
    regression for existing callers. The round-trip tests go RED. Verified.

``make-it-optional``
    Respell a non-nullable request field as ``Literal | None``. Pydantic then
    emits ``anyOf: [{enum: [...]}, {"type": "null"}]``, the enum is NESTED, and
    the generator cannot see it — the dropdown silently degrades back to free
    text. ``test_non_nullable_request_fields_publish_a_top_level_enum`` goes
    RED. Verified.

``literal-the-out-model``
    "Tidy up" by putting the same ``Literal`` on an ``*Out`` model. ``*Out``
    parses rows ALREADY in the database, so one legacy value would 500 the read
    path. ``test_out_models_publish_no_enum`` goes RED. Verified.

Note two vocabularies are unexercised in production rather than proven:
``architecture_views`` and ``architecture_standards`` are EMPTY tables. Their
production census is vacuous, so these tests are the only thing holding them.

Models are built with ``model_validate`` on a plain dict throughout — the point
of most of these cases is to send values a static checker would reject
(``" Business "``, ``"definitely_not_a_member"``), which is exactly what an HTTP
client does.
"""

from typing import Any, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from models.architecture import (
    ARCHITECTURE_LAYERS,
    ARCHITECTURE_STATES,
    ARCHITECTURE_VIEWPOINTS,
    COMPLIANCE_LEVELS,
    ELEMENT_STATUSES,
    REALISATION_BINDING_KINDS,
    RELATIONSHIP_TYPES,
    STANDARD_STATUSES,
    STANDARD_TYPES,
)
from schemas.architecture import (
    ALL_ELEMENT_TYPES,
    ELEMENT_TYPES_BY_LAYER,
    VALID_ENTITY_TYPES,
    ArchitectureElementIn,
    ArchitectureElementOut,
    ArchitectureElementRealisationBindingIn,
    ArchitectureElementRealisationBindingOut,
    ArchitectureRelationshipIn,
    ArchitectureRelationshipOut,
    ArchitectureRelationshipUpdate,
    ArchitectureViewIn,
    ArchitectureViewOut,
    StandardIn,
    StandardOut,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _literal_members(model: type[BaseModel], field: str) -> frozenset[str]:
    """The string members of a field's ``Literal``, unwrapping ``| None``.

    Fails loudly rather than returning an empty set when the annotation is not a
    ``Literal`` at all — an empty set would let a set-equality assertion pass
    vacuously and silently accept a field reverted to a bare ``str``.
    """
    annotation = model.model_fields[field].annotation
    if get_origin(annotation) is Literal:
        return frozenset(get_args(annotation))
    if get_origin(annotation) in (Union, type(int | None)):
        for arg in get_args(annotation):
            if get_origin(arg) is Literal:
                return frozenset(get_args(arg))
    raise AssertionError(
        f"{model.__name__}.{field} is annotated {annotation!r}, not a Literal — "
        "the controlled vocabulary no longer reaches OpenAPI as an enum."
    )


def _property_schema(model: type[BaseModel], field: str) -> dict[str, Any]:
    return model.model_json_schema()["properties"][field]


ELEMENT_BASE: dict[str, Any] = {
    "title": "T",
    "layer": "business",
    "element_type": "business_process",
}
RELATIONSHIP_BASE: dict[str, Any] = {
    "source_entity_type": "architecture_elements",
    "source_id": "0195f48b-d000-7000-8000-000000000001",
    "target_entity_type": "requirements",
    "target_id": "0195f48b-d000-7000-8000-000000000002",
    "relationship_type": "realization",
}
STANDARD_BASE: dict[str, Any] = {"title": "S", "standard_type": "guideline"}
VIEW_BASE: dict[str, Any] = {"title": "V", "viewpoint": "layered"}
BINDING_BASE: dict[str, Any] = {"binding_kind": "coding_container", "container": "c"}

# (model, field, runtime vocabulary) — one row per converted field.
VOCABULARY_CASES: list[tuple[type[BaseModel], str, frozenset[str]]] = [
    (ArchitectureElementIn, "layer", ARCHITECTURE_LAYERS),
    (ArchitectureElementIn, "element_type", ALL_ELEMENT_TYPES),
    (ArchitectureElementIn, "status", ELEMENT_STATUSES),
    (ArchitectureElementIn, "architecture_state", ARCHITECTURE_STATES),
    (ArchitectureRelationshipIn, "relationship_type", RELATIONSHIP_TYPES),
    (ArchitectureRelationshipIn, "source_entity_type", VALID_ENTITY_TYPES),
    (ArchitectureRelationshipIn, "target_entity_type", VALID_ENTITY_TYPES),
    (ArchitectureRelationshipUpdate, "relationship_type", RELATIONSHIP_TYPES),
    (ArchitectureRelationshipUpdate, "source_entity_type", VALID_ENTITY_TYPES),
    (ArchitectureRelationshipUpdate, "target_entity_type", VALID_ENTITY_TYPES),
    (ArchitectureViewIn, "viewpoint", ARCHITECTURE_VIEWPOINTS),
    (StandardIn, "standard_type", STANDARD_TYPES),
    (StandardIn, "status", STANDARD_STATUSES),
    (StandardIn, "compliance_level", COMPLIANCE_LEVELS),
    (ArchitectureElementRealisationBindingIn, "binding_kind", REALISATION_BINDING_KINDS),
]

_VOCABULARY_IDS = [f"{m.__name__}.{f}" for m, f, _ in VOCABULARY_CASES]


# ---------------------------------------------------------------------------
# 1. anti-drift: the Literal and the runtime frozenset are the same set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("model", "field", "vocabulary"), VOCABULARY_CASES, ids=_VOCABULARY_IDS)
def test_literal_matches_runtime_vocabulary(model, field, vocabulary):
    """MUTATION drift-the-table: change one side only -> RED."""
    assert _literal_members(model, field) == frozenset(vocabulary)


def test_vocabulary_sizes_are_the_documented_ones():
    """MUTATION drift-the-table, applied to BOTH sides at once.

    Set-equality alone cannot see an edit made consistently to the Literal and
    the frozenset together. These counts are the census of the source.
    """
    # 8 = Archi's 7 element groupings + "other" (Location, Grouping, Junction);
    # 61 = every concrete Archi 5.10.0 element/connector type (finding A10).
    assert len(ARCHITECTURE_LAYERS) == 8
    assert len(ALL_ELEMENT_TYPES) == 61
    assert len(ELEMENT_STATUSES) == 4
    assert len(ARCHITECTURE_STATES) == 3
    assert len(RELATIONSHIP_TYPES) == 16  # 20 until R352 dropped the 4 PM scheduling types
    assert len(VALID_ENTITY_TYPES) == 8  # 9 until R352 dropped pm_activities
    # 25 Archi 5.10.0 viewpoints + 2 service extensions (landscape, capability_map).
    assert len(ARCHITECTURE_VIEWPOINTS) == 27
    assert len(STANDARD_TYPES) == 5
    assert len(STANDARD_STATUSES) == 5
    assert len(COMPLIANCE_LEVELS) == 3
    assert len(REALISATION_BINDING_KINDS) == 3


# ---------------------------------------------------------------------------
# 2. every member is accepted; garbage is rejected
# ---------------------------------------------------------------------------


def test_element_in_accepts_every_layer_and_element_type():
    """MUTATION reject-everything: a Literal typo -> RED (not vacuously green)."""
    for layer, types in sorted(ELEMENT_TYPES_BY_LAYER.items()):
        for element_type in sorted(types):
            model = ArchitectureElementIn.model_validate(
                {**ELEMENT_BASE, "layer": layer, "element_type": element_type}
            )
            assert (model.layer, model.element_type) == (layer, element_type)
    assert set(ELEMENT_TYPES_BY_LAYER) == set(ARCHITECTURE_LAYERS)


def test_element_in_accepts_every_status_and_state():
    for status in sorted(ELEMENT_STATUSES):
        model = ArchitectureElementIn.model_validate({**ELEMENT_BASE, "status": status})
        assert model.status == status
    for state in sorted(ARCHITECTURE_STATES):
        model = ArchitectureElementIn.model_validate({**ELEMENT_BASE, "architecture_state": state})
        assert model.architecture_state == state
    defaults = ArchitectureElementIn.model_validate(ELEMENT_BASE)
    assert defaults.status == "draft"
    assert defaults.architecture_state is None


def test_relationship_in_accepts_every_type_and_entity_type():
    for rel_type in sorted(RELATIONSHIP_TYPES):
        model = ArchitectureRelationshipIn.model_validate(
            {**RELATIONSHIP_BASE, "relationship_type": rel_type}
        )
        assert model.relationship_type == rel_type
    for entity_type in sorted(VALID_ENTITY_TYPES):
        model = ArchitectureRelationshipIn.model_validate(
            {
                **RELATIONSHIP_BASE,
                "source_entity_type": entity_type,
                "target_entity_type": entity_type,
            }
        )
        assert model.source_entity_type == entity_type
        assert model.target_entity_type == entity_type


def test_relationship_update_accepts_members_and_omission():
    for rel_type in sorted(RELATIONSHIP_TYPES):
        model = ArchitectureRelationshipUpdate.model_validate({"relationship_type": rel_type})
        assert model.relationship_type == rel_type
    for entity_type in sorted(VALID_ENTITY_TYPES):
        model = ArchitectureRelationshipUpdate.model_validate(
            {"source_entity_type": entity_type, "target_entity_type": entity_type}
        )
        assert model.source_entity_type == entity_type
        assert model.target_entity_type == entity_type
    empty = ArchitectureRelationshipUpdate.model_validate({})
    assert empty.relationship_type is None
    assert empty.source_entity_type is None
    assert empty.target_entity_type is None


def test_view_in_accepts_every_viewpoint():
    for viewpoint in sorted(ARCHITECTURE_VIEWPOINTS):
        model = ArchitectureViewIn.model_validate({**VIEW_BASE, "viewpoint": viewpoint})
        assert model.viewpoint == viewpoint


def test_standard_in_accepts_every_member():
    for standard_type in sorted(STANDARD_TYPES):
        model = StandardIn.model_validate({**STANDARD_BASE, "standard_type": standard_type})
        assert model.standard_type == standard_type
    for status in sorted(STANDARD_STATUSES):
        model = StandardIn.model_validate({**STANDARD_BASE, "status": status})
        assert model.status == status
    for level in sorted(COMPLIANCE_LEVELS):
        model = StandardIn.model_validate({**STANDARD_BASE, "compliance_level": level})
        assert model.compliance_level == level
    defaults = StandardIn.model_validate(STANDARD_BASE)
    assert defaults.status == "assess"
    assert defaults.compliance_level == "recommended"


def test_binding_in_accepts_every_kind():
    shapes: dict[str, dict[str, Any]] = {
        "coding_component": {"container": "c", "component_id": "id"},
        "coding_container": {"container": "c"},
        "external_repo": {"repository_url": "https://example.invalid/r.git"},
    }
    assert set(shapes) == set(REALISATION_BINDING_KINDS)
    for kind, extra in shapes.items():
        model = ArchitectureElementRealisationBindingIn.model_validate(
            {"binding_kind": kind, **extra}
        )
        assert model.binding_kind == kind


@pytest.mark.parametrize(
    ("model", "field", "base"),
    [
        (ArchitectureElementIn, "layer", ELEMENT_BASE),
        (ArchitectureElementIn, "element_type", ELEMENT_BASE),
        (ArchitectureElementIn, "status", ELEMENT_BASE),
        (ArchitectureElementIn, "architecture_state", ELEMENT_BASE),
        (ArchitectureRelationshipIn, "relationship_type", RELATIONSHIP_BASE),
        (ArchitectureRelationshipIn, "source_entity_type", RELATIONSHIP_BASE),
        (ArchitectureRelationshipIn, "target_entity_type", RELATIONSHIP_BASE),
        (ArchitectureRelationshipUpdate, "relationship_type", {}),
        (ArchitectureRelationshipUpdate, "source_entity_type", {}),
        (ArchitectureRelationshipUpdate, "target_entity_type", {}),
        (ArchitectureViewIn, "viewpoint", VIEW_BASE),
        (StandardIn, "standard_type", STANDARD_BASE),
        (StandardIn, "status", STANDARD_BASE),
        (StandardIn, "compliance_level", STANDARD_BASE),
        (ArchitectureElementRealisationBindingIn, "binding_kind", BINDING_BASE),
    ],
    ids=[
        "ArchitectureElementIn.layer",
        "ArchitectureElementIn.element_type",
        "ArchitectureElementIn.status",
        "ArchitectureElementIn.architecture_state",
        "ArchitectureRelationshipIn.relationship_type",
        "ArchitectureRelationshipIn.source_entity_type",
        "ArchitectureRelationshipIn.target_entity_type",
        "ArchitectureRelationshipUpdate.relationship_type",
        "ArchitectureRelationshipUpdate.source_entity_type",
        "ArchitectureRelationshipUpdate.target_entity_type",
        "ArchitectureViewIn.viewpoint",
        "StandardIn.standard_type",
        "StandardIn.status",
        "StandardIn.compliance_level",
        "ArchitectureElementRealisationBindingIn.binding_kind",
    ],
)
def test_garbage_value_is_rejected(model, field, base):
    """MUTATION widen-to-str: drop the Literal -> RED."""
    with pytest.raises(ValidationError):
        model.model_validate({**base, field: "definitely_not_a_member"})


# ---------------------------------------------------------------------------
# 3. normalisation round-trips (every old validator did strip().lower())
# ---------------------------------------------------------------------------


def test_element_in_normalises_case_and_whitespace():
    """MUTATION drop-normalisation: remove the mode='before' validator -> RED."""
    model = ArchitectureElementIn.model_validate(
        {
            "title": "T",
            "layer": "  Business ",
            "element_type": " Business_Process ",
            "status": " DRAFT ",
            "architecture_state": " Baseline ",
        }
    )
    assert model.layer == "business"
    assert model.element_type == "business_process"
    assert model.status == "draft"
    assert model.architecture_state == "baseline"


def test_relationship_in_normalises_case_and_whitespace():
    model = ArchitectureRelationshipIn.model_validate(
        {
            **RELATIONSHIP_BASE,
            "relationship_type": " Realization ",
            "source_entity_type": " Architecture_Elements ",
            "target_entity_type": "  REQUIREMENTS ",
        }
    )
    assert model.relationship_type == "realization"
    assert model.source_entity_type == "architecture_elements"
    assert model.target_entity_type == "requirements"


def test_relationship_update_normalises_case_and_whitespace():
    model = ArchitectureRelationshipUpdate.model_validate(
        {
            "relationship_type": " Serving ",
            "source_entity_type": " TOC_Nodes ",
            "target_entity_type": " Decisions ",
        }
    )
    assert model.relationship_type == "serving"
    assert model.source_entity_type == "toc_nodes"
    assert model.target_entity_type == "decisions"


def test_view_in_normalises_case_and_whitespace():
    model = ArchitectureViewIn.model_validate({"title": "V", "viewpoint": " Capability_Map "})
    assert model.viewpoint == "capability_map"


def test_standard_in_normalises_case_and_whitespace():
    model = StandardIn.model_validate(
        {
            "title": "S",
            "standard_type": " Guideline ",
            "status": " ASSESS ",
            "compliance_level": " Mandatory ",
        }
    )
    assert model.standard_type == "guideline"
    assert model.status == "assess"
    assert model.compliance_level == "mandatory"


def test_binding_in_normalises_case_and_whitespace():
    model = ArchitectureElementRealisationBindingIn.model_validate(
        {"binding_kind": " External_Repo ", "repository_url": "https://example.invalid/r.git"}
    )
    assert model.binding_kind == "external_repo"


# ---------------------------------------------------------------------------
# 4. the enum actually reaches OpenAPI at the TOP level of the property
# ---------------------------------------------------------------------------

_TOP_LEVEL_ENUM_CASES: list[tuple[type[BaseModel], str, frozenset[str]]] = [
    (ArchitectureElementIn, "layer", ARCHITECTURE_LAYERS),
    (ArchitectureElementIn, "element_type", ALL_ELEMENT_TYPES),
    (ArchitectureElementIn, "status", ELEMENT_STATUSES),
    (ArchitectureRelationshipIn, "relationship_type", RELATIONSHIP_TYPES),
    (ArchitectureRelationshipIn, "source_entity_type", VALID_ENTITY_TYPES),
    (ArchitectureRelationshipIn, "target_entity_type", VALID_ENTITY_TYPES),
    (StandardIn, "standard_type", STANDARD_TYPES),
    (StandardIn, "status", STANDARD_STATUSES),
    (StandardIn, "compliance_level", COMPLIANCE_LEVELS),
    (ArchitectureElementRealisationBindingIn, "binding_kind", REALISATION_BINDING_KINDS),
]


@pytest.mark.parametrize(
    ("model", "field", "vocabulary"),
    _TOP_LEVEL_ENUM_CASES,
    ids=[f"{m.__name__}.{f}" for m, f, _ in _TOP_LEVEL_ENUM_CASES],
)
def test_non_nullable_request_fields_publish_a_top_level_enum(model, field, vocabulary):
    """MUTATION make-it-optional: ``Literal | None`` nests the enum under anyOf.

    The n8n generator reads ``enum`` only at the top level of a property
    (``generate-node.ts:843``), so a nested enum silently degrades the control
    back to a free-text box.
    """
    schema = _property_schema(model, field)
    assert "anyOf" not in schema, f"{model.__name__}.{field} nests its enum under anyOf"
    assert frozenset(schema["enum"]) == frozenset(vocabulary)


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (ArchitectureElementIn, "architecture_state"),
        (ArchitectureViewIn, "viewpoint"),
        (ArchitectureRelationshipUpdate, "relationship_type"),
        (ArchitectureRelationshipUpdate, "source_entity_type"),
        (ArchitectureRelationshipUpdate, "target_entity_type"),
    ],
    ids=[
        "ArchitectureElementIn.architecture_state",
        "ArchitectureViewIn.viewpoint",
        "ArchitectureRelationshipUpdate.relationship_type",
        "ArchitectureRelationshipUpdate.source_entity_type",
        "ArchitectureRelationshipUpdate.target_entity_type",
    ],
)
def test_genuinely_nullable_fields_keep_null_and_nest_their_enum(model, field):
    """These five are deliberately ``Literal | None``; pin the trade-off.

    ``architecture_state`` is genuinely nullable (100% NULL in production),
    ``viewpoint`` is NULL for a view without one (Archi's "None", migration
    0072) and
    the ``*Update`` fields form a PARTIAL body where ``None`` means "not
    supplied". Forcing them non-nullable to win a dropdown would invent a value
    on every PATCH. Their enums are therefore anyOf-NESTED and invisible to the
    n8n generator — recorded here so the cost stays explicit.
    """
    schema = _property_schema(model, field)
    branches = schema["anyOf"]
    assert {"type": "null"} in branches
    enums = [b["enum"] for b in branches if "enum" in b]
    assert len(enums) == 1
    assert frozenset(enums[0]) == _literal_members(model, field)


# ---------------------------------------------------------------------------
# 5. the In/Out split — response models must stay unconstrained
# ---------------------------------------------------------------------------

_OUT_CASES: list[tuple[type[BaseModel], str]] = [
    (ArchitectureElementOut, "layer"),
    (ArchitectureElementOut, "element_type"),
    (ArchitectureElementOut, "status"),
    (ArchitectureElementOut, "architecture_state"),
    (ArchitectureRelationshipOut, "relationship_type"),
    (ArchitectureRelationshipOut, "source_entity_type"),
    (ArchitectureRelationshipOut, "target_entity_type"),
    (ArchitectureViewOut, "viewpoint"),
    (StandardOut, "standard_type"),
    (StandardOut, "status"),
    (StandardOut, "compliance_level"),
    (ArchitectureElementRealisationBindingOut, "binding_kind"),
]


@pytest.mark.parametrize(
    ("model", "field"), _OUT_CASES, ids=[f"{m.__name__}.{f}" for m, f in _OUT_CASES]
)
def test_out_models_publish_no_enum(model, field):
    """MUTATION literal-the-out-model: a Literal here -> RED.

    ``*Out`` parses rows that are ALREADY in the database. A single legacy
    out-of-vocabulary row would 500 the read path, so tightening a response
    model is a data-migration decision, not a typing tidy-up.
    """
    schema = _property_schema(model, field)
    assert "enum" not in schema
    assert all("enum" not in branch for branch in schema.get("anyOf", []))


def test_out_model_still_accepts_a_legacy_out_of_vocabulary_row():
    """The concrete reason for the rule above: a stray stored value must READ."""
    row = ArchitectureElementOut.model_validate(
        {
            "id": "0195f48b-d000-7000-8000-000000000001",
            "slug": "s",
            "title": "T",
            "tenant_id": "019de842-c92a-7e37-8fdd-eb0d8c5396f9",
            "layer": "legacy_layer_from_2019",
            "element_type": "not_an_archimate_type",
            "status": "archived",
            "architecture_state": "interim",
        }
    )
    assert row.layer == "legacy_layer_from_2019"
    assert row.element_type == "not_an_archimate_type"
    assert row.status == "archived"
    assert row.architecture_state == "interim"
