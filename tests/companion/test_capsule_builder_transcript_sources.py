"""Tests for transcript-source-backed companion capsule generation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from tokenpak.companion.capsules.builder import CapsuleBuilder, save_capsule
from tokenpak.companion.capsules.transcript_sources.base import Message


class MockTranscriptSource:
    """In-memory transcript source used to verify source decoupling."""

    source_name = "mock"

    def __init__(self, messages: list[Message]) -> None:
        self._messages = messages
        self.loaded_session_ids: list[str] = []

    def list_sessions(self, since: datetime) -> Iterable[str]:
        assert since.tzinfo is not None
        return ["mock-session"]

    def load_messages(self, session_id: str) -> Iterable[Message]:
        self.loaded_session_ids.append(session_id)
        return self._messages


def test_build_uses_in_memory_transcript_source(tmp_path: Path) -> None:
    source = MockTranscriptSource([
        Message(role="user", content="We decided to use tokenpak/companion/capsules/builder.py."),
        Message(role="assistant", content="Root cause found because the builder owned parsing."),
        Message(role="user", content="Next add tests/companion/test_capsule_builder_transcript_sources.py."),
    ])
    builder = CapsuleBuilder(transcript_source=source, min_message_count=1)

    capsule = builder.build("mock-session")

    assert capsule is not None
    assert source.loaded_session_ids == ["mock-session"]
    assert capsule.session_id == "mock-session"
    assert capsule.source_name == "mock"
    assert "source_name: mock" in capsule.content
    assert "tokenpak/companion/capsules/builder.py" in capsule.content

    output_path = save_capsule(capsule, capsule_dir=tmp_path)
    assert output_path == tmp_path / "mock-session.md"
    assert output_path.read_text(encoding="utf-8") == capsule.content


def test_build_empty_transcript_source_returns_none() -> None:
    source = MockTranscriptSource([])
    builder = CapsuleBuilder(transcript_source=source, min_message_count=1)

    assert builder.build("empty-session") is None


def test_build_without_transcript_source_returns_none() -> None:
    builder = CapsuleBuilder(min_message_count=1)

    assert builder.build("no-source") is None


def test_mock_source_list_sessions_contract_is_timezone_aware() -> None:
    source = MockTranscriptSource([Message(role="user", content="hello")])

    assert list(source.list_sessions(datetime.now(timezone.utc))) == ["mock-session"]


def test_build_from_messages_accepts_loaded_messages() -> None:
    builder = CapsuleBuilder(min_message_count=1)

    capsule = builder.build_from_messages(
        [
            Message(role="user", content="We decided to keep tokenpak dynamic."),
            {"role": "assistant", "content": [{"type": "text", "text": "Next verify scripts."}]},
        ],
        session_id="loaded-session",
        source_name="unit-test",
    )

    assert capsule is not None
    assert capsule.session_id == "loaded-session"
    assert capsule.source_name == "unit-test"
    assert "source_name: unit-test" in capsule.content
    assert "decided to keep tokenpak dynamic" in capsule.content
    assert "Next verify scripts" in capsule.content

