"""``?model_id=`` on the three ArchiMate list routes (elements, relationships, views).

fr's model-contents editor lists one model at a time. Before this param existed FastAPI
dropped ``?model_id=`` silently (an undeclared query param is a 200, not a 422), so the
editor received every row in the tenant. Every test here therefore asserts a NARROWING
the old routes cannot produce, which makes each of them fail on the code before the param
was declared — including the "no filter" control, which pairs the unfiltered union with
the filtered subset.

Semantics:

* elements / views: the row's own ``model_id``;
* relationships: the relationship's ``model_id``, which the flush hook derives from its
  SOURCE end (``models/modeling.py::_derive_relationship_model``) on create and again
  whenever an end moves — so "the ArchiMate edges whose source end is in the model".
  PM / TOC / governance links carry no model and never match;
* a relationship joins only a model the caller could create an element into
  (``join_model_refusal``, shared with ``resolve_create_model``): its own tenant's,
  writable, of the edge's visibility. Otherwise ``422 model_integrity`` with reason
  ``model_not_writable`` (e.g. two PUBLISHED system elements related by a platform
  tenant) or ``model_visibility_mismatch``, on create and on an update that moves the
  edge to a DIFFERENT model; a move within its model is not gated, so an edge that
  already mismatched before the gate can still be re-pointed;
* a model's seal / export / render reads only rows of the model's own tenant, so a
  foreign-tenant row carrying its id (pre-gate data) never enters one;
* the filter lists rows only for a caller who may READ the model itself (the
  ``GET /modeling/models/{id}`` scope): another tenant's model — the system tenant's
  included, even for a platform tenant that inherits its PUBLISHED rows — or another
  subject's private model is an empty list, even if a row in it is visible to the caller
  (a row written before the write gate existed).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureRelationship
from settings import get_settings
from tests.tenants import T

pytestmark = pytest.mark.asyncio

TENANT = T("list-model-filter")
OTHER_TENANT = T("list-model-filter-other")
ALICE, BOB = "alice-list-model", "bob-list-model"

ELEMENTS = "/architecture/elements"
RELATIONSHIPS = "/architecture/relationships"
VIEWS = "/architecture/views"


def hdrs(
    subject: str = ALICE, *, tenant: str = TENANT, visibility: str | None = None
) -> dict[str, str]:
    h = {"X-Tenant-Id": tenant, "X-Subject-Id": subject}
    if visibility:
        h["X-Visibility"] = visibility
    return h


async def ok(resp, status: int = 200) -> Any:
    assert resp.status_code == status, f"{resp.request.url} -> {resp.status_code}: {resp.text}"
    return resp.json()


async def new_model(client: AsyncClient, headers: dict, **extra: Any) -> str:
    body = {"language": "archimate", "language_version": "3.2", "title": "M", **extra}
    return (await ok(await client.post("/modeling/models", json=body, headers=headers), 201))["id"]


async def new_element(client: AsyncClient, model_id: str, headers: dict) -> dict:
    body = {
        "title": f"actor-{uuid.uuid4().hex[:8]}",
        "element_type": "business_actor",
        "model_id": model_id,
    }
    return await ok(await client.post(ELEMENTS, json=body, headers=headers))


async def new_relationship(client: AsyncClient, source: dict, target: dict, headers: dict):
    body = {
        "source_entity_type": "architecture_elements",
        "source_id": source["id"],
        "target_entity_type": "architecture_elements",
        "target_id": target["id"],
        "relationship_type": "association",
    }
    return await ok(await client.post(RELATIONSHIPS, json=body, headers=headers))


async def new_view(client: AsyncClient, model_id: str, headers: dict) -> dict:
    body = {"title": f"view-{uuid.uuid4().hex[:8]}", "viewpoint": "layered", "model_id": model_id}
    return await ok(await client.post(VIEWS, json=body, headers=headers))


async def populate(client: AsyncClient, headers: dict, n: int) -> dict[str, Any]:
    """A model holding ``n + 1`` elements, ``n`` chained relationships and ``n`` views."""
    model_id = await new_model(client, headers)
    els = [await new_element(client, model_id, headers) for _ in range(n + 1)]
    rels = [await new_relationship(client, els[i], els[i + 1], headers) for i in range(n)]
    views = [await new_view(client, model_id, headers) for _ in range(n)]
    return {
        "model_id": model_id,
        ELEMENTS: {e["id"] for e in els},
        RELATIONSHIPS: {r["id"] for r in rels},
        VIEWS: {v["id"] for v in views},
    }


async def ids(client: AsyncClient, path: str, headers: dict, **params: Any) -> set[str]:
    return {row["id"] for row in await ok(await client.get(path, headers=headers, params=params))}


ROUTES = [ELEMENTS, RELATIONSHIPS, VIEWS]


@pytest.mark.parametrize("path", ROUTES)
async def test_filter_returns_only_that_models_rows(test_client: AsyncClient, path: str) -> None:
    a = await populate(test_client, hdrs(), 2)
    b = await populate(test_client, hdrs(), 2)

    got_a = await ids(test_client, path, hdrs(), model_id=a["model_id"], limit=1000)
    got_b = await ids(test_client, path, hdrs(), model_id=b["model_id"], limit=1000)

    assert got_a == a[path]
    assert got_b == b[path]
    # Every returned row carries the model it was filtered on.
    rows = await ok(await test_client.get(path, headers=hdrs(), params={"model_id": a["model_id"]}))
    assert rows and {r["model_id"] for r in rows} == {a["model_id"]}


@pytest.mark.parametrize("path", ROUTES)
async def test_no_filter_is_unchanged_and_the_filter_is_a_strict_subset(
    test_client: AsyncClient, path: str
) -> None:
    a = await populate(test_client, hdrs(), 2)
    b = await populate(test_client, hdrs(), 2)

    unfiltered = await ids(test_client, path, hdrs(), limit=1000)
    # No filter: every row of both models is listed, exactly as before the param existed.
    assert a[path] | b[path] <= unfiltered
    filtered = await ids(test_client, path, hdrs(), model_id=a["model_id"], limit=1000)
    assert filtered < unfiltered
    assert filtered.isdisjoint(b[path])


@pytest.mark.parametrize("path", ROUTES)
async def test_a_foreign_tenants_model_lists_nothing(test_client: AsyncClient, path: str) -> None:
    mine = await populate(test_client, hdrs(), 1)
    foreign = await populate(test_client, hdrs(tenant=OTHER_TENANT), 1)

    assert await ids(test_client, path, hdrs(), model_id=foreign["model_id"]) == set()
    resp = await test_client.get(path, headers=hdrs(), params={"model_id": foreign["model_id"]})
    assert resp.headers["X-Total-Count"] == "0"
    # Non-vacuous: the caller has rows, and the owner does see the foreign model's rows.
    assert await ids(test_client, path, hdrs(), model_id=mine["model_id"]) == mine[path]
    owner = hdrs(tenant=OTHER_TENANT)
    assert await ids(test_client, path, owner, model_id=foreign["model_id"]) == foreign[path]


@pytest.mark.parametrize("path", ROUTES)
async def test_another_subjects_private_model_lists_nothing(
    test_client: AsyncClient, path: str
) -> None:
    bobs = await populate(test_client, hdrs(BOB), 1)
    private = await populate(test_client, hdrs(ALICE, visibility="user"), 1)

    assert await ids(test_client, path, hdrs(BOB), model_id=private["model_id"]) == set()
    # Non-vacuous: Bob has visible rows, and Alice sees her private model's rows.
    assert await ids(test_client, path, hdrs(BOB), model_id=bobs["model_id"]) == bobs[path]
    assert await ids(test_client, path, hdrs(ALICE), model_id=private["model_id"]) == private[path]


@pytest.mark.parametrize("path", ROUTES)
async def test_pagination_composes_with_the_filter(test_client: AsyncClient, path: str) -> None:
    a = await populate(test_client, hdrs(), 3)
    await populate(test_client, hdrs(), 2)
    want = a[path]
    assert len(want) >= 3

    pages: list[set[str]] = []
    offset = 0
    while True:
        resp = await test_client.get(
            path, headers=hdrs(), params={"model_id": a["model_id"], "limit": 2, "offset": offset}
        )
        rows = await ok(resp)
        assert resp.headers["X-Total-Count"] == str(len(want))
        if not rows:
            break
        assert len(rows) <= 2
        pages.append({r["id"] for r in rows})
        offset += 2

    assert len(pages) == -(-len(want) // 2)
    seen: set[str] = set()
    for page in pages:
        assert seen.isdisjoint(page)
        seen |= page
    assert seen == want


async def test_openapi_documents_model_id_on_all_three_list_routes(
    test_client: AsyncClient,
) -> None:
    spec = await ok(await test_client.get("/openapi.json"))
    for path in ROUTES:
        # Shared header params are ``$ref``s with no ``name``; only inline ones matter here.
        params = {p["name"]: p for p in spec["paths"][path]["get"]["parameters"] if "name" in p}
        assert "model_id" in params, path
        p = params["model_id"]
        assert p["in"] == "query" and not p.get("required", False), p
        schema = p["schema"]
        formats = [s.get("format") for s in schema.get("anyOf", [schema])]
        assert "uuid" in formats, schema
        assert p.get("description"), p


# ── relationship visibility follows its model (audit finding 1) ──────────────────────


def _mismatch_reason(body: dict) -> str | None:
    err = body["error"]
    assert err["code"] == "model_integrity", body
    finding = err["findings"][0]
    assert finding["rule_id"] == "AM-REF-001", finding
    return finding["evidence"]["reason"]


async def test_a_tenant_relationship_cannot_join_a_private_model(
    test_client: AsyncClient,
) -> None:
    """Alice's tenant-visible edge between her private elements: refused like an element."""
    private = hdrs(ALICE, visibility="user")
    model_id = await new_model(test_client, private)
    a = await new_element(test_client, model_id, private)
    b = await new_element(test_client, model_id, private)

    # The element precedent: a tenant-visible element cannot join a private model.
    el = await test_client.post(
        ELEMENTS,
        json={"title": "t", "element_type": "business_actor", "model_id": model_id},
        headers=hdrs(ALICE),
    )
    assert el.status_code == 422, el.text
    body = {
        "source_entity_type": "architecture_elements",
        "source_id": a["id"],
        "target_entity_type": "architecture_elements",
        "target_id": b["id"],
        "relationship_type": "association",
    }
    resp = await test_client.post(RELATIONSHIPS, json=body, headers=hdrs(ALICE))
    assert resp.status_code == 422, resp.text
    # Same status, code, rule and reason as the element refusal.
    assert _mismatch_reason(resp.json()) == _mismatch_reason(el.json())
    assert _mismatch_reason(resp.json()) == "model_visibility_mismatch"
    rel_finding, el_finding = resp.json()["error"]["findings"][0], el.json()["error"]["findings"][0]
    assert rel_finding.keys() == el_finding.keys()
    assert (
        rel_finding["evidence"]
        == el_finding["evidence"]
        == {
            "model_id": model_id,
            "reason": "model_visibility_mismatch",
        }
    )

    # Nothing landed: Bob's filtered list is empty and so is his unfiltered view of it.
    assert await ids(test_client, RELATIONSHIPS, hdrs(BOB), model_id=model_id) == set()
    assert not {
        r["id"]
        for r in await ok(
            await test_client.get(RELATIONSHIPS, headers=hdrs(BOB), params={"limit": 1000})
        )
        if r["model_id"] == model_id
    }
    # Non-vacuous: the same edge with the model's visibility is accepted and listed.
    rel = await ok(await test_client.post(RELATIONSHIPS, json=body, headers=private))
    assert rel["model_id"] == model_id
    assert await ids(test_client, RELATIONSHIPS, private, model_id=model_id) == {rel["id"]}


async def test_re_pointing_a_tenant_relationship_into_a_private_model_is_refused(
    test_client: AsyncClient,
) -> None:
    shared = await populate(test_client, hdrs(ALICE), 1)
    (rid,) = shared[RELATIONSHIPS]
    private = hdrs(ALICE, visibility="user")
    pm = await new_model(test_client, private)
    p1 = await new_element(test_client, pm, private)
    p2 = await new_element(test_client, pm, private)

    resp = await test_client.put(
        f"{RELATIONSHIPS}/{rid}",
        json={"source_id": p1["id"], "target_id": p2["id"]},
        headers=hdrs(ALICE),
    )
    assert resp.status_code == 422, resp.text
    assert _mismatch_reason(resp.json()) == "model_visibility_mismatch"
    # The edge stayed where it was.
    assert await ids(test_client, RELATIONSHIPS, hdrs(BOB), model_id=pm) == set()
    assert await ids(test_client, RELATIONSHIPS, hdrs(BOB), model_id=shared["model_id"]) == {rid}


async def test_a_visible_row_in_an_unreadable_model_is_not_listed_under_it(
    test_client: AsyncClient, test_session: AsyncSession
) -> None:
    """A tenant-visible edge left in a private model (pre-gate data) stays out of the filter."""
    private = hdrs(ALICE, visibility="user")
    pm = await new_model(test_client, private)
    a = await new_element(test_client, pm, private)
    b = await new_element(test_client, pm, private)
    rel = await new_relationship(test_client, a, b, private)
    # Simulate a row written before the write gate: tenant-visible, in Alice's private model.
    await test_session.execute(
        text("UPDATE architecture_relationships SET visibility = 'tenant' WHERE id = :id"),
        {"id": rel["id"]},
    )
    await test_session.commit()

    # Non-vacuous: Bob CAN see the row itself — only the model filter withholds it.
    assert (
        await test_client.get(f"{RELATIONSHIPS}/{rel['id']}", headers=hdrs(BOB))
    ).status_code == 200
    resp = await test_client.get(RELATIONSHIPS, headers=hdrs(BOB), params={"model_id": pm})
    assert await ok(resp) == []
    assert resp.headers["X-Total-Count"] == "0"
    # Alice, who may read the model, still gets it.
    assert await ids(test_client, RELATIONSHIPS, hdrs(ALICE), model_id=pm) == {rel["id"]}


# ── relationship-list order (audit finding 2) ────────────────────────────────────────


async def test_relationship_ties_on_created_at_are_ordered_by_id_desc(
    test_client: AsyncClient, test_session: AsyncSession
) -> None:
    m = await populate(test_client, hdrs(), 6)
    await test_session.execute(
        text(
            "UPDATE architecture_relationships SET created_at = '2026-01-01T00:00:00Z' "
            "WHERE model_id = :m"
        ),
        {"m": m["model_id"]},
    )
    await test_session.commit()

    seen: list[str] = []
    for offset in range(0, 6, 2):
        rows = await ok(
            await test_client.get(
                RELATIONSHIPS,
                headers=hdrs(),
                params={"model_id": m["model_id"], "limit": 2, "offset": offset},
            )
        )
        seen += [r["id"] for r in rows]
    assert seen == sorted(m[RELATIONSHIPS], key=uuid.UUID, reverse=True)


# ── the other claims the relationships OpenAPI text makes (audit finding 3) ──────────


def _edge(
    source: tuple[str, str], target: tuple[str, str], rel_type: str
) -> ArchitectureRelationship:
    return ArchitectureRelationship(
        slug=f"probe-{uuid.uuid4().hex[:12]}",
        tenant_id=TENANT,
        tenant_name="list-model-filter",
        subject_id=ALICE,
        visibility="tenant",
        source_entity_type=source[0],
        source_id=uuid.UUID(source[1]),
        target_entity_type=target[0],
        target_id=uuid.UUID(target[1]),
        relationship_type=rel_type,
    )


async def test_model_id_derives_from_the_source_end(
    test_client: AsyncClient, test_session: AsyncSession
) -> None:
    """Ends in two models (only reachable below the write gate): the SOURCE decides."""
    m1 = await populate(test_client, hdrs(), 1)
    m2 = await populate(test_client, hdrs(), 1)
    a = next(iter(m1[ELEMENTS]))
    b = next(iter(m2[ELEMENTS]))
    fwd = _edge(("architecture_elements", a), ("architecture_elements", b), "association")
    rev = _edge(("architecture_elements", b), ("architecture_elements", a), "association")
    test_session.add_all([fwd, rev])
    await test_session.commit()

    got1 = await ids(test_client, RELATIONSHIPS, hdrs(), model_id=m1["model_id"])
    got2 = await ids(test_client, RELATIONSHIPS, hdrs(), model_id=m2["model_id"])
    assert str(fwd.id) in got1 and str(fwd.id) not in got2
    assert str(rev.id) in got2 and str(rev.id) not in got1


async def test_model_id_is_re_derived_when_an_end_moves(test_client: AsyncClient) -> None:
    m1 = await populate(test_client, hdrs(), 1)
    m2 = await populate(test_client, hdrs(), 1)
    (rid,) = m1[RELATIONSHIPS]
    c, d = sorted(m2[ELEMENTS])

    moved = await ok(
        await test_client.put(
            f"{RELATIONSHIPS}/{rid}", json={"source_id": c, "target_id": d}, headers=hdrs()
        )
    )
    assert moved["model_id"] == m2["model_id"]
    assert rid in await ids(test_client, RELATIONSHIPS, hdrs(), model_id=m2["model_id"])
    assert rid not in await ids(test_client, RELATIONSHIPS, hdrs(), model_id=m1["model_id"])


async def test_non_archimate_links_never_match(
    test_client: AsyncClient, test_session: AsyncSession
) -> None:
    """PM, TOC and governance links touching the model's elements belong to no model."""
    m = await populate(test_client, hdrs(), 1)
    el = next(iter(m[ELEMENTS]))
    other = str(uuid.uuid4())
    links = [
        _edge(("pm_resources", other), ("architecture_elements", el), "association"),
        _edge(("architecture_elements", el), ("toc_nodes", other), "toc_necessity"),
        _edge(("requirements", other), ("architecture_elements", el), "traceability"),
        _edge(("architecture_elements", el), ("principles", other), "traceability"),
    ]
    test_session.add_all(links)
    await test_session.commit()
    link_ids = {str(link.id) for link in links}

    assert all(link.model_id is None for link in links)
    # Not ArchiMate edges: absent from the default list, listed when an end's type is
    # named, and never under the model.
    assert not link_ids & await ids(test_client, RELATIONSHIPS, hdrs(), limit=1000)
    named = await ids(
        test_client, RELATIONSHIPS, hdrs(), limit=1000, target_entity_type="architecture_elements"
    ) | await ids(
        test_client, RELATIONSHIPS, hdrs(), limit=1000, source_entity_type="architecture_elements"
    )
    assert link_ids <= named
    assert await ids(test_client, RELATIONSHIPS, hdrs(), model_id=m["model_id"]) == m[RELATIONSHIPS]


# ── system-tenant models under include_system (audit finding 4) ──────────────────────

SYSTEM = get_settings().system_tenant_id
PLATFORM = T("list-model-filter-platform")


@pytest.mark.parametrize("path", [ELEMENTS, VIEWS])
async def test_a_system_model_lists_nothing_even_for_a_platform_tenant(
    test_client: AsyncClient, test_session: AsyncSession, monkeypatch, path: str
) -> None:
    """A platform tenant inherits the system tenant's PUBLISHED rows, not its models."""
    s = get_settings()
    monkeypatch.setattr(s, "platform_tenant_ids", f"{s.platform_tenant_ids},{PLATFORM}")
    sys_hdrs = hdrs("system-author", tenant=SYSTEM)
    sys_model = await populate(test_client, sys_hdrs, 1)
    table = "architecture_elements" if path == ELEMENTS else "architecture_views"
    await test_session.execute(
        text(f"UPDATE {table} SET version_status = 'PUBLISHED' WHERE model_id = :m"),  # noqa: S608
        {"m": sys_model["model_id"]},
    )
    await test_session.commit()
    me = hdrs(tenant=PLATFORM)

    # Non-vacuous: the platform tenant does see the inherited rows unfiltered…
    assert sys_model[path] <= await ids(test_client, path, me, limit=1000)
    # …but cannot read the model, so filtering on it lists nothing.
    assert (
        await test_client.get(f"/modeling/models/{sys_model['model_id']}", headers=me)
    ).status_code == 404
    resp = await test_client.get(path, headers=me, params={"model_id": sys_model["model_id"]})
    assert await ok(resp) == []
    assert resp.headers["X-Total-Count"] == "0"
    # The system tenant itself lists its model's rows.
    assert await ids(test_client, path, sys_hdrs, model_id=sys_model["model_id"]) == sys_model[path]


# ── a relationship joins only a model the caller could create into (finding 5) ─────────


def _finding(body: dict) -> dict:
    err = body["error"]
    assert err["code"] == "model_integrity", body
    return err["findings"][0]


async def _published_system_model(
    client: AsyncClient, session: AsyncSession, monkeypatch, n: int = 1
) -> tuple[dict, dict, dict]:
    """A system-tenant model with ``n + 1`` PUBLISHED elements; PLATFORM inherits them."""
    s = get_settings()
    monkeypatch.setattr(s, "platform_tenant_ids", f"{s.platform_tenant_ids},{PLATFORM}")
    sys_hdrs = hdrs("system-author", tenant=SYSTEM)
    sys_model = await populate(client, sys_hdrs, n)
    await session.execute(
        text("UPDATE architecture_elements SET version_status = 'PUBLISHED' WHERE model_id = :m"),
        {"m": sys_model["model_id"]},
    )
    await session.commit()
    return sys_hdrs, sys_model, hdrs(tenant=PLATFORM)


def _assoc(source: str, target: str) -> dict:
    return {
        "source_entity_type": "architecture_elements",
        "source_id": source,
        "target_entity_type": "architecture_elements",
        "target_id": target,
        "relationship_type": "association",
    }


async def _foreign_edges(session: AsyncSession, model_id: str, tenant: str) -> int:
    return (
        await session.execute(
            text(
                "SELECT count(*) FROM architecture_relationships "
                "WHERE model_id = :m AND tenant_id = :t"
            ),
            {"m": model_id, "t": tenant},
        )
    ).scalar_one()


async def test_a_platform_tenant_cannot_put_an_edge_into_the_system_model(
    test_client: AsyncClient, test_session: AsyncSession, monkeypatch
) -> None:
    """An edge between two PUBLISHED system elements would join the SYSTEM tenant's model."""
    sys_hdrs, sys_model, me = await _published_system_model(test_client, test_session, monkeypatch)
    a, b = sorted(sys_model[ELEMENTS])
    # Non-vacuous: the platform tenant does see both ends (the metamodel gate passes them).
    assert {a, b} <= await ids(test_client, ELEMENTS, me, limit=1000)

    resp = await test_client.post(RELATIONSHIPS, json=_assoc(a, b), headers=me)
    assert resp.status_code == 422, resp.text
    finding = _finding(resp.json())
    assert finding["rule_id"] == "AM-REF-001", finding
    assert finding["evidence"] == {
        "model_id": sys_model["model_id"],
        "reason": "model_not_writable",
    }
    # The same shape an element gets for an explicit foreign model_id.
    el = await test_client.post(
        ELEMENTS,
        json={"title": "t", "element_type": "business_actor", "model_id": sys_model["model_id"]},
        headers=me,
    )
    assert el.status_code == 422, el.text
    assert _finding(el.json())["evidence"] == finding["evidence"]
    assert await _foreign_edges(test_session, sys_model["model_id"], PLATFORM) == 0
    # The system tenant itself may relate its own elements.
    await ok(await test_client.post(RELATIONSHIPS, json=_assoc(b, a), headers=sys_hdrs))
    # Unchanged from main: the caller's OWN element as source would join the caller's own
    # model, so the model gate passes it; a system target is then an end in a second
    # model and AM-REF-003 refuses it (no cross-tenant ArchiMate edge exists either way).
    own = await populate(test_client, me, 1)
    resp = await test_client.post(
        RELATIONSHIPS, json=_assoc(sorted(own[ELEMENTS])[0], a), headers=me
    )
    assert resp.status_code == 422, resp.text
    assert _finding(resp.json())["rule_id"] == "AM-REF-003"


async def test_re_pointing_an_own_edge_into_the_system_model_is_refused(
    test_client: AsyncClient, test_session: AsyncSession, monkeypatch
) -> None:
    _, sys_model, me = await _published_system_model(test_client, test_session, monkeypatch)
    a, b = sorted(sys_model[ELEMENTS])
    own = await populate(test_client, me, 1)
    (rid,) = own[RELATIONSHIPS]

    resp = await test_client.put(
        f"{RELATIONSHIPS}/{rid}", json={"source_id": a, "target_id": b}, headers=me
    )
    assert resp.status_code == 422, resp.text
    assert _finding(resp.json())["evidence"]["reason"] == "model_not_writable"
    assert await _foreign_edges(test_session, sys_model["model_id"], PLATFORM) == 0
    assert await ids(test_client, RELATIONSHIPS, me, model_id=own["model_id"]) == {rid}


async def test_a_foreign_tenant_edge_never_enters_a_seal_or_export(
    test_client: AsyncClient, test_session: AsyncSession
) -> None:
    """A foreign-tenant row carrying the model's id (pre-gate data) is not model content."""
    sys_hdrs = hdrs("system-author", tenant=SYSTEM)
    m = await populate(test_client, sys_hdrs, 1)
    (own_rel,) = m[RELATIONSHIPS]
    a, b = sorted(m[ELEMENTS])
    foreign = ArchitectureRelationship(
        slug=f"probe-{uuid.uuid4().hex[:12]}",
        tenant_id=PLATFORM,
        tenant_name="list-model-filter-platform",
        subject_id=ALICE,
        visibility="tenant",
        source_entity_type="architecture_elements",
        source_id=uuid.UUID(a),
        target_entity_type="architecture_elements",
        target_id=uuid.UUID(b),
        relationship_type="association",
        model_id=uuid.UUID(m["model_id"]),
    )
    test_session.add(foreign)
    await test_session.commit()
    assert await _foreign_edges(test_session, m["model_id"], PLATFORM) == 1

    rev = await ok(
        await test_client.post(f"/modeling/models/{m['model_id']}/revisions", headers=sys_hdrs),
        201,
    )
    detail = await ok(
        await test_client.get(
            f"/modeling/models/{m['model_id']}/revisions/{rev['revision_id']}", headers=sys_hdrs
        )
    )
    sealed = {r["id"] for r in detail["manifest"]["relationships"]}
    assert own_rel in sealed  # non-vacuous
    assert str(foreign.id) not in sealed

    xml = (
        await test_client.get(f"/modeling/models/{m['model_id']}/exchange", headers=sys_hdrs)
    ).text
    assert own_rel in xml  # non-vacuous
    assert str(foreign.id) not in xml


async def test_deleting_a_model_never_deletes_a_foreign_tenants_rows(
    test_client: AsyncClient, test_session: AsyncSession
) -> None:
    """A foreign-tenant row carrying the model's id is not model content: delete leaves it.

    Leaving it means the ``model_id`` FK then blocks the model's own DELETE, so the delete
    refuses (409) instead of writing across tenants or surfacing an FK violation.
    """
    sys_hdrs = hdrs("system-author", tenant=SYSTEM)
    m = await populate(test_client, sys_hdrs, 1)
    a, b = sorted(m[ELEMENTS])
    foreign = ArchitectureRelationship(
        slug=f"probe-{uuid.uuid4().hex[:12]}",
        tenant_id=PLATFORM,
        tenant_name="list-model-filter-platform",
        subject_id=ALICE,
        visibility="tenant",
        source_entity_type="architecture_elements",
        source_id=uuid.UUID(a),
        target_entity_type="architecture_elements",
        target_id=uuid.UUID(b),
        relationship_type="association",
        model_id=uuid.UUID(m["model_id"]),
    )
    test_session.add(foreign)
    await test_session.commit()

    resp = await test_client.delete(f"/modeling/models/{m['model_id']}", headers=sys_hdrs)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "model_has_foreign_rows"
    # Non-vacuous: the model, its own rows and the foreign row all still exist.
    assert await _foreign_edges(test_session, m["model_id"], PLATFORM) == 1
    await ok(await test_client.get(f"/modeling/models/{m['model_id']}", headers=sys_hdrs))
    assert (
        await ids(test_client, RELATIONSHIPS, sys_hdrs, model_id=m["model_id"]) == m[RELATIONSHIPS]
    )

    # Once the foreign row is its owner's problem no longer, the model deletes cleanly.
    await test_session.execute(
        text("DELETE FROM architecture_relationships WHERE id = :id"), {"id": str(foreign.id)}
    )
    await test_session.commit()
    await ok(await test_client.delete(f"/modeling/models/{m['model_id']}", headers=sys_hdrs))
    left = (
        await test_session.execute(
            text("SELECT count(*) FROM architecture_elements WHERE model_id = :m"),
            {"m": m["model_id"]},
        )
    ).scalar_one()
    assert left == 0


async def test_re_pointing_an_already_mismatched_edge_within_its_model_is_allowed(
    test_client: AsyncClient, test_session: AsyncSession
) -> None:
    """The gate refuses only what the write breaks: a move that keeps the model passes."""
    private = hdrs(ALICE, visibility="user")
    pm = await new_model(test_client, private)
    a, b, c = [await new_element(test_client, pm, private) for _ in range(3)]
    rel = await new_relationship(test_client, a, b, private)
    # Pre-gate data: a tenant-visible edge in Alice's private model.
    await test_session.execute(
        text("UPDATE architecture_relationships SET visibility = 'tenant' WHERE id = :id"),
        {"id": rel["id"]},
    )
    await test_session.commit()
    url = f"{RELATIONSHIPS}/{rel['id']}"

    # Target-only re-point: the derived model (from the source) does not change.
    moved = await ok(await test_client.put(url, json={"target_id": c["id"]}, headers=hdrs()))
    assert moved["target_id"] == c["id"] and moved["model_id"] == pm
    # Source re-point that stays in the same model: still allowed.
    moved = await ok(await test_client.put(url, json={"source_id": c["id"]}, headers=hdrs()))
    assert moved["source_id"] == c["id"] and moved["model_id"] == pm

    # The other direction: a move into a DIFFERENT mismatching model is still refused.
    pm2 = await new_model(test_client, private)
    p1 = await new_element(test_client, pm2, private)
    p2 = await new_element(test_client, pm2, private)
    resp = await test_client.put(
        url, json={"source_id": p1["id"], "target_id": p2["id"]}, headers=hdrs()
    )
    assert resp.status_code == 422, resp.text
    assert _mismatch_reason(resp.json()) == "model_visibility_mismatch"
    assert (await ok(await test_client.get(url, headers=hdrs())))["model_id"] == pm


async def test_re_pointing_a_legacy_edge_stored_in_a_foreign_model_is_refused(
    test_client: AsyncClient, test_session: AsyncSession, monkeypatch
) -> None:
    """An edge kept by the caller's tenant inside ANOTHER tenant's model keeps no exemption.

    The same-model exemption is about "the move breaks nothing of the edge's own": an
    edge of the platform tenant stored in the SYSTEM tenant's model (pre-gate data) sits
    in a model its tenant does not own, so a re-point rewrites the system tenant's model
    content through a row that tenant never wrote. It is refused like any other move into
    that model.
    """
    _, sys_model, me = await _published_system_model(test_client, test_session, monkeypatch, n=3)
    old_source, old_target, new_source, new_target = sorted(sys_model[ELEMENTS])
    legacy = ArchitectureRelationship(
        slug=f"probe-{uuid.uuid4().hex[:12]}",
        tenant_id=PLATFORM,
        tenant_name="list-model-filter-platform",
        subject_id=ALICE,
        visibility="tenant",
        source_entity_type="architecture_elements",
        source_id=uuid.UUID(old_source),
        target_entity_type="architecture_elements",
        target_id=uuid.UUID(old_target),
        relationship_type="association",
        model_id=uuid.UUID(sys_model["model_id"]),
    )
    test_session.add(legacy)
    await test_session.commit()
    legacy_id = str(legacy.id)
    # Non-vacuous: the caller does see the edge and both new ends.
    assert (await test_client.get(f"{RELATIONSHIPS}/{legacy_id}", headers=me)).status_code == 200
    assert {new_source, new_target} <= await ids(test_client, ELEMENTS, me, limit=1000)

    resp = await test_client.put(
        f"{RELATIONSHIPS}/{legacy_id}",
        json={"source_id": new_source, "target_id": new_target},
        headers=me,
    )
    assert resp.status_code == 422, resp.text
    assert _finding(resp.json())["evidence"] == {
        "model_id": sys_model["model_id"],
        "reason": "model_not_writable",
    }
    # Nothing moved: the stored edge still ends on the pair it had.
    ends = (
        await test_session.execute(
            text("SELECT source_id, target_id FROM architecture_relationships WHERE id = :id"),
            {"id": legacy_id},
        )
    ).one()
    assert {str(ends[0]), str(ends[1])} == {old_source, old_target}
