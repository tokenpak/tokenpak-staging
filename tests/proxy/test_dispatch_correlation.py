"""Dispatch correlation columns + header allowlist (P-TELEMETRY-01).

Covers the additive dispatch-correlation surface:

1. ``CLAUDE_CODE_HEADER_ALLOWLIST`` is extended with the two dispatch
   headers (and ``OPENCLAW_HEADER_ALLOWLIST`` is NOT touched).
2. The ALTER migration that adds ``dispatch_job_id`` / ``dispatch_station_id``
   is idempotent (running ``Monitor`` twice does not raise).
3. The new columns default to the empty-string sentinel for legacy rows
   (rows written before the columns existed).
4. The correlation columns are populated when ``Monitor.log`` is called with
   the new kwargs, and read back verbatim.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from tokenpak.proxy.headers import (
    CLAUDE_CODE_HEADER_ALLOWLIST,
    OPENCLAW_HEADER_ALLOWLIST,
)
from tokenpak.proxy.monitor import Monitor

DISPATCH_HEADERS = (
    "x-tokenpak-dispatch-job-id",
    "x-tokenpak-dispatch-station-id",
)

DISPATCH_COLUMNS = {
    "dispatch_job_id",
    "dispatch_station_id",
}


def _columns(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("PRAGMA table_info(requests)")
        return {row[1] for row in cur.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Header allowlist extension
# ---------------------------------------------------------------------------

def test_claude_code_allowlist_contains_dispatch_headers():
    for h in DISPATCH_HEADERS:
        assert h in CLAUDE_CODE_HEADER_ALLOWLIST


def test_openclaw_allowlist_unchanged():
    # OPENCLAW_HEADER_ALLOWLIST is frozen — it must NOT gain the dispatch
    # headers (or any other entry).
    assert OPENCLAW_HEADER_ALLOWLIST == frozenset(
        ("x-api-key", "authorization", "anthropic-version", "anthropic-beta")
    )
    for h in DISPATCH_HEADERS:
        assert h not in OPENCLAW_HEADER_ALLOWLIST


# ---------------------------------------------------------------------------
# 2 + 3. Migration: fresh DB, idempotency, legacy-row defaults
# ---------------------------------------------------------------------------

def test_fresh_monitor_db_has_dispatch_columns():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "monitor.db"
        Monitor(db_path=str(db_path))
        assert DISPATCH_COLUMNS <= _columns(db_path)


def test_alter_migration_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "monitor.db"
        Monitor(db_path=str(db_path))
        Monitor(db_path=str(db_path))  # second pass — must not raise
        assert DISPATCH_COLUMNS <= _columns(db_path)


def test_legacy_db_gains_dispatch_columns_defaulting_empty():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "monitor.db"
        # Pre-create a requests table WITHOUT the dispatch columns and seed a
        # legacy row.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO requests (timestamp, model, input_tokens, output_tokens) "
            "VALUES ('2026-01-01T00:00:00', 'legacy-model', 1, 2)"
        )
        conn.commit()
        conn.close()

        # Migrating via Monitor adds the columns; the pre-existing legacy row
        # must default to the '' sentinel (never NULL-surprises a reader).
        Monitor(db_path=str(db_path))
        assert DISPATCH_COLUMNS <= _columns(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT dispatch_job_id, dispatch_station_id FROM requests "
                "WHERE model = 'legacy-model'"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("", "")


# ---------------------------------------------------------------------------
# 4. log() populates the correlation columns
# ---------------------------------------------------------------------------

def _wait_for_row(db_path: Path, model: str, timeout: float = 5.0):
    """Poll for the async-written row (Monitor.log enqueues to a background
    writer thread). Returns the (dispatch_job_id, dispatch_station_id) tuple."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT dispatch_job_id, dispatch_station_id FROM requests "
                "WHERE model = ?",
                (model,),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return row
        time.sleep(0.02)
    return None


def test_log_populates_dispatch_columns():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "monitor.db"
        mon = Monitor(db_path=str(db_path))
        mon.log(
            model="dispatch-populated",
            input_tokens=10,
            output_tokens=5,
            cost=0.01,
            latency_ms=12,
            status_code=200,
            endpoint="/v1/messages",
            dispatch_job_id="job-abc-123",
            dispatch_station_id="station-7",
        )
        row = _wait_for_row(db_path, "dispatch-populated")
        assert row == ("job-abc-123", "station-7")


def test_log_defaults_dispatch_columns_to_empty_string():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "monitor.db"
        mon = Monitor(db_path=str(db_path))
        # No dispatch kwargs — must persist the '' sentinel, not NULL.
        mon.log(
            model="dispatch-default",
            input_tokens=10,
            output_tokens=5,
            cost=0.01,
            latency_ms=12,
            status_code=200,
            endpoint="/v1/messages",
        )
        row = _wait_for_row(db_path, "dispatch-default")
        assert row == ("", "")
