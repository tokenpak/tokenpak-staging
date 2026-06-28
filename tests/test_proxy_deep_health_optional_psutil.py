"""Regression tests for optional psutil in legacy proxy deep health."""

from __future__ import annotations

import sys

from tokenpak.proxy.server import ProxyServer


def test_deep_health_degrades_without_psutil(monkeypatch):
    """A slim install without psutil must return memory.rss_mb=None, not 500."""
    monkeypatch.setitem(sys.modules, "psutil", None)

    result = ProxyServer().health(deep=True)

    assert result["memory"] == {"rss_mb": None}
