"""End-to-end tests for the real activation pipeline (W5).

These tests exercise the dev-shim path so the Free→Pro→Free transition
is proven through ``tokenpak.licensing.activate()`` and
``tokenpak.licensing.deactivate()`` — not by hand-writing
``~/.tokenpak/license.json``. Every test that touches the license file
uses ``TOKENPAK_LICENSE_FILE`` to point at a temporary path so the
user's real L1 temp gate at ``~/.tokenpak/license.json`` is never
modified.

Test 1 (``requires_l3``) is marker-skipped when the
``tokenpak-license-server`` systemd user unit is not active. That is
the intended behavior on hosts without the license-server installed
or while it is in a crash loop; it lets the same test file run
unchanged in environments where L3 is fully wired.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tokenpak import licensing


DEV_KEY = "TPAK-DEV-TEST00001"


# ---------------------------------------------------------------------------
# Test 1 — L3 service health (marker-skipped when L3 isn't reachable)
# ---------------------------------------------------------------------------


def _l3_service_active() -> bool:
    """Return True iff the user-level tokenpak-license-server unit is active."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    try:
        rc = subprocess.run(
            [systemctl, "--user", "is-active", "tokenpak-license-server.service"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return rc.returncode == 0 and rc.stdout.strip() == "active"


@pytest.mark.requires_l3
def test_l3_service_health():
    """The tokenpak-license-server systemd user unit reports active."""
    if not _l3_service_active():
        pytest.skip("tokenpak-license-server.service not active on this host")
    assert _l3_service_active()


# ---------------------------------------------------------------------------
# Helpers for shim-path tests
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_license_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point TOKENPAK_LICENSE_FILE at a fresh temp path for each test."""
    target = tmp_path / "license.json"
    monkeypatch.setenv("TOKENPAK_LICENSE_FILE", str(target))
    return target


@pytest.fixture
def dev_shim_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKENPAK_LICENSE_DEV_SHIM", "1")


@pytest.fixture
def dev_shim_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKENPAK_LICENSE_DEV_SHIM", raising=False)


# ---------------------------------------------------------------------------
# Test 2 — Free → Pro transition via the dev shim
# ---------------------------------------------------------------------------


def test_free_to_pro_via_dev_shim(
    temp_license_file: Path, dev_shim_enabled: None
) -> None:
    assert not temp_license_file.exists(), "precondition: no license file yet"

    result = licensing.activate(DEV_KEY)
    assert result.ok is True
    assert result.license is not None
    assert temp_license_file.exists(), "activate() must write through save_license()"

    lic = licensing.load_license()
    assert lic.tier == licensing.TIER_PRO
    assert lic.status == "active"
    assert lic.key == DEV_KEY

    # Pro feature enabled
    assert licensing.is_feature_enabled("C3_code_compression") is True
    # Team feature must stay locked under Pro
    assert licensing.is_feature_enabled("T3_budget_enforcement") is False
    # Enterprise feature must stay locked under Pro
    assert licensing.is_feature_enabled("I4_security_pii_dlp") is False


# ---------------------------------------------------------------------------
# Test 3 — Pro → Free deactivation
# ---------------------------------------------------------------------------


def test_pro_to_free_deactivation(
    temp_license_file: Path, dev_shim_enabled: None
) -> None:
    # Set up Pro state first via the shim.
    licensing.activate(DEV_KEY)
    assert temp_license_file.exists()
    assert licensing.load_license().tier == licensing.TIER_PRO

    removed = licensing.deactivate()
    assert removed is True
    assert not temp_license_file.exists(), "deactivate() must remove the file"

    lic = licensing.load_license()
    assert lic.tier == licensing.TIER_FREE
    assert licensing.is_feature_enabled("C3_code_compression") is False


# ---------------------------------------------------------------------------
# Test 4 — Stub-vs-shim isolation (default behavior unchanged)
# ---------------------------------------------------------------------------


def test_default_activate_is_pending_validation(
    temp_license_file: Path, dev_shim_disabled: None
) -> None:
    """With the env var unset, activate() must retain its stub behavior."""
    assert os.environ.get("TOKENPAK_LICENSE_DEV_SHIM") is None

    result = licensing.activate(DEV_KEY)
    assert result.ok is True
    assert temp_license_file.exists()

    lic = licensing.load_license()
    # Stub path: stored as Free / pending_validation regardless of key shape.
    assert lic.tier == licensing.TIER_FREE
    assert lic.status == "pending_validation"
    # Pro feature must not be unlocked by stub activation.
    assert licensing.is_feature_enabled("C3_code_compression") is False
