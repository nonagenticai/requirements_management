"""Bulk-delete confirmation, deterministic search, and PATCH partial update.

These three routers are the conveyor's deterministic context source, so each
behaviour here is pinned against the failure it replaces:

1. ``DELETE /{requirements,principles,decisions}/bulk`` and ``DELETE /bulk`` wiped
   the WHOLE tenant with no filter and no confirmation, reachable from the generic
   n8n dispatcher. They now require ``confirm=<tenant_id>``; anything else is a
   400 with NOTHING deleted (row counts are checked, not just the status).
2. Search ordered by ``score DESC`` only, so ties (measured: 26 distinct scores
   over 50 hits) came back in Postgres' order and ``LIMIT k`` could pick a
   different subset. The tie fixtures below insert identical rows with identical
   ``created_at`` in DESCENDING id order, so heap order is the reverse of the
   required order and a missing tiebreak cannot pass by accident.
3. PUT replaces every field; PATCH must leave omitted fields untouched.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from models.decisions import Decision
from models.principles import Principle
from models.requirements import Requirement
from tests.tenants import T

TENANT = T("scope-safety-primary")
OTHER = T("scope-safety-other")
H = {"X-Tenant-Id": TENANT}

BULK_ROUTES = {
    "/requirements/bulk": (Requirement,),
    "/principles/bulk": (Principle,),
    "/decisions/bulk": (Decision,),
    "/bulk": (Requirement, Principle, Decision),
}


async def _seed_one_of_each(client, headers=H):
    for path, body in (
        ("/requirements", {"title": f"r {uuid4().hex[:6]}", "description": "d", "effort": 1.0}),
        ("/principles", {"title": f"p {uuid4().hex[:6]}", "statement": "s"}),
        ("/decisions", {"title": f"d {uuid4().hex[:6]}", "context": "c", "decision": "d"}),
    ):
        r = await client.post(path, json=body, headers=headers)
        assert r.status_code in (200, 201), f"setup {path}: {r.status_code} {r.text}"


async def _count(session, model, tenant=TENANT) -> int:
    await session.rollback()  # see rows committed by the app's own sessions
    return (
        await session.execute(
            select(func.count()).select_from(model).where(model.tenant_id == tenant)
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# 1. Bulk delete requires confirm=<tenant_id>
# --------------------------------------------------------------------------- #
class TestBulkDeleteRequiresConfirm:
    @pytest.mark.parametrize("path", sorted(BULK_ROUTES))
    async def test_missing_confirm_is_400_and_deletes_nothing(
        self, test_client, test_session, path
    ):
        await _seed_one_of_each(test_client)
        r = await test_client.delete(path, headers=H)
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "BULK_DELETE_CONFIRM_REQUIRED", r.text
        for model in BULK_ROUTES[path]:
            assert await _count(test_session, model) == 1, f"{path} deleted {model.__name__}"

    @pytest.mark.parametrize("path", sorted(BULK_ROUTES))
    @pytest.mark.parametrize("where", ["query", "body"])
    async def test_wrong_tenant_is_400_and_deletes_nothing(
        self, test_client, test_session, path, where
    ):
        await _seed_one_of_each(test_client)
        if where == "query":
            r = await test_client.delete(path, headers=H, params={"confirm": OTHER})
        else:
            r = await test_client.request("DELETE", path, headers=H, json={"confirm": OTHER})
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "BULK_DELETE_CONFIRM_MISMATCH", r.text
        for model in BULK_ROUTES[path]:
            assert await _count(test_session, model) == 1, f"{path} deleted {model.__name__}"

    @pytest.mark.parametrize("path", sorted(BULK_ROUTES))
    @pytest.mark.parametrize("where", ["query", "body"])
    async def test_correct_tenant_deletes_only_that_tenant(
        self, test_client, test_session, path, where
    ):
        await _seed_one_of_each(test_client)
        await _seed_one_of_each(test_client, headers={"X-Tenant-Id": OTHER})
        if where == "query":
            r = await test_client.delete(path, headers=H, params={"confirm": TENANT.upper()})
        else:
            r = await test_client.request("DELETE", path, headers=H, json={"confirm": TENANT})
        assert r.status_code == 200, r.text
        assert r.json()["tenant_id"] == TENANT
        for model in BULK_ROUTES[path]:
            assert await _count(test_session, model) == 0, f"{path} kept {model.__name__}"
            assert await _count(test_session, model, OTHER) == 1, "other tenant was touched"


# --------------------------------------------------------------------------- #
# 2. Deterministic search: stable total order, filters, offset
# --------------------------------------------------------------------------- #
FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
LATER_TS = datetime(2026, 2, 1, tzinfo=UTC)


def _ids_desc(n: int) -> list[str]:
    return sorted((str(uuid4()) for _ in range(n)), reverse=True)


async def _seed_tied(
    session, kind: str, n: int = 12, *, distinct_titles: bool = False, **extra
) -> list[str]:
    """Insert ``n`` identical rows (identical score) in DESCENDING id order.

    Returns the ids in the REQUIRED order: created_at asc, then id asc. The last
    two rows get a LATER created_at, so the created_at tiebreak is exercised too:
    they must sort after every earlier row despite having the smallest ids.

    ``distinct_titles`` suffixes each title (filter tests only): project-scoped
    rows are unique on ``(project, title)``, so identical titles cannot coexist.
    """
    ids = _ids_desc(n)
    later = set(sorted(ids)[:2])  # the two SMALLEST ids are created LATER
    for i, rid in enumerate(ids):
        ts = LATER_TS if rid in later else FIXED_TS
        title = f"Latency budget {rid[:8]}" if distinct_titles else "Latency budget"
        common = {
            "id": rid,
            "slug": f"tied-{kind}-{rid}",
            "tenant_id": TENANT,
            "created_at": ts,
        }
        if kind == "requirement":
            row = Requirement(
                title=title,
                description="Latency budget for the gateway",
                effort=1.0,
                links={},
                acceptance=[],
                **common,
                **extra,
            )
        elif kind == "principle":
            row = Principle(
                title=title,
                statement="Latency budget for the gateway",
                **common,
                **extra,
            )
        else:
            row = Decision(
                title=title,
                context="Latency budget for the gateway",
                decision="Latency budget",
                **common,
                **extra,
            )
        session.add(row)
        await session.flush()  # one row per INSERT: keep heap order == this loop's order
    await session.commit()
    earlier = sorted(i for i in ids if i not in later)
    return earlier + sorted(later)


SEARCH_PATH = {
    "requirement": "/search",
    "principle": "/search/principles",
    "decision": "/search/decisions",
}


async def _search(client, kind, **params):
    r = await client.get(SEARCH_PATH[kind], headers=H, params={"q": "gateway", **params})
    assert r.status_code == 200, r.text
    return r.json()


class TestDeterministicSearch:
    @pytest.mark.parametrize("kind", sorted(SEARCH_PATH))
    async def test_tied_scores_come_back_in_the_same_total_order(
        self, test_client, test_session, kind
    ):
        expected = await _seed_tied(test_session, kind)
        first = await _search(test_client, kind, k=50)
        second = await _search(test_client, kind, k=50)
        assert len({h["similarity"] for h in first}) == 1, "fixture must produce a single tie"
        got = [h["id"] for h in first]
        assert got == [h["id"] for h in second], "same query, different order"
        assert got == expected, "ties must sort by created_at asc, then id asc"

    @pytest.mark.parametrize("kind", sorted(SEARCH_PATH))
    async def test_k_window_over_a_tie_is_the_same_subset(self, test_client, test_session, kind):
        expected = await _seed_tied(test_session, kind)
        top5 = [h["id"] for h in await _search(test_client, kind, k=5)]
        assert top5 == expected[:5]

    @pytest.mark.parametrize("kind", sorted(SEARCH_PATH))
    async def test_offset_pages_are_disjoint_and_concatenate(self, test_client, test_session, kind):
        expected = await _seed_tied(test_session, kind)
        pages = []
        for off in (0, 5, 10):
            pages += [h["id"] for h in await _search(test_client, kind, k=5, offset=off)]
        assert pages == expected

    async def test_requirement_filters_narrow(self, test_client, test_session):
        await _seed_tied(
            test_session, "requirement", n=3, distinct_titles=True, project_id="proj-a"
        )
        b = await _seed_tied(
            test_session, "requirement", n=2, distinct_titles=True, project_id="proj-b"
        )
        got = [h["id"] for h in await _search(test_client, "requirement", project_id="proj-b")]
        assert sorted(got) == sorted(b)
        assert await _search(test_client, "requirement", version_status="PUBLISHED") == []
        assert len(await _search(test_client, "requirement", version_status="DRAFT")) == 5

    async def test_principle_filters_narrow(self, test_client, test_session):
        proj = str(uuid4())
        await _seed_tied(test_session, "principle", n=3, distinct_titles=True, active=True)
        inactive = await _seed_tied(
            test_session,
            "principle",
            n=2,
            distinct_titles=True,
            active=False,
            origin_kind="project",
            origin_project_id=proj,
        )
        assert len(await _search(test_client, "principle", active_only="true")) == 3
        # project_id is deliberately NOT a principle filter (provenance != scope).
        r = await test_client.get(
            SEARCH_PATH["principle"], headers=H, params={"q": "gateway", "project_id": proj}
        )
        assert r.status_code == 200 and len(r.json()) == 5, "project_id must not narrow principles"
        got = [h["id"] for h in await _search(test_client, "principle", origin_kind="project")]
        assert sorted(got) == sorted(inactive)

    async def test_decision_filters_narrow(self, test_client, test_session):
        await _seed_tied(
            test_session, "decision", n=3, distinct_titles=True, status="proposed", project_id="p1"
        )
        acc = await _seed_tied(
            test_session, "decision", n=2, distinct_titles=True, status="accepted", project_id="p2"
        )
        got = [h["id"] for h in await _search(test_client, "decision", status="accepted")]
        assert sorted(got) == sorted(acc)
        got = [h["id"] for h in await _search(test_client, "decision", project_id="p2")]
        assert sorted(got) == sorted(acc)

    @pytest.mark.parametrize("kind", sorted(SEARCH_PATH))
    async def test_out_of_domain_version_status_is_422(self, test_client, kind):
        r = await test_client.get(
            SEARCH_PATH[kind], headers=H, params={"q": "x", "version_status": "nope"}
        )
        assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# 3. PATCH = partial update; PUT unchanged
# --------------------------------------------------------------------------- #
class TestPatchKeepsOmittedFields:
    async def test_requirement_patch_keeps_omitted_fields(self, test_client):
        r = await test_client.post(
            "/requirements",
            headers=H,
            json={
                "title": "Patch me",
                "description": "keep this",
                "effort": 3.0,
                "acceptance": ["Given a, When b, Then c"],
                "links": {"business_objectives": ["o1"]},
                "project_id": "proj-patch",
            },
        )
        assert r.status_code in (200, 201), r.text
        rid = r.json()["id"]
        p = await test_client.patch(f"/requirements/{rid}", headers=H, json={"title": "Patched"})
        assert p.status_code == 200, p.text
        body = p.json()
        assert body["title"] == "Patched"
        assert body["description"] == "keep this"
        assert body["effort"] == 3.0
        assert body["acceptance"] == ["Given a, When b, Then c"]
        assert body["links"]["business_objectives"] == ["o1"]
        assert body["project_id"] == "proj-patch"
        got = (await test_client.get(f"/requirements/{rid}", headers=H)).json()
        assert got["description"] == "keep this" and got["title"] == "Patched"

    async def test_principle_patch_keeps_rationale_and_tags(self, test_client):
        r = await test_client.post(
            "/principles",
            headers=H,
            json={"title": "P", "statement": "S", "rationale": "R", "tags": ["t1"]},
        )
        pid = r.json()["id"]
        p = await test_client.patch(f"/principles/{pid}", headers=H, json={"statement": "S2"})
        assert p.status_code == 200, p.text
        assert p.json()["statement"] == "S2"
        assert p.json()["rationale"] == "R"
        assert p.json()["tags"] == ["t1"]

    async def test_decision_patch_keeps_status(self, test_client):
        r = await test_client.post(
            "/decisions",
            headers=H,
            json={
                "title": "D",
                "context": "C",
                "decision": "X",
                "status": "accepted",
                "consequences": "Q",
            },
        )
        did = r.json()["id"]
        p = await test_client.patch(f"/decisions/{did}", headers=H, json={"context": "C2"})
        assert p.status_code == 200, p.text
        assert p.json()["context"] == "C2"
        assert p.json()["status"] == "accepted"
        assert p.json()["consequences"] == "Q"

    async def test_explicit_null_clears_an_optional_field(self, test_client):
        r = await test_client.post(
            "/principles", headers=H, json={"title": "N", "statement": "S", "rationale": "R"}
        )
        pid = r.json()["id"]
        p = await test_client.patch(f"/principles/{pid}", headers=H, json={"rationale": None})
        assert p.status_code == 200, p.text
        assert p.json()["rationale"] is None

    async def test_put_still_replaces(self, test_client):
        """Regression: PUT semantics are unchanged (omitted -> schema default)."""
        r = await test_client.post(
            "/principles", headers=H, json={"title": "U", "statement": "S", "rationale": "R"}
        )
        pid = r.json()["id"]
        u = await test_client.put(
            f"/principles/{pid}", headers=H, json={"title": "U", "statement": "S"}
        )
        assert u.status_code == 200, u.text
        assert u.json()["rationale"] is None

    @pytest.mark.parametrize(
        "body", [{}, {"no_such_field": 1}, {"effort": None}], ids=["empty", "unknown", "null-req"]
    )
    async def test_invalid_patch_is_422_and_changes_nothing(self, test_client, body):
        r = await test_client.post(
            "/requirements",
            headers=H,
            json={"title": "Bad patch", "description": "d", "effort": 2.0},
        )
        rid = r.json()["id"]
        p = await test_client.patch(f"/requirements/{rid}", headers=H, json=body)
        assert p.status_code == 422, p.text
        got = (await test_client.get(f"/requirements/{rid}", headers=H)).json()
        assert got["effort"] == 2.0 and got["title"] == "Bad patch"

    @pytest.mark.parametrize("path", ["/requirements", "/principles", "/decisions"])
    async def test_patch_missing_or_foreign_row_is_404(self, test_client, path):
        p = await test_client.patch(f"{path}/{uuid4()}", headers=H, json={"title": "x"})
        assert p.status_code == 404, p.text
