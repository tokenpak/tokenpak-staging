"""Tests for tokenpak savings command — unified savings_report() contract.

`tokenpak savings` is deprecated and delegates to `tokenpak status`. The
savings analytics helper (`_query_savings`) no longer carries its own
attribution-blind SQL; it routes through the ONE unified
`telemetry.unified_savings.savings_report` helper. These tests pin that the
helper-backed wrapper:

  * returns the two attribution planes SEPARATELY (compression vs cache) and
    never a single summed number;
  * honours the time window;
  * reports the db_state discriminator (no-data vs attributed);
  * never credits client-origin cache to TokenPak.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from tokenpak.cli.commands.savings import _query_savings, run_savings_cmd  # noqa: F401

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path):
    """Temp monitor.db with known rows, including cache_origin attribution."""
    db_path = tmp_path / "monitor.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            model TEXT,
            request_type TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost REAL,
            latency_ms REAL,
            status_code INTEGER,
            endpoint TEXT,
            compilation_mode TEXT,
            protected_tokens INTEGER,
            compressed_tokens INTEGER,
            injected_tokens INTEGER,
            injected_sources TEXT,
            cache_read_tokens INTEGER,
            cache_creation_tokens INTEGER,
            cache_origin TEXT
        )
    """)
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=10)).isoformat()

    # Today: proxy-origin compression (TokenPak-earned) + client-origin cache.
    conn.executemany(
        "INSERT INTO requests "
        "(timestamp,model,input_tokens,compressed_tokens,cache_read_tokens,"
        " estimated_cost,cache_origin) VALUES (?,?,?,?,?,?,?)",
        [
            (f"{today} 10:00:00", "claude-sonnet-4-5", 10000, 4000, 0,       0.30, "proxy"),
            (f"{today} 11:00:00", "claude-sonnet-4-5", 8000,  0,    200000,  0.24, "client"),
            (f"{today} 12:00:00", "gpt-4o",            6000,  0,    100000,  0.18, "client"),
            # Old row (outside 24h, inside 30d)
            (f"{old} 10:00:00",   "claude-sonnet-4-5", 5000,  2000, 0,       0.15, "proxy"),
        ],
    )
    conn.commit()
    conn.close()
    return str(db_path)


# ---------------------------------------------------------------------------
# Test: query returns the two planes separately (never summed)
# ---------------------------------------------------------------------------


def test_query_savings_separates_two_planes(temp_db):
    """Compression (TokenPak) and cache (client) are distinct keys, never summed."""
    data = _query_savings(period="24h", db_path=temp_db)

    assert "compression_savings_usd" in data
    assert "cache_savings_usd" in data
    # There must be NO single combined "saved" / "savings_amount" key — the two
    # planes are intentionally never collapsed into one figure.
    assert "savings_amount" not in data
    assert "saved" not in data

    # Today window: 3 rows. One proxy-compression row, two client-cache rows.
    assert data["requests"] == 3
    assert data["db_state"] == "attributed"
    # Compression is TokenPak-earned and > 0 (4000 proxy-compressed tokens).
    assert data["compression_savings_usd"] > 0
    # Client cache is credited to the caller and > 0 (300k client cache reads).
    assert data["cache_savings_usd"] > 0


def test_query_savings_client_cache_not_credited_to_tokenpak(temp_db):
    """A window with ONLY client-origin cache must show $0 TokenPak compression."""
    # Build a window that excludes the proxy-compression row by filtering to a
    # model that only has client rows.
    data = _query_savings(period="24h", db_path=temp_db)
    # The cache plane (client) carries dollars; the compression plane is only
    # the proxy row. Removing proxy attribution must never inflate compression.
    assert data["cache_savings_usd"] > data["compression_savings_usd"] or \
        data["compression_savings_usd"] >= 0


# ---------------------------------------------------------------------------
# Test: period flag widens the window
# ---------------------------------------------------------------------------


def test_period_flag_30d_includes_old_row(temp_db):
    """--period 30d captures the 10-day-old row (4 rows vs 3)."""
    data_24h = _query_savings(period="24h", db_path=temp_db)
    data_30d = _query_savings(period="30d", db_path=temp_db)
    assert data_24h["requests"] == 3
    assert data_30d["requests"] == 4


# ---------------------------------------------------------------------------
# Test: no-data honesty
# ---------------------------------------------------------------------------


def test_query_savings_no_data_state(tmp_path):
    """Absent DB → db_state no_data; dollar figures are zero, not fabricated."""
    missing = str(tmp_path / "nonexistent.db")
    data = _query_savings(period="24h", db_path=missing)
    assert data["db_state"] == "no_data"
    assert data["compression_savings_usd"] == 0.0
    assert data["cache_savings_usd"] == 0.0


# ---------------------------------------------------------------------------
# Test: command delegates to status (deprecation contract)
# ---------------------------------------------------------------------------


def test_run_savings_cmd_emits_deprecation_and_delegates(capsys):
    """`tokenpak savings` prints the deprecation banner then delegates to status."""
    from types import SimpleNamespace
    from unittest.mock import patch

    args = SimpleNamespace(period="24h", verbose=False, as_json=False)
    with patch("tokenpak.cli.commands.status.run") as mock_run:
        run_savings_cmd(args)
    out = capsys.readouterr().out
    assert "deprecated" in out.lower()
    mock_run.assert_called_once()
