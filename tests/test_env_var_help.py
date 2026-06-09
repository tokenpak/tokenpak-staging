# SPDX-License-Identifier: Apache-2.0
"""Tests for the cross-platform env-var help renderer.

The onboarding/setup guidance must show platform-appropriate shell syntax
(bash ``export``, PowerShell ``$env:``, cmd ``set``) instead of being
bash-only, so Windows users get a copy-pasteable form.
"""

from __future__ import annotations

from unittest.mock import patch

from tokenpak.cli.commands.setup import env_var_help


def test_renders_all_three_os_forms():
    out = env_var_help("ANTHROPIC_API_KEY", "sk-...")
    assert "export ANTHROPIC_API_KEY=sk-..." in out
    assert '$env:ANTHROPIC_API_KEY="sk-..."' in out
    assert "set ANTHROPIC_API_KEY=sk-..." in out


def test_default_value_placeholder():
    out = env_var_help("FOO")
    assert "export FOO=..." in out
    assert '$env:FOO="..."' in out
    assert "set FOO=..." in out


def test_posix_lists_bash_first():
    with patch("tokenpak.cli.commands.setup.os.name", "posix"):
        out = env_var_help("ANTHROPIC_API_KEY", "sk-...")
    first_line = out.splitlines()[0]
    assert first_line.strip().startswith("export ANTHROPIC_API_KEY=")


def test_windows_lists_powershell_first():
    with patch("tokenpak.cli.commands.setup.os.name", "nt"):
        out = env_var_help("ANTHROPIC_API_KEY", "sk-...")
    first_line = out.splitlines()[0]
    assert first_line.strip().startswith("$env:ANTHROPIC_API_KEY=")
    # All three forms are still present regardless of platform.
    assert "set ANTHROPIC_API_KEY=sk-..." in out
    assert "export ANTHROPIC_API_KEY=sk-..." in out
