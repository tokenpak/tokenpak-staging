# SPDX-License-Identifier: Apache-2.0
"""Cache-origin aggregation tests for dashboard cache truth."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from tokenpak.proxy.cache_stats import _get_cache_stats_by_window
from tokenpak.proxy.server import ForwardProxyHandler


def _create_requests_table(db_path: Path, with_origin: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    origin_column = ", cache_origin TEXT" if with_origin else ""
    conn.execute(f"""
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0
            {origin_column}
        )
    """)
    conn.commit()
    conn.close()


def test_cache_stats_counts_hits_by_origin(tmp_path: Path):
    db_path = tmp_path / "monitor.db"
    _create_requests_table(db_path, with_origin=True)

    now = datetime.now().isoformat()
    rows = [
        (now, 100, 0, "proxy"),
        (now, 200, 0, "client"),
        (now, 0, 10, "proxy"),
        (now, 50, 0, None),
        (now, 75, 0, "unexpected"),
    ]

    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO requests (timestamp, cache_read_tokens, cache_creation_tokens, cache_origin) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    stats = _get_cache_stats_by_window(hours=24, db_path=str(db_path))

    assert stats["total_requests"] == 5
    assert stats["cache_hits"] == 4
    assert stats["hit_rate"] == 0.8
    assert stats["cache_hits_by_origin"] == {
        "client": 1,
        "proxy": 1,
        "unknown": 2,
    }
    assert stats["cache_read_by_origin"] == {
        "client": 200,
        "proxy": 100,
        "unknown": 125,
    }


def test_cache_stats_without_origin_column_is_unknown(tmp_path: Path):
    db_path = tmp_path / "legacy-monitor.db"
    _create_requests_table(db_path, with_origin=False)

    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO requests (timestamp, cache_read_tokens, cache_creation_tokens) "
        "VALUES (?, ?, ?)",
        [(now, 120, 0), (now, 0, 30)],
    )
    conn.commit()
    conn.close()

    stats = _get_cache_stats_by_window(hours=24, db_path=str(db_path))

    assert stats["total_requests"] == 2
    assert stats["cache_hits"] == 1
    assert stats["cache_hits_by_origin"] == {
        "client": 0,
        "proxy": 0,
        "unknown": 1,
    }
    assert stats["cache_read_by_origin"] == {
        "client": 0,
        "proxy": 0,
        "unknown": 120,
    }


def test_metrics_dashboard_payload_includes_cache_origin_summary(tmp_path: Path):
    db_path = tmp_path / "monitor.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            session_id TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            estimated_cost REAL DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            attribution_source TEXT DEFAULT 'unknown',
            cache_origin TEXT DEFAULT 'unknown'
        )
    """)
    now = datetime.now().isoformat()
    conn.executemany(
        "INSERT INTO requests (timestamp, session_id, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, estimated_cost, latency_ms, "
        "attribution_source, cache_origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (now, "s1", 100, 10, 80, 0, 0.01, 100, "cli", "proxy"),
            (now, "s1", 90, 5, 40, 0, 0.01, 80, "cli", "client"),
            (now, "s2", 50, 5, 0, 10, 0.005, 120, "sdk", "proxy"),
        ],
    )
    conn.commit()
    conn.close()

    class _CaptureHandler(ForwardProxyHandler):
        payload: dict

        def _send_json(self, data: dict) -> None:
            self.payload = data

    handler = object.__new__(_CaptureHandler)
    handler.server = SimpleNamespace(
        proxy_server=SimpleNamespace(monitor=SimpleNamespace(db_path=str(db_path)))
    )

    handler._handle_metrics_dashboard()

    assert handler.payload["sessions"][0]["session_id"] == "s1"
    cache = handler.payload["cache"]
    assert cache["total_requests"] == 3
    assert cache["cache_hits"] == 2
    assert cache["tokenpak"] == {
        "origin": "proxy",
        "hits": 1,
        "hit_rate": 0.3333,
        "cache_read_tokens": 80,
    }
    assert cache["provider_client"] == {
        "origin": "client",
        "hits": 1,
        "hit_rate": 0.3333,
        "cache_read_tokens": 40,
    }
