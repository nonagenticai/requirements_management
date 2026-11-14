"""Schema-level tests for PrincipleIn / DecisionIn origin fields.

Plan ref: ~/.claude/plans/fr-asset-bindings-and-reuse.md (Phase G)

Mirrors test_requirement_origin_schema.py through the shared
OriginFieldsInMixin in schemas/common.py — ensures the validator is
inherited identically across all three asset kinds.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from schemas.decisions import DecisionIn
from schemas.principles import PrincipleIn


def _principle_payload(**overrides):
    return {
        "title": "Test principle",
        "statement": "A statement",
        **overrides,
    }


def _decision_payload(**overrides):
    return {
        "title": "Test decision",
        "context": "Some context for the decision",
        "decision": "A choice was made",
        "consequences": "It had consequences",
        **overrides,
    }


# ---------------------------------------------------------------------------
# Principle
# ---------------------------------------------------------------------------


def test_principle_default_origin_is_tenant():
    p = PrincipleIn(**_principle_payload())
    assert p.origin_kind == "tenant"
    assert p.origin_project_id is None
    assert p.origin_workflow_run_id is None


def test_principle_origin_project_requires_project_id():
    with pytest.raises(ValidationError):
        PrincipleIn(**_principle_payload(origin_kind="project"))


def test_principle_origin_workflow_requires_run_id():
    with pytest.raises(ValidationError):
        PrincipleIn(**_principle_payload(origin_kind="workflow"))


def test_principle_origin_project_ok_with_project_id():
    pid = uuid4()
    p = PrincipleIn(**_principle_payload(origin_kind="project", origin_project_id=pid))
    assert p.origin_project_id == pid


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def test_decision_default_origin_is_tenant():
    d = DecisionIn(**_decision_payload())
    assert d.origin_kind == "tenant"
    assert d.origin_project_id is None


def test_decision_origin_workflow_ok_with_run_id():
    d = DecisionIn(
        **_decision_payload(origin_kind="workflow", origin_workflow_run_id="run-7"),
    )
    assert d.origin_workflow_run_id == "run-7"


def test_decision_origin_tenant_rejects_project_id():
    with pytest.raises(ValidationError):
        DecisionIn(
            **_decision_payload(origin_kind="tenant", origin_project_id=uuid4()),
        )
