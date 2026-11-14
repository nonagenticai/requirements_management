"""The append-only card ledger: ``/pm/cards/{planka_card_id}/ledger`` (R350 (1), 2026-11-04).

A card's lifecycle context (facts and claims written by the n8n card flows) lives in its own RM
table, one row per entry. ``POST`` appends 1..50 entries in one transaction, keyed by
``(tenant_id, planka_card_id)`` only; ``GET`` lists the tenant's entries in
append order with an optional ``kind`` filter. ``laya_score`` / ``accepted`` belong to a claim
only — on a fact they are a 422.

All tests hit real PostgreSQL through the FastAPI test client; only licence introspection is
faked (the ``licences`` fixture), as in ``test_pm_project_intake.py``.
"""

import json
import uuid

import pytest
from httpx import AsyncClient

from tests.test_pm_project_intake import (  # noqa: F401 — `licences` is a fixture
    CONVEYOR_TENANT,
    CUSTOMER_TENANT,
    licences,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("licences")]

CONVEYOR = {"X-Tenant-Id": CUSTOMER_TENANT}
OTHER_TENANT = {"X-Tenant-Id": CONVEYOR_TENANT}
SOURCE = {"list": "DOING", "node": "Record facts", "tool": "rm", "execution_id": "4711"}


def _card() -> str:
    """A Planka-shaped id: a 64-bit snowflake as a string, unique per call."""
    return str(uuid.uuid4().int >> 66)


def _fact(key: str, value: object = "v") -> dict:
    return {"kind": "fact", "key": key, "value": value, "source": SOURCE}


def _claim(key: str, **laya) -> dict:
    return {"kind": "claim", "key": key, "value": {"says": key}, "source": SOURCE, **laya}


async def _append(
    c: AsyncClient, card: str, entries: list, headers: dict = CONVEYOR, want: int = 201
):
    resp = await c.post(f"/pm/cards/{card}/ledger", json={"entries": entries}, headers=headers)
    assert resp.status_code == want, resp.text
    return resp.json()


async def _list(c: AsyncClient, card: str, headers: dict = CONVEYOR, **params) -> list:
    resp = await c.get(f"/pm/cards/{card}/ledger", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_append_facts_and_claim_returns_the_created_rows(test_client: AsyncClient):
    c, card = test_client, _card()
    out = await _append(
        c,
        card,
        [
            _fact("repo", "nonagenticai/x"),
            _fact("branch", {"name": "feat/y"}),
            _claim("tests_pass", laya_score=0.82, accepted=True),
        ],
    )
    assert [e["key"] for e in out] == ["repo", "branch", "tests_pass"]
    assert {e["planka_card_id"] for e in out} == {card}
    assert out[0]["kind"] == "fact" and out[0]["value"] == "nonagenticai/x"
    assert out[0]["laya_score"] is None and out[0]["accepted"] is None
    assert out[1]["value"] == {"name": "feat/y"}
    assert out[2]["kind"] == "claim"
    assert out[2]["laya_score"] == pytest.approx(0.82) and out[2]["accepted"] is True
    assert out[0]["source"] == SOURCE
    assert all("activity_id" not in e for e in out)  # keyed by tenant + card only
    assert len({e["id"] for e in out}) == 3


async def test_source_requires_list_and_execution_id_only(test_client: AsyncClient):
    c, card = test_client, _card()
    minimal = {"list": "QA", "execution_id": "9"}
    out = await _append(c, card, [{"kind": "fact", "key": "k", "value": 1, "source": minimal}])
    assert out[0]["source"] == {**minimal, "node": None, "tool": None}
    for missing in ("list", "execution_id"):
        src = {k: v for k, v in SOURCE.items() if k != missing}
        await _append(c, card, [{"kind": "fact", "key": "k", "value": 1, "source": src}], want=422)


async def test_list_is_append_order_and_filters_by_kind(test_client: AsyncClient):
    c, card = test_client, _card()
    first = await _append(c, card, [_fact("a"), _claim("b")])
    second = await _append(c, card, [_fact("c"), _claim("d", accepted=False)])
    ids = [e["id"] for e in first + second]

    got = await _list(c, card)
    assert [e["key"] for e in got] == ["a", "b", "c", "d"]
    assert sorted(ids) == sorted(e["id"] for e in got)
    stamps = [(e["created_at"], e["id"]) for e in got]
    assert stamps == sorted(stamps)

    assert [e["key"] for e in await _list(c, card, kind="fact")] == ["a", "c"]
    assert [e["key"] for e in await _list(c, card, kind="claim")] == ["b", "d"]
    resp = await c.get(f"/pm/cards/{card}/ledger", params={"kind": "note"}, headers=CONVEYOR)
    assert resp.status_code == 422, resp.text


async def test_tenant_isolation(test_client: AsyncClient):
    c, card = test_client, _card()
    await _append(c, card, [_fact("mine")])
    assert await _list(c, card, headers=OTHER_TENANT) == []
    await _append(c, card, [_fact("theirs")], headers=OTHER_TENANT)
    assert [e["key"] for e in await _list(c, card)] == ["mine"]
    assert [e["key"] for e in await _list(c, card, headers=OTHER_TENANT)] == ["theirs"]


@pytest.mark.parametrize(
    "laya", [{"laya_score": 0.5}, {"accepted": True}, {"accepted": False}, {"reason": "r"}]
)
async def test_laya_fields_on_a_fact_are_422(test_client: AsyncClient, laya: dict):
    c, card = test_client, _card()
    await _append(c, card, [_fact("ok"), {**_fact("bad"), **laya}], want=422)
    assert await _list(c, card) == []  # nothing from the rejected batch landed


async def test_batch_size_is_1_to_50(test_client: AsyncClient):
    c, card = test_client, _card()
    await _append(c, card, [_fact(f"k{i}") for i in range(51)], want=422)
    await _append(c, card, [], want=422)
    assert await _list(c, card) == []
    out = await _append(c, card, [_fact(f"k{i}") for i in range(50)])
    assert len(out) == 50
    assert [e["key"] for e in await _list(c, card)] == [f"k{i}" for i in range(50)]


async def test_append_only_no_update_or_delete_route(test_client: AsyncClient):
    c, card = test_client, _card()
    entry = (await _append(c, card, [_fact("k")]))[0]
    for method in ("PUT", "PATCH", "DELETE"):
        resp = await c.request(method, f"/pm/cards/{card}/ledger", headers=CONVEYOR)
        assert resp.status_code == 405, (method, resp.text)
        resp = await c.request(method, f"/pm/cards/{card}/ledger/{entry['id']}", headers=CONVEYOR)
        assert resp.status_code == 404, (method, resp.text)


async def test_list_is_bounded_and_pages_with_after_id(test_client: AsyncClient):
    c, card = test_client, _card()
    await _append(c, card, [_fact(f"k{i}") for i in range(5)])
    await _append(c, card, [_claim("c0"), _fact("k5")])
    everything = [e["key"] for e in await _list(c, card)]
    assert everything == ["k0", "k1", "k2", "k3", "k4", "c0", "k5"]

    page1 = await _list(c, card, limit=3)
    assert [e["key"] for e in page1] == everything[:3]
    page2 = await _list(c, card, limit=3, after_id=page1[-1]["id"])
    assert [e["key"] for e in page2] == everything[3:6]
    page3 = await _list(c, card, limit=3, after_id=page2[-1]["id"])
    assert [e["key"] for e in page3] == everything[6:]
    assert await _list(c, card, after_id=page3[-1]["id"]) == []
    # after_id combines with kind.
    facts = await _list(c, card, kind="fact", after_id=page1[-1]["id"])
    assert [e["key"] for e in facts] == ["k3", "k4", "k5"]

    for bad in (0, 1001):
        resp = await c.get(f"/pm/cards/{card}/ledger", params={"limit": bad}, headers=CONVEYOR)
        assert resp.status_code == 422, resp.text
    # An id that is not this card's entry (or not this tenant's) is a 404, never a silent page.
    other = (await _append(c, _card(), [_fact("x")]))[0]["id"]
    for anchor in (other, str(uuid.uuid4())):
        resp = await c.get(
            f"/pm/cards/{card}/ledger", params={"after_id": anchor}, headers=CONVEYOR
        )
        assert resp.status_code == 404, resp.text
    resp = await c.get(
        f"/pm/cards/{card}/ledger", params={"after_id": page1[0]["id"]}, headers=OTHER_TENANT
    )
    assert resp.status_code == 404, resp.text


async def test_key_and_value_are_bounded(test_client: AsyncClient):
    c, card = test_client, _card()
    await _append(c, card, [_fact("k" * 200), _fact("v", "x" * (64 * 1024 - 2))])
    await _append(c, card, [_fact("k" * 201)], want=422)
    await _append(c, card, [_fact("v", "x" * (64 * 1024 - 1))], want=422)  # 64 KiB + 1 bytes
    await _append(c, card, [_fact("v", {"blob": ["y" * 1024] * 70})], want=422)
    assert [len(e["key"]) for e in await _list(c, card)] == [200, 1]


@pytest.mark.parametrize("score", ["NaN", "Infinity", "-Infinity"])
async def test_laya_score_must_be_finite(test_client: AsyncClient, score: str):
    c, card = test_client, _card()
    body = '{"entries":[{"kind":"claim","key":"k","value":1,"source":%s,"laya_score":%s}]}' % (
        json.dumps(SOURCE),
        score,
    )
    resp = await c.post(
        f"/pm/cards/{card}/ledger",
        content=body,
        headers={**CONVEYOR, "Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    assert await _list(c, card) == []


async def test_value_cap_counts_utf8_bytes(test_client: AsyncClient):
    c, card = test_client, _card()
    cjk = "漢" * 11_000  # 11k chars, 33 KB of UTF-8: under the cap
    out = await _append(c, card, [_fact("cjk", cjk)])
    assert out[0]["value"] == cjk
    # 22k chars is 66 KB of UTF-8 although only 22k characters: over the cap.
    await _append(c, card, [_fact("cjk", "漢" * 22_000)], want=422)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_nan_inside_value_is_422_not_500(test_client: AsyncClient, literal: str):
    c, card = test_client, _card()
    body = '{"entries":[{"kind":"fact","key":"k","value":{"a":{"b":%s}},"source":%s}]}' % (
        literal,
        json.dumps(SOURCE),
    )
    resp = await c.post(
        f"/pm/cards/{card}/ledger",
        content=body,
        headers={**CONVEYOR, "Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    assert await _list(c, card) == []


async def test_claim_reason_round_trips_and_is_bounded(test_client: AsyncClient):
    c, card = test_client, _card()
    out = await _append(
        c, card, [_claim("c", accepted=False, laya_score=0.3, reason="output cites no test")]
    )
    assert out[0]["reason"] == "output cites no test"
    assert (await _list(c, card))[0]["reason"] == "output cites no test"
    assert (await _append(c, card, [_fact("f")]))[0]["reason"] is None
    await _append(c, card, [_claim("c", reason="r" * 2000)])
    await _append(c, card, [_claim("c", reason="r" * 2001)], want=422)
    assert len(await _list(c, card)) == 3


NUL = "\x00"


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(_fact(f"k{NUL}"), id="key"),
        pytest.param(_claim("c", reason=f"r{NUL}"), id="reason"),
        pytest.param(_fact("v", f"x{NUL}"), id="value-str"),
        pytest.param(_fact("v", {"a": [{"b": f"x{NUL}"}]}), id="value-nested"),
        pytest.param(_fact("v", {f"k{NUL}": 1}), id="value-object-key"),
        pytest.param({**_fact("s"), "source": {**SOURCE, "list": f"D{NUL}"}}, id="source-list"),
        pytest.param({**_fact("s"), "source": {**SOURCE, "node": f"n{NUL}"}}, id="source-node"),
        pytest.param({**_fact("s"), "source": {**SOURCE, "tool": f"t{NUL}"}}, id="source-tool"),
        pytest.param(
            {**_fact("s"), "source": {**SOURCE, "execution_id": f"1{NUL}"}}, id="source-exec"
        ),
    ],
)
async def test_nul_byte_anywhere_is_422_not_500(test_client: AsyncClient, entry: dict):
    c, card = test_client, _card()
    await _append(c, card, [_fact("ok"), entry], want=422)
    assert await _list(c, card) == []
