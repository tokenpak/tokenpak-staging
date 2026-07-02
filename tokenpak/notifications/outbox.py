"""Append-first JSONL notification outbox."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tokenpak import _paths
from tokenpak.notifications.event import NotificationEvent


def default_outbox_path() -> Path:
    return _paths.under("companion", "notifications", "outbox.jsonl")


class NotificationOutbox:
    """Durable JSONL outbox for notification events."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_outbox_path()

    def append(self, event: NotificationEvent) -> dict[str, Any]:
        record = event.to_record()
        self.append_record(record)
        return record

    def append_record(self, record: dict[str, Any]) -> None:
        if not isinstance(record, dict):
            raise TypeError("notification outbox record must be a dict")
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

    def read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records
