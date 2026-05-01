"""
Extended unit tests for tokenpak._internal.memory.session_capsules.

Covers private helpers and edge cases not exercised by test_session_capsules.py:
- _normalize_lines: CRLF / CR / LF / mixed line endings
- _parse_frontmatter: no marker, no closing marker, non-key lines, lower-case keys
- _resolve_section: every alias, unknown heading, case-insensitive match
- _clean_value: multiple spaces, tab, leading/trailing whitespace
- build_session_capsule: ## and ### heading levels
- build_session_capsule: '*' and '+' bullet markers
- build_session_capsule: non-bullet prose lines captured in sections
- build_session_capsule: '---' line inside section body is skipped
- build_session_capsule: line_count metadata is accurate
- build_session_capsule: "Transcript" alias → raw_transcript_reference
- build_session_capsule: "Metadata" alias → session_metadata (no section body added,
  only frontmatter contributes to session_metadata)
- score_capsule_sections: raw_transcript_reference dict scoring
- score_capsule_sections: empty dict section scores 0
- capsule_retrieval_score: base_score=0 with empty capsule
- capsule_retrieval_score: very large capsule boost is capped at 5.0
"""

from __future__ import annotations

import pytest

from tokenpak.companion.memory.session_capsules import (
    REQUIRED_CAPSULE_SECTIONS,
    _clean_value,
    _normalize_lines,
    _parse_frontmatter,
    _resolve_section,
    build_session_capsule,
    capsule_retrieval_score,
    score_capsule_sections,
    serialize_capsule,
)


# ---------------------------------------------------------------------------
# _normalize_lines
# ---------------------------------------------------------------------------

class TestNormalizeLines:
    """Tests for the line-ending normalizer."""

    def test_lf_lines_unchanged(self):
        assert _normalize_lines("a\nb\nc") == ["a", "b", "c"]

    def test_crlf_converted_to_lf(self):
        assert _normalize_lines("a\r\nb\r\nc") == ["a", "b", "c"]

    def test_cr_only_converted_to_lf(self):
        assert _normalize_lines("a\rb\rc") == ["a", "b", "c"]

    def test_mixed_line_endings(self):
        assert _normalize_lines("a\r\nb\rc\nd") == ["a", "b", "c", "d"]

    def test_empty_string_produces_one_empty_line(self):
        assert _normalize_lines("") == [""]

    def test_single_line_no_newline(self):
        assert _normalize_lines("hello") == ["hello"]


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    """Tests for YAML-lite frontmatter parser."""

    def test_empty_list_returns_empty_dict(self):
        assert _parse_frontmatter([]) == {}

    def test_no_opening_marker_returns_empty(self):
        lines = ["key: value", "other: thing"]
        assert _parse_frontmatter(lines) == {}

    def test_opening_marker_without_closing_parses_all_valid_lines(self):
        """Missing closing --- means we parse until end of list."""
        lines = ["---", "session_id: abc", "ts: 2026-04-01"]
        result = _parse_frontmatter(lines)
        assert result == {"session_id": "abc", "ts": "2026-04-01"}

    def test_closing_marker_stops_parsing(self):
        lines = ["---", "key: val", "---", "after: stop"]
        result = _parse_frontmatter(lines)
        assert result == {"key": "val"}
        assert "after" not in result

    def test_keys_normalised_to_lowercase(self):
        lines = ["---", "Session_ID: test-123", "---"]
        result = _parse_frontmatter(lines)
        assert "session_id" in result
        assert result["session_id"] == "test-123"

    def test_non_key_value_lines_ignored(self):
        lines = ["---", "# comment", "plain text", "key: value", "---"]
        result = _parse_frontmatter(lines)
        assert result == {"key": "value"}

    def test_value_with_colon_preserved(self):
        lines = ["---", "url: http://example.com:8080/path", "---"]
        result = _parse_frontmatter(lines)
        assert result["url"] == "http://example.com:8080/path"


# ---------------------------------------------------------------------------
# _resolve_section
# ---------------------------------------------------------------------------

class TestResolveSection:
    """Tests for section heading → canonical key resolution."""

    @pytest.mark.parametrize("heading,expected", [
        ("session metadata", "session_metadata"),
        ("metadata", "session_metadata"),
        ("decisions made", "decisions_made"),
        ("decisions", "decisions_made"),
        ("artifacts created", "artifacts_created"),
        ("artifacts", "artifacts_created"),
        ("action items", "action_items"),
        ("actions", "action_items"),
        ("insights", "insights"),
        ("raw transcript reference", "raw_transcript_reference"),
        ("transcript", "raw_transcript_reference"),
    ])
    def test_all_known_aliases(self, heading: str, expected: str):
        assert _resolve_section(heading) == expected

    def test_unknown_heading_returns_none(self):
        assert _resolve_section("introduction") is None
        assert _resolve_section("summary") is None

    def test_resolution_is_case_insensitive(self):
        """_resolve_section lowercases input internally, so uppercase aliases resolve."""
        # The function does .strip().lower() before alias lookup
        assert _resolve_section("DECISIONS MADE") == "decisions_made"
        assert _resolve_section("Insights") == "insights"
        assert _resolve_section("TRANSCRIPT") == "raw_transcript_reference"

    def test_leading_trailing_whitespace_stripped(self):
        assert _resolve_section("  insights  ") == "insights"


# ---------------------------------------------------------------------------
# _clean_value
# ---------------------------------------------------------------------------

class TestCleanValue:
    """Tests for whitespace normaliser."""

    def test_multiple_spaces_collapsed(self):
        assert _clean_value("hello   world") == "hello world"

    def test_tab_collapsed(self):
        assert _clean_value("hello\tworld") == "hello world"

    def test_leading_trailing_stripped(self):
        assert _clean_value("  hello world  ") == "hello world"

    def test_empty_string(self):
        assert _clean_value("") == ""

    def test_already_clean(self):
        assert _clean_value("clean text") == "clean text"


# ---------------------------------------------------------------------------
# build_session_capsule — heading level variants
# ---------------------------------------------------------------------------

class TestBuildCapsuleHeadingLevels:
    """## and ### headings should be resolved just like # headings."""

    def test_double_hash_heading(self):
        text = "## Decisions Made\n- Decision A\n"
        capsule = build_session_capsule(text)
        assert capsule["decisions_made"] == ["Decision A"]

    def test_triple_hash_heading(self):
        text = "### Artifacts Created\n- Artifact B\n"
        capsule = build_session_capsule(text)
        assert capsule["artifacts_created"] == ["Artifact B"]

    def test_deep_heading(self):
        text = "###### Action Items\n- Do something\n"
        capsule = build_session_capsule(text)
        assert capsule["action_items"] == ["Do something"]


# ---------------------------------------------------------------------------
# build_session_capsule — bullet marker variants
# ---------------------------------------------------------------------------

class TestBuildCapsuléBulletMarkers:
    """'*' and '+' bullet markers should be captured."""

    def test_star_bullet(self):
        text = "# Decisions Made\n* Star bullet decision\n"
        capsule = build_session_capsule(text)
        assert "Star bullet decision" in capsule["decisions_made"]

    def test_plus_bullet(self):
        text = "# Insights\n+ Plus bullet insight\n"
        capsule = build_session_capsule(text)
        assert "Plus bullet insight" in capsule["insights"]

    def test_mixed_bullets(self):
        text = (
            "# Decisions Made\n"
            "- Dash item\n"
            "* Star item\n"
            "+ Plus item\n"
        )
        capsule = build_session_capsule(text)
        assert len(capsule["decisions_made"]) == 3


# ---------------------------------------------------------------------------
# build_session_capsule — non-bullet prose in sections
# ---------------------------------------------------------------------------

class TestBuildCapsuleProseLines:
    """Non-bullet prose content inside a section is captured as-is."""

    def test_prose_line_captured(self):
        text = "# Insights\nThis is a plain prose line in insights.\n"
        capsule = build_session_capsule(text)
        assert "This is a plain prose line in insights." in capsule["insights"]

    def test_hr_separator_inside_section_skipped(self):
        """Lines starting with '---' are skipped per source code check."""
        text = "# Decisions Made\n- First\n---\n- Second\n"
        capsule = build_session_capsule(text)
        # '---' line should not appear in decisions_made
        assert "---" not in capsule["decisions_made"]
        assert "First" in capsule["decisions_made"]
        assert "Second" in capsule["decisions_made"]


# ---------------------------------------------------------------------------
# build_session_capsule — metadata
# ---------------------------------------------------------------------------

class TestBuildCapsuleMetadata:
    """session_metadata accuracy."""

    def test_line_count_matches_input(self):
        text = "line one\nline two\nline three\n"
        capsule = build_session_capsule(text)
        expected_lines = len(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
        assert capsule["session_metadata"]["line_count"] == expected_lines

    def test_sha256_present_and_not_empty(self):
        capsule = build_session_capsule("any content")
        sha = capsule["session_metadata"]["sha256"]
        assert sha and len(sha) == 64

    def test_no_source_path_default_empty_string(self):
        capsule = build_session_capsule("# Insights\n- x\n")
        assert capsule["session_metadata"]["source_path"] == ""
        assert capsule["raw_transcript_reference"]["source_path"] == ""


# ---------------------------------------------------------------------------
# build_session_capsule — section aliases "Transcript" and "Metadata"
# ---------------------------------------------------------------------------

class TestBuildCapsuleAliasTranscriptMetadata:
    """Verify 'Transcript' and 'Metadata' aliases resolve correctly."""

    def test_transcript_alias_resolves(self):
        """'Transcript' heading → raw_transcript_reference section."""
        # raw_transcript_reference is special: its content is always built from
        # source_path + sha256, but the section alias should resolve without error.
        text = "# Transcript\nSee session log at /path/to/log.md\n"
        # Should not raise; raw_transcript_reference is a dict not a list
        capsule = build_session_capsule(text, "origin.md")
        assert capsule["raw_transcript_reference"]["source_path"] == "origin.md"

    def test_metadata_alias_resolves_to_session_metadata(self):
        """'Metadata' heading is an alias for session_metadata."""
        # session_metadata starts as a dict built from frontmatter+defaults.
        # Bullets/prose under a ## Metadata heading would map to session_metadata
        # but the section is stored as a list and then overwritten by the metadata dict.
        text = "## Metadata\n- meta bullet\n"
        # Should not raise
        capsule = build_session_capsule(text)
        assert "session_metadata" in capsule


# ---------------------------------------------------------------------------
# score_capsule_sections
# ---------------------------------------------------------------------------

class TestScoreCapsuleSectionsExtended:
    """Additional scoring edge cases."""

    def test_raw_transcript_reference_is_dict_non_zero(self):
        """raw_transcript_reference is always a dict with at least 3 keys → non-zero score."""
        capsule = build_session_capsule("")
        scores = score_capsule_sections(capsule)
        # raw_transcript_reference dict has source_path, sha256, fallback
        assert scores["raw_transcript_reference"] > 0.0

    def test_score_proportional_to_item_count(self):
        """Score for a list section is weight * item count."""
        text = "# Decisions Made\n- D1\n- D2\n- D3\n"
        capsule = build_session_capsule(text)
        scores = score_capsule_sections(capsule)
        # weight for decisions_made is 3.0
        assert scores["decisions_made"] == pytest.approx(3.0 * 3, rel=1e-4)

    def test_score_of_zero_for_empty_list_section(self):
        capsule = build_session_capsule("")
        scores = score_capsule_sections(capsule)
        assert scores["action_items"] == 0.0
        assert scores["insights"] == 0.0


# ---------------------------------------------------------------------------
# capsule_retrieval_score
# ---------------------------------------------------------------------------

class TestCapsuleRetrievalScoreExtended:
    """Edge cases in retrieval scoring."""

    def test_zero_base_score_with_empty_capsule(self):
        capsule = build_session_capsule("")
        score = capsule_retrieval_score(0.0, capsule)
        assert score == 0.0

    def test_boost_capped_at_five_for_very_large_capsule(self):
        """Boost should never exceed 5.0 regardless of capsule size."""
        lines = "- Item\n" * 500
        text = (
            "# Decisions Made\n" + lines +
            "# Artifacts Created\n" + lines +
            "# Action Items\n" + lines +
            "# Insights\n" + lines
        )
        capsule = build_session_capsule(text)
        score = capsule_retrieval_score(0.0, capsule)
        assert score <= 5.0

    def test_score_increases_monotonically_with_signal(self):
        """More high-signal items → higher retrieval score (up to cap)."""
        base = 1.0
        capsule_low = build_session_capsule("# Decisions Made\n- D1\n")
        capsule_high = build_session_capsule(
            "# Decisions Made\n- D1\n- D2\n- D3\n"
            "# Artifacts Created\n- A1\n- A2\n"
            "# Action Items\n- AI1\n"
            "# Insights\n- I1\n- I2\n"
        )
        score_low = capsule_retrieval_score(base, capsule_low)
        score_high = capsule_retrieval_score(base, capsule_high)
        assert score_high >= score_low
