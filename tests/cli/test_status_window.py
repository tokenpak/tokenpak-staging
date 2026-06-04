"""Coverage for `tokenpak status` time-window selection (§B/§C).

`tokenpak status` defaults to today's local calendar day, accepts a canonical
``--window <N>m|<N>h|<N>d|<N>mo`` selector, and ``--all`` for full history.
These tests pin the window parser, the shared SQL clause builder, the labels,
and end-to-end period filtering against a real monitor.db.
"""

from __future__ import annotations

import sqlite3

import click
import pytest

from tokenpak.cli.commands import status as st

# --- parser -----------------------------------------------------------------

@pytest.mark.parametrize("value", ["30m", "4h", "7d", "2mo", "1m", "90d", "12mo"])
def test_parse_window_accepts_canonical_units(value: str) -> None:
    assert st._parse_window(value) == value


@pytest.mark.parametrize("value", ["4", "4hours", "1 hour", "-1h", "4hr", "h", "", "1y"])
def test_parse_window_rejects_non_canonical(value: str) -> None:
    with pytest.raises(click.BadParameter):
        st._parse_window(value)


# --- clause builder ---------------------------------------------------------

def test_window_clause_all_time_has_no_filter() -> None:
    assert st._window_clause(None) == ("", [])


def test_window_clause_today_uses_local_calendar_day() -> None:
    where, params = st._window_clause("today")
    assert "datetime(timestamp, 'localtime')" in where
    assert "date('now', 'localtime')" in where
    assert params == []


@pytest.mark.parametrize(
    "period,expected",
    [
        ("1h", "-1 hours"),
        ("24h", "-1 days"),
        ("7d", "-7 days"),
        ("30d", "-30 days"),
        ("30m", "-30 minutes"),
        ("4h", "-4 hours"),
        ("90d", "-90 days"),
        ("2mo", "-2 months"),
        ("30h_custom", "-30 hours"),
    ],
)
def test_window_clause_maps_to_sql_modifier(period: str, expected: str) -> None:
    where, params = st._window_clause(period)
    assert where == "WHERE timestamp >= datetime('now', ?)"
    assert params == [expected]


# --- labels -----------------------------------------------------------------

@pytest.mark.parametrize(
    "period,label",
    [
        (None, "all time"),
        ("today", "today"),
        ("24h", "last 24h"),
        ("7d", "last 7d"),
        ("2mo", "last 2mo"),
        ("30h_custom", "last 30h"),
    ],
)
def test_window_label(period, label) -> None:
    assert st._window_label(period) == label


# --- end-to-end filtering against a real monitor.db -------------------------

def _make_db(tmp_path):
    db = tmp_path / "monitor.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost REAL,
            protected_tokens INTEGER,
            compressed_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_creation_tokens INTEGER
        )
        """
    )
    # Use SQLite-relative timestamps so the test is timezone-robust:
    # one row now, one 3 days ago, one 10 days ago.
    conn.execute(
        "INSERT INTO requests (timestamp,model,input_tokens,estimated_cost) "
        "VALUES (datetime('now'), 'model-recent', 1000, 0.10)"
    )
    conn.execute(
        "INSERT INTO requests (timestamp,model,input_tokens,estimated_cost) "
        "VALUES (datetime('now','-3 days'), 'model-mid', 1000, 0.10)"
    )
    conn.execute(
        "INSERT INTO requests (timestamp,model,input_tokens,estimated_cost) "
        "VALUES (datetime('now','-10 days'), 'model-old', 1000, 0.10)"
    )
    conn.commit()
    conn.close()
    return str(db)


def _labels(db, period):
    return {r["label"] for r in st._query_breakdown(db, "model", period=period)}


def test_breakdown_period_filtering(tmp_path) -> None:
    db = _make_db(tmp_path)
    assert _labels(db, None) == {"model-recent", "model-mid", "model-old"}
    assert _labels(db, "today") == {"model-recent"}
    assert _labels(db, "30m") == {"model-recent"}
    assert _labels(db, "7d") == {"model-recent", "model-mid"}
    assert _labels(db, "30d") == {"model-recent", "model-mid", "model-old"}


def test_fleet_savings_accepts_window_tokens(tmp_path) -> None:
    db = _make_db(tmp_path)
    # Narrower window returns fewer or equal models, and never errors.
    today = st._calculate_fleet_savings(db_path=db, period="today")
    all_time = st._calculate_fleet_savings(db_path=db, period=None)
    assert "error" not in today
    assert "error" not in all_time
    assert today["period"] == "today"
    assert len(today["models"]) <= len(all_time["models"])
