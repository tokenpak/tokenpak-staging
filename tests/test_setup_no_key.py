# SPDX-License-Identifier: Apache-2.0
"""Tests for the no-API-key path of ``tokenpak setup``.

Contract:
  * No API key + Anthropic available via OAuth/session + a Claude Code login
    present  → setup completes onboarding by pointing Claude Code at the
    local proxy (writes ANTHROPIC_BASE_URL into ~/.claude/settings.json),
    and does NOT emit the "no API keys detected" warning.
  * No API key + OAuth available but no Claude Code login → still no warning;
    just the OAuth info line (nothing to configure).
  * No API key + no OAuth path → mode-aware guidance with per-OS env syntax,
    not a bash-only dead-end.

These exercise the integration with the OAuth-mode-aware warning branch:
the OAuth detection (``non_direct_key_auth_available``) still gates whether
the warning is shown; the wire-in only adds onboarding completion on top.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tokenpak._cli_core as cli_core


def _run_setup(tmp_home: Path) -> None:
    cli_core.cmd_setup(SimpleNamespace())


def test_no_key_with_oauth_and_claude_login_configures_proxy(tmp_path, capsys, monkeypatch):
    home = tmp_path
    # A Claude Code login is present.
    (home / ".claude").mkdir()
    # No env API keys.
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    with patch.object(Path, "home", return_value=home), patch(
        "tokenpak.creds.auth_mode.non_direct_key_auth_available", return_value=True
    ):
        _run_setup(home)

    out = capsys.readouterr().out
    # No false-alarm warning.
    assert "No API keys detected" not in out
    # Onboarding actually completed: Claude Code wired to the proxy.
    assert "Configured Claude Code" in out
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_BASE_URL"].startswith("http")


def test_no_key_with_oauth_no_claude_login_no_warning_no_config(tmp_path, capsys, monkeypatch):
    home = tmp_path
    # No ~/.claude directory => no Claude Code login.
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    with patch.object(Path, "home", return_value=home), patch(
        "tokenpak.creds.auth_mode.non_direct_key_auth_available", return_value=True
    ):
        _run_setup(home)

    out = capsys.readouterr().out
    assert "No API keys detected" not in out
    assert "authenticated via OAuth/session" in out
    # Nothing to configure: no settings.json written.
    assert not (home / ".claude" / "settings.json").exists()


def test_no_key_no_oauth_shows_cross_platform_guidance(tmp_path, capsys, monkeypatch):
    home = tmp_path
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    with patch.object(Path, "home", return_value=home), patch(
        "tokenpak.creds.auth_mode.non_direct_key_auth_available", return_value=False
    ):
        _run_setup(home)

    out = capsys.readouterr().out
    # The mode-aware warning is preserved (no OAuth path here).
    assert "No API keys detected" in out
    # Per-OS env syntax is shown, not bash-only.
    assert "export ANTHROPIC_API_KEY=sk-..." in out
    assert '$env:ANTHROPIC_API_KEY="sk-..."' in out
    assert "set ANTHROPIC_API_KEY=sk-..." in out
