"""Pins the governance (decision / requirement / principle) controlled vocabularies.

Why this file exists: these schemas enforced their closed vocabularies with a
``@field_validator``. That returns a correct 422 but emits **no ``enum``** into
the generated OpenAPI schema, so every generated client — and the n8n node UI —
renders a free-text box for a field the API will reject. Only a ``Literal``
puts a top-level ``enum`` on the property.

The conversion has two failure modes these tests exist to catch:

* the ``Literal`` lands on a ``*Out`` RESPONSE model, where it becomes a NEW
  constraint on rows already in the database (one legacy row ⇒ 500 on read);
* the ``mode="before"`` normaliser does not reproduce the old validator's
  behaviour EXACTLY, silently widening or narrowing what the API accepts.

``decisions.status`` is the sharp case for the second: the old validator did
``v.strip()`` and nothing else — **no ``.lower()``** — so ``"Proposed"`` is
rejected today and must stay rejected.
"""

import typing
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from schemas.decisions import (
    DECISION_STATUSES,
    DecisionIn,
    DecisionOut,
    DecisionStatus,
)
from schemas.principles import PrincipleIn, PrincipleOut
from schemas.requirements import RequirementIn, RequirementOut


def _property_schema(model: type[BaseModel], field: str) -> dict:
    return model.model_json_schema()["properties"][field]


def _decision(**overrides: Any) -> DecisionIn:
    """Build a minimal valid ``DecisionIn``, overriding any field.

    ``overrides`` is deliberately ``Any``-typed: several tests pass values that
    are OUTSIDE the ``Literal`` on purpose (that is the point of the reject
    legs), and a narrower annotation would make the type checker refuse the
    very inputs under test.
    """
    payload: dict[str, Any] = {"title": "T", "context": "c", "decision": "d"}
    payload.update(overrides)
    return DecisionIn(**payload)


# ---------------------------------------------------------------------------
# decisions.status — the converted vocabulary
# ---------------------------------------------------------------------------


def test_decision_literal_and_frozenset_are_the_same_vocabulary():
    """KILLING MUTATION: ``drift-the-table``.

    Add ``"rejected"`` to ``DecisionStatus`` but not to ``DECISION_STATUSES``
    (or the reverse) and this goes RED. The two names are deliberately spelled
    out independently — rather than one derived from the other via
    ``get_args`` — precisely so that drift is a state a test can reach.
    """
    assert set(typing.get_args(DecisionStatus)) == set(DECISION_STATUSES)
    assert DECISION_STATUSES == frozenset({"proposed", "accepted", "deprecated", "superseded"})


def test_decision_request_schema_publishes_a_top_level_enum():
    """KILLING MUTATION: ``make-it-optional``.

    Change the annotation to ``DecisionStatus | None`` and this goes RED.
    Pydantic emits an optional Literal as ``anyOf: [{enum: [...]}, {null}]``;
    the n8n node generator reads ``enum`` only at the TOP level of a property
    (``generate-node.ts:843``), so a nested enum is invisible and the field
    degrades back to a free-text box — the exact way 13 production fields are
    already lost. Reverting the field to ``str`` also goes RED here.
    """
    prop = _property_schema(DecisionIn, "status")

    assert "anyOf" not in prop, f"enum must not be nested under anyOf: {prop}"
    assert "enum" in prop, f"no top-level enum on the property: {prop}"
    assert set(prop["enum"]) == set(DECISION_STATUSES)
    assert prop.get("default") == "proposed"


def test_decision_accepts_every_canonical_status_and_rejects_garbage():
    """KILLING MUTATION: ``widen-to-str``.

    Retyping ``status`` as ``str`` makes the reject leg RED. The accept leg is
    mandatory, not decoration: without it a mutant that rejects EVERYTHING (an
    empty ``Literal``, or a before-validator returning a constant) would pass
    this test vacuously.
    """
    for status in sorted(DECISION_STATUSES):
        assert _decision(status=status).status == status

    assert _decision().status == "proposed"

    with pytest.raises(ValidationError):
        _decision(status="rejected")


def test_decision_status_strips_but_does_not_lowercase():
    """KILLING MUTATION: ``add-lowercasing``.

    This is the normalisation-fidelity test. The pre-conversion validator ran
    ``v.strip()`` and NOTHING else, so ``" proposed "`` was accepted and
    ``"Proposed"`` was rejected. Both halves are pinned:

    * delete the ``mode="before"`` normaliser (``drop-normalisation``) and the
      first assertion goes RED — a bare ``Literal`` newly rejects ``" proposed "``,
      which is a live regression for callers that pad their values (and
      ``str_strip_whitespace`` in ``model_config`` does NOT reach a ``Literal``
      field, so the config is not a substitute);
    * add ``.lower()`` to that normaliser (``add-lowercasing``) and the second
      goes RED — the API would start ACCEPTING ``"Proposed"``, which it 422s
      today. Widening what a public endpoint accepts is not a typing tidy-up.
    """
    assert _decision(status="  proposed  ").status == "proposed"
    assert _decision(status="\taccepted\n").status == "accepted"

    with pytest.raises(ValidationError):
        _decision(status="Proposed")

    with pytest.raises(ValidationError):
        _decision(status="ACCEPTED")


def test_decision_response_model_does_not_constrain_status():
    """KILLING MUTATION: ``collapse-the-in-out-split``.

    Put ``DecisionStatus`` on ``DecisionOut.status`` and this goes RED.
    ``DecisionOut`` parses rows that are ALREADY in the database and carries no
    validator today, so a ``Literal`` there is a NEW constraint on stored data:
    one legacy out-of-set row turns the read path into a 500. The production
    census (10,504 ``proposed`` + 25 ``accepted``, zero strays) justifies
    freezing the WRITE side only; tightening the read side is a data-migration
    decision, not a schema tidy-up.
    """
    prop = _property_schema(DecisionOut, "status")

    assert "enum" not in prop, f"response model must not constrain status: {prop}"
    assert "anyOf" not in prop
    assert prop.get("type") == "string"
    assert typing.get_type_hints(DecisionOut)["status"] is str


# ---------------------------------------------------------------------------
# The In/Out mixin split across all three governance modules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", [DecisionIn, RequirementIn, PrincipleIn])
def test_governance_request_models_publish_the_version_status_enum(model):
    """KILLING MUTATION: ``revert-to-the-response-mixin``.

    Change any of these back to ``class XIn(VersionFieldsMixin, ...)`` and it
    goes RED: the shared base keeps ``version_status`` a plain ``str``, so the
    property loses its top-level ``enum`` and the generated client renders a
    free-text box again.
    """
    prop = _property_schema(model, "version_status")

    assert "anyOf" not in prop, f"{model.__name__}: enum nested under anyOf: {prop}"
    assert "enum" in prop, f"{model.__name__}: no top-level enum: {prop}"
    assert set(prop["enum"]) == {"DRAFT", "PUBLISHED", "DEPRECATED"}


@pytest.mark.parametrize("model", [DecisionOut, RequirementOut, PrincipleOut])
def test_governance_response_models_keep_version_status_unconstrained(model):
    """KILLING MUTATION: ``tighten-the-response-side``.

    Switch any ``*Out`` model to ``VersionFieldsIn`` and this goes RED.
    ``entity_version_history.version_status`` holds ``created`` (34,828 rows)
    and ``updated`` (5,011) — values outside the vocabulary. The moment a
    response model constrains this field, one such row is a 500 on read.
    """
    prop = _property_schema(model, "version_status")

    assert "enum" not in prop, f"{model.__name__}: response constrains it: {prop}"
    assert "anyOf" not in prop
    assert prop.get("type") == "string"
