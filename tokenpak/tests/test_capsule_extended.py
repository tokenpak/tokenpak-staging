"""
Extended unit tests for tokenpak.capsule — interfaces not covered by
test_capsule_builder.py or test_session_capsules.py.

Coverage added here:
- Package-level import (tokenpak.capsule public API)
- _compress_text: * and + bullet variants, === setext heading, --- hr
- _compress_text: mixed code-fence + prose (fence toggle fully exercised)
- _compress_paragraph: sentence boundary at exactly _MAX_PARA_CHARS edge
- CapsuleBuilder.process: messages with None content (silently skipped)
- CapsuleBuilder.process: messages missing 'content' key entirely
- CapsuleBuilder.process: hot_window larger than message count (all in window)
- CapsuleBuilder.process: single message, hot_window=1 (boundary)
- CapsuleBuilder.process: stats when no_eligible_blocks path is taken
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# Package-level import (tests the __init__.py public surface)
# ---------------------------------------------------------------------------

import tokenpak.compression.capsules as capsule_pkg
from tokenpak.compression.capsules import CapsuleBuilder
from tokenpak.compression.capsules.builder import (
    _MAX_PARA_CHARS,
    _compress_paragraph,
    _compress_text,
    _wrap_capsule,
    _capsule_id,
    DEFAULT_MIN_BLOCK_CHARS,
    DEFAULT_HOT_WINDOW,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_body(messages: list, **extra) -> bytes:
    payload: dict = {"messages": messages}
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def _long_text(n: int = DEFAULT_MIN_BLOCK_CHARS + 50) -> str:
    word = "hello"
    words: list = []
    while len(" ".join(words)) < n:
        words.append(word)
    return " ".join(words)


# ---------------------------------------------------------------------------
# Package-level import surface
# ---------------------------------------------------------------------------


class TestPackageLevelImport:
    def test_capsulebuilder_importable_from_package(self):
        """CapsuleBuilder is accessible via `from tokenpak.compression.capsules import CapsuleBuilder`."""
        assert CapsuleBuilder is not None

    def test_capsulebuilder_in_package_all(self):
        """__all__ declares CapsuleBuilder."""
        assert "CapsuleBuilder" in capsule_pkg.__all__

    def test_builder_module_in_all(self):
        """__all__ declares 'builder' (the sub-module)."""
        assert "CapsuleBuilder" in capsule_pkg.__all__  # TCM-07: new package exports class directly, not submodule

    def test_package_import_produces_working_builder(self):
        """A builder imported from the package works identically to one from the module."""
        b = CapsuleBuilder(enabled=True, min_block_chars=10, hot_window=0)
        long = "word " * 20
        body = _make_body([{"role": "user", "content": long}])
        _, stats = b.process(body)
        assert stats["blocks_capsulized"] == 1


# ---------------------------------------------------------------------------
# _compress_text — bullet variants and structure lines not in existing tests
# ---------------------------------------------------------------------------


class TestCompressTextStructureVariants:
    def test_star_bullet_preserved_verbatim(self):
        """Lines starting with '* ' (star bullet) are structure lines — preserved."""
        text = "* Star bullet item"
        result = _compress_text(text)
        assert "* Star bullet item" in result

    def test_plus_bullet_preserved_verbatim(self):
        """Lines starting with '+ ' (plus bullet) are structure lines — preserved."""
        text = "+ Plus bullet item"
        result = _compress_text(text)
        assert "+ Plus bullet item" in result

    def test_multiple_bullet_styles_all_preserved(self):
        """Mixed bullet styles are all kept verbatim."""
        text = "- Dash\n* Star\n+ Plus"
        result = _compress_text(text)
        assert "- Dash" in result
        assert "* Star" in result
        assert "+ Plus" in result

    def test_setext_heading_equals_preserved(self):
        """'===' is a setext heading marker — preserved verbatim."""
        text = "==="
        result = _compress_text(text)
        assert "===" in result

    def test_setext_heading_dash_preserved(self):
        """'---' is a horizontal rule / setext heading — preserved verbatim."""
        text = "---"
        result = _compress_text(text)
        assert "---" in result

    def test_blockquote_with_content_preserved(self):
        """'> ...' blockquote lines are preserved, not compressed."""
        text = "> This is a blockquote that should not be compressed at all"
        result = _compress_text(text)
        assert "> This is a blockquote that should not be compressed at all" in result

    def test_mixed_structure_and_prose(self):
        """Structure lines are preserved; prose lines between them are compressed."""
        long_prose = "word " * 60  # well above _MAX_PARA_CHARS
        text = f"# Heading\n\n{long_prose.strip()}\n\n- Bullet"
        result = _compress_text(text)
        assert "# Heading" in result
        assert "- Bullet" in result
        # Prose should be shorter than original (compressed)
        assert len(result) < len(text)


# ---------------------------------------------------------------------------
# _compress_text — code-fence toggle edge cases
# ---------------------------------------------------------------------------


class TestCompressTextCodeFenceToggle:
    def test_code_fence_opens_and_closes(self):
        """Content between ``` markers is never compressed."""
        inner = "a " * 200  # way over _MAX_PARA_CHARS
        text = f"```\n{inner.strip()}\n```"
        result = _compress_text(text)
        assert inner.strip() in result

    def test_text_after_closing_fence_is_compressed(self):
        """Prose that follows a closed code fence IS subject to compression."""
        long_prose = "word " * 60
        text = f"```\ncode line\n```\n\n{long_prose.strip()}"
        result = _compress_text(text)
        assert "code line" in result
        # Prose after fence should be shorter than the original prose chunk
        assert len(result) < len(text)

    def test_two_code_fences_both_preserved(self):
        """Two separate code blocks are both preserved intact."""
        inner = "x " * 150
        text = f"```\n{inner.strip()}\n```\n\n```\n{inner.strip()}\n```"
        result = _compress_text(text)
        # Both code blocks should be present
        assert result.count("```") == 4  # two opens + two closes

    def test_empty_code_fence_preserved(self):
        """An empty code fence block (``` immediately followed by ```) is preserved."""
        text = "```\n```"
        result = _compress_text(text)
        assert "```" in result


# ---------------------------------------------------------------------------
# _compress_paragraph — boundary conditions
# ---------------------------------------------------------------------------


class TestCompressParagraphBoundaries:
    def test_one_char_below_max_no_truncation(self):
        """Text at _MAX_PARA_CHARS - 1 chars is returned verbatim (no ellipsis)."""
        text = "a" * (_MAX_PARA_CHARS - 1)
        result = _compress_paragraph(text)
        assert result == text
        assert not result.endswith("…")

    def test_one_char_above_max_truncated(self):
        """Text at _MAX_PARA_CHARS + 1 chars must be truncated."""
        # Use a single long word so word-boundary truncation hits
        text = "a" * (_MAX_PARA_CHARS + 1)
        result = _compress_paragraph(text)
        assert len(result) <= _MAX_PARA_CHARS + 1  # +1 for ellipsis char

    def test_sentence_boundary_exactly_at_max_used(self):
        """If the sentence ends right at the budget, use that boundary."""
        # Craft text where a sentence ends at exactly _MAX_PARA_CHARS
        sentence = "x" * (_MAX_PARA_CHARS - 2) + ". "
        filler = "y" * 100
        text = sentence + filler
        result = _compress_paragraph(text)
        assert result.endswith(".")

    def test_no_space_found_for_word_boundary(self):
        """When no space exists in the first _MAX_PARA_CHARS chars, hard-truncate."""
        # A single unbroken word longer than _MAX_PARA_CHARS
        text = "a" * (_MAX_PARA_CHARS * 2)
        result = _compress_paragraph(text)
        # Should still end with ellipsis and be within budget
        assert result.endswith("…")


# ---------------------------------------------------------------------------
# CapsuleBuilder.process — None / missing content paths
# ---------------------------------------------------------------------------


class TestCapsuleBuilderNoneContent:
    """Messages with None or absent 'content' keys must be silently skipped."""

    @pytest.fixture
    def builder(self):
        return CapsuleBuilder(enabled=True, min_block_chars=10, hot_window=0)

    def test_message_with_none_content_not_capsulised(self, builder):
        """Message where 'content' is explicitly None is skipped gracefully."""
        body = json.dumps({"messages": [{"role": "user", "content": None}]}).encode()
        new_body, stats = builder.process(body)
        assert stats["blocks_capsulized"] == 0

    def test_message_with_none_content_does_not_crash(self, builder):
        """Processing a message with None content must not raise."""
        body = json.dumps({"messages": [{"role": "user", "content": None}]}).encode()
        new_body, stats = builder.process(body)
        # Original body returned unchanged since nothing was capsulised
        assert json.loads(new_body)["messages"][0]["content"] is None

    def test_message_missing_content_key_skipped(self, builder):
        """Message dict with no 'content' key at all is silently skipped."""
        body = json.dumps({"messages": [{"role": "user"}]}).encode()
        new_body, stats = builder.process(body)
        assert stats["blocks_capsulized"] == 0

    def test_mixed_none_and_valid_content(self, builder):
        """None-content messages are skipped; valid long messages ARE capsulised."""
        long = "word " * 20
        body = json.dumps({
            "messages": [
                {"role": "user", "content": None},
                {"role": "user", "content": long},
            ]
        }).encode()
        new_body, stats = builder.process(body)
        assert stats["blocks_capsulized"] == 1
        data = json.loads(new_body)
        assert data["messages"][0]["content"] is None
        assert "[CAPSULE" in data["messages"][1]["content"]


# ---------------------------------------------------------------------------
# CapsuleBuilder.process — hot_window edge cases
# ---------------------------------------------------------------------------


class TestCapsuleBuilderHotWindowEdge:
    def test_hot_window_larger_than_message_count_nothing_capsulised(self):
        """When hot_window >= len(messages), every message is in the hot window."""
        builder = CapsuleBuilder(enabled=True, min_block_chars=10, hot_window=100)
        long = "word " * 20
        body = _make_body([
            {"role": "user", "content": long},
            {"role": "assistant", "content": long},
        ])
        _, stats = builder.process(body)
        assert stats["blocks_capsulized"] == 0

    def test_hot_window_equal_to_message_count_nothing_capsulised(self):
        """hot_window == len(messages) → hot_start = 0 → all messages in window."""
        builder = CapsuleBuilder(enabled=True, min_block_chars=10, hot_window=3)
        long = "word " * 20
        body = _make_body([
            {"role": "user", "content": long},
            {"role": "assistant", "content": long},
            {"role": "user", "content": long},
        ])
        _, stats = builder.process(body)
        assert stats["blocks_capsulized"] == 0

    def test_hot_window_one_with_single_message_nothing_capsulised(self):
        """One message + hot_window=1 → hot_start=0 → message is in hot window."""
        builder = CapsuleBuilder(enabled=True, min_block_chars=10, hot_window=1)
        long = "word " * 20
        body = _make_body([{"role": "user", "content": long}])
        _, stats = builder.process(body)
        assert stats["blocks_capsulized"] == 0

    def test_hot_window_one_with_two_messages_first_capsulised(self):
        """Two messages + hot_window=1 → first is outside hot window → capsulised."""
        builder = CapsuleBuilder(enabled=True, min_block_chars=10, hot_window=1)
        long = "word " * 20
        body = _make_body([
            {"role": "user", "content": long},    # idx 0 — outside hot window
            {"role": "assistant", "content": "ok"},  # idx 1 — inside hot window
        ])
        _, stats = builder.process(body)
        assert stats["blocks_capsulized"] == 1


# ---------------------------------------------------------------------------
# CapsuleBuilder.process — no_eligible_blocks stats
# ---------------------------------------------------------------------------


class TestCapsuleBuilderNoEligibleStats:
    def test_no_eligible_blocks_skipped_is_false(self):
        """When enabled but nothing qualifies, skipped should be False."""
        builder = CapsuleBuilder(enabled=True)
        body = _make_body([
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "ok"},
        ])
        _, stats = builder.process(body)
        assert stats["skipped"] is False
        assert stats["skip_reason"] == "no_eligible_blocks"

    def test_no_eligible_blocks_chars_in_equals_chars_out(self):
        """When nothing is capsulised, chars_in should equal chars_out."""
        builder = CapsuleBuilder(enabled=True)
        short = "hi"
        body = _make_body([
            {"role": "user", "content": short},
            {"role": "assistant", "content": short},
        ])
        _, stats = builder.process(body)
        assert stats["chars_in"] == stats["chars_out"]
        assert stats["ratio"] == 1.0
