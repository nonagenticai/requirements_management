"""G3 + R313 §1(b) — a non-fr write to a governed asset must name its project.

George 2026-10-19: "PM" (R16) is the conveyor, and RM guards the requirements / principles /
decisions write routes. RM's identity model makes every non-delegated caller the conveyor
(``auth/author_kind.py``), so the guard is on WHAT it writes: conveyor creates / PUTs /
PATCHes / DELETEs must carry a project scope; tenant-level edits stay fr's (delegated).
``GOVERNED_WRITE_SCOPE_MODE``: ``observe`` (default — log, count, allow) or ``enforce``
(422 ``conveyor_write_requires_project_scope``). Real PostgreSQL through the test client.
"""

from uuid import UUID

import pytest
from httpx import AsyncClient

from auth.governed_scope import MODE_ENV, SCOPE_REQUIRED_CODE, governed_write_scope_mode
from main import app
from metrics import governed_write_unscoped_total
from settings import Settings
from tests.tenants import T
from tests.test_pm_project_intake import (  # noqa: F401 — `licences` is a fixture
    CUSTOMER_TENANT,
    DELEGATED,
    LICENCE,
    licences,
)

CONVEYOR = {"X-Tenant-Id": CUSTOMER_TENANT}  # a plain licence, not delegated
P = UUID("0195f48b-d000-7000-8000-00000000b301")
Q = UUID("0195f48b-d000-7000-8000-00000000b302")

KINDS = {
    "principles": {"title": "G3 reuse", "statement": "Prefer reuse", "rationale": "R"},
    "requirements": {"title": "G3 login", "description": "Users log in", "effort": 3.0},
    "decisions": {"title": "G3 postgres", "context": "We need a DB", "decision": "Postgres"},
}
SINGULAR = {"principles": "principle", "requirements": "requirement", "decisions": "decision"}


def _scoped(kind: str, project: UUID = P) -> dict:
    """The create body's project scope for ``kind`` (principles have no project_id)."""
    if kind == "principles":
        return {"origin_kind": "project", "origin_project_id": str(project)}
    return {"project_id": str(project)}


def _code(resp) -> str | None:
    body = resp.json()
    if not isinstance(body, dict):
        return None
    return (body.get("error") or {}).get("code") or (body.get("detail") or {}).get("code")


def _count(kind: str, op: str, mode: str, result: str) -> float:
    return governed_write_unscoped_total.labels(
        kind=SINGULAR.get(kind, kind), op=op, mode=mode, result=result
    )._value.get()


async def _create(c: AsyncClient, kind: str, headers: dict, title: str, **extra) -> dict:
    r = await c.post(f"/{kind}", headers=headers, json={**KINDS[kind], "title": title, **extra})
    assert r.status_code in (200, 201), r.text
    return r.json()


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #
def test_mode_defaults_to_observe_and_fails_closed_on_a_typo(monkeypatch) -> None:
    monkeypatch.delenv(MODE_ENV, raising=False)
    assert governed_write_scope_mode() == "observe"
    monkeypatch.setenv(MODE_ENV, " ENFORCE ")
    assert governed_write_scope_mode() == "enforce"
    monkeypatch.setenv(MODE_ENV, "enforced")
    with pytest.raises(ValueError):
        governed_write_scope_mode()
    with pytest.raises(ValueError):
        Settings()


def test_every_governed_write_route_is_guarded_or_named_exempt() -> None:
    """Route walk: a new mutating route on these prefixes must carry the guard (its 422
    names the code) or be listed here with why it is exempt."""
    exempt = {
        # always project-scoped by its body (``project_id`` required)
        "/{kind}/{id}/project-copy",
        # fr-only (``require_fr_delegated``)
        "/{kind}/{id}/promote",
        # derived scores / compute; they do not write the asset's content
        "/{kind}/{id}/quality",
        "/{kind}/{id}/priority",
    }
    spec = app.openapi()
    unguarded = []
    for path, ops in spec["paths"].items():
        kind = path.split("/")[1]
        if kind not in KINDS:
            continue
        norm = "/".join(
            "{kind}" if i == 1 and seg in KINDS else ("{id}" if seg.startswith("{") else seg)
            for i, seg in enumerate(path.split("/"))
        )
        for method, op in ops.items():
            if method not in ("post", "put", "patch", "delete"):
                continue
            if norm in exempt or path in exempt:
                continue
            desc = (op.get("responses", {}).get("422") or {}).get("description", "")
            if SCOPE_REQUIRED_CODE not in desc:
                unguarded.append(f"{method.upper()} {path}")
    assert unguarded == []


# --------------------------------------------------------------------------- #
# observe (the default): nothing is refused, every unscoped conveyor write is counted
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("licences")
@pytest.mark.parametrize("kind", list(KINDS))
async def test_observe_allows_and_counts(test_client, kind, monkeypatch) -> None:
    monkeypatch.delenv(MODE_ENV, raising=False)
    before = {op: _count(kind, op, "observe", "allowed") for op in ("create", "patch", "delete")}
    row = await _create(test_client, kind, CONVEYOR, f"{kind} observe")
    r = await test_client.patch(f"/{kind}/{row['id']}", headers=CONVEYOR, json={"title": "x"})
    assert r.status_code == 200, r.text
    r = await test_client.delete(f"/{kind}/{row['id']}", headers=CONVEYOR)
    assert r.status_code == 200, r.text
    for op in ("create", "patch", "delete"):
        assert _count(kind, op, "observe", "allowed") == before[op] + 1, op


@pytest.mark.usefixtures("licences")
async def test_observe_does_not_count_scoped_or_fr_writes(test_client, monkeypatch) -> None:
    monkeypatch.delenv(MODE_ENV, raising=False)
    before = _count("requirements", "create", "observe", "allowed")
    await _create(test_client, "requirements", CONVEYOR, "scoped obs", **_scoped("requirements"))
    await _create(test_client, "requirements", DELEGATED, "fr obs")
    assert _count("requirements", "create", "observe", "allowed") == before


# --------------------------------------------------------------------------- #
# enforce
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("licences")
@pytest.mark.parametrize("kind", list(KINDS))
class TestEnforce:
    @pytest.fixture(autouse=True)
    def _enforce(self, monkeypatch):
        monkeypatch.setenv(MODE_ENV, "enforce")

    async def test_unscoped_conveyor_writes_are_refused(self, test_client, kind) -> None:
        refused = _count(kind, "create", "enforce", "refused")
        for headers in (CONVEYOR, LICENCE):
            r = await test_client.post(f"/{kind}", headers=headers, json=KINDS[kind])
            assert r.status_code == 422, r.text
            assert _code(r) == SCOPE_REQUIRED_CODE
        assert _count(kind, "create", "enforce", "refused") == refused + 2
        # a tenant-level row (made by fr) the conveyor may not touch without a scope
        row = await _create(test_client, kind, DELEGATED, f"{kind} tenant row")
        put_body = {**KINDS[kind], "title": "put"}
        for method, kw in (
            ("patch", {"json": {"title": "x"}}),
            ("put", {"json": put_body}),
            ("delete", {}),
        ):
            r = await test_client.request(
                method.upper(), f"/{kind}/{row['id']}", headers=CONVEYOR, **kw
            )
            assert r.status_code == 422, (method, r.text)
            assert _code(r) == SCOPE_REQUIRED_CODE, method
        got = await test_client.get(f"/{kind}/{row['id']}", headers=CONVEYOR)
        assert got.status_code == 200 and got.json()["title"] == f"{kind} tenant row"

    async def test_scoped_conveyor_writes_on_owned_rows_pass(self, test_client, kind) -> None:
        row = await _create(test_client, kind, CONVEYOR, f"{kind} owned", **_scoped(kind))
        scope = {"scope_project_id": str(P)}
        r = await test_client.patch(
            f"/{kind}/{row['id']}", headers=CONVEYOR, params=scope, json={"title": "patched"}
        )
        assert r.status_code == 200, r.text
        put_body = {**KINDS[kind], "title": "put", **_scoped(kind)}
        r = await test_client.put(
            f"/{kind}/{row['id']}", headers=CONVEYOR, params=scope, json=put_body
        )
        assert r.status_code == 200, r.text
        r = await test_client.delete(f"/{kind}/{row['id']}", headers=CONVEYOR, params=scope)
        assert r.status_code == 200, r.text

    async def test_scoped_put_or_delete_on_an_inherited_row_is_409(self, test_client, kind) -> None:
        row = await _create(test_client, kind, DELEGATED, f"{kind} inherited")
        other = await _create(test_client, kind, CONVEYOR, f"{kind} of Q", **_scoped(kind, Q))
        scope = {"scope_project_id": str(P)}
        for target in (row, other):
            r = await test_client.put(
                f"/{kind}/{target['id']}",
                headers=CONVEYOR,
                params=scope,
                json={**KINDS[kind], "title": "steal"},
            )
            assert r.status_code == 409, r.text
            assert _code(r) == "inherited_asset_requires_copy"
            r = await test_client.delete(f"/{kind}/{target['id']}", headers=CONVEYOR, params=scope)
            assert r.status_code == 409, r.text
            assert _code(r) == "inherited_asset_requires_copy"
        # the copy-on-write path stays open to the conveyor
        r = await test_client.post(
            f"/{kind}/{row['id']}/project-copy",
            headers=CONVEYOR,
            json={"project_id": str(P), "changes": {"title": "project version"}},
        )
        assert r.status_code == 200, r.text

    async def test_fr_delegated_tenant_level_writes_stay_allowed(self, test_client, kind) -> None:
        row = await _create(test_client, kind, DELEGATED, f"{kind} fr")
        r = await test_client.patch(f"/{kind}/{row['id']}", headers=DELEGATED, json={"title": "y"})
        assert r.status_code == 200, r.text
        r = await test_client.put(
            f"/{kind}/{row['id']}", headers=DELEGATED, json={**KINDS[kind], "title": "z"}
        )
        assert r.status_code == 200, r.text
        r = await test_client.delete(f"/{kind}/{row['id']}", headers=DELEGATED)
        assert r.status_code == 200, r.text

    async def test_fr_may_still_promote(self, test_client, kind) -> None:
        original = await _create(test_client, kind, DELEGATED, f"{kind} to promote")
        copy = await test_client.post(
            f"/{kind}/{original['id']}/project-copy",
            headers=CONVEYOR,
            json={"project_id": str(P), "changes": {"title": f"{kind} promoted"}},
        )
        assert copy.status_code == 200, copy.text
        r = await test_client.post(f"/{kind}/{copy.json()['id']}/promote", headers=DELEGATED)
        assert r.status_code == 200, r.text

    async def test_conveyor_bulk_delete_is_refused(self, test_client, kind) -> None:
        tenant = T(f"g3-bulk-{kind}")
        headers = {"X-Tenant-Id": tenant}
        row = await _create(test_client, kind, headers, f"{kind} bulk survivor", **_scoped(kind))
        r = await test_client.delete(f"/{kind}/bulk", headers=headers, params={"confirm": tenant})
        assert r.status_code == 422, r.text
        assert _code(r) == SCOPE_REQUIRED_CODE
        got = await test_client.get(f"/{kind}/{row['id']}", headers=headers)
        assert got.status_code == 200, got.text
