"""Append-log writer — Phase A slice of the dynamic vault-indexing layer.

Writer side only: a per-day NDJSON append log that captures dirty events
(proxy output, vault edits, transcript lines) for the async indexer to consume
later. This module implements the on-disk format and the atomic append; it does
**not** implement the indexer, SQLite/BM25 mirror, watcher, or proxy wiring
(those are later phases), and it is **feature-flagged OFF by default** — the
proxy does not call it until a later phase enables it.

On-disk contract (binding, from the initiative design §1):
  * Segments live in a directory, one daily-rotated file per UTC date,
    ``YYYY-MM-DD.ndjson``. A new segment opens at the first append after UTC
    midnight; the prior day's segment is then immutable.
  * Records are NDJSON — one UTF-8 JSON object per line, newline-terminated.
  * The segment is opened ``O_APPEND``; a single ``write`` up to 4096 bytes
    (Linux ``PIPE_BUF``) is atomic w.r.t. other ``O_APPEND`` writers, so records
    never interleave at the byte level.
  * Each record MUST be <= 4096 bytes including the trailing newline. The
    initial slice rejects an oversized record fail-closed (a structured
    ``oversized-record`` result, nothing written); multi-record continuation is
    a deferred amendment, not implemented here.

Sibling note: ``tokenpak/vault/_atomic.py`` provides atomic whole-file
*replacement* (tmp + ``os.replace``) for publishing index artefacts; the append
log instead relies on ``O_APPEND`` record atomicity, so it does not use it.

TODO(phase-B indexer): crash-safe *replay*, de-dup by ``payload_sha256``, and
nightly-rebuild reconciliation are the indexer's responsibility and are NOT in
this slice. The writer's discipline (``O_APPEND`` + single atomic write +
``fsync``) plus :func:`read_records` tolerating a torn trailing line are the
crash-safety foundation the indexer builds on.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1
ADAPTER_VERSION = "v1"
MAX_RECORD_BYTES = 4096  # Linux PIPE_BUF — the O_APPEND single-write atomicity bound.

# Closed set (a new type needs a design amendment + coordinated writer/indexer rollout).
EVENT_TYPES = frozenset({"proxy_output", "vault_edit", "transcript_line"})

_ENABLE_ENV = "TOKENPAK_APPEND_LOG"
_VAULT_DIR_ENV = "TOKENPAK_VAULT_DIR"


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    written: bool
    path: Optional[Path]
    n_bytes: int
    stop_reason: Optional[str] = None
    record: Optional[dict] = None


# --------------------------------------------------------------------------- #
# record construction
# --------------------------------------------------------------------------- #
def _uuid7(now_ms: Optional[int] = None) -> str:
    """A time-ordered UUIDv7 string (RFC 9562 layout): 48-bit ms timestamp,
    version/variant nibbles, the rest random. Lexicographically sortable."""
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    ts = now_ms & ((1 << 48) - 1)
    raw = bytearray(ts.to_bytes(6, "big") + os.urandom(10))
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(raw)))


def _now_iso_ms() -> str:
    return _to_iso_ms(datetime.now(timezone.utc))


def _to_iso_ms(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def payload_sha256(payload: dict) -> str:
    """Hex SHA-256 over the canonical-form payload (sorted keys), so the digest
    is stable regardless of key insertion order — the basis for dedupe and
    nightly-rebuild reconciliation."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_record(
    event_type: str,
    payload: dict,
    *,
    source_adapter_id: str,
    source_adapter_version: str = ADAPTER_VERSION,
    event_id: Optional[str] = None,
    created_at=None,
) -> dict:
    """Assemble + validate one append-log record (the closed minimum field set).

    ``event_id`` / ``created_at`` may be supplied for deterministic callers
    (e.g. tests); otherwise a UUIDv7 and the current UTC time are generated.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"event_type {event_type!r} not in closed set {sorted(EVENT_TYPES)}")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    if created_at is None:
        created_at_iso = _now_iso_ms()
    elif isinstance(created_at, datetime):
        created_at_iso = _to_iso_ms(created_at)
    else:
        created_at_iso = str(created_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id or _uuid7(),
        "event_type": event_type,
        "created_at": created_at_iso,
        "source": {"adapter_id": source_adapter_id, "adapter_version": source_adapter_version},
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
    }


def serialize(record: dict) -> bytes:
    """Record -> one NDJSON line (UTF-8, newline-terminated)."""
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


# --------------------------------------------------------------------------- #
# writer
# --------------------------------------------------------------------------- #
class AppendLogWriter:
    """Appends records to daily-rotated NDJSON segments in ``directory``."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def segment_path(self, record: dict) -> Path:
        # the UTC date is the first 10 chars of the ISO created_at (YYYY-MM-DD)
        return self.directory / f"{record['created_at'][:10]}.ndjson"

    def write(self, record: dict) -> WriteResult:
        line = serialize(record)
        if len(line) > MAX_RECORD_BYTES:
            # fail-closed: the initial slice does not auto-split oversized records
            return WriteResult(False, False, None, len(line), "oversized-record", record)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.segment_path(record)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)  # single atomic write (<= PIPE_BUF) under O_APPEND
            os.fsync(fd)
        finally:
            os.close(fd)
        return WriteResult(True, True, path, len(line), None, record)


def read_records(path) -> list:
    """Read a segment defensively: yield each complete record and stop at a
    torn trailing line (a partial final write after a crash). Full replay,
    dedupe, and reconciliation are the phase-B indexer's job (see module TODO)."""
    out: list = []
    p = Path(path)
    if not p.is_file():
        return out
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.rstrip("\n")
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError:
                break  # torn final record — atomic appends never tear earlier ones
    return out


# --------------------------------------------------------------------------- #
# feature-flagged entry point (OFF by default)
# --------------------------------------------------------------------------- #
def is_enabled() -> bool:
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def default_append_log_dir() -> Path:
    # Follows the current ``.tokenpak`` convention (the canonical ``.tpk``
    # path sweep is a separate, still-pending reconciliation).
    base = os.environ.get(_VAULT_DIR_ENV)
    root = Path(base) if base else (Path.home() / "vault")
    return root / ".tokenpak" / "append-log"


def emit_event(
    event_type: str,
    payload: dict,
    *,
    source_adapter_id: str,
    directory=None,
    **record_kwargs,
) -> WriteResult:
    """Feature-flagged convenience the proxy/watcher will call in a later phase.
    No-op (nothing written) unless ``TOKENPAK_APPEND_LOG`` is enabled — so wiring
    it ahead of time cannot activate capture. The 8a slice exercises
    :func:`build_record` / :class:`AppendLogWriter` directly."""
    if not is_enabled():
        return WriteResult(True, False, None, 0, "disabled", None)
    record = build_record(event_type, payload, source_adapter_id=source_adapter_id, **record_kwargs)
    return AppendLogWriter(directory or default_append_log_dir()).write(record)
