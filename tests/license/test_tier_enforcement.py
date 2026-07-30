# SPDX-License-Identifier: Apache-2.0
"""Current-surface license tier enforcement tests."""

from __future__ import annotations

import pytest

from tokenpak import licensing
from tokenpak.licensing import (
    TIER_FREE,
    TIER_PRO,
    License,
    daemon_probe,
    is_feature_enabled,
    load_license,
    save_license,
    summary_for_cli,
)

_PRO_FEATURE = next(feature for feature, tier in licensing._GATES.items() if tier == TIER_PRO)


@pytest.fixture(autouse=True)
def _sandbox_license(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.delenv("TOKENPAK_LICENSE_DEV_SHIM", raising=False)
    monkeypatch.setattr(
        daemon_probe,
        "detect_daemon_state",
        lambda **_: "unavailable",
    )


def test_free_and_pending_paid_licenses_cannot_use_paid_features():
    assert (
        is_feature_enabled(
            _PRO_FEATURE,
            lic=License(tier=TIER_FREE, status="active"),
        )
        is False
    )
    assert (
        is_feature_enabled(
            _PRO_FEATURE,
            lic=License(tier=TIER_PRO, status="pending_validation"),
        )
        is False
    )
    assert (
        is_feature_enabled(
            _PRO_FEATURE,
            lic=License(tier=TIER_PRO, status="expired"),
        )
        is False
    )
    assert (
        is_feature_enabled(
            _PRO_FEATURE,
            lic=License(tier=TIER_PRO, status="revoked"),
        )
        is False
    )


def test_active_pro_license_enables_paid_features():
    assert (
        is_feature_enabled(
            _PRO_FEATURE,
            lic=License(tier=TIER_PRO, status="active"),
        )
        is True
    )


@pytest.mark.parametrize("retired_tier", ["team", "enterprise"])
def test_retired_paid_tiers_keep_their_pro_entitlement(retired_tier):
    """Issued legacy licenses remain paid entitlements, not product choices."""

    assert (
        is_feature_enabled(
            _PRO_FEATURE,
            lic=License(tier=retired_tier, status="active"),
        )
        is True
    )


def test_unknown_features_are_free_but_overrides_still_grant_named_features():
    assert is_feature_enabled("not_a_registered_feature") is True
    assert (
        is_feature_enabled(
            _PRO_FEATURE,
            lic=License(
                tier=TIER_FREE,
                status="active",
                features_override=[_PRO_FEATURE],
            ),
        )
        is True
    )


def test_load_license_defaults_to_free_for_missing_or_corrupt_store(tmp_path):
    assert load_license().tier == TIER_FREE

    license_file = tmp_path / "license.json"
    license_file.write_text("{not-json", encoding="utf-8")

    assert load_license().tier == TIER_FREE
    assert load_license().status == "active"


def test_summary_counts_enabled_gated_features_for_pro_license():
    lic = License(tier=TIER_PRO, status="active")
    summary = summary_for_cli(lic)

    expected = sum(
        1
        for required in licensing._GATES.values()
        if licensing._TIER_ORDER[required] <= licensing._TIER_ORDER[TIER_PRO]
    )
    assert summary["enabled_gated_count"] == expected
    assert summary["tier"] == TIER_PRO


def test_saved_license_without_tier_field_reads_as_free(tmp_path):
    license_file = tmp_path / "license.json"
    license_file.write_text('{"status": "active"}\n', encoding="utf-8")

    lic = load_license()

    assert lic.tier == TIER_FREE
    assert lic.status == "active"


def test_save_license_round_trips_active_tier():
    save_license(License(tier=TIER_PRO, status="active", email="buyer@example.com"))

    lic = load_license()

    assert lic.tier == TIER_PRO
    assert lic.status == "active"
    assert lic.email == "buyer@example.com"
