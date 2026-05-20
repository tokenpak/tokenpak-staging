"""Indexer checkpoint persistence (design §4.2).

The indexer's position in the active append-log segment is persisted to
``~/vault/.tokenpak/.indexer-checkpoint.json`` every 100 processed records.
The checkpoint file is single-writer (only the indexer process touches it),
so it does not need to hold the §3.2 lock — atomic-write alone is enough.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lock import atomic_replace

CHECKPOINT_SCHEMA_VERSION = 1
"""Bumped only via design amendment."""


def default_checkpoint_path() -> Path:
    return Path(os.path.expanduser("~/vault/.tokenpak/.indexer-checkpoint.json"))


@dataclass(frozen=True)
class Checkpoint:
    """Position state persisted between indexer runs."""

    schema_version: int
    segment_filename: str
    byte_offset: int
    last_event_id: str
    checkpointed_at: str

    def to_json_bytes(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, indent=2).encode("utf-8")


def make_checkpoint(
    *,
    segment_filename: str,
    byte_offset: int,
    last_event_id: str,
    now: datetime | None = None,
) -> Checkpoint:
    if now is None:
        now = datetime.now(timezone.utc)
    return Checkpoint(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        segment_filename=segment_filename,
        byte_offset=byte_offset,
        last_event_id=last_event_id,
        checkpointed_at=now.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{now.microsecond // 1000:03d}Z",
    )


def write_checkpoint(
    checkpoint: Checkpoint,
    *,
    path: Path | None = None,
) -> Path:
    """Persist the checkpoint via the §3.5 atomic-write pattern."""
    target = path or default_checkpoint_path()
    atomic_replace(target, checkpoint.to_json_bytes())
    return target


def read_checkpoint(path: Path | None = None) -> Checkpoint | None:
    """Read the persisted checkpoint, or ``None`` if absent/unparseable.

    A malformed checkpoint is treated as "no checkpoint" — the indexer
    will start at the head of the active segment, which is a safe replay
    of work already in SQLite (replay-idempotency, design §4.4). The
    indexer surfaces a structured warning via telemetry; this loader is
    deliberately tolerant rather than fatal.
    """
    target = path or default_checkpoint_path()
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    required = {
        "schema_version",
        "segment_filename",
        "byte_offset",
        "last_event_id",
        "checkpointed_at",
    }
    if not required.issubset(data.keys()):
        return None

    try:
        return Checkpoint(
            schema_version=int(data["schema_version"]),
            segment_filename=str(data["segment_filename"]),
            byte_offset=int(data["byte_offset"]),
            last_event_id=str(data["last_event_id"]),
            checkpointed_at=str(data["checkpointed_at"]),
        )
    except (TypeError, ValueError):
        return None


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "Checkpoint",
    "default_checkpoint_path",
    "make_checkpoint",
    "read_checkpoint",
    "write_checkpoint",
]
