# SPDX-License-Identifier: Apache-2.0
"""Static dashboard checks for cache metric truthfulness."""

from __future__ import annotations

from pathlib import Path


def test_dashboard_cache_card_uses_origin_metric_not_uptime():
    index_html = Path("tokenpak/dashboard/index.html").read_text()

    assert "TokenPak Cache Hit Rate" in index_html
    assert "cacheMetricFromDashboard" in index_html
    assert "uptime / 24" not in index_html
    assert "approximation for cache health indicator" not in index_html
