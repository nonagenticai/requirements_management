"""Integration tests for decisions CRUD endpoints."""

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def _uid():
    return uuid4().hex[:8]


class TestDecisionCreate:
    def test_create_decision(self, req_client):
        payload = {
            "title": f"Create Decision {_uid()}",
            "context": "Testing context",
            "decision": "Use integration tests",
            "consequences": "Better coverage",
            "alternatives": ["Skip testing"],
            "status": "proposed",
        }
        r = req_client.post("/decisions", json=payload)
        assert r.status_code in (200, 201), r.text
        data = r.json()
        assert "id" in data
        assert data["title"] == payload["title"]
        # cleanup
        req_client.delete(f"/decisions/{data['id']}")

    def test_create_missing_title_422(self, req_client):
        r = req_client.post(
            "/decisions",
            json={"context": "no title", "decision": "test", "consequences": "test"},
        )
        assert r.status_code == 422


class TestDecisionRead:
    def test_get_decision(self, req_client, decision_data):
        r = req_client.get(f"/decisions/{decision_data['id']}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["id"] == decision_data["id"]
        assert data["title"] == decision_data["title"]

    def test_get_nonexistent_404(self, req_client):
        r = req_client.get("/decisions/00000000-0000-7000-0000-000000000000")
        assert r.status_code in (404, 422), r.text

    def test_list_decisions(self, req_client, decision_data):
        r = req_client.get("/decisions")
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)
        assert any(d["id"] == decision_data["id"] for d in data)

    def test_list_with_status_filter(self, req_client, decision_data):
        r = req_client.get("/decisions", params={"status": "proposed"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)


class TestDecisionListFilters:
    """``origin_kind`` must narrow. ``project_id`` already did and still must.

    ⛔ The ``origin_kind`` tests FAIL on the code that preceded them: the param
    was undeclared, FastAPI dropped it silently, and the caller got the whole
    tenant back with a 200.
    """

    def _seed(self, req_client):
        marker = _uid()
        a = req_client.post(
            "/decisions",
            json={
                "title": f"filter alpha {marker}",
                "context": "c",
                "decision": "d",
                "status": "proposed",
                "project_id": f"alpha-{marker}",
            },
        )
        assert a.status_code in (200, 201), f"setup: {a.status_code}: {a.text}"
        b = req_client.post(
            "/decisions",
            json={
                "title": f"filter beta {marker}",
                "context": "c",
                "decision": "d",
                "status": "accepted",
                "project_id": f"beta-{marker}",
                "origin_kind": "project",
                "origin_project_id": "0195f48b-d000-7000-8000-0000000000aa",
            },
        )
        assert b.status_code in (200, 201), f"setup: {b.status_code}: {b.text}"
        return a.json(), b.json()

    def test_origin_kind_filter_narrows(self, req_client):
        a, b = self._seed(req_client)
        r = req_client.get("/decisions", params={"origin_kind": "project"})
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert b["id"] in ids, f"the project-origin decision is missing: {ids}"
        assert a["id"] not in ids, "a tenant-origin decision came back through origin_kind=project"

    def test_project_id_filter_still_narrows(self, req_client):
        """A regression guard on the one filter this route already honoured."""
        a, b = self._seed(req_client)
        r = req_client.get("/decisions", params={"project_id": a["project_id"]})
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert a["id"] in ids and b["id"] not in ids, f"project_id no longer narrows: {ids}"

    def test_no_filter_returns_both(self, req_client):
        """The control — a filter returning nothing would satisfy the tests above."""
        a, b = self._seed(req_client)
        r = req_client.get("/decisions")
        assert r.status_code == 200, r.text
        ids = [x["id"] for x in r.json()]
        assert a["id"] in ids and b["id"] in ids, f"unfiltered list is missing rows: {ids}"

    def test_out_of_domain_origin_kind_is_rejected(self, req_client):
        r = req_client.get("/decisions", params={"origin_kind": "nonsense"})
        assert r.status_code == 422, f"{r.status_code}: {r.text}"


class TestDecisionUpdate:
    def test_update_decision(self, req_client, decision_data):
        updated = {
            "title": f"Updated Decision {_uid()}",
            "context": decision_data.get("context", "updated"),
            "decision": decision_data.get("decision", "updated"),
            "consequences": decision_data.get("consequences", "updated"),
        }
        r = req_client.put(f"/decisions/{decision_data['id']}", json=updated)
        assert r.status_code == 200, r.text

    def test_update_verify_persisted(self, req_client, decision_data):
        new_title = f"Persisted Decision {_uid()}"
        updated = {
            "title": new_title,
            "context": decision_data.get("context", "check"),
            "decision": decision_data.get("decision", "check"),
            "consequences": decision_data.get("consequences", "check"),
        }
        r = req_client.put(f"/decisions/{decision_data['id']}", json=updated)
        assert r.status_code == 200, f"Update failed: {r.text}"
        r2 = req_client.get(f"/decisions/{decision_data['id']}")
        assert r2.status_code == 200, f"GET after update failed: {r2.text}"
        assert r2.json()["title"] == new_title


class TestDecisionDelete:
    def test_delete_decision(self, req_client):
        payload = {
            "title": f"Delete Decision {_uid()}",
            "context": "To be deleted",
            "decision": "Delete this",
            "consequences": "Gone",
        }
        cr = req_client.post("/decisions", json=payload)
        assert cr.status_code in (200, 201), f"setup: create failed -> {cr.status_code}: {cr.text}"
        did = cr.json()["id"]

        r = req_client.delete(f"/decisions/{did}")
        assert r.status_code in (200, 204), r.text

    def test_get_after_delete(self, req_client):
        payload = {
            "title": f"Vanish Decision {_uid()}",
            "context": "Should disappear",
            "decision": "Bye",
            "consequences": "None",
        }
        cr = req_client.post("/decisions", json=payload)
        assert cr.status_code in (200, 201), f"setup: create failed -> {cr.status_code}: {cr.text}"
        did = cr.json()["id"]
        dr = req_client.delete(f"/decisions/{did}")
        # ⛔ This used to read `if dr.status_code in (200, 204):`, which made the
        # whole test vacuous: an adversarial review replaced the DELETE handler
        # body with `raise RuntimeError` and this test still PASSED, because the
        # 500 simply failed the `if` and skipped the only assertion. That is the
        # same masking as pytest.skip, spelled as an `if`, and no AST guard that
        # looks for skips can see it. Assert the delete, THEN assert the 404.
        assert dr.status_code in (200, 204), f"delete -> {dr.status_code}: {dr.text}"
        r = req_client.get(f"/decisions/{did}")
        assert r.status_code == 404, r.text
