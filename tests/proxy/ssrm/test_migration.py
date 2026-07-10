"""Test that Monitor._init_db migration is idempotent + non-destructive."""

from __future__ import annotations

import os
import sqlite3
import tempfile

from tokenpak.proxy.monitor import Monitor


def test_migration_on_fresh_db_adds_all_ssrm_columns(tmp_path):
    db = tmp_path / "fresh-monitor.db"
    Monitor(str(db))
    con = sqlite3.connect(str(db))
    cols = [r[1] for r in con.execute("PRAGMA table_info(requests)")]
    ssrm_cols = sorted(c for c in cols if c.startswith("ssrm_"))
    expected = sorted([
        "ssrm_decision",
        "ssrm_effective_context_tokens",
        "ssrm_effective_context_pct",
        "ssrm_cache_read_ratio",
        "ssrm_projected_next_context_pct",
        "ssrm_fingerprint_repeat_count",
        "ssrm_session_age_turns",
        "ssrm_progress_signal",
        "ssrm_signals_json",
    ])
    assert ssrm_cols == expected


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Running Monitor._init_db twice does not raise or corrupt state.

    Note: monitor.py uses a process-global `_DB_CONNECTION` and async
    write queue. When the live proxy on the same host is concurrently
    writing to the real monitor.db, the queue worker hijacks the
    cached connection and writes go to the WRONG path. Force the
    synchronous-fallback INSERT path by neutering `_DB_WRITE_QUEUE`
    after Monitor construction, the same pattern used in
    test_phase1_no_behavior_change::test_monitor_log_path_accepts_block_decision.
    """
    import tokenpak.proxy.monitor as _mon
    db = tmp_path / "idem-monitor.db"
    Monitor(str(db))
    Monitor(str(db))
    Monitor(str(db))
    m = Monitor(str(db))
    # Force sync fallback so the row lands in OUR tmp DB regardless of
    # what other Monitor instances in this process have done.
    monkeypatch.setattr(_mon, "_DB_WRITE_QUEUE", None)
    m.log(model="claude-opus-4-7", input_tokens=10, output_tokens=5, cost=0.001,
          latency_ms=5, status_code=200, endpoint="/v1/messages")
    con = sqlite3.connect(str(db))
    cnt = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert cnt == 1


def test_migration_on_pre_phase1_db_adds_columns(tmp_path):
    """Apply migration to a DB that pre-dates Phase 1: existing rows preserved,
    new columns added with NULL/empty defaults."""
    db = str(tmp_path / "pre-phase1.db")
    # Create the schema in its v3 shape (no ssrm_* columns)
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL, model TEXT NOT NULL, request_type TEXT,
        input_tokens INTEGER, output_tokens INTEGER, estimated_cost REAL,
        latency_ms INTEGER, status_code INTEGER, endpoint TEXT,
        compilation_mode TEXT, protected_tokens INTEGER,
        compressed_tokens INTEGER, injected_tokens INTEGER DEFAULT 0,
        injected_sources TEXT DEFAULT '', cache_read_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0, would_have_saved INTEGER DEFAULT 0,
        cache_origin TEXT DEFAULT 'unknown', user_id TEXT DEFAULT ''
    )""")
    # Insert a pre-existing row via the v3 INSERT shape (no session_id even)
    con.execute(
        "INSERT INTO requests (timestamp, model, input_tokens, output_tokens, estimated_cost) "
        "VALUES (datetime('now'), 'claude-opus-4-7', 100, 10, 0.01)"
    )
    con.commit()
    con.close()

    # Now apply our migration
    Monitor(db)

    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(requests)")]
    ssrm_cols = [c for c in cols if c.startswith("ssrm_")]
    assert len(ssrm_cols) == 9
    # Existing row preserved
    rows = list(con.execute("SELECT input_tokens, ssrm_decision FROM requests"))
    assert rows[0][0] == 100
    # ssrm_decision defaults to '' for pre-existing rows
    assert rows[0][1] == ""
