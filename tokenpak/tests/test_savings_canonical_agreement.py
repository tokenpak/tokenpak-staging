# SPDX-License-Identifier: Apache-2.0
"""Canonical savings-metric agreement guard (TPK-SAVINGS-001).

P0 trust linchpin: a single install must never report different savings numbers
across ``doctor`` / ``savings`` / ``status`` / ``cost``. These tests pin the
conservative attribution rule (rows with ``compressed_tokens == 0`` or
``cache_origin != 'proxy'`` contribute **0 saved** — never the historical 100%
over-claim) and assert every savings-reporting surface derives its figure from
the one canonical ``telemetry.savings.compute_savings``.

On this (staging) tree the live surfaces wire up as:
  * ``status`` / ``doctor``  -> ``status._calculate_fleet_savings`` which now
    DELEGATES to ``compute_savings`` (doctor calls it with ``period="today"``);
  * live ``savings`` / ``cost`` -> ``_cli_core.cmd_savings`` / ``cmd_cost`` and
    the bare ``tokenpak`` summary call ``compute_savings`` directly.

These tests FAIL before the fix (``compute_savings`` does not exist, so the
module errors on import) and PASS after it.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

# Imported at module load so the whole file ERRORS pre-fix (compute_savings did
# not exist before this change).
from tokenpak.telemetry.savings import (
    SavingsResult,
    _savings_window_spec,
    compute_savings,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    compressed_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    protected_tokens INTEGER,
    cache_origin TEXT,
    estimated_cost REAL
)
"""


def _make_db(tmp_path, rows):
    """Create a monitor.db-shaped requests table with the given rows."""
    db = tmp_path / "monitor.db"
    conn = sqlite3.connect(str(db))
    conn.execute(_SCHEMA)
    conn.executemany(
        "INSERT INTO requests "
        "(timestamp, model, input_tokens, output_tokens, compressed_tokens, "
        " cache_read_tokens, cache_creation_tokens, protected_tokens, "
        " cache_origin, estimated_cost) "
        "VALUES (datetime('now'), ?, ?, ?, ?, ?, 0, 0, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(db)


# (model, input, output, compressed, cache_read, cache_origin, est_cost)
_PROXY_ROWS = [
    ("claude-sonnet-4-6", 10000, 2000, 5000, 3000, "proxy", 0.05),
    ("claude-sonnet-4-6", 8000, 1500, 4000, 2000, "proxy", 0.04),
    ("gpt-4o", 6000, 1000, 3000, 1000, "proxy", 0.03),
]

# Rows TokenPak must NOT credit: no compression, client/unknown cache origin.
_PASSTHROUGH_ROWS = [
    ("claude-sonnet-4-6", 10000, 2000, 0, 9000, "client", 0.05),
    ("claude-sonnet-4-6", 8000, 1500, 0, 7000, None, 0.04),
    ("gpt-4o", 6000, 1000, 0, 5000, "client", 0.03),
]


@pytest.fixture
def proxy_db(tmp_path):
    return _make_db(tmp_path, _PROXY_ROWS)


@pytest.fixture
def passthrough_db(tmp_path):
    return _make_db(tmp_path, _PASSTHROUGH_ROWS)


# ---------------------------------------------------------------------------
# Core conservative-attribution rule
# ---------------------------------------------------------------------------


def test_zero_saved_when_no_proxy_attribution(passthrough_db):
    """The bug fix: compressed_tokens==0 / cache_origin!='proxy' => 0 saved.

    Previously ``SUM(input_tokens - compressed_tokens)`` reported ~100% saved on
    exactly this data. The canonical metric reports 0.
    """
    res = compute_savings(window="all", db_path=passthrough_db)
    assert res.error is None
    assert res.requests == 3
    assert res.saved_tokens == 0
    assert res.saved_cost == 0.0
    assert res.savings_pct == 0.0  # never 100%


def test_proxy_attribution_is_credited(proxy_db):
    """Proxy-caused compression/cache produces real, positive savings."""
    res = compute_savings(window="all", db_path=proxy_db)
    assert res.error is None
    assert res.requests == 3
    # saved_tokens == sum of proxy-attributed compressed tokens
    assert res.saved_tokens == 5000 + 4000 + 3000
    assert res.saved_cost > 0.0
    assert 0.0 < res.savings_pct <= 100.0


def test_db_not_found_is_explicit(tmp_path):
    res = compute_savings(window="all", db_path=str(tmp_path / "nope.db"))
    assert res.error == "db_not_found"
    assert res.saved_cost == 0.0


def test_window_labels():
    """Each window resolves to an explicit human label."""
    cases = {
        "all": "all-time",
        None: "all-time",
        "last100": "last 100 reqs",
        "session": "this session",
        "24h": "last 24h",
        "today": "today",
        "7d": "last 7d",
        "30d_custom": "last 30d",
        "48h_custom": "last 48h",
        "week": "last 7 days",
        "month": "this month",
    }
    for window, label in cases.items():
        # Label is resolved independent of DB availability.
        res = compute_savings(window=window, db_path="/nonexistent/monitor.db")
        assert res.window_label == label, f"{window!r} -> {res.window_label!r}"


# ---------------------------------------------------------------------------
# Window-mapping parity — compute_savings must not change status windowing
# ---------------------------------------------------------------------------


def test_window_spec_matches_status_window_clause():
    """For every token ``status`` passes, compute_savings emits the SAME SQL.

    This is the guard that makes ``_calculate_fleet_savings`` delegation
    behaviour-preserving — if status's ``_window_clause`` and the canonical
    ``_savings_window_spec`` ever drift, status results would silently change.
    """
    from tokenpak.cli.commands.status import _window_clause

    for token in (None, "today", "1h", "24h", "7d", "30d", "12h_custom",
                  "30m", "4h", "14d", "2mo"):
        where_sql, params, _row_limit, _label = _savings_window_spec(token)
        s_where, s_params = _window_clause(token)
        assert where_sql == s_where, f"{token!r}: where mismatch"
        assert params == s_params, f"{token!r}: params mismatch"


# ---------------------------------------------------------------------------
# Cross-command agreement — every surface == one compute_savings
# ---------------------------------------------------------------------------


def test_status_agrees_with_canonical(proxy_db):
    """status._calculate_fleet_savings totals are sourced from compute_savings."""
    from tokenpak.cli.commands.status import _calculate_fleet_savings

    canon = compute_savings(window=None, db_path=proxy_db)
    fleet = _calculate_fleet_savings(db_path=proxy_db, period=None)
    assert "totals" in fleet
    assert fleet["totals"]["saved"] == round(canon.saved_cost, 2)
    assert fleet["totals"]["savings_pct"] == round(canon.savings_pct, 1)
    assert fleet["totals"]["requests"] == canon.requests
    assert fleet["db_rows"] == canon.db_rows


def test_doctor_engine_agrees_with_canonical(proxy_db):
    """doctor's token-savings check uses _calculate_fleet_savings(period='today').

    Assert that engine path equals compute_savings('today') so doctor is
    canonical transitively.
    """
    from tokenpak.cli.commands.status import _calculate_fleet_savings

    canon = compute_savings(window="today", db_path=proxy_db)
    report = _calculate_fleet_savings(db_path=proxy_db, period="today")
    # Both apply the identical 'today' filter; if data exists in-window the
    # totals must match exactly, and either way they never disagree.
    if report.get("error") == "no_data":
        assert canon.requests == 0 or not canon.models
    else:
        assert report["totals"]["saved"] == round(canon.saved_cost, 2)
        assert report["totals"]["savings_pct"] == round(canon.savings_pct, 1)


def test_doctor_no_longer_overclaims(passthrough_db):
    """Regression: the doctor engine reports 0 (not 100%) on passthrough data."""
    from tokenpak.cli.commands.status import _calculate_fleet_savings

    report = _calculate_fleet_savings(db_path=passthrough_db, period=None)
    assert report.get("error") != "db_not_found"
    totals = report["totals"]
    assert totals["saved"] == 0.0
    assert totals["savings_pct"] == 0.0
    assert sum(m["compressed_tokens"] for m in report["models"]) == 0


def test_live_cli_commands_route_through_compute_savings():
    """The live _cli_core surfaces (savings/cost/bare summary) call the canonical fn."""
    import tokenpak._cli_core as core

    for fn_name in ("cmd_savings", "cmd_cost"):
        src = inspect.getsource(getattr(core, fn_name))
        assert "compute_savings" in src, f"{fn_name} must call compute_savings"

    # The bare `tokenpak` summary lives in main(); it too must be canonical.
    assert "compute_savings" in inspect.getsource(core.main)


def test_result_type_is_dataclass(proxy_db):
    res = compute_savings(window="all", db_path=proxy_db)
    assert isinstance(res, SavingsResult)
    assert isinstance(res.models, list)
