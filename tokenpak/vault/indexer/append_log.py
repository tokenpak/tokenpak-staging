"""Per-record append-log writer.

Implements design §1 (schema) and §4.1 (durability):

- NDJSON, UTF-8, newline-terminated.
- Daily-rotated segment files named ``YYYY-MM-DD.ndjson`` (UTC).
- Records opened ``O_APPEND``; each record is ≤ 4096 bytes including the
  trailing newline (Linux ``PIPE_BUF`` atomicity for concurrent writers).
- ``fsync(2)`` after each record (OQ-1 default; measurement reported in the
  benchmark artifact attached to the PR).
- Oversized records fail closed with ``OversizedRecordError`` per §1.2 —
  callers in proxy code MUST trap and log without raising into the hot path
  (AC-impl-2: writer-side append-log failures cannot block the proxy
  response).

The writer is offline. There are no network egress paths. The module also
has no dependency on the asynchronous indexer process — that process is the
consumer of the same on-disk segments.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import struct
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable

from . import SCHEMA_VERSION

MAX_RECORD_BYTES: Final[int] = 4096
"""Hard cap per record including the trailing newline (design §1.2)."""

# Closed set of event types (design §1.3). Open-set values are rejected.
_VALID_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"proxy_output", "vault_edit", "transcript_line"}
)


class AppendLogError(Exception):
    """Base error for the append-log writer."""


class OversizedRecordError(AppendLogError):
    """Encoded record exceeds ``MAX_RECORD_BYTES``.

    Per design §1.2 the writer fails closed on oversized records. The proxy
    output path is required to catch this and continue without surfacing
    the failure to the response path (AC-impl-2).
    """

    def __init__(self, size: int, event_type: str) -> None:
        super().__init__(
            f"append-log record size={size} exceeds cap={MAX_RECORD_BYTES} "
            f"(event_type={event_type!r})"
        )
        self.size = size
        self.event_type = event_type


class InvalidEventTypeError(AppendLogError):
    """``event_type`` is not in the closed set defined by design §1.3."""


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a successful ``write_record`` call."""

    event_id: str
    payload_sha256: str
    record_bytes: int
    segment_path: Path
    fsynced: bool


def default_segment_dir() -> Path:
    """Canonical segment directory per design §1.1.

    ``~/vault/.tokenpak/append-log/`` resolved via ``$HOME`` so test
    invocations under tmp roots can override by exporting ``HOME``.
    """
    return Path(os.path.expanduser("~/vault/.tokenpak/append-log"))


def _segment_filename(now: datetime) -> str:
    """UTC-dated segment filename (design §1.1)."""
    return f"{now.strftime('%Y-%m-%d')}.ndjson"


# --- UUIDv7 (inline; OQ-4 default) -----------------------------------------
#
# RFC 9562 §5.7: 48-bit unix_ts_ms || 4-bit ver=7 || 12-bit rand_a ||
# 2-bit var=10 || 62-bit rand_b.
#
# We add a monotonic counter on top of rand_a so multiple UUIDs minted within
# the same millisecond stay lexicographically sortable (design §1.3 calls
# UUIDv7 "collision-free across writers" — same-process minting needs to be
# monotone too).

_uuid7_lock = threading.Lock()
_uuid7_last_ms: int = 0
_uuid7_counter: int = 0


def _uuid7_bytes(now_ms: int) -> bytes:
    global _uuid7_last_ms, _uuid7_counter
    with _uuid7_lock:
        if now_ms == _uuid7_last_ms:
            _uuid7_counter = (_uuid7_counter + 1) & 0x0FFF
            if _uuid7_counter == 0:
                # Counter wrapped within the same millisecond. Bump ms so
                # ordering is preserved; the next real-time tick will resync.
                now_ms += 1
                _uuid7_last_ms = now_ms
        else:
            _uuid7_last_ms = now_ms
            _uuid7_counter = secrets.randbits(12)
        counter = _uuid7_counter

    rand_b = secrets.randbits(62)

    # 128-bit assembly.
    ts = now_ms & ((1 << 48) - 1)
    ver_rand_a = (0x7 << 12) | (counter & 0x0FFF)
    var_rand_b = (0b10 << 62) | rand_b

    hi = (ts << 16) | ver_rand_a  # 64 bits
    lo = var_rand_b  # 64 bits
    return struct.pack(">QQ", hi, lo)


def _uuid7_str(now_ms: int) -> str:
    b = _uuid7_bytes(now_ms)
    return (
        f"{b[0:4].hex()}-{b[4:6].hex()}-{b[6:8].hex()}-"
        f"{b[8:10].hex()}-{b[10:16].hex()}"
    )


# --- Canonical payload SHA-256 ---------------------------------------------


def canonical_payload_sha256(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical-form payload object (design §1.3).

    Deterministic key ordering is enforced via ``json.dumps(sort_keys=True)``.
    The payload object itself is the user-supplied dict; callers must not
    rely on the writer to deep-clone.
    """
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --- Record assembly + write -----------------------------------------------


def _iso_ms(now: datetime) -> str:
    """ISO-8601 UTC with millisecond precision and a trailing ``Z``."""
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _encode_record(record: dict[str, Any]) -> bytes:
    """Encode a record dict to NDJSON bytes (one line, newline-terminated)."""
    line = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return line.encode("utf-8") + b"\n"


def build_record(
    event_type: str,
    source_adapter_id: str,
    source_adapter_version: str,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble a fully-populated append-log record.

    Splitting this from ``write_record`` lets the benchmark harness measure
    pure I/O cost separately from JSON-encoding overhead.
    """
    if event_type not in _VALID_EVENT_TYPES:
        raise InvalidEventTypeError(
            f"event_type {event_type!r} not in closed set "
            f"{sorted(_VALID_EVENT_TYPES)}"
        )
    if now is None:
        now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": _uuid7_str(now_ms),
        "event_type": event_type,
        "created_at": _iso_ms(now),
        "source": {
            "adapter_id": source_adapter_id,
            "adapter_version": source_adapter_version,
        },
        "payload": payload,
        "payload_sha256": canonical_payload_sha256(payload),
    }


# --- Writer with cached segment file descriptor ----------------------------


_writer_lock = threading.Lock()
_writer_fd: int | None = None
_writer_segment: str | None = None
_writer_dir: Path | None = None


def _close_cached_fd() -> None:
    global _writer_fd, _writer_segment, _writer_dir
    if _writer_fd is not None:
        try:
            os.close(_writer_fd)
        except OSError:
            pass
    _writer_fd = None
    _writer_segment = None
    _writer_dir = None


def _open_segment(segment_dir: Path, now: datetime) -> tuple[int, str]:
    """Open the segment for *today*, opening a fresh fd if the date rolled.

    Returns (fd, segment_filename). The fd is cached across calls.
    """
    global _writer_fd, _writer_segment, _writer_dir
    filename = _segment_filename(now)
    if (
        _writer_fd is not None
        and _writer_segment == filename
        and _writer_dir == segment_dir
    ):
        return _writer_fd, filename

    _close_cached_fd()
    segment_dir.mkdir(parents=True, exist_ok=True)
    path = segment_dir / filename
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    _writer_fd = fd
    _writer_segment = filename
    _writer_dir = segment_dir
    return fd, filename


def reset_writer_state() -> None:
    """Drop the cached segment fd. Used by tests + benchmark setup."""
    with _writer_lock:
        _close_cached_fd()


def write_record(
    event_type: str,
    source_adapter_id: str,
    source_adapter_version: str,
    payload: dict[str, Any],
    *,
    segment_dir: Path | None = None,
    fsync: bool = True,
    now: datetime | None = None,
) -> WriteResult:
    """Append one record. Atomic per Linux ``O_APPEND`` ≤ ``PIPE_BUF``.

    Raises ``OversizedRecordError`` if the encoded record exceeds 4096 bytes
    (design §1.2 fail-closed). Caller in the proxy hot path is required by
    AC-impl-2 to trap and not propagate.

    Setting ``fsync=False`` is for benchmarking only — production callers
    take the default per OQ-1.
    """
    record = build_record(
        event_type,
        source_adapter_id,
        source_adapter_version,
        payload,
        now=now,
    )
    blob = _encode_record(record)
    size = len(blob)
    if size > MAX_RECORD_BYTES:
        raise OversizedRecordError(size=size, event_type=event_type)

    actual_dir = segment_dir or default_segment_dir()
    if now is None:
        now = datetime.now(timezone.utc)

    with _writer_lock:
        fd, filename = _open_segment(actual_dir, now)
        # A single os.write up to PIPE_BUF is atomic vs concurrent O_APPEND
        # writers on Linux (design §1.2).
        os.write(fd, blob)
        if fsync:
            os.fsync(fd)

    return WriteResult(
        event_id=record["event_id"],
        payload_sha256=record["payload_sha256"],
        record_bytes=size,
        segment_path=actual_dir / filename,
        fsynced=fsync,
    )


def iter_segment(
    segment_path: Path,
    *,
    start_byte: int = 0,
) -> Iterable[tuple[int, dict[str, Any]]]:
    """Yield ``(byte_offset, record)`` from a segment starting at offset.

    Malformed trailing lines surface as ``json.JSONDecodeError`` raised by
    the caller's iteration. Recovery (truncation + quarantine) lives in
    ``recovery.py`` at a later milestone — this helper just reads.
    """
    with open(segment_path, "rb") as fh:
        fh.seek(start_byte)
        offset = start_byte
        for raw in fh:
            stripped = raw.rstrip(b"\n")
            if not stripped:
                offset += len(raw)
                continue
            record = json.loads(stripped.decode("utf-8"))
            yield offset, record
            offset += len(raw)


# Module shutdown hook: close cached fd so tests that swap HOME between
# runs don't leak fds against the old root.
import atexit

atexit.register(reset_writer_state)
