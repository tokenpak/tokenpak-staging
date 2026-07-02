"""Structured outbound notification events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

_ALLOWED_CATEGORIES = {
    "cycle.summary",
    "cycle.start",
    "cycle.deadman_alert",
    "cycle.failure",
    "runtime.warning",
}
_ALLOWED_SEVERITIES = {"info", "warning", "error", "critical"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class NotificationEvent:
    """Single outbound notification event.

    The event is the durable record. Adapter delivery is a later concern and
    must not be required for this object to be useful.
    """

    source: str
    category: str
    severity: str
    title: str
    body: str
    action_required: bool = False
    audience: str | None = None
    topic: str | None = None
    dedupe_key: str | None = None
    cycle_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require_text("source", self.source)
        _require_text("category", self.category)
        _require_text("severity", self.severity)
        _require_text("title", self.title)
        _require_text("body", self.body)
        if self.category not in _ALLOWED_CATEGORIES:
            raise ValueError(f"unsupported notification category: {self.category}")
        if self.severity not in _ALLOWED_SEVERITIES:
            raise ValueError(f"unsupported notification severity: {self.severity}")
        if self.audience is None and self.topic is None:
            raise ValueError("notification event requires audience or topic")
        if not isinstance(self.metadata, dict):
            raise TypeError("metadata must be a dict")

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "source": self.source,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "action_required": self.action_required,
            "audience": self.audience,
            "topic": self.topic,
            "dedupe_key": self.dedupe_key,
            "cycle_id": self.cycle_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "metadata": self.metadata,
        }


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
