"""Integration tests for principles CRUD endpoints."""

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def _uid():
    return uuid4().hex[:8]


class TestPrincipleCreate:
    def test_create_principle(self, req_client):
        payload = {
            "title": f"Create Principle {_uid()}",
            "statement": "Principles should be tested",
            "rationale": "Quality assurance",
            "implications": ["Must write tests"],
            "tags": ["integration-test"],
        }
        r = req_client.post("/principles", json=payload)
        assert r.status_code in (200, 201), r.text
        data = r.json()
        assert "id" in data
        assert data["title"] == payload["title"]
        # cleanup
        req_client.delete(f"/principles/{data['id']}")

    def test_create_missing_title_422(self, req_client):
        r = req_client.post(
            "/principles",
            json={"statement": "no title", "rationale": "test"},
        )
        assert r.status_code == 422


class TestPrincipleRead:
    def test_get_principle(self, req_client, principle_data):
        r = req_client.get(f"/principles/{principle_data['id']}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["id"] == principle_data["id"]
        assert data["title"] == principle_data["title"]

    def test_get_nonexistent_404(self, req_client):
        r = req_client.get("/principles/00000000-0000-7000-0000-000000000000")
        assert r.status_code in (404, 422), r.text

    def test_list_principles(self, req_client, principle_data):
        r = req_client.get("/principles")
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)
        assert any(p["id"] == principle_data["id"] for p in data)

    def test_list_active_only(self, req_client, principle_data):
        r = req_client.get("/principles", params={"active_only": True})
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)


class TestPrincipleListFilters:
    """``origin_kind`` and ``project_id`` (provenance: ``origin_project_id``) must narrow.

    ⛔ The narrowing tests FAIL on the code that preceded them: ``origin_kind``
    was undeclared, FastAPI dropped it silently, and the caller got the whole
    tenant back with a 200.
    """

    def _seed(self, req_client, origin_project_id="0195f48b-d000-7000-8000-0000000000aa"):
        """One tenant-origin and one project-origin principle. Returns (a, b)."""
        marker = _uid()
        a = req_client.post(
            "/principles",
            json={
                "title": f"filter tenant-origin {marker}",
                "statement": "Tenant-wide guidance",
            },
        )
        assert a.status_code in (200, 201), f"setup: {a.status_code}: {a.text}"
        b = req_client.post(
            "/principles",
            json={
                "title": f"filter project-origin {marker}",
                "statement": "Authored by a project intake",
                "origin_kind": "project",
                "origin_project_id": origin_project_id,
            },
        )
        assert b.status_code in (200, 201), f"setup: {b.status_code}: {b.text}"
        return a.json(), b.json()

    def test_origin_kind_filter_narrows(self, req_client):
        a, b = self._seed(req_client)
        r = req_client.get("/principles", params={"origin_kind": "project"})
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert b["id"] in ids, f"the project-origin principle is missing: {ids}"
        assert a["id"] not in ids, "a tenant-origin principle came back through origin_kind=project"

    def test_no_filter_returns_both(self, req_client):
        """The control — a filter returning nothing would satisfy the test above."""
        a, b = self._seed(req_client)
        r = req_client.get("/principles")
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert a["id"] in ids and b["id"] in ids, f"unfiltered list is missing rows: {ids}"

    def test_out_of_domain_origin_kind_is_rejected(self, req_client):
        r = req_client.get("/principles", params={"origin_kind": "nonsense"})
        assert r.status_code == 422, f"{r.status_code}: {r.text}"

    def test_project_id_filters_on_origin_project_id(self, req_client):
        """``project_id`` is PROVENANCE: the principles that project MINTED
        (``origin_project_id``), and only those — not every principle that applies
        to the project. Decided 2026-10-12 at fr's request; the parameter's
        published description says so (``routers/principles.py::list_principles``)."""
        project = str(uuid4())
        a, b = self._seed(req_client, origin_project_id=project)
        r = req_client.get("/principles", params={"project_id": project})
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert ids == [b["id"]], f"expected only the principle minted by {project}: {ids}"
        assert all(x["origin_project_id"] == project for x in r.json())

        other = req_client.get("/principles", params={"project_id": str(uuid4())})
        assert other.status_code == 200 and other.json() == [], other.text

    def test_without_project_id_the_list_is_unfiltered(self, req_client):
        """The control: absent ``project_id``, minted and tenant-wide rows both come back."""
        a, b = self._seed(req_client, origin_project_id=str(uuid4()))
        r = req_client.get("/principles", params={"limit": 1000})
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert a["id"] in ids and b["id"] in ids, f"unfiltered list is missing rows: {ids}"

    def test_project_id_must_be_a_uuid(self, req_client):
        r = req_client.get("/principles", params={"project_id": "not-a-uuid"})
        assert r.status_code == 422, f"{r.status_code}: {r.text}"


class TestPrincipleUpdate:
    def test_update_principle(self, req_client, principle_data):
        updated = {
            "title": f"Updated Principle {_uid()}",
            "statement": principle_data.get("statement", "updated"),
            "rationale": principle_data.get("rationale", "updated"),
        }
        r = req_client.put(f"/principles/{principle_data['id']}", json=updated)
        assert r.status_code == 200, r.text

    def test_update_verify_persisted(self, req_client, principle_data):
        new_title = f"Persisted Principle {_uid()}"
        updated = {
            "title": new_title,
            "statement": principle_data.get("statement", "check"),
            "rationale": principle_data.get("rationale", "check"),
        }
        r = req_client.put(f"/principles/{principle_data['id']}", json=updated)
        assert r.status_code == 200, f"Update failed: {r.text}"
        r2 = req_client.get(f"/principles/{principle_data['id']}")
        assert r2.status_code == 200, f"GET after update failed: {r2.text}"
        assert r2.json()["title"] == new_title


class TestPrincipleDelete:
    def test_delete_principle(self, req_client):
        payload = {
            "title": f"Delete Principle {_uid()}",
            "statement": "To be deleted",
            "rationale": "Testing deletion",
        }
        cr = req_client.post("/principles", json=payload)
        assert cr.status_code in (200, 201), f"setup: create failed -> {cr.status_code}: {cr.text}"
        pid = cr.json()["id"]

        r = req_client.delete(f"/principles/{pid}")
        assert r.status_code in (200, 204), r.text

    def test_get_after_delete(self, req_client):
        payload = {
            "title": f"Vanish Principle {_uid()}",
            "statement": "Should disappear",
            "rationale": "Gone",
        }
        cr = req_client.post("/principles", json=payload)
        assert cr.status_code in (200, 201), f"setup: create failed -> {cr.status_code}: {cr.text}"
        pid = cr.json()["id"]
        dr = req_client.delete(f"/principles/{pid}")
        # ⛔ This used to read `if dr.status_code in (200, 204):`, which made the
        # whole test vacuous: an adversarial review replaced the DELETE handler
        # body with `raise RuntimeError` and this test still PASSED, because the
        # 500 simply failed the `if` and skipped the only assertion. That is the
        # same masking as pytest.skip, spelled as an `if`, and no AST guard that
        # looks for skips can see it. Assert the delete, THEN assert the 404.
        assert dr.status_code in (200, 204), f"delete -> {dr.status_code}: {dr.text}"
        r = req_client.get(f"/principles/{pid}")
        assert r.status_code == 404, r.text
