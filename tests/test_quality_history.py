"""``GET …/{id}/quality/history`` — append-only quality history, heuristic-only writer.

The LLM judges that used to append ``evaluator_kind='llm'`` rows were removed on
2026-10-26 (and with them the tests that exercised these five routes). What remains:

* every ``POST …/{id}/quality`` (the heuristic scorer) appends one history row,
  newest first, and the entity row's ``quality_score`` is that same evaluation;
* historical ``evaluator_kind='llm'`` rows already in ``quality_evaluations`` are
  still READ back with their provenance — the response shape is unchanged;
* ``limit`` is honoured and bounded, and another tenant gets 404.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from models.quality_evaluation import QualityEvaluation
from tests.tenants import T

_TENANT = T("user-123")  # the tenant `authenticated_client` presents

_VIEW = {
    "title": "Application Cooperation View",
    "description": "Interactions between application components.",
    "viewpoint": "application_cooperation",
    "element_ids": [],
    "relationship_ids": [],
    "project_id": "qh-project",
}
_DECISION = {
    "title": "Use Postgres",
    "context": "We need a relational store",
    "decision": "Adopt Postgres 18",
    "consequences": "Ops owns backups",
    "status": "accepted",
}
_RISK = {
    "title": "Database migration failure",
    "description": "Risk of data loss during migration",
    "project_id": "qh-project",
    "probability": 0.3,
    "impact_score": 4.0,
    "category": "technical",
    "response_strategy": "mitigate",
    "response_plan": "Rehearse in staging",
    "status": "identified",
    "owner": "dba",
}

# (collection path, create payload, quality_evaluations.entity_type)
_ROUTES: list[Any] = [
    pytest.param("/architecture/views", _VIEW, "architecture_view", id="view"),
    pytest.param("/pm/risks", _RISK, "risk", id="risk"),
    pytest.param("/decisions", _DECISION, "decision", id="decision"),
    pytest.param(
        "/principles",
        {"title": "Data is an asset", "statement": "Treat data as an asset", "slug": "qh-p"},
        "principle",
        id="principle",
    ),
    pytest.param(
        "/architecture/elements",
        {
            "title": "Order Service",
            "description": "Handles orders",
            "layer": "application",
            "element_type": "application_component",
            "project_id": "qh-project",
        },
        "architecture_element",
        id="architecture_element",
    ),
]


async def _create(client, path: str, payload: dict) -> str:
    resp = await client.post(path, json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.parametrize(("path", "payload", "_entity_type"), _ROUTES)
async def test_heuristic_evaluations_are_appended_newest_first(
    authenticated_client, path, payload, _entity_type
) -> None:
    eid = await _create(authenticated_client, path, payload)

    for _ in range(2):
        r = await authenticated_client.post(f"{path}/{eid}/quality")
        assert r.status_code == 200, r.text

    hist = await authenticated_client.get(f"{path}/{eid}/quality/history")
    assert hist.status_code == 200, hist.text
    body = hist.json()
    assert body["entity_id"] == eid
    heur = [i for i in body["items"] if i["evaluator_kind"] == "heuristic"]
    # >= 2, not == 2: the create path may also have run the heuristic.
    assert len(heur) >= 2, body
    assert body["n"] == len(body["items"])
    stamps = [datetime.fromisoformat(i["created_at"]) for i in body["items"]]
    assert stamps == sorted(stamps, reverse=True), "newest first"
    for item in heur:
        assert item["evaluator"] == "heuristic"
        assert item["model_id"] is None
        assert item["served_by"] is None
        assert 0.0 <= item["score"] <= 1.0


@pytest.mark.parametrize(("path", "payload", "entity_type"), _ROUTES)
async def test_historical_llm_rows_are_still_readable(
    authenticated_client, test_session_factory, path, payload, entity_type
) -> None:
    """Rows written by the removed LLM judges keep their wire shape."""
    eid = await _create(authenticated_client, path, payload)
    async with test_session_factory() as s:
        s.add(
            QualityEvaluation(
                tenant_id=_TENANT,
                entity_type=entity_type,
                entity_id=UUID(eid),
                score=0.42,
                issues=["thin"],
                suggestions=["expand"],
                evaluator_kind="llm",
                evaluator="legacy_judge",
                model_id="gemma-4-26b",
                served_by="pgx-vllm",
                prompt_version="3",
                prompt_sha="deadbeef",
                correlation_id="corr-1",
                created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
        )
        await s.commit()

    body = (await authenticated_client.get(f"{path}/{eid}/quality/history")).json()
    llm = [i for i in body["items"] if i["evaluator_kind"] == "llm"]
    assert len(llm) == 1, body
    row = llm[0]
    assert row["score"] == pytest.approx(0.42)
    assert row["evaluator"] == "legacy_judge"
    assert row["model_id"] == "gemma-4-26b"
    assert row["served_by"] == "pgx-vllm"
    assert row["prompt_version"] == "3"
    assert row["prompt_sha"] == "deadbeef"
    assert row["correlation_id"] == "corr-1"
    assert row["issues"] == ["thin"] and row["suggestions"] == ["expand"]


async def test_history_limit_and_tenant_scope(authenticated_client) -> None:
    did = await _create(authenticated_client, "/decisions", {**_DECISION, "title": "T2"})
    for _ in range(3):
        assert (await authenticated_client.post(f"/decisions/{did}/quality")).status_code == 200

    limited = (await authenticated_client.get(f"/decisions/{did}/quality/history?limit=2")).json()
    assert limited["n"] == 2 and len(limited["items"]) == 2

    too_big = await authenticated_client.get(f"/decisions/{did}/quality/history?limit=501")
    assert too_big.status_code == 422

    other = await authenticated_client.get(
        f"/decisions/{did}/quality/history", headers={"X-Tenant-Id": T("someone-else")}
    )
    assert other.status_code == 404, "another tenant must not see this history"
