"""Integration tests for requirements CRUD endpoints."""

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def _uid():
    return uuid4().hex[:8]


class TestRequirementCreate:
    def test_create_requirement(self, req_client):
        payload = {
            "title": f"Create Test {_uid()}",
            "description": "Integration test create",
            "effort": 3.0,
            "links": {"business_objectives": ["obj-1"]},
        }
        r = req_client.post("/requirements", json=payload)
        assert r.status_code in (200, 201), r.text
        data = r.json()
        assert "id" in data
        assert data["title"] == payload["title"]
        # cleanup
        req_client.delete(f"/requirements/{data['id']}")

    def test_create_missing_title_422(self, req_client):
        r = req_client.post(
            "/requirements",
            json={
                "description": "no title",
                "effort": 1.0,
                "links": {"business_objectives": ["x"]},
            },
        )
        assert r.status_code == 422

    def test_create_missing_effort_422(self, req_client):
        r = req_client.post(
            "/requirements",
            json={
                "title": "no effort",
                "description": "test",
                "links": {"business_objectives": ["x"]},
            },
        )
        assert r.status_code == 422

    def test_create_empty_links_accepted(self, req_client):
        payload = {
            "title": f"empty links {_uid()}",
            "description": "test",
            "effort": 1.0,
            "links": {},
        }
        r = req_client.post("/requirements", json=payload)
        # API accepts empty links (defaults to {})
        assert r.status_code in (200, 201, 422), r.text
        if r.status_code in (200, 201):
            req_client.delete(f"/requirements/{r.json()['id']}")


class TestRequirementRead:
    def test_get_requirement(self, req_client, requirement_data):
        r = req_client.get(f"/requirements/{requirement_data['id']}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["id"] == requirement_data["id"]
        assert data["title"] == requirement_data["title"]

    def test_get_nonexistent_404(self, req_client):
        r = req_client.get("/requirements/00000000-0000-7000-0000-000000000000")
        assert r.status_code in (404, 422), r.text

    def test_list_requirements(self, req_client, requirement_data):
        r = req_client.get("/requirements")
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)
        assert any(r["id"] == requirement_data["id"] for r in data)


class TestRequirementListFilters:
    """``project_id`` and ``origin_kind`` must narrow, not be silently dropped.

    ⛔ Every test in this class FAILS on the code that preceded it. ``GET
    /requirements`` declared only ``limit`` and ``offset``, and FastAPI discards
    an undeclared query param without a word — so fr's AG-UI list action sent
    ``project_id`` and received the entire tenant with a 200, believing it was
    project-scoped. A 200 carrying the wrong rows is the worst shape this can
    take: there is nothing for the caller to notice.
    """

    def _seed(self, req_client):
        """Two rows differing in BOTH project_id and origin_kind. Returns (a, b)."""
        marker = _uid()
        a = req_client.post(
            "/requirements",
            json={
                "title": f"filter alpha {marker}",
                "description": "tenant origin, project alpha",
                "effort": 1.0,
                "project_id": f"alpha-{marker}",
            },
        )
        assert a.status_code in (200, 201), f"setup: {a.status_code}: {a.text}"
        b = req_client.post(
            "/requirements",
            json={
                "title": f"filter beta {marker}",
                "description": "project origin, project beta",
                "effort": 2.0,
                "project_id": f"beta-{marker}",
                "origin_kind": "project",
                "origin_project_id": "0195f48b-d000-7000-8000-0000000000aa",
            },
        )
        assert b.status_code in (200, 201), f"setup: {b.status_code}: {b.text}"
        return a.json(), b.json()

    def test_project_id_filter_narrows(self, req_client):
        a, b = self._seed(req_client)
        r = req_client.get("/requirements", params={"project_id": a["project_id"]})
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert a["id"] in ids, f"the project-alpha row is missing: {ids}"
        assert b["id"] not in ids, (
            "the project-beta row came back through a project-alpha filter — the "
            "param is being accepted and dropped"
        )

    def test_origin_kind_filter_narrows(self, req_client):
        a, b = self._seed(req_client)
        r = req_client.get("/requirements", params={"origin_kind": "project"})
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert b["id"] in ids, f"the project-origin row is missing: {ids}"
        assert a["id"] not in ids, "a tenant-origin row came back through origin_kind=project"

    def test_no_filter_returns_both(self, req_client):
        """The control. Without it, a filter that returns nothing at all would
        satisfy every narrowing assertion above."""
        a, b = self._seed(req_client)
        r = req_client.get("/requirements")
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert a["id"] in ids and b["id"] in ids, f"unfiltered list is missing rows: {ids}"

    def test_unmatched_project_id_returns_nothing_not_everything(self, req_client):
        a, b = self._seed(req_client)
        r = req_client.get("/requirements", params={"project_id": f"no-such-{_uid()}"})
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert a["id"] not in ids and b["id"] not in ids, (
            f"an unmatched project filter returned rows anyway: {ids}"
        )

    def test_out_of_domain_origin_kind_is_rejected(self, req_client):
        """``origin_kind`` is closed by the DB CHECK ``ck_requirements_origin_kind``;
        a value outside it can never match, so 422 beats a 200 that means nothing."""
        r = req_client.get("/requirements", params={"origin_kind": "nonsense"})
        assert r.status_code == 422, f"{r.status_code}: {r.text}"


class TestRequirementUpdate:
    def test_update_requirement(self, req_client, requirement_data):
        updated = {
            "title": f"Updated {_uid()}",
            "description": requirement_data.get("description", "updated"),
            "effort": 10.0,
            "links": requirement_data.get("links", {"business_objectives": ["x"]}),
        }
        r = req_client.put(f"/requirements/{requirement_data['id']}", json=updated)
        assert r.status_code == 200, r.text


class TestRequirementDelete:
    def test_delete_requirement(self, req_client):
        # Create then delete
        payload = {
            "title": f"Delete Me {_uid()}",
            "description": "To be deleted",
            "effort": 1.0,
            "links": {"business_objectives": ["del"]},
        }
        cr = req_client.post("/requirements", json=payload)
        assert cr.status_code in (200, 201), f"setup: create failed -> {cr.status_code}: {cr.text}"
        rid = cr.json()["id"]

        r = req_client.delete(f"/requirements/{rid}")
        assert r.status_code in (200, 204), r.text

    def test_get_after_delete(self, req_client):
        payload = {
            "title": f"Vanish {_uid()}",
            "description": "Should disappear",
            "effort": 1.0,
            "links": {"business_objectives": ["gone"]},
        }
        cr = req_client.post("/requirements", json=payload)
        assert cr.status_code in (200, 201), f"setup: create failed -> {cr.status_code}: {cr.text}"
        rid = cr.json()["id"]
        dr = req_client.delete(f"/requirements/{rid}")
        # ⛔ This used to read `if dr.status_code in (200, 204):`, which made the
        # whole test vacuous: an adversarial review replaced the DELETE handler
        # body with `raise RuntimeError` and this test still PASSED, because the
        # 500 simply failed the `if` and skipped the only assertion. That is the
        # same masking as pytest.skip, spelled as an `if`, and no AST guard that
        # looks for skips can see it. Assert the delete, THEN assert the 404.
        assert dr.status_code in (200, 204), f"delete -> {dr.status_code}: {dr.text}"
        r = req_client.get(f"/requirements/{rid}")
        assert r.status_code == 404, r.text
