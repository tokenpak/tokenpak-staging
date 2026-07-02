"""P8 section 3.2 - telemetry overhead.

Isolates the request-path latency added by ``Monitor.log()`` on top of
pass-through. NB: the production Monitor is **async** — ``log()`` enqueues the
row onto a background write queue and returns in <0.1ms; the SQLite insert is
drained off the request hot path by a writer thread. This target therefore
measures the genuine per-request telemetry cost (the enqueue + queue lock),
which is what actually adds to request latency, not the off-path disk write.
The Monitor is backed by a tmpfs DB (``/dev/shm`` when available, else a temp
dir) so the drained write incurs no disk noise. Reported as the difference
versus a no-op baseline.

Opt-in: ``pytest -m p8_latency``.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from .p8_latency_harness import requires_p8_optin, run_difference_target

pytestmark = pytest.mark.p8_latency


def _tmpfs_db_path(tmp_path_factory) -> str:
    # Prefer tmpfs (/dev/shm) to isolate from disk noise (methodology section 3.2);
    # fall back to a pytest tmp dir when /dev/shm is unavailable.
    shm = "/dev/shm"
    base = shm if os.path.isdir(shm) and os.access(shm, os.W_OK) else None
    if base is not None:
        fd, path = tempfile.mkstemp(prefix="p8_telemetry_", suffix=".db", dir=base)
        os.close(fd)
        os.unlink(path)  # Monitor creates the schema itself
        return path
    return str(tmp_path_factory.mktemp("p8_telemetry") / "telemetry.db")


def test_telemetry_overhead(request, tmp_path_factory):
    requires_p8_optin(request)
    monitor_mod = pytest.importorskip(
        "tokenpak.proxy.monitor", reason="proxy monitor unavailable"
    )
    Monitor = monitor_mod.Monitor

    db_path = _tmpfs_db_path(tmp_path_factory)
    monitor = Monitor(db_path)

    def target() -> None:
        monitor.log(
            model="claude-3-5-sonnet",
            input_tokens=1000,
            output_tokens=200,
            cost=0.0123,
            latency_ms=42.0,
            status_code=200,
            endpoint="/v1/messages",
        )

    def baseline() -> None:
        # No-op baseline: isolates the per-request Monitor.log() enqueue cost.
        pass

    try:
        record = run_difference_target(
            target="telemetry",
            method=(
                "real Monitor.log() per-request cost (async enqueue onto the "
                "background write queue; SQLite insert into tmpfs DB is drained "
                "off the request hot path) vs no-op (methodology section 3.2)"
            ),
            target_fn=target,
            baseline_fn=baseline,
        )
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except OSError:
                pass

    assert record["target"] == "telemetry"
    assert record["sample_size"] > 0
    assert record["status"] == "measured"
