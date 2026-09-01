# SPDX-License-Identifier: Apache-2.0
"""GET /savings, /recent, /cache-stats (window_24h), and /metrics/dashboard
(window_24h) route tests.

These four surfaces feed the dashboard hero metric, cache strip, recent-
requests table, and compression chart. Each is dispatched through the real
``do_GET`` handler against a real ``ProxyServer`` instance with its monitor
pointed at a temporary SQLite database — no running proxy or network I/O
required.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from tokenpak.proxy import server as proxy_server
from tokenpak.proxy.monitor import Monitor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(tmp_path, with_monitor: bool = True) -> proxy_server.ProxyServer:
    ps = proxy_server.ProxyServer(host="127.0.0.1", port=0)
    if with_monitor:
        ps.monitor = Monitor(db_path=str(tmp_path / "monitor.db"))
    else:
        ps.monitor = None
    return ps


def _fake_handler(ps: proxy_server.ProxyServer, path: str):
    handler = object.__new__(proxy_server._ProxyHandler)
    handler.path = path
    handler.headers = {}
    handler.rfile = io.BytesIO()
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 12345)
    handler._sent_headers = []
    handler.server = SimpleNamespace(proxy_server=ps)
    handler._enforce_proxy_auth = lambda: True

    def send_response(code: int, *args, **kwargs) -> None:
        handler._status = code

    def send_header(name: str, value: str) -> None:
        handler._sent_headers.append((name, value))

    handler.send_response = send_response
    handler.send_header = send_header
    handler.end_headers = lambda: None
    return handler


def _get(ps: proxy_server.ProxyServer, path: str) -> Dict[str, Any]:
    handler = _fake_handler(ps, path)
    proxy_server._ProxyHandler.do_GET(handler)
    assert handler._status == 200
    return json.loads(handler.wfile.getvalue().decode())


def _insert_request(
    db_path,
    *,
    timestamp=None,
    model="claude-sonnet-4-6",
    input_tokens=0,
    output_tokens=0,
    estimated_cost=0.0,
    compressed_tokens=0,
    cache_read_tokens=0,
    cache_origin="proxy",
    attribution_source="unknown",
    would_have_saved=None,
):
    import sqlite3
    from datetime import datetime

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO requests (timestamp, model, input_tokens, output_tokens, "
            "estimated_cost, compressed_tokens, cache_read_tokens, cache_origin, "
            "attribution_source, would_have_saved) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                timestamp or datetime.now().isoformat(),
                model,
                input_tokens,
                output_tokens,
                estimated_cost,
                compressed_tokens,
                cache_read_tokens,
                cache_origin,
                attribution_source,
                would_have_saved if would_have_saved is not None else compressed_tokens,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /savings
# ---------------------------------------------------------------------------


class TestSavingsRoute:
    def test_unavailable_when_monitor_disabled(self, tmp_path):
        ps = _make_server(tmp_path, with_monitor=False)
        body = _get(ps, "/savings")
        assert body["available"] is False

    def test_available_with_real_data(self, tmp_path):
        from datetime import date

        ps = _make_server(tmp_path)
        today = date.today().isoformat()
        _insert_request(
            ps.monitor.db_path,
            timestamp=f"{today}T12:00:00",
            compressed_tokens=1000,
            cache_origin="proxy",
        )
        body = _get(ps, "/savings")
        assert body["available"] is True
        assert body["total_requests"] == 1
        assert body["total_tokens_saved"] >= 1000

    def test_empty_db_reports_available_with_zero_requests(self, tmp_path):
        ps = _make_server(tmp_path)
        body = _get(ps, "/savings")
        assert body["available"] is True
        assert body["total_requests"] == 0


# ---------------------------------------------------------------------------
# /recent
# ---------------------------------------------------------------------------


class TestRecentRoute:
    def test_empty_when_monitor_disabled(self, tmp_path):
        ps = _make_server(tmp_path, with_monitor=False)
        body = _get(ps, "/recent")
        assert body["requests"] == []

    def test_returns_inserted_rows(self, tmp_path):
        ps = _make_server(tmp_path)
        _insert_request(
            ps.monitor.db_path, model="claude-sonnet-4-6", input_tokens=100, output_tokens=50
        )
        body = _get(ps, "/recent")
        assert len(body["requests"]) == 1
        row = body["requests"][0]
        assert row["model"] == "claude-sonnet-4-6"
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50

    def test_limit_query_param_caps_at_20(self, tmp_path):
        ps = _make_server(tmp_path)
        for i in range(25):
            _insert_request(ps.monitor.db_path, model=f"m{i}")
        body = _get(ps, "/recent?limit=100")
        assert len(body["requests"]) <= 20

    def test_limit_query_param_respected_below_20(self, tmp_path):
        ps = _make_server(tmp_path)
        for i in range(5):
            _insert_request(ps.monitor.db_path, model=f"m{i}")
        body = _get(ps, "/recent?limit=3")
        assert len(body["requests"]) == 3

    def test_bad_limit_falls_back_to_20(self, tmp_path):
        ps = _make_server(tmp_path)
        _insert_request(ps.monitor.db_path)
        body = _get(ps, "/recent?limit=not-a-number")
        assert len(body["requests"]) == 1  # only one row exists; no crash


# ---------------------------------------------------------------------------
# /cache-stats (window_24h)
# ---------------------------------------------------------------------------


class TestCacheStatsWindow:
    def test_window_24h_present_and_not_uptime_derived(self, tmp_path):
        ps = _make_server(tmp_path)
        _insert_request(ps.monitor.db_path, cache_read_tokens=500, cache_origin="proxy")
        _insert_request(ps.monitor.db_path, cache_read_tokens=500, cache_origin="client")
        body = _get(ps, "/cache-stats")
        assert "window_24h" in body
        window = body["window_24h"]
        assert window["total_requests"] == 2
        # A hit rate derived from real cache reads, not from proxy uptime —
        # both requests carry cache_read_tokens > 0, so both windows agree
        # at 1.0 regardless of how long the process has been running.
        assert window["hit_rate"] == 1.0
        assert window["tokenpak_hit_rate"] == 0.5
        assert window["cache_read_by_origin"] == {"client": 500, "proxy": 500, "unknown": 0}

    def test_window_24h_empty_db(self, tmp_path):
        ps = _make_server(tmp_path)
        body = _get(ps, "/cache-stats")
        window = body["window_24h"]
        assert window["total_requests"] == 0
        assert window["hit_rate"] == 0.0

    def test_window_24h_unavailable_when_monitor_disabled(self, tmp_path):
        ps = _make_server(tmp_path, with_monitor=False)
        body = _get(ps, "/cache-stats")
        assert "error" in body["window_24h"]


# ---------------------------------------------------------------------------
# /metrics/dashboard (window_24h)
# ---------------------------------------------------------------------------


class TestMetricsDashboardWindow:
    def test_window_24h_present_with_real_stats(self, tmp_path):
        ps = _make_server(tmp_path)
        _insert_request(ps.monitor.db_path, input_tokens=1000, compressed_tokens=300)
        body = _get(ps, "/metrics/dashboard")
        assert "window_24h" in body
        window = body["window_24h"]
        assert window["available"] is True
        assert window["requests"] == 1
        assert window["input_tokens"] == 1000
        assert window["compressed_tokens"] == 300

    def test_window_24h_unavailable_when_monitor_disabled(self, tmp_path):
        ps = _make_server(tmp_path, with_monitor=False)
        body = _get(ps, "/metrics/dashboard")
        assert body["window_24h"]["available"] is False

    def test_sessions_key_still_present(self, tmp_path):
        ps = _make_server(tmp_path)
        body = _get(ps, "/metrics/dashboard")
        assert "sessions" in body
