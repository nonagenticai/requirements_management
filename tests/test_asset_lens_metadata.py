"""lens_metadata persistence on requirements + decisions (CLR reasoning lenses).

The AA author subroutine authors CLR lenses (clr_assessment 8-category booleans
+ notes, goal_tree_relevance) per asset; RM persists them in a free-form JSONB
`lens_metadata` column exposed on RequirementOut/DecisionOut so fr can render the
real lens grid (vs only the aggregate quality summary).
"""

from __future__ import annotations

import pytest
from fastapi import status

from tests.tenants import T

HEADERS = {"X-Tenant-Id": T("user-lens")}

SAMPLE_LENS = {
    "clr_assessment": {
        "clarity": True,
        "entity_existence": True,
        "causality_existence": True,
        "cause_sufficiency": False,
        "no_additional_cause": True,
        "no_cause_effect_reversal": True,
        "predicted_effect_exists": True,
        "no_tautology": True,
        "notes": "cause_sufficiency flagged: needs a supporting precondition.",
    },
    "goal_tree_relevance": {
        "relevant": True,
        "traces_to": "NC: secure user access",
    },
}


@pytest.mark.asyncio
async def test_requirement_lens_metadata_defaults_empty(test_client):
    """A requirement created without lens_metadata returns an empty dict."""
    resp = await test_client.post(
        "/requirements",
        json={"title": "R-no-lens", "description": "d", "effort": 1.0},
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["lens_metadata"] == {}


@pytest.mark.asyncio
async def test_requirement_lens_metadata_roundtrip(test_client):
    """lens_metadata persists on create and is updatable via PUT."""
    create = await test_client.post(
        "/requirements",
        json={
            "title": "R-lens",
            "description": "d",
            "effort": 1.0,
            "lens_metadata": SAMPLE_LENS,
        },
        headers=HEADERS,
    )
    assert create.status_code == status.HTTP_200_OK, create.text
    rid = create.json()["id"]
    assert create.json()["lens_metadata"] == SAMPLE_LENS

    fetched = await test_client.get(f"/requirements/{rid}", headers=HEADERS)
    assert fetched.status_code == status.HTTP_200_OK
    assert fetched.json()["lens_metadata"]["clr_assessment"]["cause_sufficiency"] is False
    assert (
        fetched.json()["lens_metadata"]["goal_tree_relevance"]["traces_to"]
        == "NC: secure user access"
    )

    updated = await test_client.put(
        f"/requirements/{rid}",
        json={
            "title": "R-lens",
            "description": "d",
            "effort": 1.0,
            "lens_metadata": {"goal_tree_relevance": {"relevant": False}},
        },
        headers=HEADERS,
    )
    assert updated.status_code == status.HTTP_200_OK, updated.text
    assert updated.json()["lens_metadata"] == {"goal_tree_relevance": {"relevant": False}}


@pytest.mark.asyncio
async def test_decision_lens_metadata_roundtrip(test_client):
    """Decisions carry lens_metadata too."""
    create = await test_client.post(
        "/decisions",
        json={
            "title": "D-lens",
            "context": "c",
            "decision": "use SSO",
            "lens_metadata": SAMPLE_LENS,
        },
        headers=HEADERS,
    )
    assert create.status_code == status.HTTP_200_OK, create.text
    assert create.json()["lens_metadata"]["clr_assessment"]["clarity"] is True
