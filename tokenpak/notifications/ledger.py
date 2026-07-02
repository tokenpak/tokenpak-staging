"""Delivery-attempt ledger interface."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tokenpak import _paths
from tokenpak.notifications.event import utc_now_iso

_ALLOWED_STATUSES = {"pending", "sent", "failed", "muted", "skipped", "record_only"}


def default_ledger_path() -> Path:
    return _paths.under("companion", "notifications", "deliveries.jsonl")


@dataclass(frozen=True)
class DeliveryAttemptRecord:
    event_id: str
    channel_id: str | None
    status: str
    attempts: int = 0
    last_error: str | None = None
    timestamp: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id is required")
        if self.status not in _ALLOWED_STATUSES:
            raise ValueError(f"unsupported delivery status: {self.status}")
        if self.attempts < 0:
            raise ValueError("attempts must be non-negative")

    def to_record(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "channel_id": self.channel_id,
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class DeliveryAttemptLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_ledger_path()

    def record(self, attempt: DeliveryAttemptRecord) -> dict[str, Any]:
        record = attempt.to_record()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return record

    def read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
