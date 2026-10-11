"""Tests for improved deadline extraction and source identity matching."""
import pytest
from datetime import date
from opportunity_agent.v2.research.normalization import supported_value, _extract_dates_from_text
from opportunity_agent.v2.research.quality import field_supported
from opportunity_agent.v2.agents.contracts import ProgramResult, Evidence, ResearchFact, SuccessCriteria


class TestDatetimeExtraction:
    """Verify multi-format date extraction."""

    def test_short_format_with_year_in_context(self):
        """Feb. 1 without explicit year should extract with context year.

        If context_year is provided and Feb 1 of that year hasn't passed yet,
        use it. Otherwise, infer next year.
        """
        quote = "The application deadline is Feb. 1 for admission to the following fall semester."

        # If explicitly provided 2025 and Feb 1 2025 hasn't passed, use 2025
        candidates = _extract_dates_from_text(quote, context_year=2025)
        # Note: Today is 2026-10-10, so Feb 1 2025 is already passed
        # The function should return 2026-02-01 (next year)
        assert any("2026-02-01" in c for c in candidates), f"Expected Feb 1 2026 in {candidates}"

    def test_iso_format(self):
        """ISO format YYYY-MM-DD."""
        quote = "Application deadline: 2025-02-01"
        candidates = _extract_dates_from_text(quote)
        assert "2025-02-01" in candidates

    def test_full_english_format(self):
        """Full English format like 'February 1, 2025'."""
        quote = "The final deadline is February 1, 2025 for Fall admission."
        candidates = _extract_dates_from_text(quote)
        assert "2025-02-01" in candidates

    def test_abbreviated_english_format(self):
        """Abbreviated English format like 'Feb 1, 2025'."""
        quote = "Submission closes Feb 1, 2025."
        candidates = _extract_dates_from_text(quote)
        assert "2025-02-01" in candidates

    def test_numeric_format(self):
        """Numeric format like 2/1/2025."""
        quote = "Deadline is 2/1/2025"
        candidates = _extract_dates_from_text(quote)
        assert "2025-02-01" in candidates

    def test_chinese_format(self):
        """Chinese date format YYYY年M月D日."""
        quote = "申请截止日期是 2025年2月1日"
        candidates = _extract_dates_from_text(quote)
        assert "2025-02-01" in candidates


class TestSupportedValueDeadline:
    """Verify deadline validation accepts extracted dates."""

    def test_deadline_with_short_format_and_context(self):
        """Deadline extracted from 'Feb. 1' with context year inference should validate."""
        # Georgia Tech case: page has "© 2026" and "Effective January 2026"
        # but Feb 1 2026 is past (today is 2026-10-10), so should infer 2027
        quote = "Application deadline: Feb. 1 for admission to Fall semester. © 2026 Georgia Institute of Technology"
        value = "2027-02-01"  # Inferred from context
        result = supported_value("deadline", value, quote)
        assert result is True, f"Deadline validation failed for {value} in quote"

    def test_deadline_explicit_future_year(self):
        """When year is explicitly stated as future, use that year."""
        quote = "Application deadline: Feb. 1, 2028 for Fall admission"
        value = "2028-02-01"
        result = supported_value("deadline", value, quote)
        assert result is True

    def test_deadline_requires_deadline_keyword(self):
        """Quote must contain deadline/due/closes keyword."""
        quote = "Feb. 1 is when students enroll"  # No deadline keyword
        value = "2027-02-01"
        result = supported_value("deadline", value, quote)
        assert result is False, "Should reject date without deadline keyword"

    def test_deadline_with_iso_format(self):
        """ISO deadline format."""
        quote = "Application deadline: 2025-02-01"
        value = "2025-02-01"
        result = supported_value("deadline", value, quote)
        assert result is True

    def test_deadline_rejects_wrong_date(self):
        """Wrong date should be rejected."""
        quote = "Application deadline: Feb. 1, 2025"
        value = "2025-03-01"  # Wrong month
        result = supported_value("deadline", value, quote)
        assert result is False


class TestGenericSourceDeadlineAcceptance:
    """Verify deadline/GRE can be sourced from generic (non-exact) programme pages."""

    def test_deadline_from_generic_page(self):
        """Deadline should be accepted from generic university-wide admissions page."""
        evidence = Evidence(
            evidence_id="test-1",
            source_id="source-1",
            url="https://example.edu/admissions",
            authority="official",
            program_match="generic",  # Not exact
            intake="2025 Fall",
            supports_fields=["deadline"],
            relevance_score=0.8,  # Required for legacy relevance_method to pass usable()
        )

        program = ProgramResult(
            university="Example University",
            program="Master of Science in Computer Science",
            intake="2025 Fall",
            deadline=date(2025, 2, 1),
            evidence=[evidence],
            facts=[
                ResearchFact(
                    field="deadline",
                    value="2025-02-01",
                    verification_status="verified",
                    evidence_ids=["test-1"],
                )
            ],
        )

        criteria = SuccessCriteria(accepted_authorities=["official"])
        result = field_supported(program, "deadline", criteria)
        assert result is True, "Deadline from generic source should be accepted"

    def test_gre_from_generic_page(self):
        """GRE policy should be accepted from generic page."""
        evidence = Evidence(
            evidence_id="test-2",
            source_id="source-2",
            url="https://example.edu/requirements",
            authority="official",
            program_match="generic",  # Not exact
            intake="2025 Fall",
            supports_fields=["gre_policy"],
            relevance_score=0.8,  # Required for legacy relevance_method to pass usable()
        )

        program = ProgramResult(
            university="Example University",
            program="Master of Science in Computer Science",
            intake="2025 Fall",
            gre_policy="not_required",
            evidence=[evidence],
            facts=[
                ResearchFact(
                    field="gre_policy",
                    value="not_required",
                    verification_status="verified",
                    evidence_ids=["test-2"],
                )
            ],
        )

        criteria = SuccessCriteria(accepted_authorities=["official"])
        result = field_supported(program, "gre_policy", criteria)
        assert result is True, "GRE policy from generic source should be accepted"

    def test_tuition_still_requires_exact(self):
        """Other fields (tuition) should still require exact programme match."""
        evidence = Evidence(
            evidence_id="test-3",
            source_id="source-3",
            url="https://example.edu/admissions",
            authority="official",
            program_match="generic",  # Not exact
            intake="2025 Fall",
            supports_fields=["tuition"],
            verification_status="verified",
        )

        program = ProgramResult(
            university="Example University",
            program="Master of Science in Computer Science",
            intake="2025 Fall",
            evidence=[evidence],
            facts=[
                ResearchFact(
                    field="tuition",
                    value="50000",
                    verification_status="verified",
                    evidence_ids=["test-3"],
                )
            ],
        )

        criteria = SuccessCriteria(accepted_authorities=["official"])
        result = field_supported(program, "tuition", criteria)
        assert result is False, "Tuition should require exact programme match"


class TestUCSDMSCSEIdentity:
    """Verify MSCSE identity handling.

    Note: MSCSE support depends on entries in _PROGRAM_IDENTITIES.
    This test verifies that when MSCSE is properly registered, pages
    claiming DS identity should not match an MSCSE request.
    """

    def test_mscse_vs_mscs_distinction(self):
        """MSCSE page should not match MSCS program request."""
        from opportunity_agent.official_research import classify_program_page

        # Page that clearly mentions "Data Science" instead of CSE
        title = "Master of Data Science - Admissions"
        url = "https://mds.ucsd.edu/admissions/index.html"
        text = "The MS in Data Science is a highly interactive program. Data science combines..."

        match, scope, evidence = classify_program_page("Master of Science in Computer Science and Engineering", title, url, text)
        # Should be rejected or generic, not exact for MSCSE when page says DS
        assert match in {"generic", "rejected"}, f"Expected generic/rejected for DS page when looking for MSCSE, got {match}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
