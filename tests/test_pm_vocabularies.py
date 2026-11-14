"""PM controlled vocabularies are published as OpenAPI enums — and stay in sync.

WHAT THIS PINS
--------------
``src/schemas/pm.py`` used to enforce every PM vocabulary with a
``@field_validator`` membership check. That is correct at runtime (a bad value
gets a 422) but it emits **no ``enum``** into the generated OpenAPI, so every
generated client — and the n8n node UI — rendered a free-text box for a field
the API was guaranteed to reject. The fields are now ``Literal[...]``, which
puts a top-level ``enum`` on the property.

Three things can silently undo that, so each has a test below whose KILLING
MUTATION is named in its docstring:

1. ``drift-the-table`` — someone adds a value to a ``models.pm`` frozenset (or
   to a ``Literal``) and not the other. The vocabulary is now duplicated, so it
   can diverge; ``test_literal_matches_runtime_constant`` is the guard, and is
   the most valuable test in this file.
2. ``drop-normalisation`` — someone deletes the ``mode="before"`` validator that
   does ``.strip().lower()``. A bare ``Literal`` would then newly reject
   ``"Active"`` / ``" active "``, input the old validator accepted and coerced.
   That is a live regression, not a tightening.
3. ``nest-the-enum`` / ``tighten-the-response`` — someone spells a field
   ``Literal[...] | None`` (pydantic emits ``anyOf: [{enum: [...]}, {"type":
   "null"}]``, and the n8n generator reads ``enum`` only at the TOP level of a
   property, so the dropdown silently reverts to free text), or narrows an
   ``*Out`` field (a NEW constraint on rows already in the database — one legacy
   value would 500 the read path).

⛔ No ``pytest.skip`` in this file, in any spelling: a skipped test is an
unexercised claim. Every assertion below runs everywhere.
"""

from typing import Any, get_args
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from models.pm import (
    BENEFIT_STATUSES,
    BENEFIT_TYPES,
    PROGRAMME_STATUSES,
    PROJECT_STATUSES,
    RESOURCE_TYPES,
    RISK_CATEGORIES,
    RISK_RESPONSE_STRATEGIES,
    RISK_STATUSES,
)
from schemas.pm import (
    BenefitIn,
    BenefitOut,
    BenefitStatus,
    BenefitType,
    ProgrammeIn,
    ProgrammeOut,
    ProgrammeStatus,
    ProjectIn,
    ProjectOut,
    ProjectStatus,
    ResourceIn,
    ResourceOut,
    ResourceType,
    RiskCategory,
    RiskIn,
    RiskOut,
    RiskResponseStrategy,
    RiskStatus,
)

# ---------------------------------------------------------------------------
# The census: every converted field, in one table the tests iterate over.
# ---------------------------------------------------------------------------
# (model, field, Literal alias, runtime frozenset, matching *Out model)
CONVERTED: list[tuple[type[BaseModel], str, Any, frozenset[str], type[BaseModel]]] = [
    (ProgrammeIn, "status", ProgrammeStatus, PROGRAMME_STATUSES, ProgrammeOut),
    (ProjectIn, "status", ProjectStatus, PROJECT_STATUSES, ProjectOut),
    (ResourceIn, "resource_type", ResourceType, RESOURCE_TYPES, ResourceOut),
    (RiskIn, "status", RiskStatus, RISK_STATUSES, RiskOut),
    (RiskIn, "category", RiskCategory, RISK_CATEGORIES, RiskOut),
    (
        RiskIn,
        "response_strategy",
        RiskResponseStrategy,
        RISK_RESPONSE_STRATEGIES,
        RiskOut,
    ),
    (BenefitIn, "status", BenefitStatus, BENEFIT_STATUSES, BenefitOut),
    (BenefitIn, "benefit_type", BenefitType, BENEFIT_TYPES, BenefitOut),
]

# Every converted field's old validator did EXACTLY ``v.strip().lower()``, so
# every converted field must round-trip mixed case and surrounding whitespace.
# This list is deliberately identical to CONVERTED rather than a subset — if a
# field is ever converted whose old validator did NOT normalise, it belongs in
# CONVERTED but not here.
NORMALISING = list(CONVERTED)


@pytest.fixture(autouse=True)
def _clean_tables():  # noqa: PT004 — deliberate override, see docstring
    """Override ``conftest._clean_tables`` so this file needs no database.

    ⛔ This is NOT a disguised skip. Every test in this module builds a pydantic
    model in memory and inspects a JSON schema; not one opens a connection. The
    conftest fixture of this name is autouse and depends on ``test_engine``,
    which ``pytest.skip``s when postgres is unreachable — so without this
    override a developer with no local postgres gets 92 SKIPPED from a file that
    has no business touching a database, and a suite that never executed reads
    as a passing one. Shadowing the fixture by name (a documented pytest
    behaviour) removes the false dependency instead of hiding behind it: these
    tests now execute on every machine, with or without a database.
    """
    yield


def _minimal_payload(model: type[BaseModel]) -> dict[str, Any]:
    """Smallest body that constructs ``model``, with vocabulary fields omitted.

    Vocabulary fields that are REQUIRED are seeded with a known-good value; each
    test then overrides the one field it is exercising.
    """
    bodies: dict[str, dict[str, Any]] = {
        "ProgrammeIn": {"title": "P"},
        "ProjectIn": {"title": "P"},
        "ResourceIn": {"title": "R", "resource_type": "person"},
        "RiskIn": {
            "title": "R",
            "project_id": "proj-1",
            "probability": 0.5,
            "impact_score": 3.0,
            "category": "technical",
            "response_strategy": "mitigate",
        },
        "BenefitIn": {"title": "B", "benefit_type": "financial", "measurement": "EUR"},
    }
    return dict(bodies[model.__name__])


def _ids(rows: list[tuple[Any, ...]]) -> list[str]:
    return [f"{r[0].__name__}.{r[1]}" for r in rows]


# ---------------------------------------------------------------------------
# 1. Drift
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "field", "literal", "constant", "out_model"),
    CONVERTED,
    ids=_ids(CONVERTED),
)
def test_literal_matches_runtime_constant(
    model: type[BaseModel],
    field: str,
    literal: Any,
    constant: frozenset[str],
    out_model: type[BaseModel],
) -> None:
    """The Literal and the models.pm frozenset are the SAME set of values.

    KILLING MUTATION: ``drift-the-table`` — add ``"archived"`` to any
    ``models.pm`` frozenset without adding it to the matching ``Literal`` in
    ``schemas.pm`` (or the reverse). The vocabulary is duplicated across two
    modules by necessity (a ``Literal`` cannot be built from a runtime
    ``frozenset`` and still be a static type), so this set-equality is the only
    thing stopping the two halves diverging. Divergence is silent and expensive:
    the service layer would accept a value the API rejects, or the OpenAPI would
    advertise a dropdown option the DB layer refuses.
    """
    assert set(get_args(literal)) == set(constant), (
        f"{model.__name__}.{field}: Literal {sorted(get_args(literal))} != "
        f"runtime constant {sorted(constant)}"
    )


# ---------------------------------------------------------------------------
# 2. The enum actually reaches the wire, at the TOP level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "field", "literal", "constant", "out_model"),
    CONVERTED,
    ids=_ids(CONVERTED),
)
def test_request_property_publishes_a_top_level_enum(
    model: type[BaseModel],
    field: str,
    literal: Any,
    constant: frozenset[str],
    out_model: type[BaseModel],
) -> None:
    """The OpenAPI property carries ``enum`` directly, never nested in ``anyOf``.

    KILLING MUTATION: ``nest-the-enum`` — respell the annotation as
    ``Literal[...] | None``. Pydantic then emits ``anyOf: [{enum: [...]},
    {"type": "null"}]``. Runtime validation still passes, every other test in
    this file still passes, and the n8n node generator — which reads ``enum``
    only at the top level of a property (``generate-node.ts:843``) — silently
    renders a free-text box again. This is the ONLY test that catches it.
    """
    prop = model.model_json_schema()["properties"][field]

    assert "anyOf" not in prop, (
        f"{model.__name__}.{field} is nested under anyOf {prop['anyOf']}; the "
        f"downstream generator cannot see a nested enum and the field degrades "
        f"to free text. Use a non-optional Literal."
    )
    assert set(prop.get("enum", [])) == set(constant), (
        f"{model.__name__}.{field} published enum {prop.get('enum')!r}, expected {sorted(constant)}"
    )


# ---------------------------------------------------------------------------
# 3. Accept / reject — the valid case first, so "reject everything" cannot pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "field", "literal", "constant", "out_model"),
    CONVERTED,
    ids=_ids(CONVERTED),
)
def test_every_vocabulary_value_is_accepted(
    model: type[BaseModel],
    field: str,
    literal: Any,
    constant: frozenset[str],
    out_model: type[BaseModel],
) -> None:
    """EVERY value in the vocabulary constructs the model and survives round-trip.

    KILLING MUTATION: ``drop-a-value`` — delete one member from a ``Literal``.
    Without this test the reject-case test below passes vacuously: a field typed
    ``Literal["nothing_valid"]`` would satisfy "garbage is rejected" perfectly
    while rejecting every real value too. Asserting the accept side is what
    makes the reject side mean anything.
    """
    for value in sorted(constant):
        payload = _minimal_payload(model)
        payload[field] = value
        instance = model(**payload)
        assert getattr(instance, field) == value, (
            f"{model.__name__}.{field}={value!r} did not round-trip"
        )


@pytest.mark.parametrize(
    ("model", "field", "literal", "constant", "out_model"),
    CONVERTED,
    ids=_ids(CONVERTED),
)
def test_out_of_vocabulary_value_is_rejected(
    model: type[BaseModel],
    field: str,
    literal: Any,
    constant: frozenset[str],
    out_model: type[BaseModel],
) -> None:
    """A value outside the vocabulary raises ``ValidationError``.

    KILLING MUTATION: ``widen-to-str`` — revert the annotation to a plain
    ``str`` and delete the membership check. The enum disappears from OpenAPI
    AND the API starts accepting junk that the DB layer will choke on.
    """
    payload = _minimal_payload(model)
    payload[field] = "definitely_not_a_real_vocabulary_value"

    with pytest.raises(ValidationError):
        model(**payload)


# ---------------------------------------------------------------------------
# 4. Normalisation is preserved — the regression a bare Literal would cause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "field", "literal", "constant", "out_model"),
    NORMALISING,
    ids=_ids(NORMALISING),
)
def test_case_and_whitespace_are_normalised(
    model: type[BaseModel],
    field: str,
    literal: Any,
    constant: frozenset[str],
    out_model: type[BaseModel],
) -> None:
    """``"  ACTIVE  "``-style input still coerces, exactly as it did before.

    KILLING MUTATION: ``drop-normalisation`` — delete the ``mode="before"``
    ``normalise_vocab_fields`` validator from the model. The ``Literal`` alone
    is case- and whitespace-sensitive, so every caller sending ``"Identified"``
    or a value with stray padding starts getting a 422 on a request the API
    accepted and silently coerced the day before. Converting a validator to a
    Literal must not change WHICH inputs are accepted, only what OpenAPI
    advertises.
    """
    for value in sorted(constant):
        payload = _minimal_payload(model)
        payload[field] = f"  {value.upper()}  "
        instance = model(**payload)
        assert getattr(instance, field) == value, (
            f"{model.__name__}.{field} did not normalise {value.upper()!r} "
            f"back to {value!r} — normalisation was dropped"
        )


def test_non_string_input_still_reaches_the_literal_error() -> None:
    """A non-str value is a clean ``ValidationError``, not an ``AttributeError``.

    KILLING MUTATION: ``unguard-the-normaliser`` — remove the ``isinstance(v,
    str)`` guard from ``_normalise_vocab``. A ``mode="before"`` validator sees
    raw input, so ``123 .strip()`` would raise ``AttributeError`` inside the
    validator and surface as a 500 instead of the 422 this field has always
    returned for a bad type.
    """
    payload = _minimal_payload(RiskIn)
    payload["status"] = 123

    with pytest.raises(ValidationError):
        RiskIn(**payload)


# ---------------------------------------------------------------------------
# 5. The In/Out split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "field", "literal", "constant", "out_model"),
    CONVERTED,
    ids=_ids(CONVERTED),
)
def test_response_model_field_has_no_enum(
    model: type[BaseModel],
    field: str,
    literal: Any,
    constant: frozenset[str],
    out_model: type[BaseModel],
) -> None:
    """The matching ``*Out`` field stays a plain ``str`` with NO enum.

    KILLING MUTATION: ``tighten-the-response`` — apply the same ``Literal`` to
    the ``*Out`` model "for consistency". That is not a typing tidy-up, it is a
    NEW constraint on data already in the database, and a single legacy
    out-of-set row would turn a read into a 500. The request side is where
    freezing a vocabulary is safe, because nothing has been written yet.

    Deliberately asymmetric with the In side, and that asymmetry is the point:
    this test must fail if someone "fixes" it.
    """
    prop = out_model.model_json_schema()["properties"][field]

    assert "enum" not in prop, (
        f"{out_model.__name__}.{field} now publishes enum {prop.get('enum')!r}. "
        f"Response models must stay plain str — narrowing one is a data-migration "
        f"decision, and one legacy row would 500 the read path."
    )
    assert prop.get("type") == "string", (
        f"{out_model.__name__}.{field} should still be a plain string, got {prop!r}"
    )


def test_out_models_accept_a_value_outside_the_vocabulary() -> None:
    """A legacy out-of-set value parses on the response side rather than 500ing.

    This is the behavioural half of the test above: the schema shape says "no
    enum", and this says what that BUYS us. ``RiskOut`` must swallow a status
    that ``RiskIn`` would reject.

    KILLING MUTATION: ``tighten-the-response`` — same as above, caught here as a
    raised ``ValidationError`` rather than as a schema difference.

    NB the values below are deliberately short: ``RiskOut`` keeps the original
    ``max_length`` bounds (20/20/30), which are NOT what this test is about. A
    legacy value has to be out-of-vocabulary but within length, or the test
    would pass for the wrong reason.
    """
    legacy = RiskOut(
        id=uuid4(),
        slug="r-1",
        title="Legacy risk",
        tenant_id="t-1",
        project_id="proj-1",
        probability=0.5,
        impact_score=3.0,
        risk_score=1.5,
        category="legacy_category",
        response_strategy="legacy_strategy",
        status="legacy_status",
    )

    assert legacy.status == "legacy_status"
    assert legacy.category == "legacy_category"
    assert legacy.response_strategy == "legacy_strategy"

    # The same values must be REJECTED on the request side. Without this half,
    # the test above would still pass if RiskIn were quietly widened back to str.
    #
    # Built through a dict rather than as keyword arguments on purpose: with the
    # Literal in place ``ty`` rejects the bad values STATICALLY, so the direct
    # call would not type-check. That static rejection is the conversion working
    # — the point of this test is the RUNTIME 422, so the payload is assembled
    # dynamically to get past the checker and actually exercise pydantic.
    rejected = _minimal_payload(RiskIn)
    rejected["category"] = "legacy_category"
    rejected["response_strategy"] = "legacy_strategy"
    rejected["status"] = "legacy_status"
    with pytest.raises(ValidationError):
        RiskIn(**rejected)


# ---------------------------------------------------------------------------
# 6. version_status rides along on VersionFieldsIn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [ProgrammeIn, ProjectIn, ResourceIn, RiskIn, BenefitIn],
    ids=lambda m: m.__name__,
)
def test_request_models_publish_version_status_enum(model: type[BaseModel]) -> None:
    """PM request models inherit ``VersionFieldsIn``, so version_status is an enum.

    KILLING MUTATION: ``revert-the-mixin`` — change one of these ``*In`` classes
    back to ``VersionFieldsMixin``. ``version_status`` reverts to a plain ``str``
    and that model's clients lose the dropdown, silently and only for that one
    entity. The matching ``*Out`` models must stay on ``VersionFieldsMixin``,
    which the companion test below pins.
    """
    prop = model.model_json_schema()["properties"]["version_status"]

    assert "anyOf" not in prop, f"{model.__name__}.version_status nested under anyOf"
    assert set(prop.get("enum", [])) == {"DRAFT", "PUBLISHED", "DEPRECATED"}, (
        f"{model.__name__}.version_status published {prop.get('enum')!r}"
    )


@pytest.mark.parametrize(
    "out_model",
    [ProgrammeOut, ProjectOut, ResourceOut, RiskOut, BenefitOut],
    ids=lambda m: m.__name__,
)
def test_response_models_keep_version_status_as_plain_str(
    out_model: type[BaseModel],
) -> None:
    """``*Out`` models stay on ``VersionFieldsMixin`` — version_status has no enum.

    KILLING MUTATION: ``tighten-the-response`` — switch an ``*Out`` model to
    ``VersionFieldsIn``. ``entity_version_history.version_status`` holds
    ``created`` (34,828 rows) and ``updated`` (5,011), values outside the
    vocabulary; the moment a response model serves that table a ``Literal``
    turns each of those rows into a 500.
    """
    prop = out_model.model_json_schema()["properties"]["version_status"]

    assert "enum" not in prop, (
        f"{out_model.__name__}.version_status now publishes an enum; response "
        f"models must stay on VersionFieldsMixin"
    )
