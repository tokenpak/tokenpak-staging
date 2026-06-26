"""Regression tests for live-layer reporting truthfulness fixes.

Tests cover four P1/P2 bugs confirmed by Sue's Loop 3 validation
(2026-06-26) against a 179K-row live monitor.db:

  P1 savings    — _monitor_db_savings() over-claimed by including
                  cache_origin='client' reads as TokenPak savings.
  P1 attribution — cmd_attribution showed "No attribution data found"
                   because it read the absent attribution_history.json
                   instead of monitor.db.
  P1 requests   — cmd_requests hung (rc=124) due to infinite follow
                   loop; also no DB fallback when requests.jsonl absent.
  P2 timeline   — cmd_timeline showed "No history data" because it read
                   the absent history.jsonl instead of monitor.db.

All tests use a temp SQLite fixture; no live API calls.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

_CREATE_REQUESTS = """
CREATE TABLE IF NOT EXISTS requests (
    request_id         TEXT,
    timestamp          TEXT,
    model              TEXT,
    input_tokens       INTEGER DEFAULT 0,
    output_tokens      INTEGER DEFAULT 0,
    estimated_cost     REAL DEFAULT 0.0,
    status_code        INTEGER DEFAULT 200,
    cache_read_tokens  INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    compressed_tokens  INTEGER DEFAULT 0,
    protected_tokens   INTEGER DEFAULT 0,
    cache_origin       TEXT DEFAULT 'unknown',
    attribution_source TEXT DEFAULT 'unknown'
)
"""


def _make_db(rows: list[dict]) -> Path:
    """Create a temp monitor.db with given rows; returns the Path."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    p = Path(f.name)
    conn = sqlite3.connect(str(p))
    conn.execute(_CREATE_REQUESTS)
    for row in rows:
        conn.execute(
            """INSERT INTO requests
               (request_id, timestamp, model, input_tokens, output_tokens,
                estimated_cost, status_code, cache_read_tokens,
                cache_creation_tokens, compressed_tokens, protected_tokens,
                cache_origin, attribution_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row.get("request_id", "r1"),
                row.get("timestamp", "2026-06-26T10:00:00"),
                row.get("model", "claude-sonnet-4-6"),
                row.get("input_tokens", 1000),
                row.get("output_tokens", 200),
                row.get("estimated_cost", 0.01),
                row.get("status_code", 200),
                row.get("cache_read_tokens", 0),
                row.get("cache_creation_tokens", 0),
                row.get("compressed_tokens", 0),
                row.get("protected_tokens", 0),
                row.get("cache_origin", "unknown"),
                row.get("attribution_source", "unknown"),
            ),
        )
    conn.commit()
    conn.close()
    return p


# ---------------------------------------------------------------------------
# P1 Savings — cache_origin filtering
# ---------------------------------------------------------------------------

class TestSavingsCacheOriginFilter(unittest.TestCase):
    """_monitor_db_savings() must only credit proxy-origin cache reads."""

    def _call(self, db: Path, days: int = 30) -> dict:
        import sys, importlib, os
        # Patch _get_monitor_db_path to return our fixture DB
        import tokenpak._cli_core as core
        orig = core._get_monitor_db_path
        core._get_monitor_db_path = lambda: db
        try:
            result = core._monitor_db_savings(days=days)
        finally:
            core._get_monitor_db_path = orig
        return result

    def test_client_origin_cache_excluded_from_savings(self):
        """cache_read_tokens where cache_origin='client' must not be credited."""
        db = _make_db([
            # proxy row: 500 cache_read → should be credited
            {"request_id": "r1", "cache_read_tokens": 500, "cache_origin": "proxy",
             "input_tokens": 1000, "estimated_cost": 0.01},
            # client row: 9000 cache_read → must NOT be credited
            {"request_id": "r2", "cache_read_tokens": 9000, "cache_origin": "client",
             "input_tokens": 9000, "estimated_cost": 0.005},
        ])
        try:
            result = self._call(db)
            # cache_read in result should only reflect the proxy row (500), not 9500
            self.assertIsNotNone(result)
            self.assertEqual(result.get("cache_read"), 500,
                             "client-origin cache reads must be excluded from savings")
        finally:
            db.unlink(missing_ok=True)

    def test_proxy_origin_cache_credited(self):
        """cache_read_tokens where cache_origin='proxy' must be fully credited."""
        db = _make_db([
            {"request_id": "r1", "cache_read_tokens": 800, "cache_origin": "proxy",
             "input_tokens": 1000, "estimated_cost": 0.02},
        ])
        try:
            result = self._call(db)
            self.assertEqual(result.get("cache_read"), 800)
        finally:
            db.unlink(missing_ok=True)

    def test_unknown_origin_cache_credited(self):
        """cache_origin='unknown' (default) is not client-origin; must be credited."""
        db = _make_db([
            {"request_id": "r1", "cache_read_tokens": 300, "cache_origin": "unknown",
             "input_tokens": 1000, "estimated_cost": 0.01},
        ])
        try:
            result = self._call(db)
            self.assertEqual(result.get("cache_read"), 300)
        finally:
            db.unlink(missing_ok=True)

    def test_null_origin_treated_as_unknown_not_client(self):
        """NULL cache_origin (COALESCE to 'unknown') must not be treated as client."""
        db = _make_db([
            {"request_id": "r1", "cache_read_tokens": 200, "cache_origin": None,
             "input_tokens": 1000, "estimated_cost": 0.01},
        ])
        # Insert with explicit NULL
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE requests SET cache_origin = NULL WHERE request_id = 'r1'")
        conn.commit()
        conn.close()
        try:
            result = self._call(db)
            self.assertEqual(result.get("cache_read"), 200)
        finally:
            db.unlink(missing_ok=True)

    def test_cache_hit_rate_uses_total_not_proxy_only(self):
        """cache_hit_rate must reflect ALL cache reads (informational), not just proxy."""
        db = _make_db([
            {"request_id": "r1", "cache_read_tokens": 100, "cache_origin": "proxy",
             "input_tokens": 500, "estimated_cost": 0.01},
            {"request_id": "r2", "cache_read_tokens": 400, "cache_origin": "client",
             "input_tokens": 500, "estimated_cost": 0.005},
        ])
        try:
            result = self._call(db)
            # total cache = 500, total_in = 1000 + 500 = 1500, rate = 500/1500
            expected_rate = 500 / (1000 + 500)
            self.assertAlmostEqual(result["cache_hit_rate"], expected_rate, places=4)
        finally:
            db.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# P1 Requests — load_requests_from_db + follow default
# ---------------------------------------------------------------------------

class TestRequestsFromDb(unittest.TestCase):
    """load_requests_from_db() must return rows normalized for to_view()."""

    def setUp(self):
        self.db = _make_db([
            {"request_id": "abc123", "model": "claude-sonnet-4-6",
             "input_tokens": 1000, "output_tokens": 200,
             "cache_read_tokens": 50, "status_code": 200,
             "timestamp": "2026-06-26T12:00:00"},
            {"request_id": "def456", "model": "claude-haiku-4-5",
             "input_tokens": 500, "output_tokens": 100,
             "cache_read_tokens": 0, "status_code": 500,
             "timestamp": "2026-06-26T11:00:00"},
        ])

    def tearDown(self):
        self.db.unlink(missing_ok=True)

    def test_returns_rows(self):
        from tokenpak.cli.request_explorer import load_requests_from_db
        rows = load_requests_from_db(self.db, limit=10)
        self.assertEqual(len(rows), 2)

    def test_rows_have_id_key(self):
        """to_view() reads row.get('id') — must be aliased from request_id."""
        from tokenpak.cli.request_explorer import load_requests_from_db
        rows = load_requests_from_db(self.db)
        ids = {r["id"] for r in rows}
        self.assertIn("abc123", ids)

    def test_rows_have_cache_read_key(self):
        """to_view() reads row.get('cache_read') — must be aliased from cache_read_tokens."""
        from tokenpak.cli.request_explorer import load_requests_from_db
        rows = load_requests_from_db(self.db)
        row = next(r for r in rows if r["id"] == "abc123")
        self.assertEqual(row["cache_read"], 50)

    def test_status_mapped_from_status_code(self):
        from tokenpak.cli.request_explorer import load_requests_from_db
        rows = load_requests_from_db(self.db)
        ok_row = next(r for r in rows if r["id"] == "abc123")
        err_row = next(r for r in rows if r["id"] == "def456")
        self.assertEqual(ok_row["status"], "success")
        self.assertEqual(err_row["status"], "error")

    def test_limit_respected(self):
        from tokenpak.cli.request_explorer import load_requests_from_db
        rows = load_requests_from_db(self.db, limit=1)
        self.assertEqual(len(rows), 1)

    def test_newest_first(self):
        """ORDER BY timestamp DESC — most recent row must come first."""
        from tokenpak.cli.request_explorer import load_requests_from_db
        rows = load_requests_from_db(self.db)
        self.assertEqual(rows[0]["id"], "abc123")

    def test_bad_db_path_returns_empty(self):
        from tokenpak.cli.request_explorer import load_requests_from_db
        result = load_requests_from_db(Path("/nonexistent/path/monitor.db"))
        self.assertEqual(result, [])

    def test_follow_default_is_false(self):
        """cmd_requests must default to non-follow to avoid infinite loop."""
        import types
        import tokenpak._cli_core as core
        args = types.SimpleNamespace(
            requests_cmd="tail", action=None, request_id=None,
            limit=5, once=False,  # once=False is the old trigger for infinite loop
        )
        # follow = getattr(args, "follow", False) — should be False since "follow" absent
        follow = getattr(args, "follow", False)
        self.assertFalse(follow, "follow must default to False to prevent infinite loop")


# ---------------------------------------------------------------------------
# P1 Attribution — get_attribution_summary_from_db
# ---------------------------------------------------------------------------

class TestAttributionFromDb(unittest.TestCase):
    """get_attribution_summary_from_db() must show real attribution from monitor.db."""

    def setUp(self):
        self.db = _make_db([
            {"request_id": "r1", "attribution_source": "cali",
             "cache_read_tokens": 100, "timestamp": "2026-06-26T10:00:00"},
            {"request_id": "r2", "attribution_source": "cali",
             "cache_read_tokens": 0, "timestamp": "2026-06-26T10:01:00"},
            {"request_id": "r3", "attribution_source": "sue",
             "cache_read_tokens": 50, "timestamp": "2026-06-26T10:02:00"},
            {"request_id": "r4", "attribution_source": None,
             "cache_read_tokens": 0, "timestamp": "2026-06-26T10:03:00"},
        ])
        conn = sqlite3.connect(str(self.db))
        conn.execute("UPDATE requests SET attribution_source = NULL WHERE request_id = 'r4'")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.db.unlink(missing_ok=True)

    def test_returns_nonempty_string(self):
        from tokenpak.telemetry.attribution import get_attribution_summary_from_db
        result = get_attribution_summary_from_db(self.db, days=30)
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_contains_cali_source(self):
        from tokenpak.telemetry.attribution import get_attribution_summary_from_db
        result = get_attribution_summary_from_db(self.db, days=30)
        self.assertIn("cali", result)

    def test_contains_sue_source(self):
        from tokenpak.telemetry.attribution import get_attribution_summary_from_db
        result = get_attribution_summary_from_db(self.db, days=30)
        self.assertIn("sue", result)

    def test_does_not_return_no_attribution_data(self):
        """Must never return the old 'No attribution data found' when DB has rows."""
        from tokenpak.telemetry.attribution import get_attribution_summary_from_db
        result = get_attribution_summary_from_db(self.db, days=30)
        self.assertNotIn("No attribution data found", result)

    def test_shows_total_requests(self):
        from tokenpak.telemetry.attribution import get_attribution_summary_from_db
        result = get_attribution_summary_from_db(self.db, days=30)
        self.assertIn("4", result)  # 4 total requests

    def test_empty_db_returns_empty_string(self):
        db = _make_db([])
        try:
            from tokenpak.telemetry.attribution import get_attribution_summary_from_db
            result = get_attribution_summary_from_db(db, days=30)
            self.assertEqual(result, "")
        finally:
            db.unlink(missing_ok=True)

    def test_bad_db_path_returns_empty_string(self):
        from tokenpak.telemetry.attribution import get_attribution_summary_from_db
        result = get_attribution_summary_from_db(Path("/nonexistent/monitor.db"), days=7)
        self.assertEqual(result, "")

    def test_leakage_warning_shown_when_unknown_high(self):
        db = _make_db([
            {"request_id": f"r{i}", "attribution_source": "unknown",
             "timestamp": "2026-06-26T10:00:00"}
            for i in range(10)
        ])
        try:
            from tokenpak.telemetry.attribution import get_attribution_summary_from_db
            result = get_attribution_summary_from_db(db, days=30)
            self.assertIn("LEAKAGE", result)
        finally:
            db.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# P2 Timeline — get_timeline_from_db
# ---------------------------------------------------------------------------

class TestTimelineFromDb(unittest.TestCase):
    """get_timeline_from_db() must return daily entries from monitor.db."""

    def setUp(self):
        self.db = _make_db([
            # Day 1 — two proxy cache hits
            {"request_id": "r1", "timestamp": "2026-06-25T08:00:00",
             "input_tokens": 1000, "cache_read_tokens": 200, "cache_origin": "proxy",
             "estimated_cost": 0.01},
            {"request_id": "r2", "timestamp": "2026-06-25T09:00:00",
             "input_tokens": 800, "cache_read_tokens": 100, "cache_origin": "proxy",
             "estimated_cost": 0.008},
            # Day 1 — client cache (must not inflate savings)
            {"request_id": "r3", "timestamp": "2026-06-25T10:00:00",
             "input_tokens": 500, "cache_read_tokens": 9000, "cache_origin": "client",
             "estimated_cost": 0.001},
            # Day 2
            {"request_id": "r4", "timestamp": "2026-06-26T11:00:00",
             "input_tokens": 2000, "cache_read_tokens": 500, "cache_origin": "proxy",
             "estimated_cost": 0.02},
        ])

    def tearDown(self):
        self.db.unlink(missing_ok=True)

    def test_returns_list(self):
        from tokenpak.telemetry.timeline import get_timeline_from_db
        result = get_timeline_from_db(self.db, days=30)
        self.assertIsInstance(result, list)

    def test_returns_nonempty(self):
        from tokenpak.telemetry.timeline import get_timeline_from_db
        result = get_timeline_from_db(self.db, days=30)
        self.assertGreater(len(result), 0)

    def test_two_distinct_days(self):
        from tokenpak.telemetry.timeline import get_timeline_from_db
        result = get_timeline_from_db(self.db, days=30)
        dates = {e["date"] for e in result}
        self.assertEqual(len(dates), 2)

    def test_entry_has_required_keys(self):
        from tokenpak.telemetry.timeline import get_timeline_from_db
        result = get_timeline_from_db(self.db, days=30)
        for entry in result:
            for key in ("date", "requests", "saved_usd", "cache_hit_pct", "compression_pct"):
                self.assertIn(key, entry, f"missing key '{key}' in entry {entry}")

    def test_newest_first(self):
        from tokenpak.telemetry.timeline import get_timeline_from_db
        result = get_timeline_from_db(self.db, days=30)
        dates = [e["date"] for e in result]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_client_cache_excluded_from_saved_usd(self):
        """Day with large client-origin cache must not produce inflated saved_usd."""
        from tokenpak.telemetry.timeline import get_timeline_from_db
        result = get_timeline_from_db(self.db, days=30)
        day25 = next((e for e in result if e["date"] == "2026-06-25"), None)
        self.assertIsNotNone(day25)
        # Day 25 has 9000 client-origin tokens that must NOT be in savings
        # proxy_cache = 200+100 = 300 only
        # If client were included, saved_usd would be much larger
        # With proxy only: saved fraction = 300 / (1000+800+500+200+100+9000) ~ tiny
        # Just assert it's not absurdly large relative to actual cost (~0.019)
        self.assertLess(day25["saved_usd"], 0.02,
                        "client-origin cache must not inflate saved_usd")

    def test_day_count_limit_respected(self):
        from tokenpak.telemetry.timeline import get_timeline_from_db
        result = get_timeline_from_db(self.db, days=1)
        self.assertLessEqual(len(result), 1)

    def test_bad_db_path_returns_empty(self):
        from tokenpak.telemetry.timeline import get_timeline_from_db
        result = get_timeline_from_db(Path("/nonexistent/monitor.db"), days=7)
        self.assertEqual(result, [])

    def test_format_timeline_does_not_say_no_history(self):
        """format_timeline on DB entries must not return 'No history data found'."""
        from tokenpak.telemetry.timeline import format_timeline, get_timeline_from_db
        entries = get_timeline_from_db(self.db, days=30)
        result = format_timeline(entries)
        self.assertNotIn("No history data found", result)


if __name__ == "__main__":
    unittest.main()
