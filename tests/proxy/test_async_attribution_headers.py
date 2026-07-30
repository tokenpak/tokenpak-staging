"""Async proxy attribution header handling."""

from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from tokenpak.proxy import monitor as monitor_mod
from tokenpak.proxy.monitor import Monitor
from tokenpak.proxy.server_async import _build_forward_headers, _record_telemetry


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


def _drain(db_path):
    monitor_mod._DB_WRITE_QUEUE.join()
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT agent_id, cycle_id, attribution_source FROM requests ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_async_forward_headers_strip_internal_attribution_headers():
    request = SimpleNamespace(
        headers={
            "host": "localhost:8766",
            "content-type": "application/json",
            "x-tokenpak-agent": "worker-a",
            "x-tokenpak-cycle": "interactive",
            "authorization": "Bearer upstream",
        }
    )

    headers = _build_forward_headers(request, "https://api.openai.com/v1/chat/completions")

    assert "x-tokenpak-agent" not in headers
    assert "x-tokenpak-cycle" not in headers
    assert headers["authorization"] == "Bearer upstream"
    assert headers["host"] == "api.openai.com"


def test_async_invalid_attribution_values_store_sentinels_and_log_hash(tmp_path, caplog):
    ps = _ps(tmp_path / "monitor.db")

    _record_telemetry(
        ps,
        trace=None,
        model="claude-sonnet-4-6",
        input_tokens=20,
        sent_input_tokens=20,
        output_tokens=5,
        protected_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        latency_ms=12,
        status_code=200,
        endpoint="https://api.anthropic.com/v1/messages",
        headers={
            "X-Tokenpak-Agent": "bad agent",
            "X-Tokenpak-Cycle": "bad\ncycle",
        },
    )

    rows = _drain(ps.monitor.db_path)
    assert rows == [("", "", "unknown")]
    assert "x-tokenpak-agent" in caplog.text
    assert "x-tokenpak-cycle" in caplog.text
    assert "bad agent" not in caplog.text
    assert "bad\ncycle" not in caplog.text


def test_async_partial_attribution_pair_is_not_known(tmp_path):
    ps = _ps(tmp_path / "monitor.db")

    _record_telemetry(
        ps,
        trace=None,
        model="claude-sonnet-4-6",
        input_tokens=20,
        sent_input_tokens=20,
        output_tokens=5,
        protected_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        latency_ms=12,
        status_code=200,
        endpoint="https://api.anthropic.com/v1/messages",
        headers={"X-Tokenpak-Agent": "worker-a"},
    )

    assert _drain(ps.monitor.db_path) == [("worker-a", "", "unknown")]
