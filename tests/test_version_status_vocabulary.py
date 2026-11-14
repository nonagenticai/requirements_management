"""Pins the ``version_status`` vocabulary and the In/Out mixin split.

Why this file exists: a ``field_validator`` produces a correct 422 but emits NO
``enum`` into the generated OpenAPI schema, so every downstream generated client
renders ``version_status`` as a free-text box. Only a ``Literal`` puts a
top-level ``enum`` on the property. But the vocabulary mixin is inherited by
response models too, so the ``Literal`` may only go on the request-side
subclass — hence the split these tests pin from both directions.
"""

import typing

import pytest
from pydantic import ValidationError

from schemas.common import (
    SERVER_ONLY_VERSION_STATUSES,
    VERSION_STATUSES,
    VersionFieldsIn,
    VersionFieldsMixin,
    VersionStatus,
)


def _property_schema(model: type[VersionFieldsMixin]) -> dict:
    return model.model_json_schema()["properties"]["version_status"]


def _parse(raw: object) -> VersionFieldsIn:
    """Parse an untyped value the way an HTTP request body arrives.

    Deliberately NOT ``VersionFieldsIn(version_status=...)``: several of these
    values are outside the ``Literal``, which is exactly the point of the tests,
    and a static type checker would reject the call site before pydantic ever
    got to demonstrate the runtime coercion or the runtime rejection.
    """
    return VersionFieldsIn.model_validate({"version_status": raw})


def test_literal_and_frozenset_are_the_same_vocabulary():
    """KILLING MUTATION: ``drift-the-table``.

    Add ``"ARCHIVED"`` to ``VersionStatus`` but not to ``VERSION_STATUSES`` (or
    the reverse) and this goes RED. The two names are the request-side type and
    the response-side runtime check for ONE vocabulary; if they can disagree,
    the same value is accepted on write and rejected on read.
    """
    # The ONE sanctioned difference is the server-only set (G8 / R313: SUPERSEDED, written
    # only by promote): readable, never writable — the safe direction.
    assert set(typing.get_args(VersionStatus)) == set(
        VERSION_STATUSES - SERVER_ONLY_VERSION_STATUSES
    )
    assert VERSION_STATUSES == frozenset({"DRAFT", "PUBLISHED", "DEPRECATED", "SUPERSEDED"})
    assert SERVER_ONLY_VERSION_STATUSES == frozenset({"SUPERSEDED"})


def test_server_only_status_is_readable_but_not_writable():
    """KILLING MUTATION: ``let-clients-supersede``. Add SUPERSEDED to the request Literal and
    the reject half goes RED; drop it from VERSION_STATUSES and the read half does."""
    assert VersionFieldsMixin.model_validate({"version_status": "superseded"}).version_status == (
        "SUPERSEDED"
    )
    with pytest.raises(ValidationError):
        _parse("SUPERSEDED")


def test_request_schema_publishes_a_top_level_enum():
    """KILLING MUTATION: ``make-it-optional``.

    Change the annotation to ``VersionStatus | None`` and this goes RED.
    Pydantic emits an optional Literal as ``anyOf: [{enum: [...]}, {null}]``.
    The downstream n8n node generator reads ``enum`` only at the TOP level of a
    property (``generate-node.ts:843``), so a nested enum is invisible and the
    field silently degrades back to a free-text box — the exact way 13
    production fields are already lost. This test protects the whole
    node-generation chain, so it asserts both halves: ``enum`` present at the
    top level, and NO ``anyOf`` wrapper.
    """
    prop = _property_schema(VersionFieldsIn)

    assert "anyOf" not in prop, f"enum must not be nested under anyOf: {prop}"
    assert "enum" in prop, f"no top-level enum on the property: {prop}"
    assert set(prop["enum"]) == set(VERSION_STATUSES - SERVER_ONLY_VERSION_STATUSES)
    assert prop.get("default") == "DRAFT"


def test_request_model_still_normalises_case_and_whitespace():
    """KILLING MUTATION: ``drop-normalisation``.

    Delete the ``mode="before"`` validator on ``VersionFieldsIn`` and this goes
    RED: a bare ``Literal`` would newly REJECT ``" draft "``, which the previous
    ``field_validator`` accepted and coerced. That is a real API regression for
    every existing caller sending lowercase.
    """
    assert _parse(" draft ").version_status == "DRAFT"
    assert _parse("published").version_status == "PUBLISHED"
    assert _parse("DEPRECATED").version_status == "DEPRECATED"


def test_request_model_rejects_a_value_outside_the_vocabulary():
    """KILLING MUTATION: ``widen-to-str``.

    Replace ``VersionStatus`` with ``str`` on ``VersionFieldsIn`` and the reject
    leg goes RED. The accept leg is not optional decoration: without it, a
    mutant that rejects EVERYTHING (e.g. an empty Literal, or a before-validator
    that returns a constant) would pass this test vacuously.
    """
    with pytest.raises(ValidationError):
        _parse("ARCHIVED")

    # The mandatory valid leg — proves the model is not simply refusing all input.
    assert _parse("DRAFT").version_status == "DRAFT"
    assert VersionFieldsIn().version_status == "DRAFT"


def test_response_base_mixin_stays_an_unconstrained_string():
    """KILLING MUTATION: ``collapse-the-split``.

    Move the ``Literal`` onto ``VersionFieldsMixin`` (or make ``VersionFieldsIn``
    the only mixin) and this goes RED.

    WHY THIS IS THE GUARD THAT MATTERS MOST: ``VersionFieldsMixin`` is inherited
    by ``*Out`` RESPONSE models, which parse rows that are already in the
    database. ``entity_version_history.version_status`` holds ``created``
    (34,828 rows) and ``updated`` (5,011) — outside the vocabulary. A ``Literal``
    on the shared base turns one such legacy row into a **500 on the read path**.
    A later well-meaning "tidy-up" that collapses the two mixins must fail loudly
    here rather than in production.
    """
    prop = _property_schema(VersionFieldsMixin)

    assert "enum" not in prop, f"response base must not constrain the type: {prop}"
    assert "anyOf" not in prop
    assert prop.get("type") == "string"

    # And the base subclass must not be typed into rejecting arbitrary strings.
    # (It still runs its own runtime validator, which is a 422 on write — not a
    # parse-time type error — so this asserts on the SCHEMA, the thing an Out
    # model's response serialisation is built from.)
    assert typing.get_type_hints(VersionFieldsMixin)["version_status"] is str
