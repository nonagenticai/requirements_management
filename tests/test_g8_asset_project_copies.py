"""G8 / R313 — governed assets: guarded PATCH, project copy-on-write, project view, promote.

George 2026-10-19 (``~/.claude/plans/program/v4-alignment/G8-asset-versioning.md``): an update
made from a running project never overwrites the tenant-level asset. RM enforces it for every
writer (``services/governed_asset_service.py``). All tests hit real PostgreSQL through the
FastAPI test client; only licence introspection is faked (the ``licences`` fixture).
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.principles import Principle
from models.version_history import EntityVersionHistory
from services import (
    decision_service,
    principle_service,
    requirement_service,
    risk_service,
)
from services.governed_asset_service import GovernedAssetService
from tests.tenants import T
from tests.test_pm_project_intake import (  # noqa: F401 — `licences` is a fixture
    CUSTOMER_TENANT,
    DELEGATED,
    DELEGATED_SAME_TENANT,
    LICENCE,
    licences,
)

TENANT = T("g8-primary")
OTHER_TENANT = T("g8-other")
H = {"X-Tenant-Id": TENANT}
H_OTHER = {"X-Tenant-Id": OTHER_TENANT}
# The conveyor in the customer tenant (a plain licence, not delegated).
CONVEYOR = {"X-Tenant-Id": CUSTOMER_TENANT}

P = UUID("0195f48b-d000-7000-8000-00000000a001")  # the project making the edit
Q = UUID("0195f48b-d000-7000-8000-00000000a002")  # another project

KINDS = {
    "principles": {"title": "Reuse first", "statement": "Prefer reuse", "rationale": "R"},
    "requirements": {"title": "Login works", "description": "Users log in", "effort": 3.0},
    "decisions": {"title": "Use Postgres", "context": "We need a DB", "decision": "Postgres"},
}
# A content field per kind that a project edit changes.
CONTENT = {"principles": "statement", "requirements": "description", "decisions": "context"}
SERVICES = {
    "principles": principle_service,
    "requirements": requirement_service,
    "decisions": decision_service,
}


def _code(resp) -> str | None:
    body = resp.json()
    if not isinstance(body, dict):
        return None
    return (body.get("error") or {}).get("code") or (body.get("detail") or {}).get("code")


async def _create(c: AsyncClient, kind: str, headers=H, **extra) -> dict:
    r = await c.post(f"/{kind}", headers=headers, json={**KINDS[kind], **extra})
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _copy(c: AsyncClient, kind: str, oid: str, project=P, headers=H, **changes):
    return await c.post(
        f"/{kind}/{oid}/project-copy",
        headers=headers,
        json={"project_id": str(project), "changes": changes},
    )


async def _list_ids(c: AsyncClient, kind: str, headers=H, **params) -> set[str]:
    r = await c.get(f"/{kind}", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return {row["id"] for row in r.json()}


async def _history(session: AsyncSession, entity_id: str) -> list[str]:
    rows = await session.execute(
        select(EntityVersionHistory.version_status)
        .where(EntityVersionHistory.entity_id == UUID(entity_id))
        .order_by(EntityVersionHistory.id)
    )
    return list(rows.scalars())


# --------------------------------------------------------------------------- #
# Unit: ownership + server-managed fields
# --------------------------------------------------------------------------- #
class _Row:
    def __init__(self, origin_kind="tenant", origin_project_id=None, project_id=None):
        self.origin_kind = origin_kind
        self.origin_project_id = origin_project_id
        self.project_id = project_id


@pytest.mark.parametrize(
    ("svc", "row", "owned"),
    [
        (principle_service, _Row("project", P), True),
        (principle_service, _Row("project", Q), False),
        (principle_service, _Row("tenant"), False),
        # origin_kind must be 'project' — a workflow-origin row naming P is not P's.
        (principle_service, _Row("workflow", P), False),
        (requirement_service, _Row("tenant", None, str(P)), True),
        (requirement_service, _Row("tenant", None, str(P).upper()), True),
        (requirement_service, _Row("tenant", None, str(Q)), False),
        (requirement_service, _Row("tenant", None, None), False),
        (decision_service, _Row("project", P, None), True),
        (decision_service, _Row("tenant", None, str(P)), True),
    ],
)
def test_owns(svc: GovernedAssetService, row, owned: bool) -> None:
    assert svc.owns(row, P) is owned


def test_server_managed_fields_are_per_kind() -> None:
    governed = {"origin_kind", "origin_project_id", "supersedes"}
    assert governed <= principle_service.patch_server_managed
    assert "project_id" not in principle_service.patch_server_managed  # no such column
    for svc in (requirement_service, decision_service):
        assert governed | {"project_id"} <= svc.patch_server_managed
    for svc in SERVICES.values():
        # PUT (``server_managed``) keeps writing origin / project; only supersedes is kept.
        assert "supersedes" in svc.server_managed
        assert not {"origin_kind", "origin_project_id", "project_id"} & svc.server_managed
    # Blast radius: non-governed PM entities keep their own rules (PATCH may move a risk).
    assert "project_id" not in risk_service.patch_server_managed
    assert "supersedes" not in risk_service.patch_server_managed
    # superseded_by stays client-writable: the conveyor writes it on decisions today.
    assert "superseded_by" not in decision_service.patch_server_managed


async def test_patch_schema_does_not_publish_server_managed_fields(test_client) -> None:
    spec = (await test_client.get("/openapi.json")).json()
    schemas = spec["components"]["schemas"]
    for name in ("PrinciplePatch", "RequirementPatch", "DecisionPatch"):
        props = set(schemas[name]["properties"])
        assert not {"origin_kind", "origin_project_id", "supersedes", "project_id"} & props, (
            name,
            props,
        )
        assert "title" in props  # the walk sees real fields


# --------------------------------------------------------------------------- #
# 1(a)/(b): the PATCH guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", sorted(KINDS))
class TestPatchGuard:
    async def test_scoped_patch_on_inherited_row_is_409_and_changes_nothing(
        self, test_client, kind
    ):
        row = await _create(test_client, kind)
        field = CONTENT[kind]
        r = await test_client.patch(
            f"/{kind}/{row['id']}",
            headers=H,
            params={"scope_project_id": str(P)},
            json={field: "project edit"},
        )
        assert r.status_code == 409, r.text
        assert _code(r) == "inherited_asset_requires_copy"
        got = (await test_client.get(f"/{kind}/{row['id']}", headers=H)).json()
        assert got[field] == KINDS[kind][field]

    async def test_scoped_patch_on_owned_row_is_200(self, test_client, kind):
        row = await _create(test_client, kind, origin_kind="project", origin_project_id=str(P))
        field = CONTENT[kind]
        r = await test_client.patch(
            f"/{kind}/{row['id']}",
            headers=H,
            params={"scope_project_id": str(P)},
            json={field: "owned edit"},
        )
        assert r.status_code == 200, r.text
        assert r.json()[field] == "owned edit"

    async def test_scoped_patch_on_another_projects_row_is_409(self, test_client, kind):
        row = await _create(test_client, kind, origin_kind="project", origin_project_id=str(Q))
        r = await test_client.patch(
            f"/{kind}/{row['id']}",
            headers=H,
            params={"scope_project_id": str(P)},
            json={CONTENT[kind]: "x"},
        )
        assert r.status_code == 409, r.text
        assert _code(r) == "inherited_asset_requires_copy"

    async def test_unscoped_patch_stays_allowed(self, test_client, kind):
        row = await _create(test_client, kind)
        r = await test_client.patch(
            f"/{kind}/{row['id']}", headers=H, json={CONTENT[kind]: "tenant-level edit"}
        )
        assert r.status_code == 200, r.text
        assert r.json()[CONTENT[kind]] == "tenant-level edit"

    async def test_cross_tenant_scoped_patch_is_404_not_409(self, test_client, kind):
        row = await _create(test_client, kind, headers=H_OTHER)
        r = await test_client.patch(
            f"/{kind}/{row['id']}",
            headers=H,
            params={"scope_project_id": str(P)},
            json={CONTENT[kind]: "x"},
        )
        assert r.status_code == 404, r.text

    @pytest.mark.parametrize(
        "field_value",
        [
            ("origin_kind", "project"),
            ("origin_project_id", str(P)),
            ("supersedes", "some-slug"),
        ],
        ids=lambda fv: fv[0],
    )
    async def test_scope_and_provenance_fields_cannot_be_patched(
        self, test_client, kind, field_value
    ):
        row = await _create(test_client, kind)
        field, value = field_value
        r = await test_client.patch(f"/{kind}/{row['id']}", headers=H, json={field: value})
        assert r.status_code == 422, r.text
        got = (await test_client.get(f"/{kind}/{row['id']}", headers=H)).json()
        assert got["origin_kind"] == "tenant"
        assert got["origin_project_id"] is None
        assert got["supersedes"] is None


@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_no_client_can_mark_a_row_superseded(test_client, kind) -> None:
    row = await _create(test_client, kind)
    r = await test_client.patch(
        f"/{kind}/{row['id']}", headers=H, json={"version_status": "SUPERSEDED"}
    )
    assert r.status_code == 422, r.text
    body = {**KINDS[kind], "title": "x", "version_status": "SUPERSEDED"}
    r = await test_client.post(f"/{kind}", headers=H, json=body)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_no_client_can_create_a_copy_by_hand(test_client, kind) -> None:
    """``supersedes`` marks a project copy; only project-copy writes it."""
    original = await _create(test_client, kind)
    body = {
        **KINDS[kind],
        "title": "fake copy",
        "supersedes": original["slug"],
        "origin_kind": "project",
        "origin_project_id": str(P),
    }
    r = await test_client.post(f"/{kind}", headers=H, json=body)
    assert r.status_code == 422, r.text
    assert original["id"] in await _list_ids(test_client, kind, scope_project_id=str(P))


@pytest.mark.parametrize("kind", ["requirements", "decisions"])
async def test_project_id_can_no_longer_be_patched(test_client, kind) -> None:
    """NEGATIVE: before G8 ``{"project_id": X}`` MOVED the record (a field write)."""
    row = await _create(test_client, kind, project_id="proj-home")
    r = await test_client.patch(
        f"/{kind}/{row['id']}", headers=H, json={"project_id": "proj-elsewhere"}
    )
    assert r.status_code == 422, r.text
    got = (await test_client.get(f"/{kind}/{row['id']}", headers=H)).json()
    assert got["project_id"] == "proj-home"
    # The scope query parameter is not a write either.
    r = await test_client.patch(
        f"/{kind}/{row['id']}",
        headers=H,
        params={"scope_project_id": str(P)},
        json={CONTENT[kind]: "x"},
    )
    assert r.status_code == 409, r.text
    got = (await test_client.get(f"/{kind}/{row['id']}", headers=H)).json()
    assert got["project_id"] == "proj-home"


@pytest.mark.parametrize("kind", ["requirements", "decisions"])
async def test_put_can_still_move_project_and_origin(test_client, kind) -> None:
    """Lead decision 2026-10-19: PUT keeps its previous behaviour for project_id /
    origin_kind / origin_project_id (fr's withPreservedOrigin). Only PATCH refuses them."""
    row = await _create(test_client, kind, project_id="proj-home")
    body = {
        **KINDS[kind],
        "project_id": "proj-elsewhere",
        "origin_kind": "project",
        "origin_project_id": str(Q),
    }
    r = await test_client.put(f"/{kind}/{row['id']}", headers=H, json=body)
    assert r.status_code == 200, r.text
    got = (await test_client.get(f"/{kind}/{row['id']}", headers=H)).json()
    assert got["project_id"] == "proj-elsewhere"
    assert got["origin_kind"] == "project"
    assert got["origin_project_id"] == str(Q)


async def test_put_can_still_move_a_principles_origin(test_client) -> None:
    row = await _create(test_client, "principles")
    body = {**KINDS["principles"], "origin_kind": "project", "origin_project_id": str(Q)}
    r = await test_client.put(f"/principles/{row['id']}", headers=H, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["origin_project_id"] == str(Q)


async def test_put_keeps_supersedes(test_client) -> None:
    """``supersedes`` marks a project copy and stays server-managed on PUT."""
    row = await _create(test_client, "principles")
    body = {**KINDS["principles"], "supersedes": "someone-else"}
    r = await test_client.put(f"/principles/{row['id']}", headers=H, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["supersedes"] is None


@pytest.mark.parametrize("kind", sorted(KINDS))
@pytest.mark.parametrize("bad", ["proj-alpha", "", "123", None])
async def test_project_copy_needs_a_uuid_project(test_client, kind, bad) -> None:
    original = await _create(test_client, kind)
    r = await test_client.post(
        f"/{kind}/{original['id']}/project-copy", headers=H, json={"project_id": bad}
    )
    assert r.status_code == 422, r.text
    assert original["id"] in await _list_ids(test_client, kind)
    assert len(await _list_ids(test_client, kind)) == 1  # no copy was made


async def test_patch_service_refuses_server_managed_changes_directly(
    test_session: AsyncSession,
) -> None:
    """The service backstop, for callers that bypass the route's PATCH schema."""
    from fastapi import HTTPException

    from schemas.requirements import RequirementIn

    with pytest.raises(HTTPException) as exc:
        await requirement_service.patch(
            test_session, uuid4(), {"project_id": "x"}, RequirementIn, TENANT
        )
    assert exc.value.status_code == 422


# --------------------------------------------------------------------------- #
# 1(c): project-copy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", sorted(KINDS))
class TestProjectCopy:
    async def test_creates_a_draft_copy_and_leaves_the_original(
        self, test_client, test_session, kind
    ):
        original = await _create(test_client, kind)
        field = CONTENT[kind]
        r = await _copy(test_client, kind, original["id"], **{field: "project version"})
        assert r.status_code == 200, r.text
        assert r.headers["X-Project-Copy"] == "created"
        copy = r.json()
        assert copy["id"] != original["id"]
        assert copy["origin_kind"] == "project"
        assert copy["origin_project_id"] == str(P)
        assert copy["supersedes"] == original["slug"]
        assert copy["version_status"] == "DRAFT"
        assert copy[field] == "project version"
        assert copy["title"] == original["title"]
        if kind != "principles":
            assert copy["project_id"] == str(P)
        got = (await test_client.get(f"/{kind}/{original['id']}", headers=H)).json()
        assert got[field] == KINDS[kind][field]
        assert got["origin_kind"] == "tenant"
        assert await _history(test_session, copy["id"]) == ["project_copy"]
        # The copy is P's: a scoped PATCH on it is allowed.
        r = await test_client.patch(
            f"/{kind}/{copy['id']}",
            headers=H,
            params={"scope_project_id": str(P)},
            json={field: "again"},
        )
        assert r.status_code == 200, r.text

    async def test_is_idempotent_on_original_and_project(self, test_client, kind):
        original = await _create(test_client, kind)
        field = CONTENT[kind]
        first = await _copy(test_client, kind, original["id"], **{field: "v1"})
        second = await _copy(test_client, kind, original["id"], **{field: "v2"})
        assert first.status_code == second.status_code == 200, second.text
        assert second.headers["X-Project-Copy"] == "existing"
        assert second.json()["id"] == first.json()["id"]
        assert second.json()[field] == "v2"
        # Another project gets its own copy.
        other = await _copy(test_client, kind, original["id"], project=Q, **{field: "q"})
        assert other.status_code == 200, other.text
        assert other.headers["X-Project-Copy"] == "created"
        assert other.json()["id"] != first.json()["id"]

    async def test_a_repeat_with_no_changes_returns_the_copy(self, test_client, kind):
        original = await _create(test_client, kind)
        first = await _copy(test_client, kind, original["id"])
        second = await _copy(test_client, kind, original["id"])
        assert first.status_code == second.status_code == 200, second.text
        assert second.json()["id"] == first.json()["id"]

    async def test_owned_row_is_409_patch_it_instead(self, test_client, kind):
        owned = await _create(test_client, kind, origin_kind="project", origin_project_id=str(P))
        r = await _copy(test_client, kind, owned["id"])
        assert r.status_code == 409, r.text
        assert _code(r) == "asset_owned_by_project"

    async def test_a_copy_of_a_copy_is_409(self, test_client, kind):
        original = await _create(test_client, kind)
        copy = (await _copy(test_client, kind, original["id"], project=Q)).json()
        r = await _copy(test_client, kind, copy["id"])
        assert r.status_code == 409, r.text
        assert _code(r) == "asset_is_a_project_copy"

    async def test_changes_may_not_carry_server_managed_fields(self, test_client, kind):
        original = await _create(test_client, kind)
        r = await _copy(test_client, kind, original["id"], supersedes="other")
        assert r.status_code == 422, r.text
        r = await _copy(test_client, kind, original["id"], origin_project_id=str(Q))
        assert r.status_code == 422, r.text

    async def test_cross_tenant(self, test_client, kind):
        foreign = await _create(test_client, kind, headers=H_OTHER)
        r = await _copy(test_client, kind, foreign["id"])
        assert r.status_code == 404, r.text
        mine = await _create(test_client, kind)
        copy = (await _copy(test_client, kind, mine["id"])).json()
        assert copy["id"] not in await _list_ids(test_client, kind, headers=H_OTHER)
        assert copy["id"] not in await _list_ids(
            test_client, kind, headers=H_OTHER, scope_project_id=str(P)
        )
        r = await test_client.get(f"/{kind}/{copy['id']}", headers=H_OTHER)
        assert r.status_code == 404, r.text


@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_concurrent_project_copies_make_one_copy(test_client, test_session, kind) -> None:
    original = await _create(test_client, kind)
    results = await asyncio.gather(
        *(
            _copy(test_client, kind, original["id"], **{CONTENT[kind]: f"racer {i}"})
            for i in range(6)
        )
    )
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1, ids
    assert [r.headers["X-Project-Copy"] for r in results].count("created") == 1
    model = SERVICES[kind].model_class
    n = await test_session.scalar(
        select(func.count()).select_from(model).where(model.supersedes == original["slug"])
    )
    assert n == 1


async def test_race_loser_recovers_through_the_unique_index(
    test_client, test_session, monkeypatch
) -> None:
    """Deterministic interleaving: the loser's pre-check misses the winner (as it would
    mid-race), its INSERT hits ``uq_principles_tenant_supersedes_origin_project``, and the
    recovery re-SELECT finds the winner and patches it."""
    original = await _create(test_client, "principles")
    winner = (await _copy(test_client, "principles", original["id"], statement="winner")).json()

    real = GovernedAssetService._find_copy
    calls = {"n": 0}

    async def miss_once(self, session, tenant_id, slug, project_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real(self, session, tenant_id, slug, project_id)

    monkeypatch.setattr(GovernedAssetService, "_find_copy", miss_once)
    # A different title, so only the supersedes index (not the project title index) can
    # catch the duplicate.
    r = await _copy(
        test_client, "principles", original["id"], statement="loser", title="loser title"
    )
    assert r.status_code == 200, r.text
    assert calls["n"] == 2  # pre-check (missed) + recovery re-SELECT
    assert r.headers["X-Project-Copy"] == "existing"
    assert r.json()["id"] == winner["id"]
    assert r.json()["statement"] == "loser"
    n = await test_session.scalar(
        select(func.count()).select_from(Principle).where(Principle.supersedes == original["slug"])
    )
    assert n == 1


async def test_a_title_collision_in_the_project_is_409(test_client) -> None:
    """The project already owns a requirement with the original's title (its own, not a
    copy): the copy's INSERT trips ``uq_requirements_tenant_project_title``, the copy
    re-SELECT finds nothing, and the answer is an honest 409 — never a 500."""
    original = await _create(test_client, "requirements")
    await _create(test_client, "requirements", project_id=str(P))  # same title, owned by P
    r = await _copy(test_client, "requirements", original["id"], description="x")
    assert r.status_code == 409, r.text
    assert _code(r) == "project_copy_conflict"


# --------------------------------------------------------------------------- #
# 2: the project view
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_project_view_shadows_the_original(test_client, kind) -> None:
    original = await _create(test_client, kind)
    untouched = await _create(test_client, kind, title=f"untouched {kind}")
    copy = (await _copy(test_client, kind, original["id"])).json()
    q_copy = (await _copy(test_client, kind, untouched["id"], project=Q)).json()

    # Tenant-level list: unchanged by G8.
    assert {original["id"], untouched["id"], copy["id"], q_copy["id"]} <= await _list_ids(
        test_client, kind
    )
    in_p = await _list_ids(test_client, kind, scope_project_id=str(P))
    assert copy["id"] in in_p and original["id"] not in in_p
    assert untouched["id"] in in_p and q_copy["id"] not in in_p
    with_shadowed = await _list_ids(
        test_client, kind, scope_project_id=str(P), include_shadowed="true"
    )
    assert {original["id"], copy["id"], untouched["id"]} <= with_shadowed
    assert q_copy["id"] not in with_shadowed
    in_q = await _list_ids(test_client, kind, scope_project_id=str(Q))
    assert original["id"] in in_q and copy["id"] not in in_q
    assert q_copy["id"] in in_q and untouched["id"] not in in_q


async def test_project_view_composes_with_filters(test_client) -> None:
    """``scope_project_id`` + the ``project_id`` filter (requirements): the filter narrows
    to rows whose column equals it; the view only swaps copies for originals."""
    original = await _create(test_client, "requirements")
    copy = (await _copy(test_client, "requirements", original["id"])).json()
    got = await _list_ids(test_client, "requirements", scope_project_id=str(P), project_id=str(P))
    assert got == {copy["id"]}


# --------------------------------------------------------------------------- #
# 4 (RM side): promote
# --------------------------------------------------------------------------- #
async def _customer_copy(c: AsyncClient, kind: str) -> tuple[dict, dict]:
    original = await _create(c, kind, headers=CONVEYOR)
    r = await _copy(
        c, kind, original["id"], headers=CONVEYOR, **{CONTENT[kind]: "promoted content"}
    )
    assert r.status_code == 200, r.text
    return original, r.json()


@pytest.mark.usefixtures("licences")
@pytest.mark.parametrize("kind", sorted(KINDS))
class TestPromote:
    async def test_fr_promotes_the_copy_into_the_original(self, test_client, test_session, kind):
        original, copy = await _customer_copy(test_client, kind)
        r = await test_client.post(
            f"/{kind}/{copy['id']}/promote", headers=DELEGATED, json={"approved_by": "admin-1"}
        )
        assert r.status_code == 200, r.text
        promoted = r.json()
        field = CONTENT[kind]
        assert promoted["id"] == original["id"]
        assert promoted[field] == "promoted content"
        assert promoted["version"] == "1.1.0"
        assert promoted["approved_by"] == "admin-1"
        assert promoted["approved_at"] is not None
        assert copy["slug"] in promoted["version_notes"]
        # The original keeps its own scope and provenance.
        assert promoted["origin_kind"] == "tenant"
        assert promoted["supersedes"] is None
        if kind != "principles":
            assert promoted["project_id"] is None
        merged = (await test_client.get(f"/{kind}/{copy['id']}", headers=CONVEYOR)).json()
        assert merged["version_status"] == "SUPERSEDED"
        assert merged["superseded_by"] == original["slug"]
        assert merged["supersedes"] == original["slug"]
        assert (await _history(test_session, original["id"]))[-1] == "promoted"
        assert (await _history(test_session, copy["id"]))[-1] == "superseded"

        # The chain is terminal for this copy …
        again = await test_client.post(f"/{kind}/{copy['id']}/promote", headers=DELEGATED)
        assert again.status_code == 409, again.text
        assert _code(again) == "copy_already_promoted"
        r = await test_client.patch(
            f"/{kind}/{copy['id']}", headers=CONVEYOR, json={field: "late edit"}
        )
        assert r.status_code == 409, r.text
        assert _code(r) == "asset_superseded"
        put_body = {**KINDS[kind], field: "late put"}
        r = await test_client.put(f"/{kind}/{copy['id']}", headers=CONVEYOR, json=put_body)
        assert r.status_code == 409, r.text
        assert _code(r) == "asset_superseded"
        # … the project now sees the promoted original, not its merged copy …
        in_p = await _list_ids(test_client, kind, headers=CONVEYOR, scope_project_id=str(P))
        assert original["id"] in in_p and copy["id"] not in in_p
        # … and a later edit for the project REVIVES its copy (one row per original and
        # project, for life) from the original's current, merged content.
        revived = await _copy(
            test_client, kind, original["id"], headers=CONVEYOR, title="after merge"
        )
        assert revived.status_code == 200, revived.text
        assert revived.headers["X-Project-Copy"] == "revived"
        body = revived.json()
        assert body["id"] == copy["id"]
        assert body["version_status"] == "DRAFT"
        assert body["superseded_by"] is None
        assert body["title"] == "after merge"
        assert body[field] == "promoted content"
        assert body["version"] == "1.1.0"
        assert (await _history(test_session, copy["id"]))[-1] == "project_copy"
        in_p = await _list_ids(test_client, kind, headers=CONVEYOR, scope_project_id=str(P))
        assert copy["id"] in in_p and original["id"] not in in_p

    async def test_conveyor_callers_may_not_promote(self, test_client, kind):
        original, copy = await _customer_copy(test_client, kind)
        for headers in (CONVEYOR, LICENCE):
            r = await test_client.post(f"/{kind}/{copy['id']}/promote", headers=headers)
            assert r.status_code == 403, r.text
            assert _code(r) == "asset_promotion_requires_fr"
        got = (await test_client.get(f"/{kind}/{original['id']}", headers=CONVEYOR)).json()
        assert got[CONTENT[kind]] == KINDS[kind][CONTENT[kind]]
        assert got["version"] == "1.0.0"

    async def test_fail_closed_without_delegating_subjects(self, test_client, kind, monkeypatch):
        # fr's own tenant, so the middleware accepts the call; without the allowlist the
        # principal is not delegated and promotion is refused.
        original = await _create(
            test_client,
            kind,
            headers={"X-Tenant-Id": DELEGATED_SAME_TENANT["X-Effective-Tenant-Id"]},
        )
        monkeypatch.delenv("AUTH_DELEGATING_SUBJECTS", raising=False)
        r = await test_client.post(
            f"/{kind}/{original['id']}/promote", headers=DELEGATED_SAME_TENANT
        )
        assert r.status_code == 403, r.text
        assert _code(r) == "asset_promotion_requires_fr"

    async def test_promoting_a_non_copy_is_409(self, test_client, kind):
        original = await _create(test_client, kind, headers=CONVEYOR)
        r = await test_client.post(f"/{kind}/{original['id']}/promote", headers=DELEGATED)
        assert r.status_code == 409, r.text
        assert _code(r) == "not_a_project_copy"

    async def test_promote_is_tenant_scoped(self, test_client, kind):
        """fr acting for the customer cannot promote another tenant's copy."""
        original = await _create(test_client, kind, headers=H_OTHER)
        copy = (await _copy(test_client, kind, original["id"], headers=H_OTHER)).json()
        r = await test_client.post(f"/{kind}/{copy['id']}/promote", headers=DELEGATED)
        assert r.status_code == 404, r.text

    async def test_original_deleted_is_409(self, test_client, kind):
        original, copy = await _customer_copy(test_client, kind)
        assert (
            await test_client.delete(f"/{kind}/{original['id']}", headers=CONVEYOR)
        ).status_code == 200
        r = await test_client.post(f"/{kind}/{copy['id']}/promote", headers=DELEGATED)
        assert r.status_code == 409, r.text
        assert _code(r) == "original_missing"


@pytest.mark.usefixtures("licences")
async def test_concurrent_promotes_merge_once(test_client, test_session) -> None:
    original, copy = await _customer_copy(test_client, "principles")
    results = await asyncio.gather(
        *(
            test_client.post(f"/principles/{copy['id']}/promote", headers=DELEGATED)
            for _ in range(3)
        )
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409], [r.text for r in results]
    got = (await test_client.get(f"/principles/{original['id']}", headers=CONVEYOR)).json()
    assert got["version"] == "1.1.0"
    assert (await _history(test_session, original["id"])).count("promoted") == 1
