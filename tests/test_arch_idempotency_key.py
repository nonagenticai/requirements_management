"""A12: architecture concepts are identified by id; ``Idempotency-Key`` replaces title identity.

Target spec 2026-10-26 §6 (Identity, :3454-3456) and the ``POST /modeling/models`` row
(:3370): two concepts may share a title; a retry is made safe by the optional
``Idempotency-Key`` header — same key and same body return the original row, same key with
a different body is ``409 idempotency_conflict``.

Real Postgres, real routes (conftest ``test_client`` opens a FRESH session per request, so
``asyncio.gather`` is two independent transactions).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update

from models.architecture import (
    ArchitectureElement,
    ArchitectureElementRealisationBinding,
    ArchitectureRelationship,
    ArchitectureView,
    Standard,
)
from models.idempotency import IdempotencyKey
from services.idempotency import request_hash
from tests.tenants import T

ELEMENT = {
    "title": "Payments Capability",
    "description": "core payments",
    "layer": "strategy",
    "element_type": "capability",
    "status": "draft",
    "project_id": "a12-project",
}
VIEW = {
    "title": "Application cooperation view",
    "description": "How applications collaborate.",
    "project_id": "a12-project",
    "viewpoint": "application_cooperation",
}
STANDARD = {
    "title": "Use OpenTelemetry for all tracing",
    "description": "Standardise observability on OTel.",
    "project_id": "a12-project",
    "standard_type": "technology_standard",
}


async def _count(factory, model, *where) -> int:
    async with factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model).where(*where))
        ).scalar_one()


# ---------------------------------------------------------------------------
# Same-name concepts are allowed (no title identity)
# ---------------------------------------------------------------------------


async def test_two_same_title_elements_are_two_rows(
    authenticated_client: AsyncClient, test_session_factory
):
    r1 = await authenticated_client.post("/architecture/elements", json=ELEMENT)
    r2 = await authenticated_client.post("/architecture/elements", json=ELEMENT)
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)
    assert r1.json()["id"] != r2.json()["id"]
    assert (
        await _count(
            test_session_factory,
            ArchitectureElement,
            ArchitectureElement.tenant_id == T("user-123"),
            ArchitectureElement.project_id == "a12-project",
            ArchitectureElement.title == ELEMENT["title"],
        )
        == 2
    )


async def test_two_same_title_views_are_two_rows(
    authenticated_client: AsyncClient, test_session_factory
):
    r1 = await authenticated_client.post("/architecture/views", json=VIEW)
    r2 = await authenticated_client.post("/architecture/views", json=VIEW)
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)
    assert r1.json()["id"] != r2.json()["id"]
    assert (
        await _count(
            test_session_factory,
            ArchitectureView,
            ArchitectureView.tenant_id == T("user-123"),
            ArchitectureView.title == VIEW["title"],
        )
        == 2
    )


async def test_two_same_title_standards_are_two_rows(
    authenticated_client: AsyncClient, test_session_factory
):
    r1 = await authenticated_client.post("/architecture/standards", json=STANDARD)
    r2 = await authenticated_client.post("/architecture/standards", json=STANDARD)
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)
    assert r1.json()["id"] != r2.json()["id"]
    assert (
        await _count(
            test_session_factory,
            Standard,
            Standard.tenant_id == T("user-123"),
            Standard.title == STANDARD["title"],
        )
        == 2
    )


async def test_same_title_element_never_overwrites_the_first(authenticated_client: AsyncClient):
    """The A12 defect was a second POST OVERWRITING the first concept."""
    r1 = await authenticated_client.post("/architecture/elements", json=ELEMENT)
    assert r1.status_code == 200, r1.text
    r2 = await authenticated_client.post(
        "/architecture/elements", json={**ELEMENT, "description": "a different concept"}
    )
    assert r2.status_code == 200, r2.text
    first = await authenticated_client.get(f"/architecture/elements/{r1.json()['id']}")
    assert first.json()["description"] == "core payments"


# ---------------------------------------------------------------------------
# Idempotency-Key: replay, conflict, scope, expiry, concurrency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "body", "status"),
    [
        ("/architecture/elements", ELEMENT, 200),
        ("/architecture/views", VIEW, 200),
        ("/architecture/standards", STANDARD, 200),
    ],
)
async def test_same_key_same_body_replays_the_original(
    authenticated_client: AsyncClient, test_session_factory, url: str, body: dict, status: int
):
    headers = {"Idempotency-Key": f"k-{uuid4()}"}
    r1 = await authenticated_client.post(url, json=body, headers=headers)
    r2 = await authenticated_client.post(url, json=body, headers=headers)
    assert r1.status_code == status, r1.text
    assert r2.status_code == r1.status_code
    assert r2.json() == r1.json()
    model = {
        "/architecture/elements": ArchitectureElement,
        "/architecture/views": ArchitectureView,
        "/architecture/standards": Standard,
    }[url]
    assert (
        await _count(
            test_session_factory,
            model,
            model.tenant_id == T("user-123"),
            model.title == body["title"],
        )
        == 1
    )
    stored = await _count(
        test_session_factory,
        IdempotencyKey,
        IdempotencyKey.key == headers["Idempotency-Key"],
        IdempotencyKey.resource_id == r1.json()["id"],
        IdempotencyKey.status_code == status,
    )
    assert stored == 1


async def test_relationship_same_key_replays_without_project(
    authenticated_client: AsyncClient, test_session_factory
):
    """Null project = plain create (no edge get-or-create): only the key dedups."""
    src, tgt = str(uuid4()), str(uuid4())
    body = {
        "source_entity_type": "requirements",
        "source_id": src,
        "target_entity_type": "requirements",
        "target_id": tgt,
        "relationship_type": "traceability",
        "name": "traces to",
    }
    headers = {"Idempotency-Key": "rel-key-1"}
    r1 = await authenticated_client.post("/architecture/relationships", json=body, headers=headers)
    r2 = await authenticated_client.post("/architecture/relationships", json=body, headers=headers)
    assert r1.status_code == 200, r1.text
    assert (r2.status_code, r2.json()) == (r1.status_code, r1.json())
    assert (
        await _count(
            test_session_factory,
            ArchitectureRelationship,
            ArchitectureRelationship.source_id == src,
            ArchitectureRelationship.target_id == tgt,
        )
        == 1
    )
    # Control: without the key the same body is a second edge.
    r3 = await authenticated_client.post("/architecture/relationships", json=body)
    assert r3.status_code == 200, r3.text
    assert r3.json()["id"] != r1.json()["id"]


async def test_same_key_different_body_is_409_idempotency_conflict(
    authenticated_client: AsyncClient, test_session_factory
):
    headers = {"Idempotency-Key": "conflict-key"}
    r1 = await authenticated_client.post("/architecture/elements", json=ELEMENT, headers=headers)
    assert r1.status_code == 200, r1.text
    r2 = await authenticated_client.post(
        "/architecture/elements", json={**ELEMENT, "title": "Other"}, headers=headers
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "idempotency_conflict"
    assert (
        await _count(
            test_session_factory,
            ArchitectureElement,
            ArchitectureElement.tenant_id == T("user-123"),
            ArchitectureElement.project_id == "a12-project",
        )
        == 1
    )


async def test_same_key_is_scoped_per_route(authenticated_client: AsyncClient):
    headers = {"Idempotency-Key": "shared-across-routes"}
    r1 = await authenticated_client.post("/architecture/elements", json=ELEMENT, headers=headers)
    r2 = await authenticated_client.post("/architecture/views", json=VIEW, headers=headers)
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)


async def test_same_key_different_tenant_is_independent(
    test_client: AsyncClient, test_session_factory
):
    key = "tenant-scoped-key"
    ra = await test_client.post(
        "/architecture/elements",
        json=ELEMENT,
        headers={"X-Tenant-Id": T("a12-tenant-a"), "Idempotency-Key": key},
    )
    rb = await test_client.post(
        "/architecture/elements",
        json={**ELEMENT, "title": "Different body, other tenant"},
        headers={"X-Tenant-Id": T("a12-tenant-b"), "Idempotency-Key": key},
    )
    assert (ra.status_code, rb.status_code) == (200, 200), (ra.text, rb.text)
    assert ra.json()["id"] != rb.json()["id"]
    assert await _count(test_session_factory, IdempotencyKey, IdempotencyKey.key == key) == 2


async def test_expired_key_is_treated_as_absent(
    authenticated_client: AsyncClient, test_session_factory
):
    headers = {"Idempotency-Key": "old-key"}
    r1 = await authenticated_client.post("/architecture/elements", json=ELEMENT, headers=headers)
    assert r1.status_code == 200, r1.text
    async with test_session_factory() as session:
        await session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.key == "old-key")
            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
        await session.commit()
    r2 = await authenticated_client.post("/architecture/elements", json=ELEMENT, headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] != r1.json()["id"]
    # The stale claim was replaced, not duplicated.
    assert await _count(test_session_factory, IdempotencyKey, IdempotencyKey.key == "old-key") == 1


async def test_concurrent_same_key_creates_exactly_one_row(
    authenticated_client: AsyncClient, test_session_factory
):
    headers = {"Idempotency-Key": "race-key"}
    body = {**ELEMENT, "title": "Raced Capability"}
    r1, r2 = await asyncio.gather(
        authenticated_client.post("/architecture/elements", json=body, headers=headers),
        authenticated_client.post("/architecture/elements", json=body, headers=headers),
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses in ([200, 200], [200, 409]), (r1.text, r2.text)
    if statuses == [200, 409]:
        loser = r1 if r1.status_code == 409 else r2
        assert loser.json()["error"]["code"] == "idempotency_in_progress"
    else:
        assert r1.json() == r2.json()
    assert (
        await _count(
            test_session_factory,
            ArchitectureElement,
            ArchitectureElement.tenant_id == T("user-123"),
            ArchitectureElement.title == "Raced Capability",
        )
        == 1
    )


@pytest.mark.parametrize("key", ["", " ", "x" * 256])
async def test_invalid_key_is_422(authenticated_client: AsyncClient, test_session_factory, key):
    r = await authenticated_client.post(
        "/architecture/elements", json=ELEMENT, headers={"Idempotency-Key": key}
    )
    assert r.status_code == 422, r.text
    assert await _count(test_session_factory, ArchitectureElement) == 0


async def test_255_char_key_is_accepted(authenticated_client: AsyncClient):
    r = await authenticated_client.post(
        "/architecture/elements", json=ELEMENT, headers={"Idempotency-Key": "x" * 255}
    )
    assert r.status_code == 200, r.text


async def test_failed_create_releases_the_key(
    authenticated_client: AsyncClient, test_session_factory
):
    """A refused write (422) must not leave a claim that blocks the corrected retry."""
    headers = {"Idempotency-Key": "fails-then-ok"}
    bad = await authenticated_client.post(
        "/architecture/elements",
        json={**ELEMENT, "parent_id": str(uuid4())},  # unresolvable parent → refused
        headers=headers,
    )
    assert bad.status_code in (404, 422), bad.text
    assert (
        await _count(test_session_factory, IdempotencyKey, IdempotencyKey.key == "fails-then-ok")
        == 0
    )
    ok = await authenticated_client.post("/architecture/elements", json=ELEMENT, headers=headers)
    assert ok.status_code == 200, ok.text


def test_request_hash_is_key_order_independent():
    assert request_hash({"a": 1, "b": [1, 2]}) == request_hash({"b": [1, 2], "a": 1})
    assert request_hash({"a": 1}) != request_hash({"a": 2})


def test_header_is_documented_in_openapi():
    from main import app

    spec = app.openapi()
    for path in (
        "/architecture/elements",
        "/architecture/views",
        "/architecture/standards",
        "/architecture/relationships",
    ):
        params = spec["paths"][path]["post"].get("parameters", [])
        hdr = [p for p in params if p.get("in") == "header" and p["name"] == "Idempotency-Key"]
        assert len(hdr) == 1, (path, params)
        assert hdr[0].get("required", False) is False


class _CrashAfterCommit(RuntimeError):
    """Stands in for the process dying right after a durable commit."""


SYNTHETIC = {
    "title": "fr/fr.frontend.composer",
    "layer": "technology",
    "element_type": "artifact",
    "is_synthetic": True,
    "project_id": "a12-project",
    "properties": {"code_binding": {"container": "fr", "component_id": "fr.frontend.composer"}},
}


def _rel_body(project_id: str | None) -> dict:
    return {
        "source_entity_type": "requirements",
        "source_id": str(uuid4()),
        "target_entity_type": "requirements",
        "target_id": str(uuid4()),
        "relationship_type": "traceability",
        "name": "traces to",
        "project_id": project_id,
    }


@pytest.mark.parametrize(
    ("url", "body", "status", "model"),
    [
        ("/architecture/elements", ELEMENT, 200, ArchitectureElement),
        ("/architecture/elements", SYNTHETIC, 201, ArchitectureElement),
        ("/architecture/views", VIEW, 200, ArchitectureView),
        ("/architecture/standards", STANDARD, 200, Standard),
        ("/architecture/relationships", _rel_body(None), 200, ArchitectureRelationship),
        ("/architecture/relationships", _rel_body("a12-project"), 200, ArchitectureRelationship),
    ],
    ids=[
        "element",
        "synthetic-element-with-binding",
        "view",
        "standard",
        "relationship",
        "relationship-get-or-create",
    ],
)
async def test_crash_after_create_commit_still_replays(
    authenticated_client: AsyncClient,
    test_session_factory,
    monkeypatch,
    url: str,
    body: dict,
    status: int,
    model,
):
    """The process dies right after the create's FIRST durable commit.

    The claim, the row and the stored response must commit atomically: the resend with the
    same key replays the original status and id — never ``409 idempotency_in_progress`` for
    24h, never a second row.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    real_commit = AsyncSession.commit
    armed = {"on": True}

    async def commit_then_crash(self):
        await real_commit(self)
        if armed["on"]:
            armed["on"] = False
            raise _CrashAfterCommit("process died after commit")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_crash)
    headers = {"Idempotency-Key": f"crash-{uuid4()}"}
    try:
        first = await authenticated_client.post(url, json=body, headers=headers)
        assert first.status_code >= 500, first.text
    except _CrashAfterCommit:
        pass
    assert armed["on"] is False, "the crash never fired"

    retry = await authenticated_client.post(url, json=body, headers=headers)
    assert retry.status_code == status, retry.text
    async with test_session_factory() as session:
        ids = [
            str(i)
            for i in (
                await session.execute(select(model.id).where(model.tenant_id == T("user-123")))
            ).scalars()
        ]
        stored = (
            await session.execute(
                select(IdempotencyKey).where(IdempotencyKey.key == headers["Idempotency-Key"])
            )
        ).scalar_one()
    assert ids == [retry.json()["id"]]
    assert (stored.status_code, str(stored.resource_id)) == (status, retry.json()["id"])
    assert stored.response_body == retry.json()
    if "code_binding" in body.get("properties", {}):
        # The binding row committed with the element, not in a later transaction.
        assert (
            await _count(
                test_session_factory,
                ArchitectureElementRealisationBinding,
                ArchitectureElementRealisationBinding.element_id == retry.json()["id"],
            )
            == 1
        )


# ---------------------------------------------------------------------------
# Scope: the key belongs to the calling SUBJECT, and the hash covers visibility
# ---------------------------------------------------------------------------

_SUBJECT_TENANT = T("a12-subject-scope")


def _subject_headers(subject: str, key: str, visibility: str | None = None) -> dict[str, str]:
    h = {"X-Tenant-Id": _SUBJECT_TENANT, "X-Subject-Id": subject, "Idempotency-Key": key}
    if visibility:
        h["X-Visibility"] = visibility
    return h


async def test_same_key_other_subject_never_replays_a_private_create(
    test_client: AsyncClient, test_session_factory
):
    """Reviewer probe: Alice's private create must not be replayed to Bob (same tenant, key)."""
    key = "shared-key-two-subjects"
    alice = await test_client.post(
        "/architecture/elements",
        json=ELEMENT,
        headers=_subject_headers("alice-idem", key, "user"),
    )
    assert alice.status_code == 200, alice.text
    bob = await test_client.post(
        "/architecture/elements", json=ELEMENT, headers=_subject_headers("bob-idem", key, "user")
    )
    assert bob.status_code == 200, bob.text
    assert bob.json()["id"] != alice.json()["id"], "Bob was handed Alice's private id"
    assert (
        await _count(
            test_session_factory,
            ArchitectureElement,
            ArchitectureElement.tenant_id == _SUBJECT_TENANT,
        )
        == 2
    )
    assert await _count(test_session_factory, IdempotencyKey, IdempotencyKey.key == key) == 2
    # Bob still cannot read Alice's private element.
    peek = await test_client.get(
        f"/architecture/elements/{alice.json()['id']}",
        headers={"X-Tenant-Id": _SUBJECT_TENANT, "X-Subject-Id": "bob-idem"},
    )
    assert peek.status_code == 404, peek.text


async def test_same_key_same_subject_other_visibility_is_409_conflict(
    test_client: AsyncClient, test_session_factory
):
    key = "visibility-switch-key"
    r1 = await test_client.post(
        "/architecture/elements", json=ELEMENT, headers=_subject_headers("carol-idem", key, "user")
    )
    assert r1.status_code == 200, r1.text
    r2 = await test_client.post(
        "/architecture/elements",
        json=ELEMENT,
        headers=_subject_headers("carol-idem", key, "tenant"),
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "idempotency_conflict"
    assert (
        await _count(
            test_session_factory,
            ArchitectureElement,
            ArchitectureElement.tenant_id == _SUBJECT_TENANT,
        )
        == 1
    )


async def test_claim_blocked_by_a_concurrent_transaction_is_409_in_progress(
    test_client: AsyncClient, test_session_factory, monkeypatch
):
    """The claim waits at most ``CLAIM_LOCK_TIMEOUT`` (SET LOCAL), then 409 in progress."""
    from services import idempotency

    monkeypatch.setattr(idempotency, "CLAIM_LOCK_TIMEOUT", "300ms")
    key = "held-by-another-transaction"
    headers = _subject_headers("dave-idem", key)
    async with test_session_factory() as holder:
        # Uncommitted claim for exactly this scope: the route's INSERT blocks on it.
        holder.add(
            IdempotencyKey(
                tenant_id=_SUBJECT_TENANT,
                subject_id="dave-idem",
                key=key,
                method="POST",
                route="/architecture/elements",
                request_hash=request_hash(ELEMENT, visibility="tenant"),
            )
        )
        await holder.flush()
        r = await test_client.post("/architecture/elements", json=ELEMENT, headers=headers)
        await holder.rollback()
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "idempotency_in_progress"
    assert (
        await _count(
            test_session_factory,
            ArchitectureElement,
            ArchitectureElement.tenant_id == _SUBJECT_TENANT,
        )
        == 0
    )
    # The timeout was transaction-scoped: the retry after the holder is gone creates.
    ok = await test_client.post("/architecture/elements", json=ELEMENT, headers=headers)
    assert ok.status_code == 200, ok.text


async def test_keyed_request_collects_the_tenants_expired_keys(
    test_client: AsyncClient, test_session_factory
):
    """Expired keys of OTHER scopes in the caller's tenant are deleted too (bounded)."""
    old = datetime.now(UTC) - timedelta(hours=25)
    async with test_session_factory() as session:
        for i in range(3):
            session.add(
                IdempotencyKey(
                    tenant_id=_SUBJECT_TENANT,
                    subject_id=f"gone-{i}",
                    key=f"stale-{i}",
                    method="POST",
                    route="/architecture/views",
                    request_hash="x",
                    status_code=200,
                    created_at=old,
                )
            )
        # Another tenant's expired key is not this request's to collect.
        session.add(
            IdempotencyKey(
                tenant_id=T("a12-other-gc-tenant"),
                subject_id="",
                key="stale-other",
                method="POST",
                route="/architecture/views",
                request_hash="x",
                status_code=200,
                created_at=old,
            )
        )
        await session.commit()
    r = await test_client.post(
        "/architecture/elements", json=ELEMENT, headers=_subject_headers("erin-idem", "fresh")
    )
    assert r.status_code == 200, r.text
    assert (
        await _count(
            test_session_factory, IdempotencyKey, IdempotencyKey.tenant_id == _SUBJECT_TENANT
        )
        == 1
    )
    assert (
        await _count(test_session_factory, IdempotencyKey, IdempotencyKey.key == "stale-other") == 1
    )


# ---------------------------------------------------------------------------
# pgbouncer safety: the claim's lock_timeout is transaction-LOCAL and reset
# ---------------------------------------------------------------------------


async def test_claim_lock_timeout_is_reset_and_transaction_local(test_engine):
    """``begin()`` must leave ``lock_timeout`` at the server default, now and after commit.

    The service runs behind pgbouncer TRANSACTION pooling: a session-level ``SET`` survives
    the transaction and is handed, with the pooled server connection, to the NEXT client
    (incident 2026-09-22). One connection throughout, so a session-level leak is visible:

    (i)  inside the claim's own transaction, after ``begin()``: ``0`` — the reset ran, so
         the create's own statements are not bound by the claim's timeout;
    (ii) on the SAME connection after that transaction COMMITS: ``0`` — the SET was
         ``LOCAL`` (a session-level SET inside a committed transaction persists; one
         inside a rolled-back transaction would not, which is why this commits).
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from services.idempotency import Idempotency

    async with test_engine.connect() as conn:
        baseline = (await conn.execute(text("SHOW lock_timeout"))).scalar_one()
        await conn.commit()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            idem = Idempotency(
                session,
                route="/architecture/elements",
                tenant_id=T("a12-lock-timeout"),
                subject_id="lock-timeout-probe",
                visibility="tenant",
                key=f"lock-timeout-{uuid4()}",
                body=ELEMENT,
            )
            assert await idem.begin() is None, "the key was not claimed"
            in_claim_txn = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
            await session.commit()
            after_commit = (await conn.execute(text("SHOW lock_timeout"))).scalar_one()
            await conn.rollback()
        finally:
            await session.close()
    assert baseline == "0", f"precondition: server default lock_timeout is {baseline!r}"
    assert in_claim_txn == "0", f"lock_timeout not reset after the claim: {in_claim_txn!r}"
    assert after_commit == "0", f"lock_timeout leaked past the transaction: {after_commit!r}"


# ---------------------------------------------------------------------------
# Race recovery re-claims the key its rollback dropped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "body", "seed_status"),
    [
        ("/architecture/relationships", _rel_body("a12-reclaim"), 200),
        ("/architecture/elements", SYNTHETIC, 201),
    ],
    ids=["relationship-get-or-create", "synthetic-element-upsert"],
)
async def test_race_recovery_reclaims_the_key_and_replays_a_same_key_winner(
    authenticated_client: AsyncClient,
    test_session_factory,
    monkeypatch,
    url: str,
    body: dict,
    seed_status: int,
):
    """Request A hits the IntegrityError recovery; its rollback drops A's claim.

    Exactly then, request B with the SAME key and body claims the key, updates the
    survivor and commits its response. A must re-claim before touching the survivor:
    the key is now complete, so A replays B's stored response and writes nothing. Without
    the re-claim A updates the survivor again behind B's key (and, had B still been in
    flight, would deadlock against B's claim — 40P01 → 500).
    """
    import sys

    import sqlalchemy.exc
    from sqlalchemy.ext.asyncio import AsyncSession

    from services import architecture_upsert

    model = ArchitectureRelationship if "relationships" in url else ArchitectureElement
    seed = await authenticated_client.post(url, json=body)  # the committed survivor
    assert seed.status_code == seed_status, seed.text
    if model is ArchitectureElement:
        # Force the race shape: the binding fast path misses (the winner committed after
        # this request's lookup), so the optimistic INSERT hits the unique index.
        async def _miss(*_a, **_k):
            return None

        monkeypatch.setattr(architecture_upsert, "find_synthetic_by_code_binding", _miss)

    headers = {"Idempotency-Key": f"reclaim-{uuid4()}"}
    real_rollback = AsyncSession.rollback
    fired: dict[str, object] = {}

    async def rollback_then_b_wins(self):
        await real_rollback(self)
        exc_type = sys.exc_info()[0]
        if (
            "armed" not in fired
            and exc_type is not None
            and issubclass(exc_type, sqlalchemy.exc.IntegrityError)
        ):
            fired["armed"] = True  # BEFORE B runs: B's own recovery must not re-fire
            fired["b"] = await authenticated_client.post(url, json=body, headers=headers)

    monkeypatch.setattr(AsyncSession, "rollback", rollback_then_b_wins)
    a = await authenticated_client.post(url, json=body, headers=headers)
    monkeypatch.setattr(AsyncSession, "rollback", real_rollback)

    b = fired.get("b")
    assert b is not None, "request A never reached the IntegrityError recovery path"
    assert b.status_code == 200, b.text
    assert b.json()["id"] == seed.json()["id"]
    async with test_session_factory() as session:
        stored = (
            await session.execute(
                select(IdempotencyKey).where(IdempotencyKey.key == headers["Idempotency-Key"])
            )
        ).scalar_one()
        row = (
            await session.execute(select(model).where(model.id == seed.json()["id"]))
        ).scalar_one()
    assert a.status_code == stored.status_code == 200, a.text
    assert a.json() == stored.response_body == b.json(), "A did not replay B's response"
    # A wrote nothing after B: the survivor is exactly as B's stored response shows it.
    assert row.updated_at == datetime.fromisoformat(b.json()["updated_at"]), (
        row.updated_at,
        b.json()["updated_at"],
    )


async def test_keyed_create_deadlock_is_409_in_progress(test_session):
    """Backstop: an uncaught 40P01 on a keyed create is retryable 409, not 500."""
    import sqlalchemy.exc
    from fastapi import HTTPException

    from services.idempotency import Idempotency

    class _Orig(Exception):
        sqlstate = "40P01"

    def _deadlock() -> sqlalchemy.exc.OperationalError:
        return sqlalchemy.exc.OperationalError("UPDATE ...", {}, _Orig("deadlock detected"))

    def _idem(key: str | None) -> Idempotency:
        return Idempotency(
            test_session,
            route="/architecture/elements",
            tenant_id=T("a12-deadlock"),
            subject_id="",
            visibility="tenant",
            key=key,
            body=ELEMENT,
        )

    with pytest.raises(HTTPException) as caught:
        async with _idem("deadlock-key").guard():
            raise _deadlock()
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "idempotency_in_progress"
    # Unkeyed: untouched (no idempotency contract to retry under).
    with pytest.raises(sqlalchemy.exc.OperationalError):
        async with _idem(None).guard():
            raise _deadlock()


@pytest.mark.parametrize(
    ("url", "body", "seed_status"),
    [
        ("/architecture/relationships", _rel_body("a12-reclaim-busy"), 200),
        ("/architecture/elements", SYNTHETIC, 201),
    ],
    ids=["relationship-get-or-create", "synthetic-element-upsert"],
)
async def test_race_recovery_with_the_key_in_flight_elsewhere_is_409_in_progress(
    authenticated_client: AsyncClient,
    test_session_factory,
    monkeypatch,
    url: str,
    body: dict,
    seed_status: int,
):
    """Same race, but the same-key request B is still IN FLIGHT when A recovers.

    B's claim is uncommitted (a holder transaction, released after 2s). A's re-claim waits
    ``CLAIM_LOCK_TIMEOUT`` (300ms here) and answers ``409 idempotency_in_progress`` without
    touching the survivor. Without the re-claim A goes on to update the survivor and blocks
    on B's claim (in production, while B waits on A's row lock: 40P01 → 500); here the
    holder lets go first, so A then commits a 200 behind B's key — the failure signal.
    """
    import sys

    import sqlalchemy.exc
    from sqlalchemy.ext.asyncio import AsyncSession

    from services import architecture_upsert, idempotency

    monkeypatch.setattr(idempotency, "CLAIM_LOCK_TIMEOUT", "300ms")
    model = ArchitectureRelationship if "relationships" in url else ArchitectureElement
    seed = await authenticated_client.post(url, json=body)
    assert seed.status_code == seed_status, seed.text
    if model is ArchitectureElement:

        async def _miss(*_a, **_k):
            return None

        monkeypatch.setattr(architecture_upsert, "find_synthetic_by_code_binding", _miss)

    key = f"reclaim-busy-{uuid4()}"
    real_rollback = AsyncSession.rollback
    holder = test_session_factory()
    fired: dict[str, object] = {}

    async def _release_later() -> None:
        await asyncio.sleep(2)
        await real_rollback(holder)

    async def rollback_then_b_claims(self):
        await real_rollback(self)
        exc_type = sys.exc_info()[0]
        if (
            "armed" not in fired
            and exc_type is not None
            and issubclass(exc_type, sqlalchemy.exc.IntegrityError)
        ):
            fired["armed"] = True
            holder.add(
                IdempotencyKey(
                    tenant_id=T("user-123"),
                    subject_id="",  # a tenant-only caller (``get_subject_id``)
                    key=key,
                    method="POST",
                    route=url,
                    request_hash=request_hash(body, visibility="tenant"),
                )
            )
            await holder.flush()  # B's claim: held, uncommitted
            fired["release"] = asyncio.create_task(_release_later())

    monkeypatch.setattr(AsyncSession, "rollback", rollback_then_b_claims)
    try:
        a = await authenticated_client.post(url, json=body, headers={"Idempotency-Key": key})
    finally:
        monkeypatch.setattr(AsyncSession, "rollback", real_rollback)
        release = fired.get("release")
        if isinstance(release, asyncio.Task):
            await release
        await holder.close()
    assert fired.get("armed"), "request A never reached the IntegrityError recovery path"
    assert a.status_code == 409, a.text
    assert a.json()["error"]["code"] == "idempotency_in_progress"
    # A wrote no key behind B's (now abandoned) claim.
    assert await _count(test_session_factory, IdempotencyKey, IdempotencyKey.key == key) == 0


async def test_real_two_session_deadlock_on_a_keyed_write_is_409_then_replays(
    test_session_factory,
):
    """A GENUINE Postgres 40P01 (not an injected one) on the pre-reclaim interleaving.

    B holds the key's claim (uncommitted) and waits for the survivor's row lock; A —
    which lost its claim in a recovery rollback and did NOT re-claim (the shape
    ``Idempotency.reclaim`` now prevents) — holds that row lock and stages onto B's
    claim. That closes the cycle. A's ``deadlock_timeout`` is 100ms and B's 10s (both
    ``SET LOCAL``), so Postgres deterministically aborts A, which waits second.

    Expected: A ends ``409 idempotency_in_progress`` (never a 500), B commits, exactly
    one key row and one element exist, and A's same-key retry replays B's response.
    """
    from fastapi import HTTPException
    from pydantic import BaseModel
    from sqlalchemy import text

    from services.idempotency import Idempotency, Replay

    class Out(BaseModel):
        id: str

    tenant = T("a12-real-deadlock")
    key = f"real-deadlock-{uuid4()}"
    async with test_session_factory() as seed:
        survivor = ArchitectureElement(
            slug=f"deadlock-survivor-{uuid4().hex[:8]}",
            title="Deadlock survivor",
            tenant_id=tenant,
            tenant_name="t",
            subject_id="",
            visibility="tenant",
            layer="strategy",
            element_type="capability",
            status="draft",
            properties={},
        )
        seed.add(survivor)
        await seed.commit()
        sid = survivor.id

    def idem(session) -> Idempotency:
        return Idempotency(
            session,
            route="/architecture/elements",
            tenant_id=tenant,
            subject_id="",
            visibility="tenant",
            key=key,
            body=ELEMENT,
        )

    touch = text("UPDATE architecture_elements SET description = :d WHERE id = :id")
    async with test_session_factory() as a, test_session_factory() as b:
        idem_a, idem_b = idem(a), idem(b)
        assert await idem_b.begin() is None  # B claims the key (uncommitted)
        await b.execute(text("SET LOCAL deadlock_timeout = '10s'"))
        b_pid = (await b.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await a.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
        await a.execute(touch, {"d": "A", "id": sid})  # A holds the survivor's row lock

        async def run_b() -> int:
            async with idem_b.guard():
                await b.execute(touch, {"d": "B", "id": sid})  # waits on A's row lock
                await idem_b.stage(200, {"id": str(sid)}, Out)
                await b.commit()
            return 200

        b_task = asyncio.create_task(asyncio.wait_for(run_b(), timeout=10))
        async with test_session_factory() as probe:
            for _ in range(200):  # ≤10s: wait until B is really blocked on the lock
                waiting = (
                    await probe.execute(
                        text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :p"),
                        {"p": b_pid},
                    )
                ).scalar_one_or_none()
                await probe.rollback()
                if waiting == "Lock":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("B never blocked on A's row lock; the interleaving was not driven")

        with pytest.raises(HTTPException) as caught:
            async with idem_a.guard():
                await asyncio.wait_for(idem_a.stage(200, {"id": str(sid)}, Out), timeout=10)
                await a.commit()
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "idempotency_in_progress"
        assert await b_task == 200

    async with test_session_factory() as check:
        keys = (
            (await check.execute(select(IdempotencyKey).where(IdempotencyKey.key == key)))
            .scalars()
            .all()
        )
        row = (
            await check.execute(select(ArchitectureElement).where(ArchitectureElement.id == sid))
        ).scalar_one()
    assert [(k.status_code, k.response_body) for k in keys] == [(200, {"id": str(sid)})]
    assert row.description == "B"  # A's write was rolled back, B's committed
    assert (
        await _count(
            test_session_factory, ArchitectureElement, ArchitectureElement.tenant_id == tenant
        )
        == 1
    )
    # A retries with the same key: B's stored response is replayed, nothing written.
    async with test_session_factory() as retry:
        assert await idem(retry).begin() == Replay(status_code=200, body={"id": str(sid)})
