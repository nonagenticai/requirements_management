"""Concurrency (race) tests for the AA-intake get-or-create backstop (WS1).

The intake gates were best-effort check-then-insert races: two concurrent
re-POSTs both pass the SELECT, both mint a distinct auto-incremented slug, and
both INSERT a duplicate. The WS1 partial unique indexes are the real backstop —
one of the two racing INSERTs fails loud, and ``get_or_create`` recovers by
rolling back and re-SELECTing the survivor (mirrors
``services/realisation_bindings.attach_binding``).

The conftest ``test_client`` overrides ``get_session`` with a factory that opens
a FRESH session per request, so ``asyncio.gather(client.post, client.post)`` is
two real, independent transactions racing for the same identity key. The
assertion — both requests succeed (no 500), both return the SAME id, and exactly
ONE row lands — holds regardless of the exact interleaving: whichever request
loses the INSERT race takes the ``IntegrityError`` → rollback → re-SELECT →
update path and returns the winner's id.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import func, select

from models.architecture import (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    Standard,
)
from models.decisions import Decision
from models.pm import Project, Resource, Risk
from models.principles import Principle
from models.requirements import Requirement
from models.toc import TocNode, TocTree
from tests.briefs import MIN_BRIEF
from tests.project_types import typed
from tests.tenants import T


async def _gather_two(client: AsyncClient, url: str, payload: dict, headers: dict | None = None):
    first, second = await asyncio.gather(
        client.post(url, json=payload, headers=headers),
        client.post(url, json=payload, headers=headers),
    )
    # Endpoints that distinguish create (201) from reuse (200) — e.g.
    # /pm/risks with a project_id — return 201 for the race winner and 200 for the
    # loser (updated in place); endpoints that always return 200 return 200 for
    # both. Either way both must be 2xx and resolve to the SAME surviving row.
    assert first.status_code in (200, 201), first.text
    assert second.status_code in (200, 201), second.text
    # Both concurrent POSTs resolve to the SAME surviving row.
    assert first.json()["id"] == second.json()["id"]
    return first.json()["id"]


async def _count(test_session_factory, model, *where) -> int:
    async with test_session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model).where(*where))
        ).scalar()


async def test_requirements_race_single_row(
    authenticated_client: AsyncClient, test_session_factory
):
    payload = {
        "title": "Users can reset their password",
        "description": "As a user I can reset my password via email.",
        "effort": 3.0,
        "project_id": "gc-project",
    }
    await _gather_two(authenticated_client, "/requirements", payload)
    count = await _count(
        test_session_factory,
        Requirement,
        Requirement.tenant_id == T("user-123"),
        Requirement.project_id == "gc-project",
        Requirement.title == "Users can reset their password",
    )
    assert count == 1


async def test_decisions_race_single_row(authenticated_client: AsyncClient, test_session_factory):
    payload = {
        "title": "Adopt Postgres as the primary datastore",
        "context": "We need a relational store with strong consistency.",
        "decision": "Use Postgres 18 with psycopg 3.",
        "project_id": "gc-project",
    }
    await _gather_two(authenticated_client, "/decisions", payload)
    count = await _count(
        test_session_factory,
        Decision,
        Decision.tenant_id == T("user-123"),
        Decision.project_id == "gc-project",
        Decision.title == "Adopt Postgres as the primary datastore",
    )
    assert count == 1


async def test_pm_resources_race_single_row(
    authenticated_client: AsyncClient, test_session_factory
):
    stable_key = f"card-{uuid4()}:resource:0"
    payload = {"title": "Senior Developer", "resource_type": "person", "stable_key": stable_key}
    await _gather_two(authenticated_client, "/pm/resources", payload)
    count = await _count(
        test_session_factory,
        Resource,
        Resource.tenant_id == T("user-123"),
        Resource.stable_key == stable_key,
    )
    assert count == 1


async def test_pm_resources_same_title_different_stable_key_race_two_rows(
    authenticated_client: AsyncClient, test_session_factory
):
    """Concurrent same-title / DIFFERENT-stable_key resource POSTs → TWO rows, no 500.

    The two racers derive the SAME base slug from the shared title but carry
    distinct ``stable_key`` values, so the identity index
    ``uq_pm_resources_tenant_stable_key`` does NOT collide — only the plain
    per-tenant slug constraint ``uq_pm_resources_tenant_slug`` does. On the
    unfixed code the loser rolls back, re-SELECTs on its OWN stable_key (finding 0
    rows, since the winner carries the other key), and re-raises → HTTP 500.

    The bounded slug-regeneration retry in ``EntityService.get_or_create``
    recognises the empty identity re-SELECT as "not an identity dup → benign slug
    collision", regenerates the slug on a fresh create, and both rows land. Both
    must be 2xx, resolve to DISTINCT ids, and exactly two rows must exist. (Even
    under a sequential fallback the regeneration yields two rows, so this test is
    not flaky in the failing direction — it can only reliably PASS with the fix.)
    """
    card = uuid4()
    title = f"Stand up the shared platform capability {card}"
    base = {"title": title, "resource_type": "team"}
    first, second = await asyncio.gather(
        authenticated_client.post(
            "/pm/resources", json={**base, "stable_key": f"card-{card}:resource:0"}
        ),
        authenticated_client.post(
            "/pm/resources", json={**base, "stable_key": f"card-{card}:resource:1"}
        ),
    )
    assert first.status_code in (200, 201), first.text
    assert second.status_code in (200, 201), second.text
    # Same title but distinct stable_key → two independent identities → distinct ids.
    assert first.json()["id"] != second.json()["id"]

    count = await _count(
        test_session_factory,
        Resource,
        Resource.tenant_id == T("user-123"),
        Resource.title == title,
    )
    assert count == 2


async def test_principles_race_single_row(authenticated_client: AsyncClient, test_session_factory):
    origin = str(uuid4())
    payload = {
        "title": "Prefer managed services over self-hosting",
        "statement": "We use managed services unless a hard requirement forbids it.",
        "rationale": "Reduces operational burden on a small platform team.",
        "implications": ["Vendor evaluation is part of every design review."],
        "tags": ["platform"],
        "origin_project_id": origin,
        "origin_kind": "project",
    }
    await _gather_two(authenticated_client, "/principles", payload)
    count = await _count(
        test_session_factory,
        Principle,
        Principle.tenant_id == T("user-123"),
        Principle.origin_project_id == origin,
        Principle.title == "Prefer managed services over self-hosting",
    )
    assert count == 1


async def test_architecture_relationships_race_single_row(
    authenticated_client: AsyncClient, test_session_factory
):
    src, tgt = str(uuid4()), str(uuid4())
    payload = {
        "source_entity_type": "requirements",
        "source_id": src,
        "target_entity_type": "requirements",
        "target_id": tgt,
        "relationship_type": "traceability",
        "name": "traces to",
        "project_id": "gc-project",
    }
    await _gather_two(authenticated_client, "/architecture/relationships", payload)
    count = await _count(
        test_session_factory,
        ArchitectureRelationship,
        ArchitectureRelationship.tenant_id == T("user-123"),
        ArchitectureRelationship.project_id == "gc-project",
        ArchitectureRelationship.source_id == src,
        ArchitectureRelationship.target_id == tgt,
        ArchitectureRelationship.relationship_type == "traceability",
    )
    assert count == 1


async def test_requirements_user_visibility_race_single_row(
    authenticated_client: AsyncClient, test_session_factory
):
    """FIX 5: two concurrent user-visible creates must both 2xx (idempotent).

    With ``X-Visibility: user`` the survivor row is filed under
    ``visibility='user'``. The WS1 tenant identity index is partial on
    ``visibility='tenant'`` so it does NOT fire here. FIX 5 filters the recovery
    re-SELECT on the actual caller visibility (+ subject scope) so the loser
    resolves the survivor → 200. rm#191: the slug constraint only collides when
    both racers derive the slug before either commits — otherwise both INSERTs
    landed (duplicate). ``uq_requirements_user_project_title`` (0049) is now the
    backstop in every interleaving; see the barrier test below.
    """
    payload = {
        "title": "Users can enable two-factor auth",
        "description": "As a user I can enable 2FA on my account.",
        "effort": 2.0,
        "project_id": "gc-project-uv",
    }
    await _gather_two(
        authenticated_client, "/requirements", payload, headers={"X-Visibility": "user"}
    )
    count = await _count(
        test_session_factory,
        Requirement,
        Requirement.tenant_id == T("user-123"),
        Requirement.project_id == "gc-project-uv",
        Requirement.title == "Users can enable two-factor auth",
        Requirement.visibility == "user",
    )
    assert count == 1


async def test_requirements_user_visibility_winner_commits_first_single_row(
    authenticated_client: AsyncClient, test_session_factory, monkeypatch
):
    """rm#191: deterministic replay of the interleaving that duplicated rows.

    A barrier holds racer B's ``create`` (and so its ``_generate_unique_slug``
    SELECT) until racer A has COMMITTED. B then sees A's slug, mints
    ``<slug>-1``, and the slug constraint cannot collide — only a user-scoped
    identity index can force one survivor. Both requests are still in flight
    concurrently (gathered); only the order inside the service is fixed.
    """
    from services import requirement_service

    original_create = requirement_service.create
    a_committed = asyncio.Event()
    calls = 0

    async def barrier_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                return await original_create(*args, **kwargs)
            finally:
                a_committed.set()
        await asyncio.wait_for(a_committed.wait(), timeout=10)
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(requirement_service, "create", barrier_create)
    project_id = f"gc-project-uv-{uuid4()}"
    payload = {
        "title": "Users can export their data",
        "description": "As a user I can export my data as JSON.",
        "effort": 1.0,
        "project_id": project_id,
    }
    await _gather_two(
        authenticated_client, "/requirements", payload, headers={"X-Visibility": "user"}
    )
    assert calls >= 2  # the barrier really interposed both racers
    count = await _count(
        test_session_factory,
        Requirement,
        Requirement.tenant_id == T("user-123"),
        Requirement.project_id == project_id,
        Requirement.visibility == "user",
    )
    assert count == 1


async def test_architecture_elements_null_project_plain_create(
    authenticated_client: AsyncClient, test_session_factory
):
    """A null-project arch element create is a plain create (A12: id identity).

    Even SAME-title null-project creates each mint their own row — no title
    get-or-create, no 500 on the second (the slug gets a suffix).
    """
    base = {
        "description": "no project scope",
        "layer": "strategy",
        "element_type": "capability",
        "status": "draft",
        "title": "Null Scope Capability",
    }
    r1 = await authenticated_client.post("/architecture/elements", json=base)
    assert r1.status_code == 200, r1.text
    r2 = await authenticated_client.post("/architecture/elements", json=base)
    assert r2.status_code == 200, r2.text
    assert r1.json()["id"] != r2.json()["id"]
    assert r1.json()["slug"] != r2.json()["slug"]

    count = await _count(
        test_session_factory,
        ArchitectureElement,
        ArchitectureElement.tenant_id == T("user-123"),
        ArchitectureElement.project_id.is_(None),
        ArchitectureElement.title == "Null Scope Capability",
    )
    assert count == 2


async def test_architecture_elements_same_title_race_two_rows_no_500(
    authenticated_client: AsyncClient, test_session_factory
):
    """A12: title is not identity. Two concurrent same-title creates are two rows.

    Both derive the same base slug and race ``uq_arch_elements_tenant_slug``; the
    loser regenerates its slug inside a savepoint (``EntityService.create``)
    instead of answering 500.
    """
    payload = {
        "title": "Payments Capability",
        "description": "core payments",
        "layer": "strategy",
        "element_type": "capability",
        "status": "draft",
        "project_id": "gc-project",
    }
    first, second = await asyncio.gather(
        authenticated_client.post("/architecture/elements", json=payload),
        authenticated_client.post("/architecture/elements", json=payload),
    )
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert first.json()["id"] != second.json()["id"]
    count = await _count(
        test_session_factory,
        ArchitectureElement,
        ArchitectureElement.tenant_id == T("user-123"),
        ArchitectureElement.project_id == "gc-project",
        ArchitectureElement.title == "Payments Capability",
        ArchitectureElement.element_type == "capability",
    )
    assert count == 2


# ---------------------------------------------------------------------------
# Group A (WS1): risks, standards, views, toc trees, toc nodes, projects
# ---------------------------------------------------------------------------


async def test_risks_race_single_row(authenticated_client: AsyncClient, test_session_factory):
    payload = {
        "title": "Vendor API rate limits may throttle onboarding",
        "description": "Third-party API caps requests per minute.",
        "project_id": "gc-project",
        "probability": 0.4,
        "impact_score": 3.0,
        "category": "technical",
        "response_strategy": "mitigate",
    }
    await _gather_two(authenticated_client, "/pm/risks", payload)
    count = await _count(
        test_session_factory,
        Risk,
        Risk.tenant_id == T("user-123"),
        Risk.project_id == "gc-project",
        Risk.title == "Vendor API rate limits may throttle onboarding",
    )
    assert count == 1


async def test_standards_same_title_race_two_rows_no_500(
    authenticated_client: AsyncClient, test_session_factory
):
    """A12: standards are identified by id; a concurrent same-title pair is two rows."""
    payload = {
        "title": "Use OpenTelemetry for all tracing",
        "description": "Standardise observability on OTel.",
        "project_id": "gc-project",
        "standard_type": "technology_standard",
    }
    first, second = await asyncio.gather(
        authenticated_client.post("/architecture/standards", json=payload),
        authenticated_client.post("/architecture/standards", json=payload),
    )
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert first.json()["id"] != second.json()["id"]
    count = await _count(
        test_session_factory,
        Standard,
        Standard.tenant_id == T("user-123"),
        Standard.project_id == "gc-project",
        Standard.title == "Use OpenTelemetry for all tracing",
    )
    assert count == 2


async def test_views_same_title_race_two_rows_no_500(
    authenticated_client: AsyncClient, test_session_factory
):
    """A12: views are identified by id; a concurrent same-title pair is two rows."""
    payload = {
        "title": "Application cooperation view",
        "description": "How applications collaborate.",
        "project_id": "gc-project",
        "viewpoint": "application_cooperation",
    }
    first, second = await asyncio.gather(
        authenticated_client.post("/architecture/views", json=payload),
        authenticated_client.post("/architecture/views", json=payload),
    )
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert first.json()["id"] != second.json()["id"]
    count = await _count(
        test_session_factory,
        ArchitectureView,
        ArchitectureView.tenant_id == T("user-123"),
        ArchitectureView.project_id == "gc-project",
        ArchitectureView.title == "Application cooperation view",
    )
    assert count == 2


async def test_views_empty_string_project_scope_no_500(
    authenticated_client: AsyncClient, test_session_factory
):
    """An empty-string ``project_id`` must be treated as no scope (plain create).

    The gate ``if payload.project_id:`` is falsy for ``""`` → plain create, but
    the partial index is ``WHERE project_id IS NOT NULL`` and ``'' IS NOT NULL``
    is TRUE — so without normalisation a duplicate ``""``-scoped view would trip
    the index on the plain-create path → uncaught IntegrityError → 500. The
    ``ProjectScope`` type coerces ``""`` → ``None`` so gate and index agree: two
    same-title ``""``-project creates each land their own null-scope row, no 500.
    """
    base = {
        "description": "empty project scope",
        "viewpoint": "application_cooperation",
        "project_id": "",
        "title": "Empty Scope View",
    }
    r1 = await authenticated_client.post("/architecture/views", json=base)
    assert r1.status_code in (200, 201), r1.text
    r2 = await authenticated_client.post("/architecture/views", json=base)
    assert r2.status_code in (200, 201), r2.text  # NOT a 500
    assert r1.json()["id"] != r2.json()["id"]  # "" == no scope → no dedup

    count = await _count(
        test_session_factory,
        ArchitectureView,
        ArchitectureView.tenant_id == T("user-123"),
        ArchitectureView.project_id.is_(None),
        ArchitectureView.title == "Empty Scope View",
    )
    assert count == 2


async def test_toc_trees_race_single_row(authenticated_client: AsyncClient, test_session_factory):
    payload = {
        "title": "Delivery bottleneck analysis",
        "description": "Current reality tree for delivery flow.",
        "project_id": "gc-project",
        "tree_type": "crt",
    }
    await _gather_two(authenticated_client, "/toc", payload)
    count = await _count(
        test_session_factory,
        TocTree,
        TocTree.tenant_id == T("user-123"),
        TocTree.project_id == "gc-project",
        TocTree.tree_type == "crt",
        TocTree.title == "Delivery bottleneck analysis",
    )
    assert count == 1


async def test_toc_nodes_race_single_row(authenticated_client: AsyncClient, test_session_factory):
    # A node lives under a tree; create the tree first, then race two node POSTs.
    tree_resp = await authenticated_client.post(
        "/toc",
        json={"title": "CRT for node race", "tree_type": "crt"},
    )
    assert tree_resp.status_code == 200, tree_resp.text
    tree_id = tree_resp.json()["id"]

    payload = {
        "tree_id": tree_id,  # overwritten from the path by the router
        "title": "Throughput is capped by the review queue",
        "statement": "Undesirable effect: PRs wait days for review.",
        "node_kind": "ude",
    }
    await _gather_two(authenticated_client, f"/toc/{tree_id}/nodes", payload)
    count = await _count(
        test_session_factory,
        TocNode,
        TocNode.tenant_id == T("user-123"),
        TocNode.tree_id == tree_id,
        TocNode.title == "Throughput is capped by the review queue",
    )
    assert count == 1


async def test_projects_race_single_row(authenticated_client: AsyncClient, test_session_factory):
    origin = str(uuid4())
    payload = {
        "title": "Customer onboarding platform",
        "description": "Greenfield onboarding rebuild.",
        "origin_kind": "project",
        "origin_project_id": origin,
        "brief": MIN_BRIEF,
    }
    payload = await typed(authenticated_client, payload)
    await _gather_two(authenticated_client, "/pm/projects", payload)
    count = await _count(
        test_session_factory,
        Project,
        Project.tenant_id == T("user-123"),
        Project.origin_project_id == origin,
        Project.title == "Customer onboarding platform",
    )
    assert count == 1


# ---------------------------------------------------------------------------
# Empty-string project scope on the OLDER shipped nullable-scope schemas.
#
# The gate ``if payload.project_id:`` is falsy for ``""`` → plain create, but
# the WS1 partial unique index is ``WHERE project_id IS NOT NULL`` and
# ``'' IS NOT NULL`` is TRUE — so without normalisation a duplicate ``""``-scoped
# create would trip the index on the uncaught plain-create path → IntegrityError
# → HTTP 500. Typing ``RequirementIn/DecisionIn/ArchitectureElementIn.project_id``
# as ``ProjectScope`` coerces ``"" -> None`` so gate and index agree: two
# same-title ``""``-project creates each land their own null-scope row, no 500.
# Twin of ``test_views_empty_string_project_scope_no_500`` for the 3 older schemas.
# ---------------------------------------------------------------------------


async def test_requirements_empty_string_project_scope_no_500(
    authenticated_client: AsyncClient, test_session_factory
):
    base = {
        "description": "As a user I can reset my password via email.",
        "effort": 3.0,
        "project_id": "",
        "title": "Empty Scope Requirement",
    }
    r1 = await authenticated_client.post("/requirements", json=base)
    assert r1.status_code in (200, 201), r1.text
    r2 = await authenticated_client.post("/requirements", json=base)
    assert r2.status_code in (200, 201), r2.text  # NOT a 500
    assert r1.json()["id"] != r2.json()["id"]  # "" == no scope → no dedup

    count = await _count(
        test_session_factory,
        Requirement,
        Requirement.tenant_id == T("user-123"),
        Requirement.project_id.is_(None),
        Requirement.title == "Empty Scope Requirement",
    )
    assert count == 2


async def test_decisions_empty_string_project_scope_no_500(
    authenticated_client: AsyncClient, test_session_factory
):
    base = {
        "context": "We need a relational store with strong consistency.",
        "decision": "Use Postgres 18 with psycopg 3.",
        "project_id": "",
        "title": "Empty Scope Decision",
    }
    r1 = await authenticated_client.post("/decisions", json=base)
    assert r1.status_code in (200, 201), r1.text
    r2 = await authenticated_client.post("/decisions", json=base)
    assert r2.status_code in (200, 201), r2.text  # NOT a 500
    assert r1.json()["id"] != r2.json()["id"]  # "" == no scope → no dedup

    count = await _count(
        test_session_factory,
        Decision,
        Decision.tenant_id == T("user-123"),
        Decision.project_id.is_(None),
        Decision.title == "Empty Scope Decision",
    )
    assert count == 2


async def test_architecture_elements_empty_string_project_scope_no_500(
    authenticated_client: AsyncClient, test_session_factory
):
    # ``""`` is normalised to ``None`` (the persisted rows are NULL-scoped, not
    # ``""``-scoped) and neither POST 500s. A12: no title identity, so two serial
    # same-title POSTs are two rows.
    base = {
        "description": "core payments",
        "layer": "strategy",
        "element_type": "capability",
        "status": "draft",
        "project_id": "",
        "title": "Empty Scope Capability",
    }
    r1 = await authenticated_client.post("/architecture/elements", json=base)
    assert r1.status_code == 200, r1.text
    r2 = await authenticated_client.post("/architecture/elements", json=base)
    assert r2.status_code == 200, r2.text  # NOT a 500
    assert r1.json()["id"] != r2.json()["id"]  # id identity, no title dedup

    # Both rows are NULL-scoped (proves ``"" -> None`` normalisation; without it
    # the rows would carry ``project_id == ""`` and this count is 0).
    count = await _count(
        test_session_factory,
        ArchitectureElement,
        ArchitectureElement.tenant_id == T("user-123"),
        ArchitectureElement.project_id.is_(None),
        ArchitectureElement.title == "Empty Scope Capability",
    )
    assert count == 2


async def test_architecture_relationships_empty_string_project_scope_no_500(
    authenticated_client: AsyncClient, test_session_factory
):
    src, tgt = str(uuid4()), str(uuid4())
    base = {
        "source_entity_type": "requirements",
        "source_id": src,
        "target_entity_type": "requirements",
        "target_id": tgt,
        "relationship_type": "traceability",
        "name": "traces to",
        "project_id": "",
    }
    r1 = await authenticated_client.post("/architecture/relationships", json=base)
    assert r1.status_code in (200, 201), r1.text
    r2 = await authenticated_client.post("/architecture/relationships", json=base)
    assert r2.status_code in (200, 201), r2.text  # NOT a 500
    assert r1.json()["id"] != r2.json()["id"]  # "" == no scope → plain create, no dedup

    # Both edges are NULL-scoped (proves ``"" -> None``; without it the 2nd POST
    # trips uq_arch_rels_tenant_edge on the uncaught plain-create path → 500 and
    # this count is 0).
    count = await _count(
        test_session_factory,
        ArchitectureRelationship,
        ArchitectureRelationship.tenant_id == T("user-123"),
        ArchitectureRelationship.project_id.is_(None),
        ArchitectureRelationship.source_id == src,
        ArchitectureRelationship.target_id == tgt,
        ArchitectureRelationship.relationship_type == "traceability",
    )
    assert count == 2


# ---------------------------------------------------------------------------
# Slug-collision CLASS in the bespoke sibling get-or-create surfaces
# (twins of test_pm_resources_same_title_different_stable_key_race_two_rows, which
# proved the EntityService.get_or_create case). Each here derives the SAME base
# slug from a DIFFERENT identity, so only the plain uq_<table>_tenant_slug
# constraint collides — the identity re-SELECT is empty. On the unfixed code the
# loser re-raises → HTTP 500; the bounded slug-regeneration retry recognises the
# empty re-SELECT as a benign slug collision, regenerates the slug on a fresh
# create, and both rows land. Both must be 2xx, resolve to DISTINCT ids, and
# exactly two rows must exist. (Even a sequential fallback yields two rows via
# the -1 slug suffix, so these are not flaky in the FAILING direction — they can
# only reliably PASS with the fix.)
# ---------------------------------------------------------------------------


async def test_architecture_relationships_same_slug_different_edge_race_two_rows(
    authenticated_client: AsyncClient, test_session_factory
):
    """RelationshipService.get_or_create slug-collision race (the MOST exposed site).

    ``_generate_slug`` keys the slug on ``source_id[:8]`` / ``target_id[:8]`` —
    the uuid7 TIMESTAMP prefix, not entropy. Two genuinely-different edges whose
    endpoints share that 8-char prefix derive the SAME base slug but a DISTINCT
    edge identity (full ``source_id`` differs), so ``uq_arch_rels_tenant_edge``
    does NOT collide — only ``uq_arch_rels_tenant_slug`` does → empty identity
    re-SELECT → (unfixed) re-raise → 500.
    """
    project_id = f"gc-project-{uuid4()}"
    prefix = uuid4().hex[:8]  # shared 8-char slug-prefix, unique per test run
    # Same target for both edges; sources share the prefix but differ in full.
    src1 = f"{prefix}-0000-4000-8000-000000000001"
    src2 = f"{prefix}-0000-4000-8000-000000000002"
    tgt = str(uuid4())
    base = {
        "source_entity_type": "requirements",
        "target_entity_type": "requirements",
        "target_id": tgt,
        "relationship_type": "traceability",
        "name": "traces to",
        "project_id": project_id,
    }
    first, second = await asyncio.gather(
        authenticated_client.post("/architecture/relationships", json={**base, "source_id": src1}),
        authenticated_client.post("/architecture/relationships", json={**base, "source_id": src2}),
    )
    assert first.status_code in (200, 201), first.text
    assert second.status_code in (200, 201), second.text
    # Distinct edges (distinct source_id) → distinct surviving rows.
    assert first.json()["id"] != second.json()["id"]

    count = await _count(
        test_session_factory,
        ArchitectureRelationship,
        ArchitectureRelationship.tenant_id == T("user-123"),
        ArchitectureRelationship.project_id == project_id,
        ArchitectureRelationship.target_id == tgt,
        ArchitectureRelationship.relationship_type == "traceability",
    )
    assert count == 2


async def test_architecture_synthetic_same_title_different_component_race_two_rows(
    authenticated_client: AsyncClient, test_session_factory
):
    """upsert_synthetic_by_code_binding slug-collision race.

    Synthetic elements have no title identity (and since A12 neither do
    non-synthetic ones) but share ``uq_arch_elements_tenant_slug``. Two
    concurrent synthetic POSTs with the SAME title (→ same base slug) but a
    DIFFERENT ``code_binding.component_id`` (→ distinct synthetic identity) both
    miss the binding-table fast path and race on the slug constraint. The
    recovery re-SELECT keys on THIS caller's component_id → finds the winner's
    other-component row nowhere → empty → (unfixed) re-raise → 500.
    """
    project_id = f"gc-project-{uuid4()}"
    title = "Synthetic payments component"
    container = "payments-svc/src/payments/core.py"
    base = {
        "title": title,
        "description": "code-bound synthetic artifact",
        "layer": "application",
        "element_type": "application_component",
        "status": "draft",
        "project_id": project_id,
        "is_synthetic": True,
    }

    def _payload(component_id: str) -> dict:
        return {
            **base,
            "properties": {"code_binding": {"container": container, "component_id": component_id}},
        }

    first, second = await asyncio.gather(
        authenticated_client.post("/architecture/elements", json=_payload("comp-1")),
        authenticated_client.post("/architecture/elements", json=_payload("comp-2")),
    )
    assert first.status_code in (200, 201), first.text
    assert second.status_code in (200, 201), second.text
    # Distinct component bindings → distinct surviving synthetic rows.
    assert first.json()["id"] != second.json()["id"]

    count = await _count(
        test_session_factory,
        ArchitectureElement,
        ArchitectureElement.tenant_id == T("user-123"),
        ArchitectureElement.project_id == project_id,
        ArchitectureElement.title == title,
        ArchitectureElement.is_synthetic.is_(True),
    )
    assert count == 2
