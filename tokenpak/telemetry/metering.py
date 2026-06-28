"""
Usage Metering — License-keyed tracking of tokens, requests, and models.

Tracks usage per license_id for daily reporting to license server.
Enables usage-based pricing for Team/Enterprise tiers.
"""

import logging
import queue
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Python 3.12 deprecated the default date/datetime adapters/converters.
# Register explicit ones so sqlite3 doesn't emit DeprecationWarning.
sqlite3.register_adapter(date, lambda d: d.isoformat())
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())
sqlite3.register_converter("date", lambda b: date.fromisoformat(b.decode()))
sqlite3.register_converter("datetime", lambda b: datetime.fromisoformat(b.decode()))

# Bounded background-writer queue size.  When the queue is full the writer
# path falls back to a synchronous write so records are never dropped and the
# in-flight footprint stays bounded (no unreaped per-request threads).
_WRITE_QUEUE_MAX_SIZE = 10000
_WRITE_BATCH_SIZE = 256


@dataclass
class UsageRecord:
    """A single usage record."""

    model: str
    input_tokens: int
    output_tokens: int
    saved_tokens: int
    request_type: str
    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).isoformat()


class UsageMeter:
    """
    Per-license usage collector.

    Tracks:
    - Tokens processed (input, output, saved)
    - Requests made
    - Models used

    Reports daily to license server.
    """

    def __init__(self, key_id: str, db_path: Optional[Path] = None):
        """
        Initialize meter for a license key.

        Args:
            key_id: License key ID to track
            db_path: Path to SQLite database (default: ~/.tokenpak/usage.db)
        """
        self.key_id = key_id

        if db_path is None:
            db_path = Path.home() / ".tokenpak" / "usage.db"

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        # Retained for backward compatibility; the single bounded-queue writer
        # below means this list no longer grows on the per-request path.
        self._pending_threads: list = []
        self._pending_lock = threading.Lock()
        self._init_schema()

        # Single bounded-queue background writer replaces the previous
        # one-daemon-thread-per-record() design, which grew unbounded because
        # _pending_threads was only ever cleared in flush().
        self._write_queue: "queue.Queue" = queue.Queue(maxsize=_WRITE_QUEUE_MAX_SIZE)
        self._writer_stop = threading.Event()
        self._writer_thread = threading.Thread(
            target=self._writer_worker,
            daemon=True,
            name=f"UsageMeter-writer-{key_id}",
        )
        self._writer_thread.start()

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with a 30-second busy timeout.

        WAL mode is initialized once in ``_init_schema``. It persists at the
        database level, while ``synchronous=NORMAL`` is connection-local and is
        applied to each connection.
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self):
        """Create SQLite schema if not exists."""
        with closing(self._connect()) as conn:
            # WAL prevents read/write conflicts under concurrent thread access.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    model TEXT,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    saved_tokens INTEGER DEFAULT 0,
                    request_type TEXT,
                    reported BOOLEAN DEFAULT 0
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_key_id ON usage(key_id);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON usage(timestamp);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_reported ON usage(reported);
            """)
            conn.commit()

    def _write_rows(self, rows: list[tuple]) -> None:
        """Persist usage rows in one short-lived SQLite transaction.

        Serialized via ``self._lock``; the connection is always closed via
        ``contextlib.closing`` so no descriptor leaks on the write path.
        """
        if not rows:
            return
        with self._lock:
            with closing(self._connect()) as conn:
                for row in rows:
                    conn.execute(
                        """
                        INSERT INTO usage
                        (key_id, timestamp, model, input_tokens, output_tokens, saved_tokens, request_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        row,
                    )
                conn.commit()

    def _write_row(self, row: tuple) -> None:
        """Persist a single usage row."""
        self._write_rows([row])

    def _writer_worker(self) -> None:
        """Drain the bounded write queue on a single long-lived daemon thread."""
        while not self._writer_stop.is_set():
            try:
                row = self._write_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if row is None:  # poison pill
                self._write_queue.task_done()
                break
            rows = [row]
            stop_after_batch = False
            while len(rows) < _WRITE_BATCH_SIZE:
                try:
                    row = self._write_queue.get_nowait()
                except queue.Empty:
                    break
                if row is None:
                    self._write_queue.task_done()
                    stop_after_batch = True
                    break
                rows.append(row)
            try:
                self._write_rows(rows)
            except Exception:  # pragma: no cover — defensive
                logger.debug("usage write failed", exc_info=True)
            finally:
                for _ in rows:
                    self._write_queue.task_done()
            if stop_after_batch:
                break

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        saved_tokens: int,
        request_type: str,
    ) -> None:
        """
        Record a single request's usage.

        Called after each request completes. Non-blocking.

        Also forwards the event to the license-server usage meter
        (``tokenpak.licensing.usage_meter``) so that token counts keyed by
        ``license_id`` reach the license server with graceful degradation.
        Forwarding is best-effort and silent on failure.

        Args:
            model: Model name (e.g., "claude-sonnet")
            input_tokens: Input tokens processed
            output_tokens: Output tokens generated
            saved_tokens: Tokens saved by compression
            request_type: Type of request (e.g., "chat", "completion")
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        row = (
            self.key_id,
            timestamp,
            model,
            input_tokens,
            output_tokens,
            saved_tokens,
            request_type,
        )

        # Hand the row to the single background writer (non-blocking).  If the
        # bounded queue is full, write synchronously rather than spawn an
        # unreaped thread or drop the record — keeps the footprint bounded.
        try:
            self._write_queue.put_nowait(row)
        except queue.Full:
            self._write_row(row)

        # License bridge: forward the event to the license-server usage meter
        # so tokens reach the central /usage endpoint keyed by license_id.
        # Lazy import + broad try/except so a missing/broken bridge module
        # never breaks the local telemetry path.
        try:
            from tokenpak.licensing import usage_meter as _license_usage

            _license_usage.record_usage(
                tokens_in=input_tokens,
                tokens_out=output_tokens,
                model=model,
                license_id=self.key_id,
            )
        except Exception:  # pragma: no cover — defensive
            logger.debug("license usage_meter forwarding skipped", exc_info=True)

    def _drain_pending_writes(self, timeout: float = 5.0) -> None:
        """Block until the background writer has committed every row queued so
        far, giving readers read-after-write consistency.

        ``record()`` is non-blocking — it hands the row to the bounded queue
        and returns — so a read issued right after a record could otherwise
        race the still-pending write and under-count.  Read paths call this
        first to wait out any in-flight writes.  Bounded by ``timeout`` so a
        stuck writer can never hang a reader indefinitely.
        """
        q = self._write_queue
        deadline = time.monotonic() + timeout
        with q.all_tasks_done:
            while q.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                q.all_tasks_done.wait(remaining)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for queued background writes to be committed.

        Useful in tests to ensure all records are persisted before assertions.
        """
        deadline = time.monotonic() + timeout
        self._drain_pending_writes(timeout)
        # Legacy path: join any straggler threads (defensive; normally empty).
        with self._pending_lock:
            threads = list(self._pending_threads)
            self._pending_threads.clear()
        for t in threads:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)

    def get_daily_summary(self, date: str) -> Dict[str, Any]:
        """
        Aggregate usage for a given date (YYYY-MM-DD).

        Args:
            date: Date string in YYYY-MM-DD format

        Returns:
            Dictionary with aggregated usage:
            {
                "date": "2026-03-22",
                "key_id": "test-key",
                "total_requests": 42,
                "total_input_tokens": 50000,
                "total_output_tokens": 10000,
                "total_saved_tokens": 5000,
                "by_model": {
                    "claude-sonnet": {
                        "requests": 30,
                        "input_tokens": 40000,
                        "output_tokens": 8000,
                        "saved_tokens": 4000
                    },
                    "claude-opus": {
                        "requests": 12,
                        "input_tokens": 10000,
                        "output_tokens": 2000,
                        "saved_tokens": 1000
                    }
                },
                "by_type": {
                    "chat": {"requests": 35, "input_tokens": 45000, ...},
                    "completion": {"requests": 7, "input_tokens": 5000, ...}
                }
            }
        """
        # Read-after-write consistency: ensure any rows queued by record() are
        # committed before we aggregate, so a summary read right after a
        # request does not under-count still-pending background writes.
        self._drain_pending_writes()
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row

            # Get all records for the day
            cursor = conn.execute(
                """
                SELECT * FROM usage
                WHERE key_id = ? AND DATE(timestamp) = ?
                """,
                (self.key_id, date),
            )
            records = cursor.fetchall()

        if not records:
            return {
                "date": date,
                "key_id": self.key_id,
                "total_requests": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_saved_tokens": 0,
                "by_model": {},
                "by_type": {},
            }

        # Aggregate by model
        by_model = {}
        by_type = {}
        total_input = 0
        total_output = 0
        total_saved = 0

        for row in records:
            model = row["model"] or "unknown"
            request_type = row["request_type"] or "unknown"
            input_tokens = row["input_tokens"] or 0
            output_tokens = row["output_tokens"] or 0
            saved_tokens = row["saved_tokens"] or 0

            # By model
            if model not in by_model:
                by_model[model] = {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "saved_tokens": 0,
                }
            by_model[model]["requests"] += 1
            by_model[model]["input_tokens"] += input_tokens
            by_model[model]["output_tokens"] += output_tokens
            by_model[model]["saved_tokens"] += saved_tokens

            # By type
            if request_type not in by_type:
                by_type[request_type] = {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "saved_tokens": 0,
                }
            by_type[request_type]["requests"] += 1
            by_type[request_type]["input_tokens"] += input_tokens
            by_type[request_type]["output_tokens"] += output_tokens
            by_type[request_type]["saved_tokens"] += saved_tokens

            # Totals
            total_input += input_tokens
            total_output += output_tokens
            total_saved += saved_tokens

        return {
            "date": date,
            "key_id": self.key_id,
            "total_requests": len(records),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_saved_tokens": total_saved,
            "by_model": by_model,
            "by_type": by_type,
        }

    def report_to_server(self, server_url: str, timeout: int = 10) -> bool:
        """
        Upload unreported usage to license server.

        Batches all unreported rows and sends as:
        POST {server_url}/usage
        {
            "key_id": "...",
            "usage": [
                {"date": "2026-03-22", "summary": {...}},
                ...
            ]
        }

        On success, marks rows as reported.
        On failure, returns False (retried on next call).

        Args:
            server_url: Base URL of license server
            timeout: Request timeout in seconds

        Returns:
            True if upload successful (or no data to report)
            False if network error or server error
        """
        # Drain pending background writes first so a report issued right after
        # a request includes those rows rather than racing the writer.
        self._drain_pending_writes()

        # Get all unreported rows, grouped by date
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                SELECT DISTINCT DATE(timestamp) as date FROM usage
                WHERE key_id = ? AND reported = 0
                ORDER BY date
                """,
                (self.key_id,),
            )
            unreported_dates = [row[0] for row in cursor.fetchall()]

        if not unreported_dates:
            logger.debug(f"No unreported usage for {self.key_id}")
            return True

        # Build payload
        usage_by_date = []
        for d in unreported_dates:
            summary = self.get_daily_summary(d)
            usage_by_date.append(summary)

        payload = {
            "key_id": self.key_id,
            "usage": usage_by_date,
        }

        # Send to server
        try:
            endpoint = server_url.rstrip("/") + "/usage"
            response = requests.post(
                endpoint,
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()

            # Mark as reported
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE usage SET reported = 1
                    WHERE key_id = ? AND DATE(timestamp) IN ({})
                    """.format(",".join("?" * len(unreported_dates))),
                    [self.key_id] + unreported_dates,
                )
                conn.commit()

            logger.info(f"Reported usage for {self.key_id}: {len(unreported_dates)} dates")
            return True

        except requests.RequestException as e:
            logger.warning(f"Failed to report usage to {server_url}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error reporting usage: {e}")
            return False

    def cleanup_old_data(self, days: int = 90) -> int:
        """
        Delete usage data older than N days (default 90).

        Returns: Number of rows deleted
        """
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                DELETE FROM usage
                WHERE key_id = ? AND DATE(timestamp) < DATE('now', '-' || ? || ' days')
                """,
                (self.key_id, days),
            )
            conn.commit()
            return cursor.rowcount


class UsageMeterManager:
    """
    Manages multiple UsageMeter instances (one per license key).

    Thread-safe singleton for use in proxy.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_manager()
        return cls._instance

    def _init_manager(self):
        """Initialize manager state."""
        self._meters: Dict[str, UsageMeter] = {}
        self._lock = threading.Lock()

    def get_meter(self, key_id: str) -> UsageMeter:
        """Get or create meter for key_id."""
        if key_id not in self._meters:
            with self._lock:
                if key_id not in self._meters:
                    self._meters[key_id] = UsageMeter(key_id)
        return self._meters[key_id]

    def record_usage(
        self,
        key_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        saved_tokens: int,
        request_type: str = "chat",
    ) -> None:
        """Record usage for a license key."""
        meter = self.get_meter(key_id)
        meter.record(model, input_tokens, output_tokens, saved_tokens, request_type)

    def get_daily_summary(self, key_id: str, date: str) -> Dict[str, Any]:
        """Get daily summary for a license key."""
        meter = self.get_meter(key_id)
        return meter.get_daily_summary(date)

    def report_all(self, server_url: str) -> Dict[str, bool]:
        """Report all pending usage for all meters. Returns {key_id: success}."""
        results = {}
        for key_id, meter in self._meters.items():
            results[key_id] = meter.report_to_server(server_url)
        return results
