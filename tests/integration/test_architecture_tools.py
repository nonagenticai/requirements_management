"""Integration coverage for the /architecture/tools endpoints.

These are among the 30 routes (of 195 declared) that no test in this repo had
ever called. One of them — ``traceability-matrix`` — OOM-killed the production
pod when it was finally exercised. See
``091f684:docs/incidents/2026-09-04-the-traceability-matrix-that-oom-killed-the-pod.md``.

⛔ A skip here is a defect, not a convenience: ``tests/integration/`` bans
``pytest.skip`` outright (AST walk, ``MAX_ESCAPES = 0``) and rewrites any skip
into a failure. Assert on the status and body instead.
"""

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

TOOLS = "/architecture/tools"


def _uid():
    return uuid4().hex[:8]


@pytest.fixture
def traceable_project(req_client):
    """A small project: 3 application elements chained to 1 technology element.

    Yields ``(project_id, element_ids)`` and deletes everything afterwards.
    """
    project_id = f"itest-trace-{_uid()}"
    ids = []
    for i, (layer, etype) in enumerate(
        [
            ("application", "application_component"),
            ("application", "application_service"),
            ("application", "application_function"),
            ("technology", "node"),
        ]
    ):
        r = req_client.post(
            "/architecture/elements",
            json={
                "title": f"trace-{i}-{_uid()}",
                "layer": layer,
                "element_type": etype,
                "project_id": project_id,
            },
        )
        assert r.status_code in (200, 201), r.text
        ids.append(r.json()["id"])

    rel_ids = []
    for src, tgt in [(0, 1), (1, 2), (2, 3)]:
        r = req_client.post(
            "/architecture/relationships",
            json={
                "source_entity_type": "architecture_elements",
                "source_id": ids[src],
                "target_entity_type": "architecture_elements",
                "target_id": ids[tgt],
                "relationship_type": "association",
                "project_id": project_id,
            },
        )
        assert r.status_code in (200, 201), r.text
        rel_ids.append(r.json()["id"])

    yield project_id, ids

    for rid in rel_ids:
        req_client.delete(f"/architecture/relationships/{rid}")
    for eid in ids:
        req_client.delete(f"/architecture/elements/{eid}")


class TestTraceabilityMatrix:
    def test_returns_bounded_shape_with_truncation_metadata(
        self, req_client, traceable_project
    ) -> None:
        """The route must answer, and must declare how much it left out."""
        project_id, _ = traceable_project
        r = req_client.post(
            f"{TOOLS}/traceability-matrix",
            json={"project_id": project_id, "root_layer": "application"},
        )
        assert r.status_code == 200, r.text
        body = r.json()

        for key in ("rows", "coverage", "roots_total", "roots_returned", "truncated"):
            assert key in body, f"{key} missing from {sorted(body)}"
        assert body["roots_returned"] == len(body["rows"])
        assert body["roots_returned"] <= body["roots_total"]

    def test_a_root_traces_to_the_technology_element_it_is_chained_to(
        self, req_client, traceable_project
    ) -> None:
        """Asserts the VALUES we created, not just the shape.

        A shape-only assertion would pass against an empty result; this one
        names the element the chain must reach.
        """
        project_id, ids = traceable_project
        r = req_client.post(
            f"{TOOLS}/traceability-matrix",
            json={"project_id": project_id, "root_layer": "application"},
        )
        assert r.status_code == 200, r.text
        rows = r.json()["rows"]

        row = next((x for x in rows if x["root_element"]["id"] == ids[0]), None)
        assert row is not None, (
            f"root {ids[0]} absent from {[x['root_element']['id'] for x in rows]}"
        )
        traced_tech = [e["id"] for e in row["traced_elements"].get("technology", [])]
        assert ids[3] in traced_tech

    def test_unknown_root_layer_yields_no_rows_not_an_error(
        self, req_client, traceable_project
    ) -> None:
        """No element sits on 'strategy', so there is nothing to root on."""
        project_id, _ = traceable_project
        r = req_client.post(
            f"{TOOLS}/traceability-matrix",
            json={"project_id": project_id, "root_layer": "strategy"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["rows"] == []
        assert r.json()["roots_total"] == 0


class TestValidateRelationship:
    """Correct in BOTH directions — a validator that only ever says 'valid' is
    indistinguishable from one that is not running."""

    def test_a_legal_edge_is_accepted(self, req_client) -> None:
        r = req_client.post(
            f"{TOOLS}/validate-relationship",
            json={
                "source_layer": "application",
                "source_element_type": "application_component",
                "target_layer": "application",
                "target_element_type": "application_service",
                "relationship_type": "serving",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["valid"] is True
        assert r.json()["violations"] == []

    def test_specialization_across_differing_types_is_rejected(self, req_client) -> None:
        r = req_client.post(
            f"{TOOLS}/validate-relationship",
            json={
                "source_layer": "application",
                "source_element_type": "application_component",
                "target_layer": "business",
                "target_element_type": "business_actor",
                "relationship_type": "specialization",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["valid"] is False
        assert any("specialization is not permitted" in v for v in body["violations"]), body[
            "violations"
        ]

    def test_an_unknown_relationship_type_is_named_in_the_violation(self, req_client) -> None:
        r = req_client.post(
            f"{TOOLS}/validate-relationship",
            json={
                "source_layer": "application",
                "source_element_type": "application_component",
                "target_layer": "application",
                "target_element_type": "application_service",
                "relationship_type": "not_a_real_relationship",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["valid"] is False
        assert any("not_a_real_relationship" in v for v in body["violations"])
