# SPDX-License-Identifier: Apache-2.0
"""Structural tests for the served dashboard HTML (tokenpak/dashboard/index.html).

Asserts the seven-block information hierarchy, the four verbatim empty-state
strings, hero placement/rank, and that the cache strip is wired to real
cache-token data rather than a proxy-uptime proxy value. No live proxy or
network access is required — the file is read directly from disk.
"""

from __future__ import annotations

import re

import pytest

from tokenpak.dashboard import DASHBOARD_DIR

INDEX_HTML = DASHBOARD_DIR / "index.html"

EMPTY_STATES = {
    "no_requests": "Send a request through TokenPak to see savings here. Run `tokenpak demo` to try it now.",
    "no_creds": "No provider credentials discovered. Run `tokenpak creds doctor` to see what TokenPak found.",
    "proxy_down": "Proxy is not running. Start it with `tokenpak serve`.",
    "disconnected": "Can't reach `127.0.0.1:8766`. Check the proxy, or your firewall.",
}


@pytest.fixture(scope="module")
def html() -> str:
    assert INDEX_HTML.exists(), f"dashboard index.html not found at {INDEX_HTML}"
    return INDEX_HTML.read_text()


class TestFileExists:
    def test_index_html_exists(self):
        assert INDEX_HTML.exists()


class TestSevenBlockOrder:
    """Block order must match the amended §2 hierarchy: hero, status strip,
    compression chart, cache strip, recent requests, mode/session
    breakdown, quick actions."""

    def test_blocks_appear_in_order(self, html):
        markers = [
            ("hero", 'id="hero-section"'),
            ("status_strip", 'id="status-strip"'),
            ("compression_chart", 'id="compression-section"'),
            ("cache_strip", 'id="cache-strip"'),
            ("recent_requests", 'id="recent-requests-section"'),
            ("mode_session", 'id="mode-selector"'),
            ("quick_actions", 'id="quick-actions"'),
        ]
        positions = []
        for name, marker in markers:
            idx = html.find(marker)
            assert idx != -1, f"missing block marker for {name}: {marker}"
            positions.append((name, idx))
        ordered = [name for name, _ in positions]
        sorted_by_position = [name for name, _ in sorted(positions, key=lambda p: p[1])]
        assert ordered == sorted_by_position, f"blocks out of order: {ordered}"


class TestHeroMetric:
    def test_hero_has_no_sibling_of_equal_rank(self, html):
        # Only one element uses the hero-value class treatment for the
        # top-level savings figure (the dollars line reuses the class
        # deliberately as part of the same metric, not a competing one).
        assert html.count('class="hero-value hero-dollars"') == 1

    def test_hero_precedes_status_strip(self, html):
        assert html.find('id="hero-section"') < html.find('id="status-strip"')

    def test_hero_shows_tokens_and_dollars(self, html):
        assert 'id="hero-tokens"' in html
        assert 'id="hero-dollars"' in html

    def test_hero_uses_signal_value_color_token(self, html):
        assert "--tp-signal-value" in html
        assert "#EDE085" in html


class TestStatusStripFourStates:
    def test_all_four_signal_states_styled(self, html):
        for state in ("healthy", "idle", "degraded", "error"):
            assert f".status-dot.{state}" in html

    def test_status_strip_is_single_combined_element(self, html):
        assert html.count('id="status-strip"') == 1

    def test_status_strip_carries_all_four_facts(self, html):
        assert 'id="status-creds"' in html
        assert 'id="status-last-request"' in html
        assert 'id="status-queue"' in html
        assert 'id="status-label"' in html


class TestEmptyStatesVerbatim:
    @pytest.mark.parametrize("key,text", list(EMPTY_STATES.items()))
    def test_string_present_verbatim(self, html, key, text):
        assert text in html, f"missing verbatim §10 string for {key}"

    def test_no_bare_em_dash_placeholders_in_hero_or_recent_table(self, html):
        # The old first-run state rendered a bare "—"; the new hero/empty
        # markup must use the real copy strings instead.
        hero_block = html[html.find('id="hero-section"') : html.find('id="status-strip"')]
        assert "—</div>" not in hero_block


class TestCacheStripTruth:
    def test_cache_strip_not_labeled_from_uptime(self, html):
        cache_block = html[
            html.find('id="cache-strip"') : html.find('id="recent-requests-section"')
        ]
        assert "uptime_hours" not in cache_block
        assert "uptime" not in cache_block.lower()

    def test_cache_strip_uses_window_24h_cache_data(self, html):
        assert "cacheData.window_24h" in html or "cache_read_by_origin" in html

    def test_cache_strip_distinguishes_tokenpak_vs_provider(self, html):
        assert 'id="tokenpak-cache-hit-rate"' in html
        assert 'id="provider-cache-hit-rate"' in html

    def test_no_stray_uptime_derived_cache_hit_rate_formula(self, html):
        # The known truth violation was `Math.min(uptime_hours/24, 1)`
        # rendered as a cache hit rate. It must not reappear anywhere.
        assert not re.search(r"uptime_hours\s*\|\|\s*0\s*\)\s*/\s*24", html)


class TestRecentRequestsTable:
    def test_seven_columns_present(self, html):
        table_block = html[html.find('id="recentTable"') : html.find('id="recentTable"') + 1200]
        for column in ("Time", "Client", "Model", "Tokens in", "Tokens out", "Savings", "Origin"):
            assert f">{column}<" in table_block

    def test_recent_table_reads_from_recent_endpoint(self, html):
        assert "/recent?limit=20" in html


class TestPerformanceBudget:
    def test_refresh_interval_is_5s_not_30s(self, html):
        match = re.search(r"REFRESH_INTERVAL\s*=\s*(\d+)", html)
        assert match, "REFRESH_INTERVAL not found"
        assert int(match.group(1)) == 5000

    def test_no_external_resource_references(self, html):
        assert "http://" not in html
        assert "https://" not in html


class TestModeSessionBreakdownRetained:
    def test_mode_panel_still_present(self, html):
        assert 'id="mode-panel"' in html
        assert 'id="sessionsTable"' in html
