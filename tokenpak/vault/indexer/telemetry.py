"""Telemetry helpers for the dynamic indexer.

Counters, latency observations, and lag-gauge writes flow to
``06_RUNTIME/TELEMETRY/vault-index-telemetry.jsonl`` (vault-relative) as
NDJSON. The helpers are offline-only: no network egress, no Pro-daemon
presence checks.

This module deliberately implements only the *helpers*. Counter values
live in process memory; the indexer surfaces them by periodically calling
``emit_event`` with a snapshot. The OSS CLI surfaces in ``cli/status.py``
and ``cli/doctor.py`` will read the most recent snapshot from the JSONL
stream at the milestone where the CLI changes land.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Iterator

TELEMETRY_SCHEMA_VERSION = 1


def default_telemetry_path() -> Path:
    return Path(
        os.path.expanduser("~/vault/06_RUNTIME/TELEMETRY/vault-index-telemetry.jsonl")
    )


# --- In-memory counter / latency-sample registry --------------------------


class _Registry:
    """Process-local counters and a bounded latency-sample ring per name."""

    _RING_CAP = 1024

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._latency_rings: dict[str, deque[float]] = {}
        self._gauges: dict[str, float] = {}

    def incr(self, name: str, delta: int = 1) -> int:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + delta
            return self._counters[name]

    def observe_latency_ms(self, name: str, value_ms: float) -> None:
        with self._lock:
            ring = self._latency_rings.get(name)
            if ring is None:
                ring = deque(maxlen=self._RING_CAP)
                self._latency_rings[name] = ring
            ring.append(value_ms)

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "latency_samples": {
                    name: list(ring) for name, ring in self._latency_rings.items()
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._latency_rings.clear()
            self._gauges.clear()


_registry = _Registry()


def incr(name: str, delta: int = 1) -> int:
    """Increment a process-local counter."""
    return _registry.incr(name, delta)


def observe_latency_ms(name: str, value_ms: float) -> None:
    """Record a latency observation into the bounded ring."""
    _registry.observe_latency_ms(name, value_ms)


def set_gauge(name: str, value: float) -> None:
    """Set a gauge value (e.g., replay-lag, sealed-segment count)."""
    _registry.set_gauge(name, value)


def snapshot() -> dict[str, Any]:
    """Return a copy of all counters, gauges, and latency rings."""
    return _registry.snapshot()


def reset() -> None:
    """Used by tests + benchmarks for isolation between runs."""
    _registry.reset()


# --- Percentile + emission helpers ---------------------------------------


def percentiles(values: list[float], qs: list[float]) -> dict[str, float]:
    """Return percentiles named ``pN`` for each q in *qs* (q ∈ [0, 100]).

    Returns an empty dict if *values* is empty. The implementation is
    interpolated (NIST type 7) to match what the benchmark harness reports.
    """
    if not values:
        return {}
    s = sorted(values)
    n = len(s)
    out: dict[str, float] = {}
    for q in qs:
        if n == 1:
            out[f"p{int(q)}"] = s[0]
            continue
        rank = (q / 100.0) * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        frac = rank - lo
        out[f"p{int(q)}"] = s[lo] + frac * (s[hi] - s[lo])
    return out


def emit_event(
    name: str,
    *,
    payload: dict[str, Any] | None = None,
    path: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Append a structured telemetry event as one NDJSON line.

    Errors are swallowed by design — telemetry must never raise into the
    caller. A failed write is silent; the caller continues. The nightly
    rebuild's reconciliation pass remains the authoritative recovery
    layer for missing operational events.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    target = path or default_telemetry_path()
    record = {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "name": name,
        "emitted_at": now.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{now.microsecond // 1000:03d}Z",
        "payload": payload or {},
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


@contextmanager
def timed(name: str) -> Iterator[None]:
    """Record the elapsed wall time in milliseconds under *name*."""
    start = perf_counter_ns()
    try:
        yield
    finally:
        elapsed_ms = (perf_counter_ns() - start) / 1_000_000.0
        observe_latency_ms(name, elapsed_ms)


__all__ = [
    "TELEMETRY_SCHEMA_VERSION",
    "default_telemetry_path",
    "emit_event",
    "incr",
    "observe_latency_ms",
    "percentiles",
    "reset",
    "set_gauge",
    "snapshot",
    "timed",
]
