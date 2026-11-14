"""``ADMCompletenessRequest.phase`` must be a closed vocabulary, not a bare ``str``.

Why this file exists — the defect it removes
--------------------------------------------
``phase`` was the last plain ``str`` parameter in ``src/schemas/tools.py`` after
its five siblings were converted to closed ``Literal``s, and
``check_adm_completeness`` answered an unknown phase with **HTTP 200**::

    {"phase": "Z", "completeness_score": 0.0, "passed": [],
     "missing": ["unknown ADM phase 'Z'; valid: A-H"], "recommendations": []}

A caller that reads ``completeness_score`` — which is every caller, and every
dashboard — cannot tell that from "this project genuinely has zero ADM
completeness". A typo therefore produced a confident, wrong, *successful*
answer. Two changes close it: the ``AdmPhase`` Literal rejects the typo at the
request boundary (422, naming the field), and the runtime's unknown-phase branch
now RAISES instead of returning a scored success.

``phase`` is also the one vocabulary in the module whose canonical spelling is
UPPER-case, so ``lower_strip`` is wrong for it and it carries its own
``upper_strip`` ``mode="before"`` normaliser. The pre-Literal coercion was
``phase.upper().strip()``, so ``"a"`` and ``" a "`` were accepted before and must
still be — a bare Literal would have been a real regression for those callers.
"""

from typing import get_args

import pytest
from pydantic import ValidationError

from schemas.tools import ADMCompletenessRequest, AdmPhase
from tools.architecture.adm_completeness import ADM_DELIVERABLES, check_adm_completeness

PROJECT = {"project_id": "proj-adm-phase-test"}


def _members() -> tuple[str, ...]:
    """The declared ``Literal`` members of ``ADMCompletenessRequest.phase``."""
    return get_args(ADMCompletenessRequest.model_fields["phase"].annotation)


# ---------------------------------------------------------------------------
# (a) anti-drift: the Literal vs the table the runtime actually consults
# ---------------------------------------------------------------------------


def test_phase_literal_is_set_equal_to_adm_deliverables() -> None:
    """KILLING MUTATION: drop (or add) a key on either side -> RED.

    ``check_adm_completeness`` dispatches on ``ADM_DELIVERABLES``; the schema
    decides what a client may send. If they drift, either a valid phase 422s at
    the boundary or an unroutable phase reaches the tool. Extending one without
    the other must fail CI, which is the whole point of this assertion.
    """
    assert set(_members()) == set(ADM_DELIVERABLES)
    # The alias and the field must not diverge either.
    assert set(get_args(AdmPhase)) == set(ADM_DELIVERABLES)
    # Member order is the TOGAF phase order, which is what the dropdown shows.
    assert list(_members()) == list(ADM_DELIVERABLES)


# ---------------------------------------------------------------------------
# (b) the pre-Literal coercion survives: "a", " a " and "A" all mean "A"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sent", ["A", "a", " a ", "\ta\n", " A"])
def test_lowercase_and_padded_phases_are_accepted_and_coerced(sent: str) -> None:
    """KILLING MUTATION: delete ``_normalise_phase``, or write it with
    ``lower_strip`` instead of ``upper_strip`` -> every non-canonical spelling
    422s -> RED.

    ``check_adm_completeness`` did ``phase.upper().strip()``, so all of these
    were accepted before the conversion. Note ``str_strip_whitespace=True`` on
    ``ProjectScopeRequest`` does NOT help: it is a property of the *str* core
    schema and never reaches a ``Literal`` field.
    """
    # ty: the parametrised/looped values widen to `str`, and the deliberately
    # out-of-set values below are the POINT of the test — all are runtime
    # assertions, exactly as `test_tool_param_case_normalisation.py` does.
    assert ADMCompletenessRequest(**PROJECT, phase=sent).phase == "A"  # ty: ignore[invalid-argument-type]


def test_every_canonical_member_is_accepted() -> None:
    """The ACCEPT leg — without it "reject everything" passes vacuously."""
    members = _members()
    assert members, "ADMCompletenessRequest.phase declares no Literal members"
    for value in members:
        assert ADMCompletenessRequest(**PROJECT, phase=value).phase == value  # ty: ignore[invalid-argument-type]
        assert (
            ADMCompletenessRequest(**PROJECT, phase=value.lower()).phase  # ty: ignore[invalid-argument-type]
            == value
        )


def test_non_string_input_still_reports_a_type_error() -> None:
    """The normaliser passes non-``str`` through untouched.

    One that stringified its input would turn a client bug (``phase=1``) into a
    silently-accepted value.
    """
    with pytest.raises(ValidationError):
        ADMCompletenessRequest(**PROJECT, phase=1)  # ty: ignore[invalid-argument-type]


# ---------------------------------------------------------------------------
# (c) the defect: an unknown phase must NOT be answerable with a 200
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sent", ["Z", "AB", "", "  ", "I", "a b", "phase-a"])
def test_unknown_phase_is_rejected_at_the_boundary_not_answered_with_200(sent: str) -> None:
    """KILLING MUTATION: revert ``phase`` to
    ``str = Field(..., min_length=1, max_length=1)`` -> "Z" is accepted, the
    endpoint answers 200 with ``completeness_score: 0.0`` -> RED.

    ``"AB"`` and ``""`` are here because the reverted field policed LENGTH; the
    Literal polices MEMBERSHIP and must subsume it. ``"I"`` is the realistic
    typo: the phase after H, which TOGAF does not have.
    """
    with pytest.raises(ValidationError) as excinfo:
        ADMCompletenessRequest(**PROJECT, phase=sent)  # ty: ignore[invalid-argument-type]
    assert "phase" in str(excinfo.value)


@pytest.mark.parametrize("sent", ["Z", "AB", "I"])
def test_direct_call_raises_rather_than_returning_a_scored_success(sent: str) -> None:
    """The tool is public and is called directly, bypassing the schema.

    KILLING MUTATION: restore the ``return {... "completeness_score": 0.0 ...}``
    branch -> RED. A caller reading the score cannot distinguish that from an
    empty-but-valid project, which is the entire defect.
    """
    with pytest.raises(ValueError, match="unknown ADM phase"):
        check_adm_completeness(sent, [], [], [], [], 0, 0, 0)


# ---------------------------------------------------------------------------
# (d) the CONTROL: a valid phase still produces a real, non-degenerate answer
# ---------------------------------------------------------------------------


def test_a_valid_phase_still_produces_a_real_result() -> None:
    """Without this leg, "reject/raise on everything" would pass vacuously.

    Phase A with one of its six element types present must score strictly
    between 0 and 1, list that element type under ``passed`` and keep the full
    output shape — i.e. the tool still WORKS, it just stopped inventing a
    successful answer for input it cannot route.
    """
    elements = [{"element_type": "stakeholder", "id": "e1"}]
    result = check_adm_completeness("a", elements, [], [], [], 0, 0, 0)

    assert result["phase"] == "A"
    assert 0.0 < result["completeness_score"] < 1.0
    assert any("stakeholder" in p for p in result["passed"])
    assert result["missing"], "a partial project must still report what is missing"
    assert not any("unknown" in m for m in result["missing"])
    assert set(result) == {
        "phase",
        "completeness_score",
        "passed",
        "missing",
        "recommendations",
    }


def test_every_phase_is_routable_end_to_end() -> None:
    """Each accepted member must actually reach a deliverables table.

    KILLING MUTATION: add a ninth member to the Literal without adding it to
    ``ADM_DELIVERABLES`` -> the schema accepts it and the tool raises -> RED.
    This is the set-equality test's behavioural twin.
    """
    assert _members()  # get_args() returns () the moment phase stops being a Literal
    for value in _members():
        phase = ADMCompletenessRequest(**PROJECT, phase=value).phase  # ty: ignore[invalid-argument-type]
        result = check_adm_completeness(phase, [], [], [], [], 0, 0, 0)
        assert result["phase"] == value
        assert result["missing"], f"phase {value} declares no required deliverables"


# ---------------------------------------------------------------------------
# (e) the downstream contract: a TOP-LEVEL enum in the JSON schema
# ---------------------------------------------------------------------------


def test_json_schema_carries_a_top_level_enum() -> None:
    """KILLING MUTATION: write the field as ``AdmPhase | None`` -> pydantic
    emits ``anyOf: [{enum: [...]}, {type: "null"}]`` -> RED.

    The n8n node generator reads ``ps.enum`` only at the TOP level of a property
    schema, so an enum nested under ``anyOf`` is invisible and the field
    degrades to the free-text box that let the typo through in the first place.
    """
    prop = ADMCompletenessRequest.model_json_schema()["properties"]["phase"]
    assert "anyOf" not in prop, "phase nested its enum under anyOf"
    assert "allOf" not in prop, "phase nested its enum under allOf"
    assert prop["enum"] == list(_members())
    assert prop["description"].strip()
    assert prop["examples"], "phase has no examples"
    for example in prop["examples"]:
        assert example in _members()
