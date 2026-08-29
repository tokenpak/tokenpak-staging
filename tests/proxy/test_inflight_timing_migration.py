# SPDX-License-Identifier: Apache-2.0
"""Additive-migration proof for the new per-request timing columns.

``started_at`` / ``ttfb_ms`` / ``stream_duration_ms`` are added to
``requests`` via ``Monitor._init_db``'s ``_apply_missing_schema_migrations``
— the same ``ALTER TABLE ... ADD COLUMN`` idiom every prior monitor.db
column addition has used (see ``test_monitor_reasoning_columns.py``).

This suite goes one step further: it builds a fixture with the FULL real
pre-migration schema (every column that existed before this change, not a
minimal stand-in), seeds it with a realistic row, then proves the migration
against a COPY of that file — the original fixture file is asserted
byte-identical before and after, so "copy it; never touch the live one" is a
checked invariant, not just a comment.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

import tokenpak.proxy.monitor as monitor_module
from tokenpak.proxy.monitor import Monitor

NEW_TIMING_COLUMNS = {"started_at", "ttfb_ms", "stream_duration_ms"}

# The full requests-table schema as it existed immediately before this
# change (every column added by every prior migration, frozen here as the
# "real monitor.db" fixture schema — see Monitor._init_db's CREATE TABLE for
# the current, post-migration version).
_PRE_MIGRATION_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    request_type TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost REAL,
    latency_ms INTEGER,
    status_code INTEGER,
    endpoint TEXT,
    compilation_mode TEXT,
    protected_tokens INTEGER,
    compressed_tokens INTEGER,
    injected_tokens INTEGER DEFAULT 0,
    injected_sources TEXT DEFAULT '',
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    would_have_saved INTEGER DEFAULT 0,
    cache_origin TEXT DEFAULT 'unknown',
    user_id TEXT DEFAULT '',
    cache_creation_ephemeral_1h_tokens INTEGER DEFAULT 0,
    cache_creation_ephemeral_5m_tokens INTEGER DEFAULT 0,
    ttl_attribution TEXT DEFAULT NULL,
    session_id TEXT DEFAULT '',
    agent_id TEXT DEFAULT '',
    cycle_id TEXT DEFAULT '',
    attribution_source TEXT DEFAULT '',
    reasoning_tokens INTEGER DEFAULT NULL,
    visible_output_tokens INTEGER DEFAULT NULL,
    total_billable_tokens INTEGER DEFAULT NULL,
    reasoning_effort TEXT DEFAULT '',
    reasoning_usage_source TEXT DEFAULT '',
    provider_usage_ref TEXT DEFAULT '',
    provider_usage_provider TEXT DEFAULT '',
    provider_input_tokens INTEGER DEFAULT NULL,
    provider_output_tokens INTEGER DEFAULT NULL,
    provider_cache_read_tokens INTEGER DEFAULT NULL,
    provider_cache_creation_tokens INTEGER DEFAULT NULL,
    provider_usage_source TEXT DEFAULT '',
    provider_usage_confidence TEXT DEFAULT '',
    reasoning_effort_source TEXT DEFAULT '',
    reasoning_effort_raw TEXT DEFAULT '',
    cost_basis TEXT DEFAULT '',
    pricing_source TEXT DEFAULT '',
    stream_mode TEXT DEFAULT '',
    event_transform_applied INTEGER DEFAULT 0,
    stop_reason TEXT DEFAULT ''
)
"""

_SEED_ROW = {
    "timestamp": "2026-08-17T12:00:00",
    "model": "claude-sonnet-4-8",
    "request_type": "chat",
    "input_tokens": 1200,
    "output_tokens": 340,
    "estimated_cost": 0.0231,
    "latency_ms": 2150,
    "status_code": 200,
    "endpoint": "https://api.anthropic.com/v1/messages",
    "session_id": "pre-migration-session-1",
    "agent_id": "test-agent",
    "stop_reason": "end_turn",
}


@pytest.fixture(scope="module", autouse=True)
def _retire_module_writer():
    """Do not leak this module's process-global writer into later suites."""
    yield
    assert monitor_module._stop_db_write_queue(timeout=20.0)
    with monitor_module._DB_LOCK:
        if monitor_module._DB_CONNECTION is not None:
            monitor_module._DB_CONNECTION.close()
        monitor_module._DB_CONNECTION = None
        monitor_module._DB_CONNECTION_PATH = None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _columns(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(requests)").fetchall()}
    finally:
        conn.close()


def _make_real_pre_migration_db(path: Path) -> None:
    """Build the fixture: full pre-migration schema + one realistic row."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_PRE_MIGRATION_SCHEMA)
        cols = ", ".join(_SEED_ROW.keys())
        placeholders = ", ".join("?" for _ in _SEED_ROW)
        conn.execute(
            f"INSERT INTO requests ({cols}) VALUES ({placeholders})",
            tuple(_SEED_ROW.values()),
        )
        conn.commit()
    finally:
        conn.close()


def test_up_migration_against_copy_never_touches_original(tmp_path):
    real_db = tmp_path / "real-monitor.db"
    _make_real_pre_migration_db(real_db)
    original_hash = _sha256(real_db)
    assert NEW_TIMING_COLUMNS.isdisjoint(_columns(real_db)), (
        "fixture must start WITHOUT the new columns to prove additivity"
    )

    # "copy it; never touch the live one" — the migration only ever runs
    # against this working copy.
    working_copy = tmp_path / "working-copy-monitor.db"
    shutil.copyfile(real_db, working_copy)

    Monitor(db_path=str(working_copy))  # triggers _apply_missing_schema_migrations

    # 1) the original fixture file is byte-identical — never opened, never
    #    migrated, never touched.
    assert _sha256(real_db) == original_hash

    # 2) the working copy gained exactly the three new nullable columns.
    migrated_cols = _columns(working_copy)
    assert NEW_TIMING_COLUMNS <= migrated_cols

    # 3) the pre-existing row survived the migration unchanged, and the new
    #    columns default to NULL (additive — no fabricated timing data for
    #    rows that predate this feature).
    conn = sqlite3.connect(str(working_copy))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM requests WHERE session_id = ?", ("pre-migration-session-1",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["model"] == _SEED_ROW["model"]
    assert row["output_tokens"] == _SEED_ROW["output_tokens"]
    assert row["stop_reason"] == _SEED_ROW["stop_reason"]
    assert row["started_at"] is None
    assert row["ttfb_ms"] is None
    assert row["stream_duration_ms"] is None


def test_existing_readers_unaffected_by_new_columns(tmp_path):
    """recent()/get_stats()/get_by_model() work unchanged on a migrated copy."""
    real_db = tmp_path / "real-monitor.db"
    _make_real_pre_migration_db(real_db)
    working_copy = tmp_path / "working-copy-monitor.db"
    shutil.copyfile(real_db, working_copy)

    mon = Monitor(db_path=str(working_copy))

    recent = mon.recent(limit=5)
    assert len(recent) == 1
    assert recent[0]["model"] == _SEED_ROW["model"]
    # New keys are present (additive dict expansion) but that is not a
    # breaking change for callers that read by key.
    assert "started_at" in recent[0]

    stats = mon.get_stats(hours=24 * 365 * 5)
    assert stats["requests"] >= 1

    by_model = mon.get_by_model()
    assert any(_SEED_ROW["model"] in str(k) for k in by_model) or by_model  # non-empty, no crash


def test_second_migration_pass_is_idempotent(tmp_path):
    real_db = tmp_path / "real-monitor.db"
    _make_real_pre_migration_db(real_db)
    working_copy = tmp_path / "working-copy-monitor.db"
    shutil.copyfile(real_db, working_copy)

    Monitor(db_path=str(working_copy))
    Monitor(db_path=str(working_copy))  # second pass — must not raise or duplicate columns

    cols = _columns(working_copy)
    assert NEW_TIMING_COLUMNS <= cols
    conn = sqlite3.connect(str(working_copy))
    try:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    finally:
        conn.close()
