"""Audit the offline transcript fixture corpus used for capsule generation."""

from __future__ import annotations

from pathlib import Path

from tokenpak.companion.transcript.parser import parse_transcript

from tokenpak.companion.capsules.builder import CapsuleBuilder
from tokenpak.companion.capsules.transcript_sources.base import Message

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_messages(name: str) -> list[Message]:
    path = FIXTURES / name
    summary = parse_transcript(path)
    return [
        Message(
            role=message.role,
            content=message.content,
            timestamp=message.timestamp,
            metadata={"type": message.type, "source_path": str(path)},
        )
        for message in summary.messages
    ]


def test_basic_session_fixture_builds_session_capsule() -> None:
    """Normal transcript fixture should produce a deterministic capsule."""
    path = FIXTURES / "basic_session.jsonl"
    summary = parse_transcript(path)
    capsule = CapsuleBuilder(min_message_count=1).build_from_messages(
        _fixture_messages("basic_session.jsonl"),
        session_id="basic-session-fixture",
        source_name="fixture-basic",
    )

    assert summary.message_count == 6
    assert summary.parse_errors == 0
    assert capsule is not None
    assert capsule.message_count == 6
    assert "source_name: fixture-basic" in capsule.content
    assert "Please help me refactor the authentication module" in capsule.content
    assert "/home/trix/project/auth.py" in capsule.content
    assert "# Raw transcript reference" in capsule.content


def test_empty_fixture_returns_no_capsule() -> None:
    """Empty transcripts are a graceful no-context path, not an error."""
    summary = parse_transcript(FIXTURES / "empty.jsonl")
    capsule = CapsuleBuilder(min_message_count=1).build_from_messages(
        _fixture_messages("empty.jsonl"),
        session_id="empty-fixture",
        source_name="fixture-empty",
    )

    assert summary.message_count == 0
    assert summary.tokens_est == 0
    assert summary.parse_errors == 0
    assert capsule is None


def test_malformed_fixture_skips_bad_lines_but_builds_from_valid_messages() -> None:
    """Malformed JSONL lines are counted and skipped while valid lines remain usable."""
    summary = parse_transcript(FIXTURES / "malformed.jsonl")
    capsule = CapsuleBuilder(min_message_count=1).build_from_messages(
        _fixture_messages("malformed.jsonl"),
        session_id="malformed-fixture",
        source_name="fixture-malformed",
    )

    assert summary.parse_errors == 2
    assert summary.message_count == 3
    assert capsule is not None
    assert capsule.message_count == 3
    assert "source_name: fixture-malformed" in capsule.content
    assert "# Raw transcript reference" in capsule.content
    assert any("Valid line after malformed" in message.content for message in summary.messages)


def test_multiblock_fixture_preserves_tool_context_for_capsule() -> None:
    """Multi-block content is flattened before capsule summarization."""
    summary = parse_transcript(FIXTURES / "multiblock_assistant.jsonl")
    capsule = CapsuleBuilder(min_message_count=1).build_from_messages(
        _fixture_messages("multiblock_assistant.jsonl"),
        session_id="multiblock-fixture",
        source_name="fixture-multiblock",
    )

    assert summary.message_count == 4
    assert any(message.tool_calls for message in summary.messages)
    assert capsule is not None
    assert "tool_use:Bash" in capsule.content
    assert "python3 -m pytest tests/ -v" in capsule.content
    assert "tests/test_auth.py" in capsule.content
    assert any("FAILED tests/test_auth.py::test_login" in message.content for message in summary.messages)
