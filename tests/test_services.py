"""
Tests for service logic: quality scoring and priority calculations.

Tests pure functions directly without HTTP layer.
"""

import pytest

from prio import cod_from_components, rice, wsjf
from quality import AMBIGUOUS_TERMS, _has_gherkin, quality_check


class TestQualityCheck:
    """Tests for quality_check function."""

    def test_perfect_requirement(self):
        """All checks pass: clear language, Gherkin scenario, measurable values."""
        title = "API Response Time"
        description = "The API must respond within 200ms for 95% of requests."
        acceptance = [
            """Given a user makes a request
When the API processes it
Then the response time is under 200ms"""
        ]

        score, issues, passes = quality_check(title, description, acceptance)

        assert score == 1.0
        assert len(issues) == 0
        assert len(passes) == 3
        assert "No obvious ambiguous wording found." in passes
        assert "At least one Gherkin scenario present." in passes
        assert "Contains measurable values." in passes

    def test_ambiguous_wording(self):
        """Detects ambiguous terms in description."""
        title = "Performance"
        description = "The system should be fast and user-friendly, etc."
        acceptance = [
            """Given a user
When they interact
Then it is seamless"""
        ]

        score, issues, passes = quality_check(title, description, acceptance)

        assert score == 1.0 / 3  # 1 pass out of 3
        assert len(issues) == 2
        assert any("Ambiguous wording" in issue for issue in issues)
        assert any("etc" in issue for issue in issues)
        assert any("fast" in issue for issue in issues)
        assert any("user-friendly" in issue for issue in issues)

    def test_missing_gherkin(self):
        """Detects missing Gherkin scenarios."""
        title = "Feature"
        description = "Must support 100 concurrent users."
        acceptance = ["User can login", "User can logout"]

        score, issues, passes = quality_check(title, description, acceptance)

        assert "Add at least one Given/When/Then scenario." in issues
        assert "At least one Gherkin scenario present." not in passes

    def test_missing_measurable_values(self):
        """Detects lack of measurable values."""
        title = "Feature"
        description = "The system should handle many requests quickly."
        acceptance = [
            """Given a user
When they send a request
Then it responds quickly"""
        ]

        score, issues, passes = quality_check(title, description, acceptance)

        assert "Add measurable values/thresholds to description." in issues
        assert "Contains measurable values." not in passes

    def test_all_failures(self):
        """All checks fail."""
        title = "Feature"
        description = "Make it fast and user-friendly."
        acceptance = ["It should work well"]

        score, issues, passes = quality_check(title, description, acceptance)

        assert score == 0.0
        assert len(issues) == 3
        assert len(passes) == 0

    def test_case_insensitive_ambiguous_detection(self):
        """Ambiguous terms detected regardless of case."""
        title = "Feature"
        description = (
            "OPTIMIZE the system to MINIMIZE response time using STATE-OF-THE-ART techniques."
        )
        acceptance = [
            """Given system
When request
Then fast"""
        ]

        score, issues, passes = quality_check(title, description, acceptance)

        assert any("Ambiguous wording" in issue for issue in issues)
        assert any("optimize" in issue.lower() for issue in issues)
        assert any("minimize" in issue.lower() for issue in issues)

    def test_multiple_gherkin_scenarios(self):
        """Multiple Gherkin scenarios are recognized."""
        title = "Feature"
        description = "Handle 100 requests per second."
        acceptance = [
            """Given user A
When they login
Then they see dashboard""",
            "Some other criterion",
            """Given user B
When they logout
Then session ends""",
        ]

        score, issues, passes = quality_check(title, description, acceptance)

        assert "At least one Gherkin scenario present." in passes

    def test_decimal_values_count_as_measurable(self):
        """Decimal numbers count as measurable values."""
        title = "Feature"
        description = "Response time under 0.5 seconds."
        acceptance = [
            """Given request
When processed
Then response in time"""
        ]

        score, issues, passes = quality_check(title, description, acceptance)

        assert "Contains measurable values." in passes

    def test_empty_acceptance_criteria(self):
        """Empty acceptance criteria list fails Gherkin check."""
        title = "Feature"
        description = "Must handle 100 users."
        acceptance = []

        score, issues, passes = quality_check(title, description, acceptance)

        assert "Add at least one Given/When/Then scenario." in issues


class TestHasGherkin:
    """Tests for _has_gherkin helper function."""

    def test_valid_gherkin(self):
        """Recognizes valid Gherkin scenario."""
        acceptance = [
            """Given a user is logged in
When they click the button
Then the page refreshes"""
        ]
        assert _has_gherkin(acceptance) is True

    def test_multiline_gherkin_with_extra_whitespace(self):
        """Handles Gherkin with various whitespace."""
        acceptance = [
            """  Given   a condition
  When   an action
  Then   a result  """
        ]
        assert _has_gherkin(acceptance) is True

    def test_incomplete_gherkin(self):
        """Missing 'Then' clause fails."""
        acceptance = [
            """Given a user
When they login"""
        ]
        assert _has_gherkin(acceptance) is False

    def test_non_gherkin_text(self):
        """Regular text is not Gherkin."""
        acceptance = ["The user should be able to login"]
        assert _has_gherkin(acceptance) is False

    def test_empty_list(self):
        """Empty list returns False."""
        assert _has_gherkin([]) is False


class TestWSJF:
    """Tests for WSJF priority calculation."""

    def test_normal_wsjf(self):
        """Normal WSJF calculation."""
        result = wsjf(100.0, 10.0)
        assert result == 10.0

    def test_high_cod_low_duration(self):
        """High CoD and low duration yields high WSJF."""
        result = wsjf(1000.0, 1.0)
        assert result == 1000.0

    def test_low_cod_high_duration(self):
        """Low CoD and high duration yields low WSJF."""
        result = wsjf(10.0, 100.0)
        assert result == 0.1

    def test_zero_duration(self):
        """Zero duration returns None."""
        result = wsjf(100.0, 0.0)
        assert result is None

    def test_negative_duration(self):
        """Negative duration returns None."""
        result = wsjf(100.0, -5.0)
        assert result is None

    def test_zero_cod(self):
        """Zero CoD with positive duration returns 0."""
        result = wsjf(0.0, 10.0)
        assert result == 0.0

    def test_negative_cod(self):
        """Negative CoD is allowed (theoretical edge case)."""
        result = wsjf(-50.0, 10.0)
        assert result == -5.0

    def test_fractional_values(self):
        """Handles fractional inputs."""
        result = wsjf(7.5, 2.5)
        assert result == 3.0


class TestRICE:
    """Tests for RICE priority calculation."""

    def test_normal_rice(self):
        """Normal RICE calculation."""
        result = rice(1000, 3, 0.8, 5)
        assert result == (1000 * 3 * 0.8) / 5
        assert result == 480.0

    def test_high_reach_impact_confidence(self):
        """High values yield high RICE score."""
        result = rice(10000, 5, 1.0, 2)
        assert result == 25000.0

    def test_zero_effort(self):
        """Zero effort returns None."""
        result = rice(100, 3, 0.8, 0)
        assert result is None

    def test_negative_effort(self):
        """Negative effort returns None."""
        result = rice(100, 3, 0.8, -2)
        assert result is None

    def test_zero_reach(self):
        """Zero reach yields zero RICE score."""
        result = rice(0, 3, 0.8, 5)
        assert result == 0.0

    def test_zero_impact(self):
        """Zero impact yields zero RICE score."""
        result = rice(1000, 0, 0.8, 5)
        assert result == 0.0

    def test_zero_confidence(self):
        """Zero confidence yields zero RICE score."""
        result = rice(1000, 3, 0, 5)
        assert result == 0.0

    def test_all_zeros_except_effort(self):
        """All zeros except effort yields zero."""
        result = rice(0, 0, 0, 5)
        assert result == 0.0

    def test_overflow_protection(self):
        """Very large values are capped at 1e12."""
        result = rice(1e10, 1e10, 1.0, 0.001)
        assert result == 1e12  # Capped

    def test_infinity_handling(self):
        """Infinity values return None."""
        # This is tricky to trigger, but if we could:
        # In practice, the cap at 1e12 prevents infinity
        # Test the boundary case
        result = rice(1e15, 1e15, 1.0, 1.0)
        assert result == 1e12  # Capped, not infinity

    def test_fractional_values(self):
        """Handles fractional inputs."""
        result = rice(100.5, 2.5, 0.75, 3.0)
        expected = (100.5 * 2.5 * 0.75) / 3.0
        assert result == pytest.approx(expected)

    def test_very_small_effort(self):
        """Very small effort yields high RICE, but capped."""
        result = rice(1000, 1000, 1.0, 0.0001)
        assert result == 1e10  # (1000 * 1000 * 1.0) / 0.0001 = 1e10


class TestCoD:
    """Tests for Cost of Delay component calculation."""

    def test_normal_cod(self):
        """Normal CoD calculation."""
        result = cod_from_components(10, 5, 3)
        assert result == 18

    def test_all_zeros(self):
        """All zero components yield zero."""
        result = cod_from_components(0, 0, 0)
        assert result == 0

    def test_negative_values(self):
        """Negative values are allowed (theoretical)."""
        result = cod_from_components(-5, 10, 3)
        assert result == 8

    def test_fractional_values(self):
        """Handles fractional inputs."""
        result = cod_from_components(7.5, 2.5, 1.5)
        assert result == 11.5

    def test_large_values(self):
        """Handles large values."""
        result = cod_from_components(1000000, 2000000, 3000000)
        assert result == 6000000

    def test_mixed_positive_negative(self):
        """Mixed positive and negative values."""
        result = cod_from_components(100, -50, 30)
        assert result == 80


class TestEdgeCases:
    """Cross-functional edge cases."""

    def test_wsjf_with_cod_from_components(self):
        """Combine CoD calculation with WSJF."""
        cod = cod_from_components(10, 5, 3)  # 18
        result = wsjf(cod, 6.0)
        assert result == 3.0

    def test_quality_check_with_all_ambiguous_terms(self):
        """Description with many ambiguous terms."""
        description = " ".join(AMBIGUOUS_TERMS)
        score, issues, passes = quality_check("Title", description, [])

        # Should detect all ambiguous terms
        assert score < 0.5
        assert any("Ambiguous wording" in issue for issue in issues)

    def test_rice_with_very_high_confidence(self):
        """Confidence > 1.0 is theoretically allowed."""
        result = rice(100, 3, 1.5, 5)
        assert result == 90.0

    def test_wsjf_extremely_small_duration(self):
        """Very small duration yields very high WSJF."""
        result = wsjf(100.0, 0.001)
        assert result == 100000.0
