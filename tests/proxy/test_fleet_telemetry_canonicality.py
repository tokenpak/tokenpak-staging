"""Fleet telemetry canonicality checks for proxy monitor rows."""

from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from tokenpak.proxy import monitor as monitor_mod
from tokenpak.proxy.headers import forward_headers
from tokenpak.proxy.monitor import Monitor
from tokenpak.proxy.request import ROUTE_CLAUDE_CODE
from tokenpak.proxy.server_async import _record_telemetry


def _ps(db_path):
    return SimpleNamespace(
        _session_lock=threading.Lock(),
        _compression_lock=threading.Lock(),
        _last_lock=threading.Lock(),
        _compression_ratios=[],
        session={
            "requests": 0,
            "input_tokens": 0,
            "sent_input_tokens": 0,
            "saved_tokens": 0,
            "protected_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
            "cost_saved": 0.0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        compression_stats=MagicMock(),
        trace_storage=MagicMock(),
        monitor=Monitor(str(db_path)),
        compilation_mode="hybrid",
    )


def _rows(db_path):
    monitor_mod._DB_WRITE_QUEUE.join()
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT agent_id, cycle_id, attribution_source FROM requests ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _request_columns(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    finally:
        conn.close()


def test_monitor_migrates_legacy_requests_table_with_attribution_source(tmp_path):
    db_path = tmp_path / "monitor.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                request_type TEXT,
                agent_id TEXT DEFAULT '',
                cycle_id TEXT DEFAULT ''
            )"""
        )
        conn.execute(
            "INSERT INTO requests (timestamp, model, request_type, agent_id, cycle_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-01-01T00:00:00", "claude-sonnet-4-6", "chat", "worker-a", "cycle-1"),
        )
        conn.commit()
    finally:
        conn.close()

    assert "attribution_source" not in _request_columns(db_path)

    Monitor(str(db_path))

    assert "attribution_source" in _request_columns(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT model, agent_id, cycle_id, attribution_source FROM requests"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("claude-sonnet-4-6", "worker-a", "cycle-1", "unknown")


def test_async_proxy_writes_exactly_one_header_attributed_monitor_row(tmp_path):
    ps = _ps(tmp_path / "monitor.db")

    _record_telemetry(
        ps,
        trace=None,
        model="claude-sonnet-4-6",
        input_tokens=100,
        sent_input_tokens=90,
        output_tokens=10,
        protected_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        latency_ms=25,
        status_code=200,
        endpoint="https://api.anthropic.com/v1/messages",
        headers={
            "X-Tokenpak-Agent": "worker-a",
            "X-Tokenpak-Cycle": "interactive",
        },
    )

    assert _rows(ps.monitor.db_path) == [("worker-a", "interactive", "header")]


def test_async_proxy_missing_attribution_writes_unknown_sentinel(tmp_path):
    ps = _ps(tmp_path / "monitor.db")

    _record_telemetry(
        ps,
        trace=None,
        model="claude-haiku-4-5",
        input_tokens=50,
        sent_input_tokens=50,
        output_tokens=5,
        protected_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        latency_ms=15,
        status_code=200,
        endpoint="https://api.anthropic.com/v1/messages",
        headers={},
    )

    assert _rows(ps.monitor.db_path) == [("", "", "unknown")]


def test_claude_code_passthrough_strips_attribution_headers_even_with_client_auth():
    headers = forward_headers(
        {
            "authorization": "Bearer upstream",
            "x-tokenpak-agent": "worker-a",
            "x-tokenpak-cycle": "interactive",
            "content-type": "application/json",
        },
        ROUTE_CLAUDE_CODE,
        client_has_auth=True,
    )

    assert "x-tokenpak-agent" not in {k.lower() for k in headers}
    assert "x-tokenpak-cycle" not in {k.lower() for k in headers}
    assert headers["authorization"] == "Bearer upstream"
