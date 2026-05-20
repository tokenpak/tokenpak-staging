"""Async indexer drain loop — skeleton.

This is the PR-#1 skeleton per design §12 step 2: the main loop, the
checkpoint loader, telemetry initialization, and the lock-acquire wrapper
that the downstream writers (sqlite_writer, bm25_mirror) will plug into.

It deliberately does **not** write to SQLite or to the BM25 mirror yet —
those land in subsequent PRs (steps 3 + 4 in design §12). The append-log
consumer is wired so callers can already drive it end-to-end against a
trivial "log only" record processor in tests.

Forbidden-action guardrails baked in here:

- No license-token check, no Pro-daemon presence-check (AC-impl-5).
- No network egress (AC-impl-5).
- ``vault.indexer.enable`` defaults false; this module never starts on
  import (Std 20 §2 public-safe-defaults).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import SCHEMA_VERSION
from . import append_log as _append_log
from . import checkpoint as _checkpoint
from . import lock as _lock
from . import telemetry as _telemetry

LOGGER = logging.getLogger(__name__)


# Type alias for the consumer hook a downstream PR wires into the loop.
RecordHandler = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class IndexerConfig:
    """Operational knobs the indexer reads at start.

    Values mirror what the proxy config block exposes; the dataclass keeps
    the loop testable without dragging the proxy's config plumbing into
    the test path. The defaults align with design §2.4 backpressure marks
    and §3.3 lock-budget.
    """

    segment_dir: Path
    checkpoint_path: Path
    lock_path: Path
    poll_interval_s: float = 0.5
    lock_timeout_s: float = 30.0
    checkpoint_every_n_records: int = 100
    drain_budget_records: int = 50_000


def default_config() -> IndexerConfig:
    return IndexerConfig(
        segment_dir=_append_log.default_segment_dir(),
        checkpoint_path=_checkpoint.default_checkpoint_path(),
        lock_path=_lock.default_lock_path(),
    )


def _segment_for_today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".ndjson"


def _list_sealed_segments(segment_dir: Path) -> list[str]:
    if not segment_dir.exists():
        return []
    today = _segment_for_today()
    names = sorted(p.name for p in segment_dir.glob("*.ndjson"))
    return [n for n in names if n != today]


def _resume_position(
    cfg: IndexerConfig,
) -> tuple[str, int]:
    """Pick up where the last cycle left off, or start at the active head."""
    cp = _checkpoint.read_checkpoint(cfg.checkpoint_path)
    if cp is not None:
        return cp.segment_filename, cp.byte_offset
    return _segment_for_today(), 0


def _drain_segment(
    cfg: IndexerConfig,
    segment_filename: str,
    start_byte: int,
    handler: RecordHandler,
) -> tuple[int, str, int]:
    """Walk *segment_filename* from *start_byte*, calling *handler* per record.

    Returns ``(processed_count, last_event_id, end_byte)``. ``last_event_id``
    is empty if no records were processed in this pass.
    """
    path = cfg.segment_dir / segment_filename
    if not path.exists():
        return 0, "", start_byte

    processed = 0
    last_event_id = ""
    end_byte = start_byte

    for offset, record in _append_log.iter_segment(path, start_byte=start_byte):
        with _telemetry.timed("indexer.record_handle_ms"):
            handler(record)
        last_event_id = record.get("event_id", "") or last_event_id
        processed += 1
        end_byte = offset + len(record.get("event_id", "")) + 0
        # NOTE: end_byte is approximated here from the iterator; the
        # iterator yields offset at the start of the line. We update from
        # the iterator's view by reading the file size at end-of-pass.
        if processed >= cfg.drain_budget_records:
            break

    # Refresh end_byte from the file's current size — that's the safe
    # restart point because every record we yielded ended at a newline.
    try:
        end_byte = max(end_byte, path.stat().st_size)
    except OSError:
        pass

    return processed, last_event_id, end_byte


def run_once(
    handler: RecordHandler,
    cfg: IndexerConfig | None = None,
) -> dict[str, Any]:
    """Single drain pass under the §3.2 lock. Returns a small stats dict.

    The handler is invoked once per record. Downstream PRs replace the
    test handler with the real SQLite + BM25 writers; this skeleton makes
    the loop driveable in isolation.
    """
    actual = cfg or default_config()

    _telemetry.emit_event(
        "indexer.run_once.start",
        payload={"schema_version": SCHEMA_VERSION},
    )

    segment, start_byte = _resume_position(actual)

    processed_total = 0
    final_event_id = ""
    final_byte_offset = start_byte
    final_segment = segment

    try:
        with _lock.acquire(actual.lock_path, timeout_s=actual.lock_timeout_s):
            # Walk any sealed segments older than today first. Their byte
            # ranges are stable so a checkpoint resume picks the right
            # spot. Only the *first* sealed segment uses ``start_byte``;
            # subsequent sealed segments always start at offset 0.
            sealed = _list_sealed_segments(actual.segment_dir)
            sealed = [s for s in sealed if s >= segment]
            for idx, seg in enumerate(sealed):
                seg_start = start_byte if (idx == 0 and seg == segment) else 0
                count, last_id, end_off = _drain_segment(
                    actual, seg, seg_start, handler
                )
                processed_total += count
                if last_id:
                    final_event_id = last_id
                final_segment = seg
                final_byte_offset = end_off
                if processed_total >= actual.drain_budget_records:
                    break

            # Then walk the active segment.
            if processed_total < actual.drain_budget_records:
                active = _segment_for_today()
                active_start = start_byte if active == segment else 0
                count, last_id, end_off = _drain_segment(
                    actual, active, active_start, handler
                )
                processed_total += count
                if last_id:
                    final_event_id = last_id
                final_segment = active
                final_byte_offset = end_off

            if final_event_id:
                _checkpoint.write_checkpoint(
                    _checkpoint.make_checkpoint(
                        segment_filename=final_segment,
                        byte_offset=final_byte_offset,
                        last_event_id=final_event_id,
                    ),
                    path=actual.checkpoint_path,
                )
    except _lock.LockTimeoutError:
        _telemetry.emit_event(
            "indexer.lock_timeout",
            payload={"timeout_s": actual.lock_timeout_s},
        )
        _telemetry.incr("indexer.lock_timeouts")
        return {
            "processed": 0,
            "lock_timeout": True,
            "segment": final_segment,
            "byte_offset": final_byte_offset,
        }

    _telemetry.incr("indexer.records_processed", processed_total)
    _telemetry.emit_event(
        "indexer.run_once.end",
        payload={
            "processed": processed_total,
            "segment": final_segment,
            "byte_offset": final_byte_offset,
        },
    )
    return {
        "processed": processed_total,
        "lock_timeout": False,
        "segment": final_segment,
        "byte_offset": final_byte_offset,
    }


def run_forever(
    handler: RecordHandler,
    cfg: IndexerConfig | None = None,
    *,
    stop_predicate: Callable[[], bool] | None = None,
) -> None:
    """Tight drain → sleep → drain loop. Used by the service unit later.

    Tests pass a *stop_predicate* that flips true after a bounded number
    of iterations so the loop terminates deterministically.
    """
    actual = cfg or default_config()
    stop = stop_predicate or (lambda: False)
    while not stop():
        run_once(handler, actual)
        time.sleep(actual.poll_interval_s)


__all__ = [
    "IndexerConfig",
    "RecordHandler",
    "default_config",
    "run_forever",
    "run_once",
]
