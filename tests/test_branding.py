"""Tests for tokenpak.branding — dynamic license-tier badge on the wordmark.

The badge is read exclusively through the OSS licensing module and must
fail closed to a plain ``TokenPak`` wordmark on any error.
"""

import pytest

from tokenpak import branding
from tokenpak import licensing as L


def _set_license(monkeypatch, *, tier, status="active"):
    monkeypatch.setattr(
        L, "load_license", lambda *a, **k: L.License(tier=tier, status=status)
    )


def test_free_renders_plain(monkeypatch):
    _set_license(monkeypatch, tier=L.TIER_FREE)
    assert branding.tier_badge() == ""
    assert branding.product_label() == "TokenPak"
    assert branding.product_label(upper=True) == "TOKENPAK"


def test_pro_active_renders_pro(monkeypatch):
    _set_license(monkeypatch, tier=L.TIER_PRO)
    assert branding.tier_badge() == "PRO"
    assert branding.product_label() == "TokenPak PRO"
    assert branding.product_label(upper=True) == "TOKENPAK PRO"


def test_team_and_enterprise_are_tier_aware(monkeypatch):
    _set_license(monkeypatch, tier=L.TIER_TEAM)
    assert branding.tier_badge() == "TEAM"
    assert branding.product_label() == "TokenPak TEAM"

    _set_license(monkeypatch, tier=L.TIER_ENTERPRISE)
    assert branding.tier_badge() == "ENTERPRISE"
    assert branding.product_label() == "TokenPak ENTERPRISE"


@pytest.mark.parametrize("bad_status", ["expired", "revoked", "pending_cancel"])
def test_inactive_status_drops_badge(monkeypatch, bad_status):
    _set_license(monkeypatch, tier=L.TIER_PRO, status=bad_status)
    assert branding.tier_badge() == ""
    assert branding.product_label() == "TokenPak"


def test_pending_validation_keeps_badge(monkeypatch):
    _set_license(monkeypatch, tier=L.TIER_PRO, status="pending_validation")
    assert branding.tier_badge() == "PRO"


def test_fail_closed_when_licensing_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("license backend down")

    monkeypatch.setattr(L, "load_license", boom)
    assert branding.tier_badge() == ""
    assert branding.product_label() == "TokenPak"
    assert branding.product_label(upper=True) == "TOKENPAK"
