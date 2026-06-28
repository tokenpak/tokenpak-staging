"""Regression tests for dashboard cache-metric truthfulness."""

from __future__ import annotations

from pathlib import Path

INDEX_HTML = Path(__file__).parents[2] / "tokenpak" / "dashboard" / "index.html"


def _index_html() -> str:
    return INDEX_HTML.read_text()


def test_cache_hit_rate_is_not_derived_from_uptime() -> None:
    html = _index_html()

    assert "uptime / 24" not in html
    assert "uptime_hours || 0) / 24" not in html
    assert "cachePct = Math.min" not in html


def test_cache_hit_rate_requires_cache_evidence_or_missing_state() -> None:
    html = _index_html()

    assert "function cacheHitRateFromStats(stats)" in html
    assert "stats.cache_hits" in html
    assert "stats.cache_misses" in html
    assert "stats.cache_hit_rate" in html
    assert "CACHE_STATS_URL = '/cache-stats'" in html
    assert "function mergeCacheStats(stats, cacheStats)" in html
    assert "not yet measured" in html
    assert "No cache hit evidence in session stats" in html
