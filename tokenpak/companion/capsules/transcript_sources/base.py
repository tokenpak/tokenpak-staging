"""Transcript source contracts for companion memory capsules.

This module owns the small protocol between capsule generation and any
surface that can provide prior-session messages. Implementations discover and
parse platform-specific transcript storage; callers consume normalized
``Message`` objects only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol


@dataclass(frozen=True)
class Message:
    """Normalized transcript message consumed by capsule builders.

    Attributes:
        role: Message role or source-specific type label.
        content: Human-readable text extracted from the transcript.
        timestamp: Optional source timestamp, preserved for traceability.
        metadata: Optional source metadata; not required for summarization.
    """

    role: str
    content: str
    timestamp: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class TranscriptSource(Protocol):
    """Protocol implemented by platform-specific transcript providers."""

    source_name: str

    def list_sessions(self, since: datetime) -> Iterable[str]:
        """Return session IDs with transcript activity at or after ``since``."""
        ...

    def load_messages(self, session_id: str) -> Iterable[Message]:
        """Return normalized messages for ``session_id`` or an empty iterable."""
        ...
