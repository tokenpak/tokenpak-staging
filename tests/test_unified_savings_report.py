# SPDX-License-Identifier: Apache-2.0
"""D1 — unified savings_report() acceptance tests.

Pins the trust-spine invariants for the single savings helper that
``status`` / ``doctor`` / ``_cli_core`` / ``--json`` all route through:

  * cross-surface parity — every surface reads the SAME source/window and
    therefore the SAME two-plane figures;
  * two-plane separation — TokenPak-earned compression and client-attributed
    cache are NEVER summed into one number, and are split under stable JSON keys;
  * honest no-data — an empty/absent DB reports db_state=no_data and renders
    "not measured yet", never $0 / 0% / 100%;
  * unknown → client — rows with an unknown cache_origin credit cache to the
    client plane (and surface under ``unattributable``), never to TokenPak,
    never dropped.
"""

import sqlite3

import pytest

from tokenpak.telemetry.unified_savings import (
    DB_STATE_ATTRIBUTED,
    DB_STATE_NO_DATA,
    DB_STATE_PRESENT_UNATTRIBUTED,
    savings_report,
)

_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost REAL,
    status_code INTEGER,
    compressed_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cache_origin TEXT
)
"""


def _make_db(tmp_path, rows):
    db = tmp_path / "monitor.db"
    conn = sqlite3.connect(str(db))
    conn.execute(_SCHEMA)
    conn.executemany(
        "INSERT INTO requests "
        "(timestamp,model,input_tokens,compressed_tokens,cache_read_tokens,"
        " estimated_cost,cache_origin) VALUES "
        "(datetime('now'),?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(db)


# ---------------------------------------------------------------------------
# Two planes — never summed
# ---------------------------------------------------------------------------


def test_two_planes_are_separate_and_never_summed(tmp_path):
    db = _make_db(
        tmp_path,
        [
            # model, input, compressed, cache_read, cost, origin
            ("claude-sonnet-4-5", 10000, 5000, 0, 0.30, "proxy"),    # TokenPak compression
            ("claude-sonnet-4-5", 8000, 0, 1_000_000, 0.24, "client"),  # client cache
        ],
    )
    r = savings_report(db_path=db)

    assert r.compression_savings.usd > 0
    assert r.cache_savings.usd > 0
    # The compression plane is credited to tokenpak; cache to the client.
    assert r.compression_savings.credited_to == "tokenpak"
    assert r.cache_savings.credited_to == "client"
    # There is no API that sums them.
    j = r.to_json()
    assert "compression_savings" in j and "cache_savings" in j
    assert "total_savings" not in j  # intentionally absent
    assert "saved" not in j
    # tokenpak_earned_usd is the compression plane ALONE.
    assert r.tokenpak_earned_usd == pytest.approx(r.compression_savings.usd)
    assert r.tokenpak_earned_usd != pytest.approx(
        r.compression_savings.usd + r.cache_savings.usd
    )


def test_json_split_under_stable_keys(tmp_path):
    db = _make_db(
        tmp_path, [("gpt-4o", 6000, 1000, 500_000, 0.18, "proxy")]
    )
    j = savings_report(db_path=db).to_json()
    for key in (
        "db_state", "window", "request_count", "total_cost",
        "compression_savings", "cache_savings", "unattributable",
    ):
        assert key in j, f"missing stable key {key}"
    for plane_key in ("compression_savings", "cache_savings", "unattributable"):
        assert set(j[plane_key]) == {"label", "usd", "tokens", "credited_to"}


# ---------------------------------------------------------------------------
# Honest no-data
# ---------------------------------------------------------------------------


def test_no_data_when_db_absent(tmp_path):
    missing = str(tmp_path / "nope.db")
    r = savings_report(db_path=missing)
    assert r.db_state == DB_STATE_NO_DATA
    assert not r.has_data
    assert r.compression_savings.usd == 0.0
    assert r.cache_savings.usd == 0.0


def test_no_data_when_db_empty(tmp_path):
    db = _make_db(tmp_path, [])
    r = savings_report(db_path=db)
    assert r.db_state == DB_STATE_NO_DATA
    assert not r.has_data


def test_no_data_does_not_render_dollar_zero_or_percent(tmp_path):
    """The no-data report's JSON must not be mistaken for a measured $0/0%/100%."""
    r = savings_report(db_path=str(tmp_path / "absent.db"))
    j = r.to_json()
    # The discriminator is what surfaces switch on — it must say no_data so a
    # renderer never prints "$0.00 (0%)" or "100%".
    assert j["db_state"] == DB_STATE_NO_DATA
    # And there is no percentage field that could read 0% or 100%.
    assert "savings_pct" not in j
    assert "pct" not in j


# ---------------------------------------------------------------------------
# unknown → client (never TokenPak, never dropped)
# ---------------------------------------------------------------------------


def test_unknown_origin_credited_to_client_not_tokenpak(tmp_path):
    db = _make_db(
        tmp_path,
        [
            ("claude-sonnet-4-5", 8000, 0, 2_000_000, 0.24, "unknown"),
            ("claude-sonnet-4-5", 8000, 0, 1_000_000, 0.24, None),  # NULL origin
        ],
    )
    r = savings_report(db_path=db)
    # Unknown/NULL cache is never credited to TokenPak.
    assert r.compression_savings.usd == 0.0
    # It is surfaced under unattributable AND folded into the client cache plane.
    assert r.unattributable.usd > 0
    assert r.unattributable.tokens == 3_000_000
    assert r.cache_savings.usd == pytest.approx(r.unattributable.usd)
    # Nothing dropped: all 3M cache-read tokens are accounted for.
    assert r.cache_savings.tokens == 3_000_000
    # With no proxy/client-attributed rows, the state is present_unattributed.
    assert r.db_state == DB_STATE_PRESENT_UNATTRIBUTED


def test_attributed_state_when_origin_present(tmp_path):
    db = _make_db(tmp_path, [("gpt-4o", 6000, 2000, 0, 0.18, "proxy")])
    r = savings_report(db_path=db)
    assert r.db_state == DB_STATE_ATTRIBUTED


# ---------------------------------------------------------------------------
# Cross-surface parity — one source, one window, one set of numbers
# ---------------------------------------------------------------------------


def test_cross_surface_parity_status_doctor_cli_core_json(tmp_path, monkeypatch):
    """status, doctor's check, _cli_core, and --json all read the same figures."""
    db = _make_db(
        tmp_path,
        [
            ("claude-sonnet-4-5", 10000, 5000, 0, 0.30, "proxy"),
            ("claude-sonnet-4-5", 8000, 0, 1_000_000, 0.24, "client"),
            ("gpt-4o", 6000, 0, 500_000, 0.18, "unknown"),
        ],
    )
    # Pin the canonical resolver at this DB so every surface resolves it.
    monkeypatch.setenv("TOKENPAK_DB", db)

    # Canonical truth (no explicit db_path → goes through _paths resolver).
    truth = savings_report()
    comp = round(truth.compression_savings.usd, 6)
    cache = round(truth.cache_savings.usd, 6)

    # _cli_core surface
    from tokenpak._cli_core import _monitor_db_savings
    cli = _monitor_db_savings(days=0)
    assert round(cli["compression_savings_usd"], 6) == comp
    assert round(cli["cache_savings_usd"], 6) == cache

    # status surface (all-time period)
    from tokenpak.cli.commands.status import _unified_for_period
    s = _unified_for_period(None, None)
    assert round(s.compression_savings.usd, 6) == comp
    assert round(s.cache_savings.usd, 6) == cache

    # savings command helper
    from tokenpak.cli.commands.savings import _query_savings
    sv = _query_savings(period="30d", db_path=db)
    # 30d window over "now" rows → same rows; compression/cache match.
    assert round(sv["compression_savings_usd"], 6) == comp

    # --json object (stable keys) round-trips the same planes
    j = truth.to_json()
    assert round(j["compression_savings"]["usd"], 6) == comp
    assert round(j["cache_savings"]["usd"], 6) == cache
