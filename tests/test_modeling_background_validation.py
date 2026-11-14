"""Background validation of a sealed revision above MODEL_SYNC_MAX_CONCEPTS, real Postgres.

Sealing is bounded by MODEL_ASYNC_MAX_CONCEPTS, so a model above the sync bound can be
sealed, validated in the background and published.

The sync bound is lowered through settings instead of building 5000 rows. Proves: 202 →
poll → the same verdict and findings as a synchronous run of the same revision; the hard
MODEL_ASYNC_MAX_CONCEPTS bound; a run orphaned by a restart (owned by another process
boot) reads as ``failed``; publication refuses while a run is queued or failed.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from models.modeling import ModelingValidationRun
from services import modeling_validation as run_svc
from settings import get_settings
from tests.test_modeling_lifecycle import BOB, element, hdrs, ok, populated
from tests.test_modeling_lifecycle_part2 import code, publish, review, run, seal

pytestmark = pytest.mark.asyncio

KEEP = ("status", "clean", "counts", "examined", "expected", "enabled_rules", "ruleset_id")


async def get_run(client: AsyncClient, run_id: str) -> dict[str, Any]:
    return await ok(await client.get(f"/modeling/validation-runs/{run_id}", headers=hdrs()))


async def findings(client: AsyncClient, run_id: str) -> list[dict[str, Any]]:
    resp = await client.get(f"/modeling/validation-runs/{run_id}/findings", headers=hdrs())
    assert resp.status_code == 200, resp.text
    return resp.json()


async def poll(client: AsyncClient, run_id: str, timeout_s: float = 30.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        got = await get_run(client, run_id)
        if got["status"] not in ("queued", "running"):
            return got
        assert asyncio.get_running_loop().time() < deadline, f"run still {got['status']}"
        await asyncio.sleep(0.05)


async def large_revision(client: AsyncClient) -> tuple[str, str]:
    """A sealed revision with findings (duplicate, unused elements): 5 concepts + 1 rel."""
    w = await populated(client)
    mid = w["model"]["id"]
    await ok(await element(client, mid, "business_actor", title="Dup"))
    await ok(await element(client, mid, "business_actor", title="Dup"))
    rev = await seal(client, mid)
    return mid, rev["revision_id"]


async def test_background_run_matches_a_sync_run(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid, rid = await large_revision(test_client)
    sync = await ok(await run(test_client, mid, {"revision_id": rid}), 201)
    sync_findings = await findings(test_client, sync["run_id"])
    assert sync_findings, "the fixture must produce findings or the comparison is vacuous"

    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 3)
    resp = await run(test_client, mid, {"revision_id": rid})
    assert resp.status_code == 202, resp.text
    queued = resp.json()
    assert queued["status"] in ("queued", "running") and queued["clean"] is False
    assert queued["target"] == "revision" and queued["revision_id"] == rid
    assert queued["content_hash"] == sync["content_hash"] and queued["error"] is None

    done = await poll(test_client, queued["run_id"])
    assert done["status"] == "complete" and done["error"] is None
    assert {k: done[k] for k in KEEP} == {k: sync[k] for k in KEEP}
    assert done["started_at"] is not None and done["finished_at"] is not None
    assert await findings(test_client, done["run_id"]) == sync_findings

    # Same contract as sync for rule refusals; the draft head stays synchronous-only.
    bad = await run(test_client, mid, {"revision_id": rid, "rules": ["AM-NOPE-999"]})
    assert bad.status_code == 422 and code(bad) == "unknown_rule"
    draft = await run(test_client, mid)
    assert draft.status_code == 422 and code(draft) == "input_too_large"
    # Hard upper bound.
    monkeypatch.setattr(get_settings(), "model_async_max_concepts", 4)
    big = await run(test_client, mid, {"revision_id": rid})
    assert big.status_code == 422 and code(big) == "input_too_large"
    assert "MODEL_ASYNC_MAX_CONCEPTS=4" in big.json()["error"]["message"]


async def test_publish_refuses_while_queued_then_accepts(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid, rid = await large_revision(test_client)
    await ok(await review(test_client, mid, rid, "approve", headers=hdrs(BOB)), 201)
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 3)
    slots = run_svc.slots()
    held = 0
    while not slots.locked():  # occupy every slot: the run cannot leave `queued`
        await slots.acquire()
        held += 1
    try:
        queued = await ok(await run(test_client, mid, {"revision_id": rid}), 202)
        await asyncio.sleep(0.1)
        assert (await get_run(test_client, queued["run_id"]))["status"] == "queued"
        assert code(await publish(test_client, mid, rid)) == "revision_not_validated"
    finally:
        for _ in range(held):
            slots.release()
    done = await poll(test_client, queued["run_id"])
    assert done["status"] == "complete" and done["clean"] is True
    pub = await ok(await publish(test_client, mid, rid), 201)
    assert pub["validation_run_id"] == queued["run_id"]


async def test_a_run_orphaned_by_a_restart_reads_failed_and_never_publishes(
    test_client: AsyncClient, test_session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid, rid = await large_revision(test_client)
    await ok(await review(test_client, mid, rid, "approve", headers=hdrs(BOB)), 201)
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 3)
    slots = run_svc.slots()
    held = 0
    while not slots.locked():
        await slots.acquire()
        held += 1
    try:
        queued = await ok(await run(test_client, mid, {"revision_id": rid}), 202)
        running = await ok(await run(test_client, mid, {"revision_id": rid}), 202)
        # Both now belong to a process boot that no longer exists (the pod died).
        async with test_session_factory() as s:
            for rid_, status in ((queued["run_id"], "queued"), (running["run_id"], "running")):
                await s.execute(
                    update(ModelingValidationRun)
                    .where(ModelingValidationRun.id == uuid.UUID(rid_))
                    .values(status=status, owner_boot_id=uuid.uuid4())
                )
            await s.commit()
        got = await get_run(test_client, queued["run_id"])
        assert got["status"] == "failed" and got["error"] == run_svc.ORPHANED
        assert got["clean"] is False and got["finished_at"] is not None
        resp = await test_client.get(f"/modeling/models/{mid}/validation-runs", headers=hdrs())
        assert resp.status_code == 200, resp.text
        assert {r["run_id"]: r["status"] for r in resp.json()} == {
            queued["run_id"]: "failed",
            running["run_id"]: "failed",
        }
        assert code(await publish(test_client, mid, rid)) == "revision_not_validated"
    finally:
        for _ in range(held):
            slots.release()
    # This boot's tasks find nothing they own and leave the verdict alone.
    await asyncio.gather(*run_svc.background_tasks())
    for r in (queued, running):
        assert (await get_run(test_client, r["run_id"]))["status"] == "failed"
    assert code(await publish(test_client, mid, rid)) == "revision_not_validated"


async def test_seal_above_the_sync_bound_then_validate_in_background_and_publish(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole large-model path: seal (201) → 202 → complete → publish by a 2nd subject."""
    w = await populated(test_client)
    mid = w["model"]["id"]
    await ok(await element(test_client, mid, "business_actor", title="Dup"))
    await ok(await element(test_client, mid, "business_actor", title="Dup"))
    # 5 concepts + 1 relationship: above the sync bound, within the async one.
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 3)
    monkeypatch.setattr(get_settings(), "model_async_max_concepts", 6)
    draft = await run(test_client, mid)
    assert draft.status_code == 422 and code(draft) == "input_too_large"
    rev = await seal(test_client, mid)
    rid = rev["revision_id"]
    queued = await ok(await run(test_client, mid, {"revision_id": rid}), 202)
    done = await poll(test_client, queued["run_id"])
    assert done["status"] == "complete" and done["clean"] is True
    assert done["content_hash"] == rev["content_hash"]
    assert await findings(test_client, done["run_id"]), "findings are stored as for a sync run"
    await ok(await review(test_client, mid, rid, "approve", headers=hdrs(BOB)), 201)
    pub = await ok(await publish(test_client, mid, rid, headers=hdrs(BOB)), 201)
    assert pub["validation_run_id"] == queued["run_id"] and pub["published_by"] == BOB


async def test_the_background_queue_is_bounded_with_503_and_retry_after(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid, rid = await large_revision(test_client)
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 3)
    monkeypatch.setattr(get_settings(), "model_async_max_queued", 2)
    slots = run_svc.slots()
    held = 0
    while not slots.locked():  # nothing can leave `queued`: the queue fills
        await slots.acquire()
        held += 1
    try:
        first = await ok(await run(test_client, mid, {"revision_id": rid}), 202)
        second = await ok(await run(test_client, mid, {"revision_id": rid}), 202)
        full = await run(test_client, mid, {"revision_id": rid})
        assert full.status_code == 503, full.text
        assert code(full) == run_svc.QUEUE_FULL
        assert "MODEL_ASYNC_MAX_QUEUED=2" in full.json()["error"]["message"]
        assert int(full.headers["Retry-After"]) > 0
        # A synchronous run is not queued and is not refused.
        monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 5000)
        assert (await run(test_client, mid, {"revision_id": rid})).status_code == 201
    finally:
        for _ in range(held):
            slots.release()
    for queued in (first, second):
        assert (await poll(test_client, queued["run_id"]))["status"] == "complete"
    # Drained: the queue accepts again.
    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 3)
    again = await ok(await run(test_client, mid, {"revision_id": rid}), 202)
    assert (await poll(test_client, again["run_id"]))["status"] == "complete"


async def test_seal_is_bounded_by_a_transaction_local_statement_timeout(
    test_client: AsyncClient, test_session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select, text

    from models.modeling import ModelingModel

    w = await populated(test_client)
    mid = w["model"]["id"]
    monkeypatch.setattr(get_settings(), "model_seal_statement_timeout_ms", 300)
    # A concurrent writer holds the model row: the seal's FOR UPDATE waits on it.
    async with test_session_factory() as holder:
        await holder.execute(
            select(ModelingModel.id).where(ModelingModel.id == uuid.UUID(mid)).with_for_update()
        )
        resp = await asyncio.wait_for(
            test_client.post(f"/modeling/models/{mid}/revisions", headers=hdrs()), timeout=10
        )
        await holder.rollback()
    assert resp.status_code == 503, resp.text
    assert code(resp) == "seal_timeout"
    assert "MODEL_SEAL_STATEMENT_TIMEOUT_MS=300" in resp.json()["error"]["message"]
    assert int(resp.headers["Retry-After"]) > 0
    # Lock released: the seal succeeds, and nothing was sealed by the refused attempt.
    rev = await seal(test_client, mid)
    assert rev["seq"] == 1
    # SET LOCAL: the timeout never outlives the seal's transaction on a pooled connection.
    async with test_session_factory() as s:
        assert (await s.execute(text("SHOW statement_timeout"))).scalar_one() == "0"
