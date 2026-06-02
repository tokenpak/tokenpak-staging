"""Fixture-based snapshot tests for the confidence-tiered value report.

Covers the four acceptance scenarios from the Universal Value Reporting CLI
proposal: API-only, Codex/subscription-heavy, local-only, and mixed sessions.
Asserts explicit confidence labels and that a Codex-like fixture (raw>sent,
zero provider cache) yields non-zero modeled (estimated) value.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tokenpak.cli.commands.value_report import (
    TIER_CONFIRMED,
    TIER_ESTIMATED,
    TIER_UNPRICED,
    ValueReport,
)
from tokenpak.telemetry.pricing import PricingCatalog

# Deterministic catalog so the dollar assertions don't depend on the bundled
# pricing data. Anthropic model has a cache-read rate (confirmable); the
# Codex/OpenAI model has none (cache reads fall to unpriced).
CATALOG = PricingCatalog.from_dict(
    {
        "_meta": {"version": "test-v1"},
        "models": {
            "claude-sonnet-4-6": {
                "provider": "anthropic",
                "input": 3.0,
                "output": 15.0,
                "cache_read": 0.3,
                "cache_write": 3.75,
            },
            "gpt-5.3-codex": {
                "provider": "openai",
                "input": 1.5,
                "output": 6.0,
            },
        },
    }
)

_COLUMNS = (
    "timestamp TEXT",
    "model TEXT",
    "input_tokens INTEGER DEFAULT 0",
    "output_tokens INTEGER DEFAULT 0",
    "estimated_cost REAL DEFAULT 0.0",
    "status_code INTEGER DEFAULT 200",
    "endpoint TEXT",
    "compressed_tokens INTEGER DEFAULT 0",
    "would_have_saved INTEGER DEFAULT 0",
    "cache_read_tokens INTEGER DEFAULT 0",
    "cache_origin TEXT",
)


def _seed(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS requests "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, " + ", ".join(_COLUMNS) + ")"
    )
    keys = [
        "timestamp",
        "model",
        "input_tokens",
        "output_tokens",
        "estimated_cost",
        "status_code",
        "endpoint",
        "compressed_tokens",
        "would_have_saved",
        "cache_read_tokens",
        "cache_origin",
    ]
    defaults = {
        "timestamp": "2026-05-31T10:00:00",
        "model": "claude-sonnet-4-6",
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost": 0.0,
        "status_code": 200,
        "endpoint": "https://api.anthropic.com/v1/messages",
        "compressed_tokens": 0,
        "would_have_saved": 0,
        "cache_read_tokens": 0,
        "cache_origin": "proxy",
    }
    conn.executemany(
        f"INSERT INTO requests ({', '.join(keys)}) VALUES ({', '.join(['?'] * len(keys))})",
        [tuple((dict(defaults, **r))[k] for k in keys) for r in rows],
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Scenario 1 — API-only (Anthropic; proxy cache + compression)
# --------------------------------------------------------------------------
def test_api_only_confirmed_and_estimated(tmp_path):
    db = tmp_path / "monitor.db"
    _seed(
        db,
        [
            {
                "model": "claude-sonnet-4-6",
                "compressed_tokens": 10_000,
                "cache_read_tokens": 20_000,
                "cache_origin": "proxy",
            }
        ],
    )
    r = ValueReport.build(db_path=db, catalog=CATALOG)
    assert r.db_available is True
    # confirmed = 20000 * (3.0 - 0.3) / 1e6
    assert r.confirmed.dollars == pytest.approx(0.054)
    # estimated = 10000 * 3.0 / 1e6
    assert r.estimated.dollars == pytest.approx(0.030)
    assert r.unpriced.tokens == 0


# --------------------------------------------------------------------------
# Scenario 2 — Codex / subscription-heavy (raw>sent, zero provider cache)
# Acceptance: MUST yield non-zero modeled (estimated) value.
# --------------------------------------------------------------------------
def test_codex_subscription_yields_nonzero_modeled_value(tmp_path):
    db = tmp_path / "monitor.db"
    _seed(
        db,
        [
            {
                "model": "gpt-5.3-codex",
                "endpoint": "https://api.openai.com/v1/responses",
                "compressed_tokens": 50_000,  # raw > sent
                "cache_read_tokens": 80_000,
                "cache_origin": "client",  # platform-managed, not credited
            }
        ],
    )
    r = ValueReport.build(db_path=db, catalog=CATALOG)
    # estimated = 50000 * 1.5 / 1e6 = 0.075 — explicitly non-zero, not $0.00
    assert r.estimated.dollars == pytest.approx(0.075)
    assert r.estimated.dollars > 0.0
    # Zero provider (proxy) cache => no confirmed dollars.
    assert r.confirmed.dollars == 0.0
    # Client cache is surfaced as unpriced token efficiency.
    assert r.unpriced.tokens == 80_000


# --------------------------------------------------------------------------
# Scenario 3 — local-only (unknown/unpriced model)
# --------------------------------------------------------------------------
def test_local_only_falls_back_to_unpriced(tmp_path):
    db = tmp_path / "monitor.db"
    _seed(
        db,
        [
            {
                "model": "local-llama",
                "endpoint": "http://localhost:11434/api/chat",
                "compressed_tokens": 5_000,
                "cache_read_tokens": 0,
                "cache_origin": "unknown",
            }
        ],
    )
    r = ValueReport.build(db_path=db, catalog=CATALOG)
    assert r.confirmed.dollars == 0.0
    assert r.estimated.dollars == 0.0
    assert r.unpriced.tokens >= 5_000
    row = next(m for m in r.per_model if m.model == "local-llama")
    assert row.priced is False


# --------------------------------------------------------------------------
# Scenario 4 — mixed session
# --------------------------------------------------------------------------
def test_mixed_session_populates_all_tiers(tmp_path):
    db = tmp_path / "monitor.db"
    _seed(
        db,
        [
            {
                "model": "claude-sonnet-4-6",
                "compressed_tokens": 8_000,
                "cache_read_tokens": 10_000,
                "cache_origin": "proxy",
            },
            {
                "model": "gpt-5.3-codex",
                "endpoint": "https://api.openai.com/v1/responses",
                "compressed_tokens": 20_000,
                "cache_read_tokens": 30_000,
                "cache_origin": "client",
            },
            {
                "model": "local-llama",
                "endpoint": "http://localhost:11434",
                "compressed_tokens": 4_000,
                "cache_origin": "unknown",
            },
        ],
    )
    r = ValueReport.build(db_path=db, catalog=CATALOG)
    assert r.total_requests == 3
    assert r.confirmed.dollars > 0.0
    assert r.estimated.dollars > 0.0
    assert r.unpriced.tokens > 0
    assert len(r.per_model) == 3


# --------------------------------------------------------------------------
# Rendering — explicit confidence labels are present (snapshot-style)
# --------------------------------------------------------------------------
def test_render_concise_has_labels(tmp_path):
    db = tmp_path / "monitor.db"
    _seed(db, [{"model": "claude-sonnet-4-6", "compressed_tokens": 1_000}])
    text = ValueReport.build(db_path=db, catalog=CATALOG).render(verbose=False)
    assert "Confirmed:" in text
    assert "Estimated:" in text
    assert "Unpriced:" in text
    assert "confidence-tiered" in text


def test_render_verbose_has_per_model_table(tmp_path):
    db = tmp_path / "monitor.db"
    _seed(
        db,
        [
            {"model": "claude-sonnet-4-6", "compressed_tokens": 1_000},
            {"model": "gpt-5.3-codex", "compressed_tokens": 2_000, "cache_origin": "client"},
        ],
    )
    text = ValueReport.build(db_path=db, catalog=CATALOG).render(verbose=True)
    assert "MODEL" in text
    assert "CONFIRMED$" in text
    assert "claude-sonnet-4-6" in text


def test_explain_describes_three_tiers(tmp_path):
    db = tmp_path / "monitor.db"
    _seed(db, [{"model": "claude-sonnet-4-6", "compressed_tokens": 1_000}])
    text = ValueReport.build(db_path=db, catalog=CATALOG).explain()
    assert TIER_CONFIRMED in text
    assert TIER_ESTIMATED in text
    assert TIER_UNPRICED in text
    assert "cache_origin='proxy'" in text


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------
def test_missing_db_is_graceful(tmp_path):
    r = ValueReport.build(db_path=tmp_path / "nope.db", catalog=CATALOG)
    assert r.db_available is False
    assert r.render() == (
        "  Value: no telemetry yet (run some requests through the proxy)."
    )


def test_as_dict_is_json_serializable(tmp_path):
    db = tmp_path / "monitor.db"
    _seed(db, [{"model": "claude-sonnet-4-6", "compressed_tokens": 1_000, "cache_read_tokens": 500}])
    d = ValueReport.build(db_path=db, catalog=CATALOG).as_dict()
    json.dumps(d)  # must not raise
    assert d["tiers"][TIER_CONFIRMED]["label"] == TIER_CONFIRMED
    assert d["total_requests"] == 1


def test_since_filter_excludes_old_rows(tmp_path):
    db = tmp_path / "monitor.db"
    _seed(
        db,
        [
            {"timestamp": "2026-01-01T00:00:00", "model": "claude-sonnet-4-6", "compressed_tokens": 9_999},
            {"timestamp": "2026-05-31T12:00:00", "model": "claude-sonnet-4-6", "compressed_tokens": 1_000},
        ],
    )
    r = ValueReport.build(db_path=db, since="2026-05-01T00:00:00", catalog=CATALOG)
    assert r.total_requests == 1
    assert r.estimated.dollars == pytest.approx(0.003)  # 1000 * 3.0 / 1e6


def test_schema_drift_missing_origin_column(tmp_path):
    """Older schema with no cache_origin column: cache reads are unattributed
    and must be surfaced as unpriced rather than crashing."""
    db = tmp_path / "monitor.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY, model TEXT, "
        "compressed_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO requests (model, compressed_tokens, cache_read_tokens) VALUES (?, ?, ?)",
        ("claude-sonnet-4-6", 2_000, 7_000),
    )
    conn.commit()
    conn.close()
    r = ValueReport.build(db_path=db, catalog=CATALOG)
    assert r.db_available is True
    assert r.confirmed.dollars == 0.0  # no origin => not creditable as confirmed
    assert r.estimated.dollars == pytest.approx(0.006)  # 2000 * 3.0 / 1e6
    assert r.unpriced.tokens == 7_000


def test_real_bundled_catalog_prices_known_model(tmp_path):
    """Integration: default (bundled) catalog prices a real Anthropic model."""
    db = tmp_path / "monitor.db"
    _seed(
        db,
        [
            {
                "model": "claude-sonnet-4-6",
                "compressed_tokens": 100_000,
                "cache_read_tokens": 50_000,
                "cache_origin": "proxy",
            }
        ],
    )
    r = ValueReport.build(db_path=db)  # catalog=None => bundled
    assert r.confirmed.dollars > 0.0
    assert r.estimated.dollars > 0.0
