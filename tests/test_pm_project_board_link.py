"""The project ↔ Planka board link (George 10-13, v4 R73 / D20).

fr links the board it built (``PUT /pm/projects/{eid}/planka-board``); n8n goes from a Planka
webhook's board id to the RM project (``GET /pm/projects/by-planka-board/{planka_board_id}``).

All tests hit real PostgreSQL through the FastAPI test client; only licence introspection is
faked (the ``licences`` fixture), as in ``test_pm_project_intake.py``.
"""

import uuid

import pytest
from httpx import AsyncClient

from tests.briefs import MIN_BRIEF
from tests.project_types import typed
from tests.test_pm_project_intake import (  # noqa: F401 — `licences` is a fixture
    CONVEYOR_TENANT,
    CUSTOMER_TENANT,
    DELEGATED,
    LICENCE,
    licences,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("licences")]

# The conveyor in the customer's tenant: a plain (non-delegated) licence.
CONVEYOR = {"X-Tenant-Id": CUSTOMER_TENANT}
PM_ONLY = "project_writes_are_pm_only"
LISTS = ("TODO", "READY", "DOING", "QA", "DONE", "BLOCKED")
SHA = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret


def _code(resp) -> str | None:
    body = resp.json()
    return (body.get("error") or {}).get("code") if isinstance(body, dict) else None


def _board() -> str:
    """A Planka-shaped id: a 64-bit snowflake as a string, unique per call."""
    return str(uuid.uuid4().int >> 66)


def _link(board: str | None = None, *, planka_project_id: str | None = "1700") -> dict:
    board = board or _board()
    return {
        "planka_board_id": board,
        "planka_project_id": planka_project_id,
        "planka_list_ids": {name: f"{board}{i}" for i, name in enumerate(LISTS)},
    }


async def _project(c: AsyncClient, headers: dict = DELEGATED, **extra) -> dict:
    body = {"title": f"p-{uuid.uuid4().hex[:8]}", "brief": MIN_BRIEF, **extra}
    resp = await c.post("/pm/projects", json=await typed(c, body, headers), headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestLink:
    async def test_fr_links_the_board_and_n8n_finds_everything(self, test_client: AsyncClient):
        c = test_client
        fr_id = str(uuid.uuid4())
        p = await _project(
            c,
            origin_kind="project",
            origin_project_id=fr_id,
            wip_limit=2,
            repositories=[
                {"url": "https://github.com/acme/other", "primary": False},
                {
                    "url": "https://github.com/acme/main",
                    "branch": "conveyor-proof",
                    "primary": True,
                    # A pinned primary repo (fr's `repo.primary.pin` → resolved 40-hex sha):
                    # the conveyor imports the workspace at THIS commit, not at the branch.
                    "sha": SHA,
                },
            ],
        )
        link = _link()
        resp = await c.put(f"/pm/projects/{p['id']}/planka-board", json=link, headers=DELEGATED)
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["changed"] is True
        assert out["planka_linked_at"] is not None

        for headers in (DELEGATED, CONVEYOR):
            got = await c.get(
                f"/pm/projects/by-planka-board/{link['planka_board_id']}", headers=headers
            )
            assert got.status_code == 200, got.text
            g = got.json()
            assert g["project_id"] == p["id"]
            assert g["tenant_id"] == CUSTOMER_TENANT
            assert g["fr_project_id"] == fr_id
            assert g["title"] == p["title"]
            assert g["status"] == p["status"]
            assert g["project_type_id"] == p["project_type_id"]
            assert g["project_type"] == "test_setup_only"
            assert g["current_phase"] == "setup"
            assert g["current_phase_id"] == p["current_phase_id"]
            assert (g["wip_limit"], g["effective_wip_limit"], g["wip_limit_source"]) == (
                2,
                2,
                "project",
            )
            assert g["repository_url"] == "https://github.com/acme/main", "the primary one"
            assert g["repository_branch"] == "conveyor-proof", "the primary one's branch"
            assert g["repository_sha"] == SHA, "the primary one's pin — read FIRST by the flows"
            assert [(r["url"], r.get("branch"), r.get("sha")) for r in g["repositories"]] == [
                ("https://github.com/acme/other", None, None),
                ("https://github.com/acme/main", "conveyor-proof", SHA),
            ]
            assert g["planka_board_id"] == link["planka_board_id"]
            assert g["planka_project_id"] == "1700"
            assert g["planka_list_ids"] == link["planka_list_ids"]
            assert g["lists_by_id"] == {v: k for k, v in link["planka_list_ids"].items()}
            assert "changed" not in g

        # The project itself carries the link too.
        proj = (await c.get(f"/pm/projects/{p['id']}", headers=DELEGATED)).json()
        assert proj["planka_board_id"] == link["planka_board_id"]
        assert proj["planka_project_id"] == "1700"
        assert proj["planka_list_ids"] == link["planka_list_ids"]
        assert proj["planka_linked_at"] == out["planka_linked_at"]

    async def test_an_unpinned_project_reports_no_sha(self, test_client: AsyncClient):
        """``repository_sha`` null is the flows' signal to take ``repository_branch``."""
        c = test_client
        p = await _project(
            c,
            repositories=[{"url": "https://github.com/acme/x", "branch": "main", "primary": True}],
        )
        link = _link()
        await c.put(f"/pm/projects/{p['id']}/planka-board", json=link, headers=DELEGATED)
        got = await c.get(
            f"/pm/projects/by-planka-board/{link['planka_board_id']}", headers=CONVEYOR
        )
        assert got.status_code == 200, got.text
        g = got.json()
        assert g["repository_branch"] == "main"
        assert g["repository_sha"] is None

    async def test_resending_the_same_link_is_a_no_op(self, test_client: AsyncClient):
        c = test_client
        p = await _project(c)
        link = _link()
        first = await c.put(f"/pm/projects/{p['id']}/planka-board", json=link, headers=DELEGATED)
        assert first.status_code == 200, first.text
        again = await c.put(f"/pm/projects/{p['id']}/planka-board", json=link, headers=DELEGATED)
        assert again.status_code == 200, again.text
        assert again.json()["changed"] is False
        assert again.json()["planka_linked_at"] == first.json()["planka_linked_at"]

    async def test_fr_cannot_relink_but_pm_can(self, test_client: AsyncClient):
        c = test_client
        p = await _project(c)
        first = _link()
        assert (
            await c.put(f"/pm/projects/{p['id']}/planka-board", json=first, headers=DELEGATED)
        ).status_code == 200
        for other in (_link(), {**first, "planka_project_id": "9999"}):
            resp = await c.put(
                f"/pm/projects/{p['id']}/planka-board", json=other, headers=DELEGATED
            )
            assert resp.status_code == 403, resp.text
            assert _code(resp) == PM_ONLY
        got = (await c.get(f"/pm/projects/{p['id']}", headers=DELEGATED)).json()
        assert got["planka_board_id"] == first["planka_board_id"], "nothing changed"

        second = _link()
        resp = await c.put(f"/pm/projects/{p['id']}/planka-board", json=second, headers=CONVEYOR)
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True
        assert resp.json()["planka_board_id"] == second["planka_board_id"]
        old = await c.get(
            f"/pm/projects/by-planka-board/{first['planka_board_id']}", headers=CONVEYOR
        )
        assert old.status_code == 404, "the old board no longer resolves"

    async def test_planka_project_id_is_optional(self, test_client: AsyncClient):
        c = test_client
        p = await _project(c)
        link = _link(planka_project_id=None)
        resp = await c.put(f"/pm/projects/{p['id']}/planka-board", json=link, headers=DELEGATED)
        assert resp.status_code == 200, resp.text
        assert resp.json()["planka_project_id"] is None

    async def test_a_board_links_to_one_project_per_tenant(self, test_client: AsyncClient):
        """Within one tenant a board belongs to one project: the second link is 409."""
        c = test_client
        a, b = await _project(c), await _project(c)
        link = _link()
        assert (
            await c.put(f"/pm/projects/{a['id']}/planka-board", json=link, headers=DELEGATED)
        ).status_code == 200
        resp = await c.put(f"/pm/projects/{b['id']}/planka-board", json=link, headers=DELEGATED)
        assert resp.status_code == 409, resp.text
        assert _code(resp) == "planka_board_in_use"
        got = (await c.get(f"/pm/projects/{b['id']}", headers=DELEGATED)).json()
        assert got["planka_board_id"] is None

    async def test_the_same_board_id_links_independently_in_another_tenant(
        self, test_client: AsyncClient
    ):
        """Planka is per tenant (BYO instance), so board ids are unique per tenant only.

        Tenant B linking the board id tenant A already linked must succeed (a global UNIQUE
        would let A lock B out with a 409 and leak that the id is linked somewhere), and each
        tenant's lookup returns its own project.
        """
        c = test_client
        mine = await _project(c)
        theirs = await _project(c, headers=LICENCE)
        assert theirs["tenant_id"] == CONVEYOR_TENANT
        assert mine["tenant_id"] == CUSTOMER_TENANT
        link = _link()
        resp = await c.put(f"/pm/projects/{theirs['id']}/planka-board", json=link, headers=LICENCE)
        assert resp.status_code == 200, resp.text
        resp = await c.put(f"/pm/projects/{mine['id']}/planka-board", json=link, headers=DELEGATED)
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True

        board = link["planka_board_id"]
        for headers, project in ((DELEGATED, mine), (CONVEYOR, mine), (LICENCE, theirs)):
            got = await c.get(f"/pm/projects/by-planka-board/{board}", headers=headers)
            assert got.status_code == 200, got.text
            assert got.json()["project_id"] == project["id"]
            assert got.json()["tenant_id"] == project["tenant_id"]

    async def test_another_tenants_board_is_invisible(self, test_client: AsyncClient):
        c = test_client
        theirs = await _project(c, headers=LICENCE)
        link = _link()
        assert (
            await c.put(f"/pm/projects/{theirs['id']}/planka-board", json=link, headers=LICENCE)
        ).status_code == 200
        got = await c.get(
            f"/pm/projects/by-planka-board/{link['planka_board_id']}", headers=DELEGATED
        )
        assert got.status_code == 404, got.text
        assert _code(got) == "planka_board_not_linked"

    async def test_another_tenants_project_is_404(self, test_client: AsyncClient):
        c = test_client
        theirs = await _project(c, headers=LICENCE)
        resp = await c.put(
            f"/pm/projects/{theirs['id']}/planka-board", json=_link(), headers=DELEGATED
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda b: b["planka_list_ids"].pop("BLOCKED"), id="missing-list"),
            pytest.param(lambda b: b["planka_list_ids"].update(DROPPED="1"), id="extra-list"),
            pytest.param(
                lambda b: b["planka_list_ids"].update(READY=b["planka_list_ids"]["TODO"]),
                id="duplicate-list-id",
            ),
            pytest.param(lambda b: b.update(planka_board_id="12 34"), id="bad-board-id"),
            pytest.param(lambda b: b.update(planka_board_id=""), id="empty-board-id"),
            pytest.param(lambda b: b.pop("planka_list_ids"), id="no-lists"),
            pytest.param(lambda b: b.update(board_name="x"), id="unknown-field"),
        ],
    )
    async def test_invalid_links_are_422(self, test_client: AsyncClient, mutate):
        c = test_client
        p = await _project(c)
        body = _link()
        mutate(body)
        resp = await c.put(f"/pm/projects/{p['id']}/planka-board", json=body, headers=DELEGATED)
        assert resp.status_code == 422, resp.text
        got = (await c.get(f"/pm/projects/{p['id']}", headers=DELEGATED)).json()
        assert got["planka_board_id"] is None


class TestLookup:
    async def test_an_unlinked_board_is_404(self, test_client: AsyncClient):
        resp = await test_client.get(f"/pm/projects/by-planka-board/{_board()}", headers=CONVEYOR)
        assert resp.status_code == 404, resp.text
        assert _code(resp) == "planka_board_not_linked"

    async def test_a_new_project_has_no_board(self, test_client: AsyncClient):
        p = await _project(test_client)
        assert p["planka_board_id"] is None
        assert p["planka_list_ids"] is None
        assert p["planka_linked_at"] is None
