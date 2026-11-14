"""Schema-level tests for RequirementIn origin fields.

Plan ref: ~/.claude/plans/fr-asset-bindings-and-reuse.md (Phase B)

Exercises the model_validator that enforces origin_kind/origin_project_id/
origin_workflow_run_id consistency at the API boundary, mirroring the
CHECK constraint added by migration 2026_05_19_0024.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from schemas.requirements import RequirementIn


def _base_payload(**overrides):
    return {
        "title": "Test req",
        "description": "Test description",
        "effort": 1.0,
        "links": {"business_objectives": ["obj-1"]},
        **overrides,
    }


def test_default_origin_is_tenant():
    r = RequirementIn(**_base_payload())
    assert r.origin_kind == "tenant"
    assert r.origin_project_id is None
    assert r.origin_workflow_run_id is None
    assert r.created_by_user_id == ""


def test_origin_project_requires_project_id():
    with pytest.raises(ValidationError):
        RequirementIn(**_base_payload(origin_kind="project"))


def test_origin_project_with_project_id_accepted():
    pid = uuid4()
    r = RequirementIn(**_base_payload(origin_kind="project", origin_project_id=pid))
    assert r.origin_kind == "project"
    assert r.origin_project_id == pid


def test_origin_project_rejects_workflow_run_id():
    with pytest.raises(ValidationError):
        RequirementIn(
            **_base_payload(
                origin_kind="project",
                origin_project_id=uuid4(),
                origin_workflow_run_id="run-42",
            )
        )


def test_origin_workflow_requires_run_id():
    with pytest.raises(ValidationError):
        RequirementIn(**_base_payload(origin_kind="workflow"))


def test_origin_workflow_with_run_id_accepted():
    r = RequirementIn(**_base_payload(origin_kind="workflow", origin_workflow_run_id="run-99"))
    assert r.origin_kind == "workflow"
    assert r.origin_workflow_run_id == "run-99"


def test_origin_tenant_rejects_project_id():
    with pytest.raises(ValidationError):
        RequirementIn(**_base_payload(origin_kind="tenant", origin_project_id=uuid4()))


def test_origin_tenant_rejects_workflow_run_id():
    with pytest.raises(ValidationError):
        RequirementIn(**_base_payload(origin_kind="tenant", origin_workflow_run_id="run-1"))


def test_origin_kind_pattern_rejects_unknown():
    with pytest.raises(ValidationError):
        RequirementIn(**_base_payload(origin_kind="bogus"))
