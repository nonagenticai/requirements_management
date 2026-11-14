"""``POST /architecture/tools/derive-relationships`` against real Postgres.

Rows are written through the public element / relationship routes, so the route
reads exactly what the write path stores. Asserts the derivation, its
provenance (stored relationship ids in chain order) and the scope contract.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.tenants import T

pytestmark = pytest.mark.asyncio

TENANT = T("derive-relationships")
ALICE, BOB = "alice-derive", "bob-derive"
ROUTE = "/architecture/tools/derive-relationships"


def hdrs(subject: str = ALICE, visibility: str | None = None) -> dict[str, str]:
    h = {"X-Tenant-Id": TENANT, "X-Subject-Id": subject}
    if visibility:
        h["X-Visibility"] = visibility
    return h


async def ok(resp, status: int = 200) -> Any:
    assert resp.status_code == status, f"{resp.request.url} -> {resp.status_code}: {resp.text}"
    return resp.json()


async def element(client: AsyncClient, element_type: str, headers: dict, **extra: Any) -> str:
    body = {"title": f"{element_type}-{uuid.uuid4().hex[:8]}", "element_type": element_type}
    body.update(extra)
    return (await ok(await client.post("/architecture/elements", json=body, headers=headers)))["id"]


async def relate(client: AsyncClient, rtype: str, src: str, tgt: str, headers: dict) -> str:
    body = {
        "source_entity_type": "architecture_elements",
        "source_id": src,
        "target_entity_type": "architecture_elements",
        "target_id": tgt,
        "relationship_type": rtype,
    }
    return (await ok(await client.post("/architecture/relationships", json=body, headers=headers)))[
        "id"
    ]


async def chain(client: AsyncClient, headers: dict, **scope: Any) -> dict[str, str]:
    """actor -assignment-> role -assignment-> process -serving-> process."""
    ids = {
        "actor": await element(client, "business_actor", headers, **scope),
        "role": await element(client, "business_role", headers, **scope),
        "p1": await element(client, "business_process", headers, **scope),
        "p2": await element(client, "business_process", headers, **scope),
    }
    ids["r1"] = await relate(client, "assignment", ids["actor"], ids["role"], headers)
    ids["r2"] = await relate(client, "assignment", ids["role"], ids["p1"], headers)
    ids["r3"] = await relate(client, "serving", ids["p1"], ids["p2"], headers)
    return ids


async def test_model_scope_derives_with_provenance(test_client: AsyncClient):
    model = await ok(
        await client_post_model(test_client),
        201,
    )
    ids = await chain(test_client, hdrs(), model_id=model["id"])

    body = await ok(
        await test_client.post(
            ROUTE,
            json={"model_id": model["id"], "source_id": ids["actor"]},
            headers=hdrs(),
        )
    )
    got = {(d["type"], d["target_id"]): d for d in body["derived"]}
    # AM-DER-STRUCTURAL-CHAIN: assignment ∘ assignment; AM-DER-STRUCT-DEPENDENCY: … ∘ serving.
    assert got[("assignment", ids["p1"])]["path"] == [ids["r1"], ids["r2"]]
    assert got[("assignment", ids["p1"])]["rule_id"] == "AM-DER-STRUCTURAL-CHAIN"
    to_p2 = got[("serving", ids["p2"])]
    assert to_p2["path"] == [ids["r1"], ids["r2"], ids["r3"]]
    assert to_p2["rule_chain"] == ["AM-DER-STRUCTURAL-CHAIN", "AM-DER-STRUCT-DEPENDENCY"]
    assert to_p2["certainty"] == "valid" and to_p2["direct_exists"] is False
    assert {d["source_id"] for d in body["derived"]} == {ids["actor"]}
    assert body["examined"]["relationships"] == 3

    short = await ok(
        await test_client.post(
            ROUTE,
            json={"model_id": model["id"], "source_id": ids["actor"], "max_path_length": 2},
            headers=hdrs(),
        )
    )
    assert {(d["type"], d["target_id"]) for d in short["derived"]} == {("assignment", ids["p1"])}


async def client_post_model(client: AsyncClient, subject: str = ALICE, **extra: Any):
    body = {"language": "archimate", "language_version": "3.2", "title": "M", **extra}
    return await client.post("/modeling/models", json=body, headers=hdrs(subject))


async def test_another_subjects_private_model_is_404(test_client: AsyncClient):
    model = await ok(await client_post_model(test_client, BOB, visibility="user"), 201)
    resp = await test_client.post(ROUTE, json={"model_id": model["id"]}, headers=hdrs(ALICE))
    assert resp.status_code == 404, resp.text


async def test_project_scope_reads_the_effective_view(test_client: AsyncClient):
    project = f"proj-{uuid.uuid4().hex[:8]}"
    ids = await chain(test_client, hdrs(), project_id=project)
    body = await ok(
        await test_client.post(
            ROUTE,
            json={"project_id": project, "target_id": ids["p2"]},
            headers=hdrs(),
        )
    )
    assert {(d["source_id"], d["type"]) for d in body["derived"]} == {
        (ids["actor"], "serving"),
        (ids["role"], "serving"),
    }


@pytest.mark.parametrize(
    "payload",
    [{}, {"model_id": str(uuid.uuid4()), "project_id": "p"}],
    ids=["no-scope", "both-scopes"],
)
async def test_exactly_one_scope_is_required(test_client: AsyncClient, payload):
    resp = await test_client.post(ROUTE, json=payload, headers=hdrs())
    assert resp.status_code == 422, resp.text
