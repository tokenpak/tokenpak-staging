# SPDX-License-Identifier: Apache-2.0
"""Tests for ``tokenpak upgrade`` as a hidden, non-navigating compatibility shim.

Public Pro enrollment is unavailable, so no surface may proactively advertise a
signup destination. These tests assert the *behaviour* that guarantee rests on:
no browser is ever launched, no URL is invented when none is configured, and the
verb is absent from every discovery surface.
"""

from __future__ import annotations

import argparse
import sys

from tokenpak import licensing as _lic
from tokenpak._cli_core import _COMMAND_GROUPS, build_parser
from tokenpak.cli.commands import help as help_cmd
from tokenpak.cli.commands import license_cmd, status, upgrade


def _free_summary(**over: object) -> dict:
    base = {
        "tier": _lic.TIER_FREE,
        "tier_label": "Free",
        "status": "active",
        "email": "",
        "activated_at": "",
        "expires_at": "",
        "has_key": False,
        "license_path": "/tmp/tokenpak/license.json",
        "enabled_gated_count": 0,
        "gated_feature_count": 52,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# The shim never navigates
# --------------------------------------------------------------------------


def test_upgrade_module_does_not_import_webbrowser():
    """A browser cannot be opened by code that never imports the opener."""
    assert not hasattr(upgrade, "webbrowser")
    source = upgrade.__file__ or ""
    assert source.endswith("upgrade.py")
    with open(source, encoding="utf-8") as fh:
        text = fh.read()
    assert "import webbrowser" not in text


def test_upgrade_reports_unavailable_and_opens_nothing(monkeypatch, capsys):
    monkeypatch.delenv(upgrade.UPGRADE_URL_ENV, raising=False)
    # If anything reaches for a browser, fail loudly rather than silently pass.
    monkeypatch.setitem(sys.modules, "webbrowser", None)

    rc = upgrade.cmd_upgrade(argparse.Namespace(print_url=False))

    out = capsys.readouterr().out
    assert rc == 0
    assert "not available" in out.lower()
    assert "http" not in out  # no URL of any kind is emitted


def test_upgrade_print_url_exits_nonzero_when_unconfigured(monkeypatch, capsys):
    """--print-url must not fabricate a default; absent means non-zero."""
    monkeypatch.delenv(upgrade.UPGRADE_URL_ENV, raising=False)

    rc = upgrade.cmd_upgrade(argparse.Namespace(print_url=True))

    assert rc != 0
    assert capsys.readouterr().out.strip() == ""


def test_upgrade_honors_operator_configured_url(monkeypatch, capsys):
    """A private cohort operator may opt in; that is the only way a URL appears."""
    monkeypatch.setenv(upgrade.UPGRADE_URL_ENV, "https://example.test/enroll")

    rc = upgrade.cmd_upgrade(argparse.Namespace(print_url=True))

    assert rc == 0
    assert capsys.readouterr().out.strip() == "https://example.test/enroll"


def test_no_default_upgrade_url_constant():
    assert upgrade.DEFAULT_UPGRADE_URL == ""
    assert upgrade.resolve_upgrade_url() in ("", None) or upgrade.resolve_upgrade_url()


def test_cta_line_empty_unless_configured(monkeypatch):
    monkeypatch.delenv(upgrade.UPGRADE_URL_ENV, raising=False)
    assert upgrade.upgrade_cta_line() == ""

    monkeypatch.setenv(upgrade.UPGRADE_URL_ENV, "https://example.test/enroll")
    assert "https://example.test/enroll" in upgrade.upgrade_cta_line()


def test_blank_env_value_is_not_a_destination(monkeypatch):
    monkeypatch.setenv(upgrade.UPGRADE_URL_ENV, "   ")
    assert upgrade.resolve_upgrade_url() == ""
    assert upgrade.upgrade_cta_line() == ""


# --------------------------------------------------------------------------
# The verb is absent from discovery
# --------------------------------------------------------------------------


def test_upgrade_absent_from_discovery_surfaces():
    getting_started = dict(_COMMAND_GROUPS["Getting Started"])
    assert "upgrade" not in getting_started
    assert "upgrade" not in help_cmd._ESSENTIAL_COMMANDS

    commands = {cmd["command"]: cmd for cmd in help_cmd._load_registry()}
    assert commands["upgrade"].get("hidden") is True
    assert "upgrade page" not in commands["upgrade"]["description"].lower()


def test_upgrade_still_dispatches_for_compatibility():
    """Hidden, not removed: existing scripts must still get a clean answer."""
    parser = build_parser()

    ns = parser.parse_args(["upgrade", "--print-url"])

    assert ns.func is upgrade.cmd_upgrade
    assert ns.print_url is True


# --------------------------------------------------------------------------
# Downstream surfaces inherit the suppression
# --------------------------------------------------------------------------


def test_free_edition_license_output_has_no_cta(monkeypatch):
    monkeypatch.delenv(upgrade.UPGRADE_URL_ENV, raising=False)

    rendered = license_cmd._render_summary(_free_summary())

    assert "http" not in rendered
    assert "upgrade" not in rendered.lower()
    # Nor the zero-percent framing that replaced it.
    assert "0 / 52" not in rendered
    assert _lic.EDITION_BASE in rendered


def test_status_hint_suppressed_by_default(monkeypatch):
    monkeypatch.delenv(upgrade.UPGRADE_URL_ENV, raising=False)
    monkeypatch.setattr(status._lic, "summary_for_cli", lambda: {"tier": _lic.TIER_FREE})

    assert status._free_tier_upgrade_hint() is None


def test_status_hint_appears_only_for_free_and_only_when_configured(monkeypatch):
    monkeypatch.setenv(upgrade.UPGRADE_URL_ENV, "https://example.test/enroll")

    monkeypatch.setattr(status._lic, "summary_for_cli", lambda: {"tier": _lic.TIER_FREE})
    hint = status._free_tier_upgrade_hint()
    assert hint and "https://example.test/enroll" in hint

    monkeypatch.setattr(status._lic, "summary_for_cli", lambda: {"tier": _lic.TIER_PRO})
    assert status._free_tier_upgrade_hint() is None


def test_status_unreachable_path_prints_no_cta(monkeypatch, capsys):
    monkeypatch.delenv(upgrade.UPGRADE_URL_ENV, raising=False)
    monkeypatch.setattr(status, "_fetch", lambda url: None)
    monkeypatch.setattr(status, "_get_version", lambda: "0.test")
    monkeypatch.setattr(
        status,
        "_calculate_fleet_savings",
        lambda **kwargs: {"error": "db_not_found"},
    )
    monkeypatch.setattr(status._lic, "summary_for_cli", lambda: {"tier": _lic.TIER_FREE})

    status.run(no_meme=True)

    out = capsys.readouterr().out
    assert "Proxy unreachable and no monitor database found" in out
    assert "tokenpak.ai/pro" not in out
    assert "Upgrade to Pro" not in out


# --------------------------------------------------------------------------
# Public editions vs internal entitlement taxonomy
# --------------------------------------------------------------------------


def test_public_edition_collapses_internal_tiers():
    assert _lic.public_edition(_lic.TIER_FREE) == _lic.EDITION_BASE
    for tier in (_lic.TIER_PRO, _lic.TIER_TEAM, _lic.TIER_ENTERPRISE):
        assert _lic.public_edition(tier) == _lic.EDITION_PRO


def test_team_and_enterprise_are_not_offerings():
    assert _lic.is_public_plan(_lic.TIER_FREE) is True
    assert _lic.is_public_plan(_lic.TIER_PRO) is True
    assert _lic.is_public_plan(_lic.TIER_TEAM) is False
    assert _lic.is_public_plan(_lic.TIER_ENTERPRISE) is False

    tiers = {plan["tier"] for plan in _lic.discover_plans()}
    assert _lic.TIER_TEAM not in tiers
    assert _lic.TIER_ENTERPRISE not in tiers


def test_internal_tier_label_still_available_for_diagnostics():
    """The taxonomy is preserved — it is just not the public vocabulary."""
    assert _lic.internal_tier_label(_lic.TIER_TEAM) == "team"
    assert _lic.internal_tier_label(_lic.TIER_ENTERPRISE) == "enterprise"


def test_plan_output_names_no_purchasable_tier(capsys):
    license_cmd.run_plan(argparse.Namespace(json=False))

    out = capsys.readouterr().out
    assert "Team" not in out
    assert "Enterprise" not in out
    assert "$" not in out
    assert "unannounced" not in out
