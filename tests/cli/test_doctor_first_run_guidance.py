# SPDX-License-Identifier: Apache-2.0
"""Focused doctor copy contracts for first-run guidance."""

from __future__ import annotations

from tokenpak.cli.commands import doctor


def test_lifecycle_summary_uses_parser_real_restart_command():
    out = doctor.build_lifecycle_summary(
        version="1.0.0",
        setup_present=True,
        route_state="active",
        proxy_state="stopped",
        update_state="current",
        update_latest=None,
    )
    assert "Run: tokenpak restart" in out
    assert "tokenpak proxy " + "restart" not in out


def test_lifecycle_summary_reports_running_proxy_version_separately():
    out = doctor.build_lifecycle_summary(
        version="1.9.0",
        setup_present=True,
        route_state="active",
        proxy_state="running",
        update_state="current",
        update_latest=None,
        running_proxy_version="1.9.0",
    )
    assert "Installed package" in out
    assert "Running proxy" in out
    assert "v1.9.0 matches installed package" not in out


def test_lifecycle_summary_warns_on_unacknowledged_runtime_skew():
    out = doctor.build_lifecycle_summary(
        version="1.9.0",
        setup_present=True,
        route_state="active",
        proxy_state="running",
        update_state="current",
        update_latest=None,
        running_proxy_version="1.7.1",
    )
    assert "v1.7.1 != package v1.9.0" in out
    assert "Run: tokenpak restart" in out


def test_lifecycle_summary_can_label_acknowledged_runtime_skew():
    out = doctor.build_lifecycle_summary(
        version="1.9.0",
        setup_present=True,
        route_state="active",
        proxy_state="running",
        update_state="current",
        update_latest=None,
        running_proxy_version="1.7.1",
        runtime_version_acknowledged=True,
    )
    assert "v1.7.1 (acknowledged)" in out
    assert "package v1.9.0" in out
    assert "Run: tokenpak restart" not in out


def test_api_key_setup_detail_includes_windows_and_posix_examples():
    detail = doctor._api_key_setup_detail()
    assert "no direct API key" in detail
    assert "export ANTHROPIC_API_KEY=sk-..." in detail
    assert 'setx ANTHROPIC_API_KEY "sk-..."' in detail
    assert "set ANTHROPIC_API_KEY=sk-..." in detail
