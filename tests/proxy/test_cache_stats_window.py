# SPDX-License-Identifier: Apache-2.0
"""Unit tests for tokenpak.proxy.cache_stats._get_cache_stats_by_window().

Covers the product-attributed tokenpak_hit_rate / tokenpak_cache_hits
fields alongside the pre-existing provider-level hit_rate, and confirms
the two are computed independently (cache_origin == 'proxy' only feeds
the tokenpak figure).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from tokenpak.proxy.cache_stats import _get_cache_stats_by_window

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path, rows):
    db_path = tmp_path / "monitor.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_origin TEXT DEFAULT 'unknown'
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO requests (timestamp, cache_read_tokens, cache_creation_tokens, cache_origin) "
            "VALUES (?, ?, ?, ?)",
            (
                r.get("timestamp", datetime.now().isoformat()),
                r.get("cache_read_tokens", 0),
                r.get("cache_creation_tokens", 0),
                r.get("cache_origin", "unknown"),
            ),
        )
    conn.commit()
    conn.close()
    return str(db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoDbPath:
    def test_missing_db_path_returns_error_dict_with_zeroed_rates(self):
        result = _get_cache_stats_by_window(hours=24, db_path=None)
        assert result["total_requests"] == 0
        assert result["hit_rate"] == 0.0
        assert result["tokenpak_hit_rate"] == 0.0
        assert "error" in result


class TestEmptyDatabase:
    def test_empty_db_all_rates_zero(self, tmp_path):
        db = _make_db(tmp_path, [])
        result = _get_cache_stats_by_window(hours=24, db_path=db)
        assert result["total_requests"] == 0
        assert result["hit_rate"] == 0.0
        assert result["tokenpak_hit_rate"] == 0.0
        assert result["cache_read_by_origin"] == {"client": 0, "proxy": 0, "unknown": 0}


class TestProviderVsTokenpakHitRate:
    def test_proxy_origin_counts_toward_both_rates(self, tmp_path):
        db = _make_db(
            tmp_path,
            [{"cache_read_tokens": 100, "cache_origin": "proxy"}],
        )
        result = _get_cache_stats_by_window(hours=24, db_path=db)
        assert result["total_requests"] == 1
        assert result["cache_hits"] == 1
        assert result["hit_rate"] == 1.0
        assert result["tokenpak_cache_hits"] == 1
        assert result["tokenpak_hit_rate"] == 1.0

    def test_client_origin_counts_toward_provider_rate_only(self, tmp_path):
        db = _make_db(
            tmp_path,
            [{"cache_read_tokens": 100, "cache_origin": "client"}],
        )
        result = _get_cache_stats_by_window(hours=24, db_path=db)
        assert result["hit_rate"] == 1.0
        assert result["tokenpak_cache_hits"] == 0
        assert result["tokenpak_hit_rate"] == 0.0

    def test_unknown_origin_counts_toward_provider_rate_only(self, tmp_path):
        db = _make_db(
            tmp_path,
            [{"cache_read_tokens": 100, "cache_origin": "unknown"}],
        )
        result = _get_cache_stats_by_window(hours=24, db_path=db)
        assert result["hit_rate"] == 1.0
        assert result["tokenpak_hit_rate"] == 0.0

    def test_mixed_origins_split_correctly(self, tmp_path):
        db = _make_db(
            tmp_path,
            [
                {"cache_read_tokens": 100, "cache_origin": "proxy"},
                {"cache_read_tokens": 100, "cache_origin": "client"},
                {"cache_read_tokens": 0, "cache_origin": "unknown"},  # no cache read → not a hit
            ],
        )
        result = _get_cache_stats_by_window(hours=24, db_path=db)
        assert result["total_requests"] == 3
        assert result["cache_hits"] == 2
        assert result["hit_rate"] == round(2 / 3, 4)
        assert result["tokenpak_cache_hits"] == 1
        assert result["tokenpak_hit_rate"] == round(1 / 3, 4)


class TestCacheReadByOrigin:
    def test_token_totals_bucketed_by_origin(self, tmp_path):
        db = _make_db(
            tmp_path,
            [
                {"cache_read_tokens": 10, "cache_origin": "proxy"},
                {"cache_read_tokens": 20, "cache_origin": "client"},
                {"cache_read_tokens": 30, "cache_origin": "unknown"},
            ],
        )
        result = _get_cache_stats_by_window(hours=24, db_path=db)
        assert result["cache_read_by_origin"] == {"client": 20, "proxy": 10, "unknown": 30}
        assert result["cache_read_tokens"] == 60


class TestWindowCutoff:
    def test_rows_outside_window_excluded(self, tmp_path):
        stale = (datetime.now() - timedelta(hours=48)).isoformat()
        fresh = datetime.now().isoformat()
        db = _make_db(
            tmp_path,
            [
                {"timestamp": stale, "cache_read_tokens": 100, "cache_origin": "proxy"},
                {"timestamp": fresh, "cache_read_tokens": 100, "cache_origin": "proxy"},
            ],
        )
        result = _get_cache_stats_by_window(hours=24, db_path=db)
        assert result["total_requests"] == 1
        assert result["tokenpak_cache_hits"] == 1


class TestLegacyRowsWithoutOriginColumn:
    def test_missing_cache_origin_column_treated_as_unknown(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO requests (timestamp, cache_read_tokens) VALUES (?, ?)",
            (datetime.now().isoformat(), 50),
        )
        conn.commit()
        conn.close()

        result = _get_cache_stats_by_window(hours=24, db_path=str(db_path))
        assert result["total_requests"] == 1
        assert result["hit_rate"] == 1.0
        assert result["tokenpak_hit_rate"] == 0.0
        assert result["cache_read_by_origin"] == {"client": 0, "proxy": 0, "unknown": 50}
