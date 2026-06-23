"""Companion startup banner renders the dynamic license-tier badge.

Drives the real ``launcher._render_banner`` so the wired banner path is what
gets exercised. ``color=False`` strips ANSI so the rows can be asserted exactly.
"""

from tokenpak import licensing as L
from tokenpak.companion import launcher


def _set_license(monkeypatch, *, tier, status="active"):
    monkeypatch.setattr(
        L, "load_license", lambda *a, **k: L.License(tier=tier, status=status)
    )


def _banner(**kw):
    kw.setdefault("version", "v1.9.3")
    kw.setdefault("mode", "Balanced")
    kw.setdefault("budget", "Unlimited")
    kw.setdefault("proxy_url", "http://localhost:8766")
    kw.setdefault("color", False)
    return launcher._render_banner(**kw)


def test_banner_pro_matches_target(monkeypatch):
    _set_license(monkeypatch, tier=L.TIER_PRO)
    lines = _banner()
    # Kevin's target block, verbatim.
    assert lines[1] == "  📦 TokenPak PRO Claude Companion"
    assert lines[2] == "     TokenPak PRO v1.9.3"
    assert lines[3] == "     Ready • Mode: Balanced • Budget: Unlimited"
    assert "     Proxy active → http://localhost:8766" in lines


def test_banner_free_has_no_badge(monkeypatch):
    _set_license(monkeypatch, tier=L.TIER_FREE)
    lines = _banner()
    assert lines[1] == "  📦 TokenPak Claude Companion"
    assert lines[2] == "     TokenPak v1.9.3"
    assert not any("PRO" in line for line in lines)


def test_banner_team_is_tier_aware(monkeypatch):
    _set_license(monkeypatch, tier=L.TIER_TEAM)
    lines = _banner()
    assert lines[1] == "  📦 TokenPak TEAM Claude Companion"
    assert lines[2] == "     TokenPak TEAM v1.9.3"


def test_banner_bare_tag_and_proxy_optional(monkeypatch):
    _set_license(monkeypatch, tier=L.TIER_FREE)
    lines = _banner(bare=True, proxy_url="")
    assert lines[3] == "     Ready • Mode: Balanced • Budget: Unlimited • Bare: ON"
    assert not any("Proxy active" in line for line in lines)
