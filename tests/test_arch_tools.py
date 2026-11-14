"""Comprehensive unit tests for architecture tool pure functions.

Covers:
- ADM completeness checker (all phases A-H, empty/partial/complete)
- Capability heatmap builder (maturity, strategic importance, investment need)
- Standards compliance checker (mandatory/recommended, violations/warnings)
- View generator (viewpoint filtering, element/relationship matching)

No database required -- all functions are pure.
"""

from __future__ import annotations

from typing import cast

import pytest

from tools.architecture.adm_completeness import ADM_DELIVERABLES, check_adm_completeness
from tools.architecture.capability_heatmap import (
    build_capability_heatmap,
)
from tools.architecture.compliance import (
    check_compliance,
)
from tools.architecture.view_generator import VIEWPOINT_FILTERS, generate_view

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ID = 0


def _next_id() -> str:
    global _ID
    _ID += 1
    return f"00000000-0000-0000-0000-{_ID:012d}"


def _elem(element_type: str, layer: str = "", title: str = "", **extra) -> dict:
    return {
        "id": _next_id(),
        "title": title or element_type,
        "layer": layer,
        "element_type": element_type,
        **extra,
    }


def _rel(source_id: str, target_id: str, rel_type: str = "association") -> dict:
    return {
        "id": _next_id(),
        "source_id": source_id,
        "target_id": target_id,
        "relationship_type": rel_type,
    }


def _view(viewpoint: str) -> dict:
    return {"viewpoint": viewpoint}


# ===================================================================
# 1. ADM Completeness Checker
# ===================================================================


class TestADMCompletenessEmpty:
    """Each phase with completely empty inputs should yield score 0."""

    @pytest.mark.parametrize("phase", list(ADM_DELIVERABLES.keys()))
    def test_empty_inputs_score_zero(self, phase: str) -> None:
        result = check_adm_completeness(
            phase=phase,
            elements=[],
            relationships=[],
            views=[],
            standards=[],
            requirements_count=0,
            decisions_count=0,
            principles_count=0,
        )
        assert result["phase"] == phase
        assert result["completeness_score"] == 0.0
        assert result["passed"] == []
        assert len(result["missing"]) > 0
        assert len(result["recommendations"]) > 0

    def test_unknown_phase_raises_instead_of_scoring_zero(self) -> None:
        """Superseded 2026-09-04: this used to assert the 200-shaped success.

        It pinned the defect. ``completeness_score: 0.0`` for phase "Z" is
        indistinguishable from "this project genuinely has zero ADM
        completeness" for any caller that reads the score. ``phase`` is now
        ``Literal["A".."H"]`` at the request boundary and the tool raises for a
        phase it cannot route. Full coverage:
        ``tests/test_adm_phase_is_a_closed_vocabulary.py``.
        """
        with pytest.raises(ValueError, match="unknown ADM phase"):
            check_adm_completeness(
                phase="Z",
                elements=[],
                relationships=[],
                views=[],
                standards=[],
                requirements_count=0,
                decisions_count=0,
                principles_count=0,
            )

    def test_phase_case_insensitive(self) -> None:
        r_lower = check_adm_completeness("a", [], [], [], [], 0, 0, 0)
        r_upper = check_adm_completeness("A", [], [], [], [], 0, 0, 0)
        assert r_lower["phase"] == r_upper["phase"] == "A"

    def test_phase_strips_whitespace(self) -> None:
        r = check_adm_completeness("  b  ", [], [], [], [], 0, 0, 0)
        assert r["phase"] == "B"


class TestADMCompletenessPartial:
    """Phase A with partial data."""

    def test_phase_a_one_element_type(self) -> None:
        elements = [_elem("stakeholder", layer="motivation")]
        result = check_adm_completeness(
            phase="A",
            elements=elements,
            relationships=[],
            views=[],
            standards=[],
            requirements_count=0,
            decisions_count=0,
            principles_count=0,
        )
        # Phase A: 6 element types + 3 rel types + 2 viewpoints + 1 principles = 12 checks
        assert 0 < result["completeness_score"] < 1.0
        assert any("stakeholder" in p for p in result["passed"])
        # The other 5 element types should be missing
        missing_et = [m for m in result["missing"] if "element_type" in m]
        assert len(missing_et) == 5

    def test_phase_b_partial_with_decisions_and_requirements(self) -> None:
        elements = [
            _elem("business_process", layer="business"),
            _elem("business_function", layer="business"),
        ]
        rels = [_rel(elements[0]["id"], elements[1]["id"], "composition")]
        result = check_adm_completeness(
            phase="B",
            elements=elements,
            relationships=rels,
            views=[],
            standards=[],
            requirements_count=3,
            decisions_count=2,
            principles_count=0,
        )
        # 2 of 6 element types, 1 of 6 rel types, 0 of 2 viewpoints, decisions yes, requirements yes
        # = 5 / (6+6+2+1+1) = 5/16
        assert result["completeness_score"] == pytest.approx(5 / 16, abs=0.001)

    def test_standards_add_to_passed_but_not_score(self) -> None:
        """Standards are informational, they add to passed[] but don't affect the score denominator."""
        result = check_adm_completeness(
            phase="A",
            elements=[],
            relationships=[],
            views=[],
            standards=[{"id": "std1", "title": "Some Standard"}],
            requirements_count=0,
            decisions_count=0,
            principles_count=0,
        )
        assert result["completeness_score"] == 0.0
        assert any("standards defined" in p for p in result["passed"])


class TestADMCompletenessComplete:
    """Phase with all deliverables present should yield score 1.0."""

    def _build_complete_phase_a(self) -> dict:
        deliverables = ADM_DELIVERABLES["A"]
        element_types = cast(list[str], deliverables["element_types"])
        rel_types = cast(list[str], deliverables["relationship_types"])
        viewpoints = cast(list[str], deliverables["viewpoints"])
        elements = [_elem(et, layer="motivation") for et in element_types]
        rels = []
        for rt in rel_types:
            if len(elements) >= 2:
                rels.append(_rel(elements[0]["id"], elements[1]["id"], rt))
        views = [_view(vp) for vp in viewpoints]
        return {
            "elements": elements,
            "relationships": rels,
            "views": views,
            "standards": [],
            "requirements_count": 0,  # not required for A
            "decisions_count": 0,  # not required for A
            "principles_count": 5,  # required for A
        }

    def test_phase_a_complete(self) -> None:
        data = self._build_complete_phase_a()
        result = check_adm_completeness(phase="A", **data)
        assert result["completeness_score"] == 1.0
        assert result["missing"] == []
        assert len(result["passed"]) > 0

    @pytest.mark.parametrize("phase", list(ADM_DELIVERABLES.keys()))
    def test_all_phases_can_reach_100(self, phase: str) -> None:
        """Build complete input for any phase and verify score 1.0."""
        d = ADM_DELIVERABLES[phase]
        element_types = cast(list[str], d["element_types"])
        rel_types = cast(list[str], d["relationship_types"])
        viewpoints = cast(list[str], d["viewpoints"])
        elements = [_elem(et, layer="any") for et in element_types]
        rels = []
        for rt in rel_types:
            if len(elements) >= 2:
                rels.append(_rel(elements[0]["id"], elements[1]["id"], rt))
        views = [_view(vp) for vp in viewpoints]

        result = check_adm_completeness(
            phase=phase,
            elements=elements,
            relationships=rels,
            views=views,
            standards=[],
            requirements_count=10 if d.get("requires_requirements") else 0,
            decisions_count=10 if d.get("requires_decisions") else 0,
            principles_count=10 if d.get("requires_principles") else 0,
        )
        assert result["completeness_score"] == 1.0, (
            f"Phase {phase} did not reach 100%: missing={result['missing']}"
        )

    def test_output_structure(self) -> None:
        result = check_adm_completeness("A", [], [], [], [], 0, 0, 0)
        assert set(result.keys()) == {
            "phase",
            "completeness_score",
            "passed",
            "missing",
            "recommendations",
        }
        assert isinstance(result["completeness_score"], float)
        assert isinstance(result["passed"], list)
        assert isinstance(result["missing"], list)
        assert isinstance(result["recommendations"], list)


# ===================================================================
# 2. Capability Heatmap Builder
# ===================================================================


class TestCapabilityHeatmapEmpty:
    def test_empty_input(self) -> None:
        result = build_capability_heatmap([], [])
        assert result["capabilities"] == []
        assert result["averages"] == {
            "maturity": 0.0,
            "strategic_importance": 0.0,
            "investment_need": 0.0,
        }

    def test_no_capabilities_in_elements(self) -> None:
        elems = [_elem("business_process", layer="business")]
        result = build_capability_heatmap(elems, [])
        assert result["capabilities"] == []

    def test_single_capability_no_relationships(self) -> None:
        cap = _elem("capability", layer="strategy", title="Cap A")
        result = build_capability_heatmap([cap], [])
        assert len(result["capabilities"]) == 1
        scores = result["capabilities"][0]["scores"]
        assert scores["maturity"] == 0.0
        assert scores["strategic_importance"] == 0.0
        # investment_need = (5.0 - maturity) + gap_count = 5.0, capped at 5
        assert scores["investment_need"] == 5.0


class TestCapabilityHeatmapMaturity:
    def test_realization_links_increase_maturity(self) -> None:
        cap = _elem("capability", layer="strategy", title="Cap A")
        app1 = _elem("application_component", layer="application", title="App 1")
        app2 = _elem("application_service", layer="application", title="App 2")
        rels = [
            _rel(cap["id"], app1["id"], "realization"),
            _rel(cap["id"], app2["id"], "realization"),
        ]
        result = build_capability_heatmap([cap, app1, app2], rels)
        assert result["capabilities"][0]["scores"]["maturity"] == 2.0

    def test_maturity_capped_at_5(self) -> None:
        cap = _elem("capability", layer="strategy")
        elems = [cap]
        rels = []
        # Add 7 realization links to app components
        for i in range(7):
            app = _elem("application_component", layer="application", title=f"App {i}")
            elems.append(app)
            rels.append(_rel(cap["id"], app["id"], "realization"))
        result = build_capability_heatmap(elems, rels)
        assert result["capabilities"][0]["scores"]["maturity"] == 5.0

    def test_non_realization_link_ignored_for_maturity(self) -> None:
        cap = _elem("capability", layer="strategy")
        app = _elem("application_component", layer="application")
        rels = [_rel(cap["id"], app["id"], "association")]
        result = build_capability_heatmap([cap, app], rels)
        assert result["capabilities"][0]["scores"]["maturity"] == 0.0

    def test_realization_to_non_realization_type_ignored(self) -> None:
        """A realization link to a non-realization-layer type (e.g., goal) doesn't count."""
        cap = _elem("capability", layer="strategy")
        goal = _elem("goal", layer="motivation")
        rels = [_rel(cap["id"], goal["id"], "realization")]
        result = build_capability_heatmap([cap, goal], rels)
        assert result["capabilities"][0]["scores"]["maturity"] == 0.0


class TestCapabilityHeatmapStrategicImportance:
    def test_goal_links_increase_strategic_importance(self) -> None:
        cap = _elem("capability", layer="strategy")
        g1 = _elem("goal", layer="motivation")
        g2 = _elem("outcome", layer="motivation")
        rels = [
            _rel(cap["id"], g1["id"], "association"),
            _rel(cap["id"], g2["id"], "influence"),
        ]
        result = build_capability_heatmap([cap, g1, g2], rels)
        # Both goal and outcome are _GOAL_OUTCOME_TYPES, any rel_type counts
        assert result["capabilities"][0]["scores"]["strategic_importance"] == 2.0

    def test_strategic_importance_capped_at_5(self) -> None:
        cap = _elem("capability", layer="strategy")
        elems = [cap]
        rels = []
        for i in range(8):
            g = _elem("goal", layer="motivation", title=f"Goal {i}")
            elems.append(g)
            rels.append(_rel(cap["id"], g["id"], "association"))
        result = build_capability_heatmap(elems, rels)
        assert result["capabilities"][0]["scores"]["strategic_importance"] == 5.0


class TestCapabilityHeatmapInvestmentNeed:
    def test_investment_need_with_gaps(self) -> None:
        cap = _elem("capability", layer="strategy")
        gap = _elem("gap", layer="implementation_migration")
        rels = [_rel(cap["id"], gap["id"], "association")]
        result = build_capability_heatmap([cap, gap], rels)
        # maturity = 0, investment_need = min(5.0, (5.0 - 0) + 1) = 5.0 (capped)
        assert result["capabilities"][0]["scores"]["investment_need"] == 5.0

    def test_high_maturity_lowers_investment_need(self) -> None:
        cap = _elem("capability", layer="strategy")
        apps = []
        rels = []
        for i in range(4):
            app = _elem("application_component", layer="application", title=f"App {i}")
            apps.append(app)
            rels.append(_rel(cap["id"], app["id"], "realization"))
        result = build_capability_heatmap([cap, *apps], rels)
        scores = result["capabilities"][0]["scores"]
        assert scores["maturity"] == 4.0
        # investment_need = min(5.0, (5.0 - 4.0) + 0) = 1.0
        assert scores["investment_need"] == 1.0


class TestCapabilityHeatmapDimensions:
    def test_custom_dimensions(self) -> None:
        cap = _elem("capability", layer="strategy")
        result = build_capability_heatmap([cap], [], dimensions=["maturity"])
        scores = result["capabilities"][0]["scores"]
        assert "maturity" in scores
        assert "strategic_importance" not in scores
        assert "investment_need" not in scores

    def test_averages_computed_across_capabilities(self) -> None:
        cap1 = _elem("capability", layer="strategy", title="Cap 1")
        cap2 = _elem("capability", layer="strategy", title="Cap 2")
        app = _elem("application_component", layer="application")
        # Only cap1 has a realization link
        rels = [_rel(cap1["id"], app["id"], "realization")]
        result = build_capability_heatmap([cap1, cap2, app], rels)
        # cap1 maturity=1, cap2 maturity=0 => avg = 0.5
        assert result["averages"]["maturity"] == 0.5

    def test_element_summary_structure(self) -> None:
        cap = _elem("capability", layer="strategy", title="My Cap")
        result = build_capability_heatmap([cap], [])
        elem = result["capabilities"][0]["element"]
        assert set(elem.keys()) == {"id", "title", "layer", "element_type"}
        assert elem["title"] == "My Cap"
        assert elem["element_type"] == "capability"

    def test_bidirectional_neighbor_lookup(self) -> None:
        """Relationships are indexed bidirectionally: app -> cap realization counts."""
        cap = _elem("capability", layer="strategy")
        app = _elem("application_component", layer="application")
        # Relationship from app to cap (reverse direction)
        rels = [_rel(app["id"], cap["id"], "realization")]
        result = build_capability_heatmap([cap, app], rels)
        # Because neighbors are bidirectional, cap finds app as realization neighbor
        assert result["capabilities"][0]["scores"]["maturity"] == 1.0


# ===================================================================
# 3. Compliance Checker
# ===================================================================


class TestComplianceEmpty:
    def test_no_standards(self) -> None:
        result = check_compliance(
            elements=[_elem("application_component", layer="application")],
            standards=[],
            relationships=[],
        )
        # No standards => no scored checks => honest "no data" is None, not a
        # fabricated 1.0 pass.
        assert result["compliance_score"] is None
        assert result["violations"] == []
        assert result["warnings"] == []
        assert result["compliant"] == []

    def test_no_elements(self) -> None:
        std = {
            "id": _next_id(),
            "title": "Tech Std",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        result = check_compliance(elements=[], standards=[std], relationships=[])
        # No applicable elements => standard is skipped => no scored checks =>
        # honest "no data" is None, not a fabricated 1.0 pass.
        assert result["compliance_score"] is None

    def test_empty_everything(self) -> None:
        result = check_compliance([], [], [])
        # Nothing to score => honest "no data" is None, not a fabricated 1.0 pass.
        assert result["compliance_score"] is None


class TestComplianceMandatory:
    def test_mandatory_standard_compliant(self) -> None:
        std = {
            "id": _next_id(),
            "title": "Tech Standard",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        elem = _elem("node", layer="technology", title="Server")
        rel = _rel(elem["id"], std["id"], "realization")
        result = check_compliance([elem], [std], [rel])
        assert result["compliance_score"] == 1.0
        assert len(result["compliant"]) == 1
        assert result["violations"] == []

    def test_mandatory_standard_violation(self) -> None:
        std_id = _next_id()
        std = {
            "id": std_id,
            "title": "Tech Standard",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        elem = _elem("node", layer="technology", title="Unlinked Server")
        result = check_compliance([elem], [std], [])
        assert result["compliance_score"] == 0.0
        assert len(result["violations"]) == 1
        assert result["violations"][0]["standard"]["id"] == std_id
        unlinked = result["violations"][0]["unlinked_elements"]
        assert len(unlinked) == 1
        assert unlinked[0]["id"] == elem["id"]

    def test_partial_compliance(self) -> None:
        """Two elements, one linked, one not -> violation with correct counts."""
        std = {
            "id": _next_id(),
            "title": "Tech Standard",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        linked_elem = _elem("device", layer="technology", title="Linked Device")
        unlinked_elem = _elem("node", layer="technology", title="Unlinked Node")
        rel = _rel(linked_elem["id"], std["id"], "realization")
        result = check_compliance([linked_elem, unlinked_elem], [std], [rel])
        assert result["compliance_score"] == 0.0  # violation -> not compliant
        assert len(result["violations"]) == 1
        assert result["violations"][0]["applicable_count"] == 2
        assert result["violations"][0]["linked_count"] == 1


class TestComplianceRecommended:
    def test_recommended_unlinked_is_warning_not_violation(self) -> None:
        std = {
            "id": _next_id(),
            "title": "App Guideline",
            "standard_type": "application_standard",
            "compliance_level": "recommended",
            "status": "approved",
        }
        elem = _elem("application_component", layer="application")
        result = check_compliance([elem], [std], [])
        assert result["violations"] == []
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["standard"]["compliance_level"] == "recommended"


class TestComplianceOptional:
    def test_optional_unlinked_no_violation_no_warning(self) -> None:
        """Optional standards that are not linked don't produce violations or warnings."""
        std = {
            "id": _next_id(),
            "title": "Optional Guideline",
            "standard_type": "guideline",
            "compliance_level": "optional",
            "status": "approved",
        }
        elem = _elem("application_component", layer="application")
        result = check_compliance([elem], [std], [])
        assert result["violations"] == []
        assert result["warnings"] == []

    def test_compliant_optional_excluded_from_compliant_and_score(self) -> None:
        """A fully-linked optional standard must NOT inflate compliance_score.

        Optional standards carry no compliance obligation (see
        architecture_standards.applicable_standards_for_element, which already
        excludes ``optional``). A compliant optional standard was landing in
        ``compliant`` and inflating the denominator-relative score; it must now
        be excluded from scoring entirely and surfaced under ``optional``.
        """
        opt = {
            "id": _next_id(),
            "title": "Optional Guideline",
            "standard_type": "guideline",
            "compliance_level": "optional",
            "status": "approved",
        }
        mand = {
            "id": _next_id(),
            "title": "Mandatory Tech Std",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        # guideline applies to all elements; link it fully. mandatory is unlinked.
        elem = _elem("node", layer="technology", title="Server")
        rel = _rel(elem["id"], opt["id"], "realization")
        result = check_compliance([elem], [opt, mand], [rel])
        # Optional is fully linked but MUST NOT count toward the score.
        # Only the mandatory violation is scored: 0 compliant / 1 check = 0.0.
        assert result["compliance_score"] == 0.0
        assert result["compliant"] == []
        assert len(result["violations"]) == 1
        # The optional standard is surfaced separately, not dropped.
        opt_ids = [o["standard"]["id"] for o in result["optional"]]
        assert opt["id"] in opt_ids

    def test_optional_adherence_metric(self) -> None:
        """optional_adherence = fully-linked optional / applicable optional."""
        linked_opt = {
            "id": _next_id(),
            "title": "Linked Optional",
            "standard_type": "guideline",
            "compliance_level": "optional",
            "status": "approved",
        }
        unlinked_opt = {
            "id": _next_id(),
            "title": "Unlinked Optional",
            "standard_type": "guideline",
            "compliance_level": "optional",
            "status": "approved",
        }
        elem = _elem("node", layer="technology", title="Server")
        rel = _rel(elem["id"], linked_opt["id"], "realization")
        result = check_compliance([elem], [linked_opt, unlinked_opt], [rel])
        # 1 of 2 applicable optional standards fully adhered.
        assert result["optional_adherence"] == pytest.approx(0.5, abs=0.001)
        assert len(result["optional"]) == 2

    def test_optional_adherence_none_when_no_applicable_optional(self) -> None:
        """No applicable optional standards => optional_adherence is honest None."""
        mand = {
            "id": _next_id(),
            "title": "Mandatory Tech Std",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        elem = _elem("node", layer="technology")
        rel = _rel(elem["id"], mand["id"], "realization")
        result = check_compliance([elem], [mand], [rel])
        assert result["optional_adherence"] is None
        assert result["optional"] == []
        # Sanity: mandatory scoring unaffected.
        assert result["compliance_score"] == 1.0


class TestComplianceStatusFilter:
    def test_retired_standard_skipped(self) -> None:
        std = {
            "id": _next_id(),
            "title": "Retired Standard",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "retired",
        }
        elem = _elem("node", layer="technology")
        result = check_compliance([elem], [std], [])
        # Retired standards are skipped entirely => no scored checks => honest
        # "no data" is None, not a fabricated 1.0 pass.
        assert result["violations"] == []
        assert result["compliance_score"] is None

    def test_hold_standard_skipped(self) -> None:
        std = {
            "id": _next_id(),
            "title": "Hold Standard",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "hold",
        }
        elem = _elem("node", layer="technology")
        result = check_compliance([elem], [std], [])
        # Hold standards are skipped => no scored checks => honest "no data" is
        # None, not a fabricated 1.0 pass.
        assert result["violations"] == []
        assert result["compliance_score"] is None


class TestComplianceLayerFiltering:
    def test_technology_standard_only_checks_tech_layer(self) -> None:
        std = {
            "id": _next_id(),
            "title": "Tech Standard",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        # Business layer element should not be applicable to tech standard
        biz_elem = _elem("business_process", layer="business")
        result = check_compliance([biz_elem], [std], [])
        # No applicable elements -> standard is skipped
        assert result["violations"] == []

    def test_data_standard_checks_data_element_types(self) -> None:
        std = {
            "id": _next_id(),
            "title": "Data Standard",
            "standard_type": "data_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        data_elem = _elem("data_object", layer="application")
        non_data_elem = _elem("application_component", layer="application")
        result = check_compliance([data_elem, non_data_elem], [std], [])
        # Only data_object is applicable, not application_component
        assert len(result["violations"]) == 1
        assert result["violations"][0]["applicable_count"] == 1

    def test_guideline_applies_to_all_elements(self) -> None:
        std = {
            "id": _next_id(),
            "title": "General Guideline",
            "standard_type": "guideline",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        e1 = _elem("node", layer="technology")
        e2 = _elem("business_process", layer="business")
        result = check_compliance([e1, e2], [std], [])
        assert result["violations"][0]["applicable_count"] == 2


class TestComplianceUncoveredElements:
    def test_uncovered_elements_listed(self) -> None:
        std = {
            "id": _next_id(),
            "title": "Tech Standard",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        linked_elem = _elem("node", layer="technology")
        unlinked_elem = _elem("device", layer="technology")
        rel = _rel(linked_elem["id"], std["id"], "realization")
        result = check_compliance([linked_elem, unlinked_elem], [std], [rel])
        uncovered_ids = [e["id"] for e in result["uncovered_elements"]]
        assert unlinked_elem["id"] in uncovered_ids
        assert linked_elem["id"] not in uncovered_ids

    def test_linking_relationship_types(self) -> None:
        """Only realization, association, traceability count as linking."""
        std = {
            "id": _next_id(),
            "title": "Tech Standard",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        elem = _elem("node", layer="technology")
        # A 'serving' relationship should NOT count
        rel = _rel(elem["id"], std["id"], "serving")
        result = check_compliance([elem], [std], [rel])
        assert len(result["violations"]) == 1  # still a violation

    def test_compliance_score_formula(self) -> None:
        """Score = compliant / (violations + warnings + compliant)."""
        std1 = {
            "id": _next_id(),
            "title": "Compliant Std",
            "standard_type": "technology_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        std2 = {
            "id": _next_id(),
            "title": "Violating Std",
            "standard_type": "application_standard",
            "compliance_level": "mandatory",
            "status": "approved",
        }
        tech_elem = _elem("node", layer="technology")
        app_elem = _elem("application_component", layer="application")
        # Only link tech_elem to std1
        rel = _rel(tech_elem["id"], std1["id"], "realization")
        result = check_compliance([tech_elem, app_elem], [std1, std2], [rel])
        # std1: tech_elem linked (but app_elem not applicable) => compliant
        # std2: app_elem not linked => violation
        assert len(result["compliant"]) == 1
        assert len(result["violations"]) == 1
        assert result["compliance_score"] == pytest.approx(0.5, abs=0.001)


# ===================================================================
# 4. View Generator
# ===================================================================


class TestViewGeneratorEmpty:
    def test_empty_input(self) -> None:
        result = generate_view("organization", [], [])
        assert result["viewpoint"] == "organization"
        assert result["element_ids"] == []
        assert result["relationship_ids"] == []
        assert result["element_count"] == 0
        assert result["relationship_count"] == 0

    def test_unknown_viewpoint(self) -> None:
        result = generate_view("nonexistent_viewpoint", [_elem("node", layer="technology")], [])
        assert result["viewpoint"] == "nonexistent_viewpoint"
        assert result["element_ids"] == []
        assert result["element_count"] == 0

    def test_output_structure(self) -> None:
        result = generate_view("layered", [], [])
        assert set(result.keys()) == {
            "viewpoint",
            "element_ids",
            "boundary_element_ids",
            "relationship_ids",
            "element_count",
            "relationship_count",
        }


class TestViewGeneratorOrganization:
    def test_organization_includes_business_actors(self) -> None:
        actor = _elem("business_actor", layer="business", title="CEO")
        role = _elem("business_role", layer="business", title="CTO")
        app = _elem("application_component", layer="application", title="CRM")
        result = generate_view("organization", [actor, role, app], [])
        assert actor["id"] in result["element_ids"]
        assert role["id"] in result["element_ids"]
        assert app["id"] not in result["element_ids"]
        assert result["element_count"] == 2

    def test_organization_filters_wrong_layer(self) -> None:
        node = _elem("business_actor", layer="technology")
        result = generate_view("organization", [node], [])
        assert node["id"] not in result["element_ids"]


class TestViewGeneratorApplicationCooperation:
    def test_app_cooperation_includes_app_layer(self) -> None:
        app = _elem("application_component", layer="application")
        svc = _elem("application_service", layer="application")
        biz = _elem("business_process", layer="business")
        result = generate_view("application_cooperation", [app, svc, biz], [])
        # allowed_types is None, so all app-layer elements pass; biz is filtered by layer
        assert app["id"] in result["element_ids"]
        assert svc["id"] in result["element_ids"]
        assert biz["id"] not in result["element_ids"]

    def test_app_cooperation_filters_relationships(self) -> None:
        app1 = _elem("application_component", layer="application")
        app2 = _elem("application_service", layer="application")
        serving_rel = _rel(app1["id"], app2["id"], "serving")
        invalid_rel = _rel(app1["id"], app2["id"], "influence")
        result = generate_view("application_cooperation", [app1, app2], [serving_rel, invalid_rel])
        assert serving_rel["id"] in result["relationship_ids"]
        assert invalid_rel["id"] not in result["relationship_ids"]


class TestViewGeneratorTechnology:
    def test_technology_viewpoint_no_type_filter(self) -> None:
        """Technology viewpoint: layers=['technology'], element_types=None, rel_types=None."""
        node = _elem("node", layer="technology")
        device = _elem("device", layer="technology")
        app = _elem("application_component", layer="application")
        result = generate_view("technology", [node, device, app], [])
        assert node["id"] in result["element_ids"]
        assert device["id"] in result["element_ids"]
        assert app["id"] not in result["element_ids"]

    def test_technology_all_relationship_types_allowed(self) -> None:
        """rel_types is None => all relationship types pass."""
        n1 = _elem("node", layer="technology")
        n2 = _elem("device", layer="technology")
        r1 = _rel(n1["id"], n2["id"], "composition")
        r2 = _rel(n1["id"], n2["id"], "influence")
        result = generate_view("technology", [n1, n2], [r1, r2])
        assert len(result["relationship_ids"]) == 2


class TestViewGeneratorLayered:
    def test_layered_includes_all_layers(self) -> None:
        """Layered viewpoint: layers=None, types=None, rels=None => everything."""
        elems = [
            _elem("capability", layer="strategy"),
            _elem("business_process", layer="business"),
            _elem("application_component", layer="application"),
            _elem("node", layer="technology"),
        ]
        result = generate_view("layered", elems, [])
        assert result["element_count"] == 4

    def test_layered_includes_all_relationships(self) -> None:
        e1 = _elem("capability", layer="strategy")
        e2 = _elem("node", layer="technology")
        r = _rel(e1["id"], e2["id"], "anything_goes")
        result = generate_view("layered", [e1, e2], [r])
        assert len(result["relationship_ids"]) == 1


class TestViewGeneratorCapabilityMap:
    def test_capability_map_filters(self) -> None:
        cap = _elem("capability", layer="strategy")
        res = _elem("resource", layer="strategy")
        biz = _elem("business_process", layer="business")
        node = _elem("node", layer="technology")
        result = generate_view("capability_map", [cap, res, biz, node], [])
        assert cap["id"] in result["element_ids"]
        assert res["id"] in result["element_ids"]
        assert biz["id"] not in result["element_ids"]
        assert node["id"] not in result["element_ids"]

    def test_capability_map_relationships(self) -> None:
        cap = _elem("capability", layer="strategy")
        res = _elem("resource", layer="strategy")
        valid_rel = _rel(cap["id"], res["id"], "composition")
        invalid_rel = _rel(cap["id"], res["id"], "serving")
        result = generate_view("capability_map", [cap, res], [valid_rel, invalid_rel])
        assert valid_rel["id"] in result["relationship_ids"]
        assert invalid_rel["id"] not in result["relationship_ids"]


class TestViewGeneratorRelationshipFiltering:
    def test_relationship_needs_at_least_one_matched_element(self) -> None:
        """A relationship is only included if at least one endpoint is a matched element."""
        app = _elem("application_component", layer="application")
        biz = _elem("business_process", layer="business")
        # organization viewpoint: only business layer, specific types
        rel = _rel(app["id"], biz["id"], "serving")
        # app is NOT matched (wrong type for organization), biz IS (if it were right type)
        # but business_process is not in organization's element_types
        result = generate_view("organization", [app, biz], [rel])
        assert len(result["relationship_ids"]) == 0

    def test_relationship_included_if_one_endpoint_matches(self) -> None:
        actor = _elem("business_actor", layer="business")
        outside = _elem("application_component", layer="application")
        # Actor matches organization viewpoint, outside does not
        rel = _rel(actor["id"], outside["id"], "serving")
        # organization viewpoint has relationship_types=None, so any type is allowed
        result = generate_view("organization", [actor, outside], [rel])
        # actor is matched, so the relationship is included — and its other end
        # is declared as a boundary occurrence, never left dangling (A05).
        assert rel["id"] in result["relationship_ids"]
        assert result["boundary_element_ids"] == [outside["id"]]
        assert outside["id"] not in result["element_ids"]

    def test_viewpoint_case_and_whitespace(self) -> None:
        result = generate_view("  LAYERED  ", [], [])
        assert result["viewpoint"] == "layered"

    @pytest.mark.parametrize("viewpoint", list(VIEWPOINT_FILTERS.keys()))
    def test_all_viewpoints_return_valid_structure(self, viewpoint: str) -> None:
        result = generate_view(viewpoint, [], [])
        assert "viewpoint" in result
        assert "element_ids" in result
        assert "relationship_ids" in result
        assert result["element_count"] == 0

    def test_element_ids_sorted(self) -> None:
        e1 = _elem("node", layer="technology")
        e2 = _elem("device", layer="technology")
        result = generate_view("technology", [e1, e2], [])
        assert result["element_ids"] == sorted(result["element_ids"])
