"""Integration tests for requirement quality and priority endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestQuality:
    def test_quality_check(self, req_client, requirement_data):
        r = req_client.post(f"/requirements/{requirement_data['id']}/quality")
        # Quality scoring is pure in-process heuristics (src/quality.py) with no
        # model load and no external call, so a 5xx here is a real defect. The
        # tuple this replaced accepted 500/502/503 as success.
        assert r.status_code == 200, r.text

    def test_quality_check_returns_scores(self, req_client, requirement_data):
        r = req_client.post(f"/requirements/{requirement_data['id']}/quality")
        assert r.status_code == 200, f"quality -> {r.status_code}: {r.text}"
        data = r.json()
        # `assert isinstance(data, dict) and len(data) > 0` — which this replaced —
        # is always true for a fixed response_model, so it passed even when an
        # adversarial review made the endpoint return an all-empty QualityReport.
        # Assert the actual contract instead (src/schemas/requirements.py::QualityReport).
        assert set(data) >= {"score", "issues", "passes"}, f"quality shape: {data!r}"
        assert isinstance(data["score"], int | float), f"score not numeric: {data!r}"
        assert 0.0 <= data["score"] <= 1.0, f"score out of range: {data!r}"
        # The fixture's requirement has a title, description, effort and acceptance
        # criteria, so a scorer that actually ran cannot report a flat zero.
        assert data["score"] > 0.0 or data["issues"], (
            f"score 0.0 with no issues listed means nothing was scored: {data!r}"
        )

    def test_quality_nonexistent_404(self, req_client):
        r = req_client.post("/requirements/00000000-0000-7000-0000-000000000000/quality")
        assert r.status_code in (404, 422), r.text


class TestPriority:
    def test_priority_calculation(self, req_client, requirement_data):
        r = req_client.post(
            f"/requirements/{requirement_data['id']}/priority",
            params={"business_value": 8, "time_criticality": 5, "risk_reduction": 3},
        )
        # WSJF/RICE is arithmetic (src/prio.py). Same reasoning as above: the
        # tuple this replaced accepted a calculation error as a pass.
        assert r.status_code == 200, r.text

    def test_priority_returns_scores(self, req_client, requirement_data):
        r = req_client.post(
            f"/requirements/{requirement_data['id']}/priority",
            params={"business_value": 8, "time_criticality": 5, "risk_reduction": 3},
        )
        assert r.status_code == 200, f"priority -> {r.status_code}: {r.text}"
        data = r.json()
        # Same reasoning as the quality test above: `isinstance(data, dict)` plus
        # `len(data) > 0` is always true for a fixed response_model, and passed when
        # an adversarial review mutated the endpoint to return wsjf=None, rice=None.
        # The fixture sets effort=5.0 and the request supplies all three WSJF
        # components, so both scores are computable (src/prio.py).
        assert set(data) >= {"wsjf", "rice"}, f"priority shape: {data!r}"
        assert data["wsjf"] is not None, f"wsjf not computed: {data!r}"
        assert data["rice"] is not None, f"rice not computed: {data!r}"
        assert data["wsjf"] > 0, f"wsjf should be positive for these inputs: {data!r}"
