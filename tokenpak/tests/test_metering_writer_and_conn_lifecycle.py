"""Focused regression tests for bounded metering writes and SQLite lifecycle.

Three properties are covered:

0. Read-after-write consistency: because ``record()`` is non-blocking (it
   enqueues to the background writer), the read paths drain pending writes
   first, so a summary read issued right after a request sees that request's
   row instead of racing the writer and under-counting.

1. ``UsageMeter.record()`` no longer spawns one unreaped daemon thread per
   call (the previous design appended a ``Thread`` to ``_pending_threads`` on
   every request and only cleared it in ``flush()``, so it grew without
   bound).  Records are now handed to a single bounded-queue background
   writer, so both ``_pending_threads`` and the live thread count stay bounded
   under a 10k-``record()`` burst.

2. Every telemetry module touched by the connection-leak sweep now closes its
   SQLite connection even when a DB operation raises mid-method (standardised
   on ``contextlib.closing``), so opens == closes — no descriptor leak.

These tests fail against the pre-patch code.
"""

import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tokenpak.telemetry.metering import UsageMeter


class TestBoundedWriter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "usage.db"
        self.meter = UsageMeter(key_id="bound-key", db_path=self.db_path)

    def tearDown(self):
        # Stop the writer before the temp DB is removed so background writes
        # don't race the cleanup.
        self.meter._writer_stop.set()
        self.meter._writer_thread.join(timeout=2)
        self._tmp.cleanup()

    def test_record_does_not_spawn_a_thread_per_call(self):
        """10k record() calls keep _pending_threads and live threads bounded.

        The DB write and the license bridge are stubbed so the test isolates
        the *threading* behaviour rather than write throughput.
        """
        n_threads_before = threading.active_count()
        N = 10_000

        with mock.patch.object(self.meter, "_write_rows", lambda rows: None), \
                mock.patch("tokenpak.licensing.usage_meter.record_usage",
                           lambda **kw: None):
            for _ in range(N):
                self.meter.record("claude-sonnet-4-6", 10, 1, 0, "chat")

            # Old design: one daemon Thread appended to _pending_threads per
            # call (cleared only in flush()).  New design never appends on the
            # per-request path.
            self.assertLessEqual(
                len(self.meter._pending_threads), 1,
                "record() must not append a background thread per call",
            )
            # A single long-lived writer thread, not ~N live threads.
            self.assertLess(
                threading.active_count() - n_threads_before, 50,
                "record() must not spawn one live thread per call",
            )
            # The write queue is bounded by construction.
            self.assertLessEqual(
                self.meter._write_queue.qsize(), 10_000,
                "write queue must stay bounded",
            )

    def test_records_are_persisted_via_the_background_writer(self):
        """Rows enqueued by record() are written, and flush() drains them."""
        for _ in range(5):
            self.meter.record("claude-haiku-4-5", 100, 10, 5, "chat")
        self.meter.flush(timeout=5.0)
        with sqlite3.connect(self.db_path) as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM usage").fetchone()
        self.assertEqual(count, 5)

    def test_read_after_write_is_consistent_without_flush(self):
        """get_daily_summary() drains pending writes internally, so a read
        issued immediately after record() — no flush(), no sleep() — sees every
        row.  Pre-patch the read raced the background writer and under-counted;
        the in-read drain makes it deterministic and fixes the production
        under-count on a summary/dashboard read right after a request.

        The writer is slowed so it cannot keep up with the burst, guaranteeing
        rows are still pending when the read is issued — which makes the
        pre-patch race deterministic rather than timing-dependent.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        real_write_rows = self.meter._write_rows

        def slow_write_rows(rows):
            time.sleep(0.05)
            real_write_rows(rows)

        N = 25
        with mock.patch.object(self.meter, "_write_rows", slow_write_rows):
            for _ in range(N):
                self.meter.record("claude-sonnet-4-6", 100, 10, 5, "chat")
            # No flush, no sleep: get_daily_summary() must drain the queue.
            summary = self.meter.get_daily_summary(today)

        self.assertEqual(summary["total_requests"], N)
        self.assertEqual(summary["total_input_tokens"], 100 * N)


# --- connection-lifecycle / close-balance under injected errors -------------

_BAL = {"opened": 0, "closed": 0, "fail": False}
_REAL_CONNECT = sqlite3.connect


class _TrackingConnection(sqlite3.Connection):
    """A sqlite3 connection that counts open/close and can inject failures.

    When ``_BAL['fail']`` is set, the first non-PRAGMA ``execute`` (and any
    ``cursor()``) raises ``OperationalError`` — simulating a mid-method DB
    error.  The ``close`` counter then proves the connection was still closed.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _BAL["opened"] += 1

    def close(self):
        _BAL["closed"] += 1
        super().close()

    def execute(self, sql, *args, **kwargs):
        if _BAL["fail"] and not str(sql).lstrip().upper().startswith("PRAGMA"):
            raise sqlite3.OperationalError("injected error")
        return super().execute(sql, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        if _BAL["fail"]:
            raise sqlite3.OperationalError("injected error")
        return super().cursor(*args, **kwargs)


def _tracking_connect(*args, **kwargs):
    kwargs["factory"] = _TrackingConnection
    return _REAL_CONNECT(*args, **kwargs)


class TestConnectionCloseBalance(unittest.TestCase):
    """Every touched module must close its connection even when a DB op raises
    mid-method (contextlib.closing), so opens == closes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        _BAL.update(opened=0, closed=0, fail=False)

    def tearDown(self):
        _BAL["fail"] = False
        self._tmp.cleanup()

    def _assert_balanced(self, label, call):
        """Run ``call`` with connection tracking + injected failure active and
        assert at least one connection opened and all opened were closed."""
        _BAL.update(opened=0, closed=0, fail=True)
        with mock.patch("sqlite3.connect", side_effect=_tracking_connect):
            try:
                call()
            except sqlite3.OperationalError:
                pass  # some callers swallow the error, others propagate it
        _BAL["fail"] = False
        self.assertGreaterEqual(_BAL["opened"], 1, f"{label}: expected a connection to open")
        self.assertEqual(
            _BAL["opened"], _BAL["closed"],
            f"{label}: connection leaked ({_BAL['opened']} opened / {_BAL['closed']} closed)",
        )

    def test_metering_write_row(self):
        meter = UsageMeter(key_id="k", db_path=self.dir / "usage.db")
        meter._writer_stop.set()
        meter._writer_thread.join(timeout=2)
        self._assert_balanced(
            "metering._write_row",
            lambda: meter._write_row(
                ("k", "2026-06-21T00:00:00+00:00", "m", 1, 1, 0, "chat")
            ),
        )

    def test_artifact_store_retrieve(self):
        from tokenpak.telemetry.artifact_store import ArtifactStore

        store = ArtifactStore(db_path=str(self.dir / "artifacts.db"))
        self._assert_balanced(
            "artifact_store.retrieve_artifact",
            lambda: store.retrieve_artifact("missing-id"),
        )

    def test_anomalies_ensure_schema(self):
        from tokenpak.telemetry.integrity.anomalies import AnomalyDetector

        det = AnomalyDetector(db_path=str(self.dir / "anom.db"))
        self._assert_balanced("anomalies._ensure_schema", det._ensure_schema)

    def test_reconciliation_ensure_schema(self):
        from tokenpak.telemetry.integrity.reconciliation import ReconciliationManager

        mgr = ReconciliationManager(db_path=str(self.dir / "recon.db"))
        self._assert_balanced("reconciliation._ensure_schema", mgr._ensure_schema)

    def test_dashboard_detect_cost_spike(self):
        from tokenpak.telemetry.dashboard.dashboard import _detect_cost_spike

        # Build the schema first (unpatched) so the failing path exercises the
        # closing() wrapper rather than a missing-table error.
        db = self.dir / "dash.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS requests (timestamp TEXT, estimated_cost REAL)"
            )
            conn.commit()
        self._assert_balanced(
            "dashboard._detect_cost_spike",
            lambda: _detect_cost_spike(str(db)),
        )


if __name__ == "__main__":
    unittest.main()
