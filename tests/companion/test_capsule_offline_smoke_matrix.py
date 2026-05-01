"""Offline smoke matrix for transcript fixture capsule generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tokenpak.companion.transcript.parser import parse_transcript

from tokenpak.companion.capsules.builder import CapsuleBuilder
from tokenpak.companion.capsules.transcript_sources.base import Message

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class SmokeExpectation:
    """Expected local-only capsule behavior for one transcript fixture."""

    fixture: str
    message_count: int
    parse_errors: int
    smoke_capsule: bool
    production_capsule: bool
    expected_excerpt: str | None = None
    expected_artifact: str | None = None


EXPECTATIONS = (
    SmokeExpectation(
        fixture="basic_session.jsonl",
        message_count=6,
        parse_errors=0,
        smoke_capsule=True,
        production_capsule=True,
        expected_excerpt="Please help me refactor the authentication module",
        expected_artifact="/home/trix/project/auth.py",
    ),
    SmokeExpectation(
        fixture="empty.jsonl",
        message_count=0,
        parse_errors=0,
        smoke_capsule=False,
        production_capsule=False,
    ),
    SmokeExpectation(
        fixture="malformed.jsonl",
        message_count=3,
        parse_errors=2,
        smoke_capsule=True,
        production_capsule=False,
        expected_excerpt="Hello world",
    ),
    SmokeExpectation(
        fixture="multiblock_assistant.jsonl",
        message_count=4,
        parse_errors=0,
        smoke_capsule=True,
        production_capsule=False,
        expected_excerpt="Run the tests and fix any failures",
        expected_artifact="tests/test_auth.py",
    ),
)


def _messages_for_fixture(name: str) -> list[Message]:
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


def test_offline_smoke_matrix_matches_documented_fixture_outputs() -> None:
    """All fixture smoke rows are generated locally with no provider/network dependency."""
    smoke_builder = CapsuleBuilder(min_message_count=1)
    production_builder = CapsuleBuilder()

    for expected in EXPECTATIONS:
        summary = parse_transcript(FIXTURES / expected.fixture)
        messages = _messages_for_fixture(expected.fixture)
        smoke_capsule = smoke_builder.build_from_messages(
            messages,
            session_id=expected.fixture.removesuffix(".jsonl"),
            source_name=f"fixture:{expected.fixture}",
        )
        production_capsule = production_builder.build_from_messages(
            messages,
            session_id=expected.fixture.removesuffix(".jsonl"),
            source_name=f"fixture:{expected.fixture}",
        )

        assert summary.message_count == expected.message_count
        assert summary.parse_errors == expected.parse_errors
        assert (smoke_capsule is not None) is expected.smoke_capsule
        assert (production_capsule is not None) is expected.production_capsule

        if smoke_capsule is None:
            continue
        assert f"source_name: fixture:{expected.fixture}" in smoke_capsule.content
        assert f"message_count: {expected.message_count}" in smoke_capsule.content
        assert "# Raw transcript reference" in smoke_capsule.content
        if expected.expected_excerpt:
            assert expected.expected_excerpt in smoke_capsule.content
        if expected.expected_artifact:
            assert expected.expected_artifact in smoke_capsule.content


def test_offline_smoke_matrix_covers_all_jsonl_fixtures() -> None:
    """The matrix stays in sync as fixture files are added or removed."""
    expected_names = {expectation.fixture for expectation in EXPECTATIONS}
    fixture_names = {path.name for path in FIXTURES.glob("*.jsonl")}

    assert expected_names == fixture_names
