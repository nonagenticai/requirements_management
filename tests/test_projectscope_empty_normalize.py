"""Schema-level regression tests for the ``ProjectScope`` empty-string normalisation.

Latent HTTP-500 defect (mirrors the ArchitectureView/Standard/TocTree fix, see
memory ``aa-group-a-idempotency-and-empty-scope-2026-07-17``):

The AA get-or-create routers gate on ``if payload.project_id:`` — which is falsy
for ``""`` → the request routes to a plain ``create`` (no IntegrityError catch).
But the entities' partial unique index is ``WHERE project_id IS NOT NULL`` and
``'' IS NOT NULL`` is TRUE, so the index STILL covers a ``""``-scoped row. A
duplicate ``""``-scoped create therefore trips the index on the uncaught
plain-create path → uncaught ``IntegrityError`` → HTTP 500. Gate and index
disagree on whether ``""`` is "scoped".

The three older shipped nullable-scope In schemas (``RequirementIn``,
``DecisionIn``, ``ArchitectureElementIn``) carried this latent gap — their
``project_id`` was a raw ``str | None`` with no normalisation, never hit only
because real AA n8n callers always send a real project_id, never ``""``. Typing
them as ``ProjectScope`` coerces ``"" -> None`` at the API boundary, so an empty
scope means "no project" everywhere and gate + index agree (a DB-level route
test for the 500->200 behaviour lives in
``tests/test_aa_identity_race.py::test_views_empty_string_project_scope_no_500``).
"""

import pytest

from schemas.architecture import ArchitectureElementIn
from schemas.decisions import DecisionIn
from schemas.requirements import RequirementIn


def _requirement(project_id: str | None) -> RequirementIn:
    return RequirementIn(
        title="Test req",
        description="Test description",
        effort=1.0,
        links={"business_objectives": ["obj-1"]},
        project_id=project_id,
    )


def _decision(project_id: str | None) -> DecisionIn:
    return DecisionIn(
        title="Test decision",
        context="Some context",
        decision="We will do the thing",
        project_id=project_id,
    )


def _element(project_id: str | None) -> ArchitectureElementIn:
    return ArchitectureElementIn(
        title="Test element",
        layer="technology",
        element_type="node",
        project_id=project_id,
    )


# Each factory + the human-readable schema name, so the parametrised cases
# assert identical normalisation behaviour across all three fixed schemas.
_FACTORIES = [
    pytest.param(_requirement, id="RequirementIn"),
    pytest.param(_decision, id="DecisionIn"),
    pytest.param(_element, id="ArchitectureElementIn"),
]


@pytest.mark.parametrize("factory", _FACTORIES)
def test_empty_string_project_id_normalises_to_none(factory):
    """``project_id=""`` must become ``None`` so the get-or-create gate skips it."""
    obj = factory(project_id="")
    assert obj.project_id is None


@pytest.mark.parametrize("factory", _FACTORIES)
def test_whitespace_only_project_id_normalises_to_none(factory):
    """Whitespace-only scope (stripped to ``""`` first) must also become ``None``."""
    obj = factory(project_id="   ")
    assert obj.project_id is None


@pytest.mark.parametrize("factory", _FACTORIES)
def test_none_project_id_stays_none(factory):
    """An explicit/absent ``None`` scope is unchanged."""
    obj = factory(project_id=None)
    assert obj.project_id is None


@pytest.mark.parametrize("factory", _FACTORIES)
def test_real_project_id_is_preserved(factory):
    """A real project scope is passed through untouched (still gated + deduped)."""
    obj = factory(project_id="gc-project")
    assert obj.project_id == "gc-project"
