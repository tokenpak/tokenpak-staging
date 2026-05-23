"""tests/cli/test_setup_no_keys_routing.py

Lane 1 of the UX onboarding initiative (p0-tokenpak-ux-lane1-stop-the-
bleeding) — covers ``tokenpak setup`` routing when no provider API keys
are present in the environment.

Pre-Lane-1 behavior: cmd_setup printed a single "set ANTHROPIC_API_KEY"
hint and returned, dead-ending the user. Post-Lane-1 routing
(DECISION-UX-01 + UX-03):

  · TTY + not --no-tui → guided menu (``tokenpak.cli.commands.menu.run_menu``)
  · non-TTY OR --no-tui → deterministic print-only instructions covering
    both the Claude Code OAuth-passthrough path and the direct-key path
"""

from __future__ import annotations

import argparse
import io
import sys
import types

import pytest

from tokenpak import _cli_core

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Strip provider API keys + redirect HOME so config_file doesn't exist."""
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOKENPAK_SHELL", raising=False)
    # Reset --no-tui global so tests don't bleed.
    monkeypatch.setattr(_cli_core, "_NO_TUI_FLAG", False)
    return tmp_path


def _stub_args():
    return argparse.Namespace()


def _patch_isatty(monkeypatch, *, stdin: bool, stdout: bool):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout)


# ---------------------------------------------------------------------------
# TTY + no keys → guided menu
# ---------------------------------------------------------------------------


def test_no_keys_tty_routes_to_guided_menu(clean_env, monkeypatch, capsys):
    """TTY + no env keys + not --no-tui → run_menu is invoked, no dead-end print."""
    calls = []

    def fake_run_menu():
        calls.append("menu")

    fake_menu_mod = types.ModuleType("tokenpak.cli.commands.menu")
    fake_menu_mod.run_menu = fake_run_menu
    monkeypatch.setitem(sys.modules, "tokenpak.cli.commands.menu", fake_menu_mod)

    _patch_isatty(monkeypatch, stdin=True, stdout=True)

    _cli_core.cmd_setup(_stub_args())

    assert calls == ["menu"], "guided menu should have been invoked exactly once"
    out = capsys.readouterr().out
    assert "Launching guided setup" in out
    # Must NOT print the legacy dead-end text.
    assert "export ANTHROPIC_API_KEY='sk-...'" not in out or "Path B" in out


# ---------------------------------------------------------------------------
# Non-TTY + no keys → deterministic print
# ---------------------------------------------------------------------------


def test_no_keys_non_tty_prints_deterministic_instructions(clean_env, monkeypatch, capsys):
    _patch_isatty(monkeypatch, stdin=False, stdout=False)
    monkeypatch.setenv("TOKENPAK_SHELL", "posix")

    _cli_core.cmd_setup(_stub_args())

    out = capsys.readouterr().out
    # AC #1 + #5: deterministic, no prompt, both paths covered
    assert "Path A" in out and "Path B" in out
    # AC #2: Claude Code OAuth-passthrough copy (phrasing may wrap across lines)
    assert "no API key required" in out
    assert "OAuth" in out and "byte-preserved" in out
    # AC #3: shell-aware rendering — posix uses export NAME='...'
    assert "export ANTHROPIC_BASE_URL='http://localhost:8766'" in out


def test_no_keys_non_tty_rendering_is_stable(clean_env, monkeypatch, capsys):
    """AC #5: same env → same output across runs (modulo timestamps; we have none)."""
    _patch_isatty(monkeypatch, stdin=False, stdout=False)
    monkeypatch.setenv("TOKENPAK_SHELL", "posix")

    _cli_core.cmd_setup(_stub_args())
    first = capsys.readouterr().out

    _cli_core.cmd_setup(_stub_args())
    second = capsys.readouterr().out

    assert first == second


# ---------------------------------------------------------------------------
# --no-tui flag on TTY → print-only
# ---------------------------------------------------------------------------


def test_no_keys_tty_with_no_tui_skips_guided_menu(clean_env, monkeypatch, capsys):
    """--no-tui on a TTY still short-circuits to the deterministic print path."""
    calls = []

    def fake_run_menu():
        calls.append("menu")

    fake_menu_mod = types.ModuleType("tokenpak.cli.commands.menu")
    fake_menu_mod.run_menu = fake_run_menu
    monkeypatch.setitem(sys.modules, "tokenpak.cli.commands.menu", fake_menu_mod)

    monkeypatch.setattr(_cli_core, "_NO_TUI_FLAG", True)
    _patch_isatty(monkeypatch, stdin=True, stdout=True)

    _cli_core.cmd_setup(_stub_args())

    assert calls == [], "guided menu must NOT run when --no-tui is set"
    out = capsys.readouterr().out
    assert "Path A" in out


# ---------------------------------------------------------------------------
# Shell-aware rendering branches (AC #3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shell,expected_fragment",
    [
        ("posix", "export ANTHROPIC_BASE_URL='http://localhost:8766'"),
        ("cmd", "set ANTHROPIC_BASE_URL=http://localhost:8766"),
        ("powershell", "$env:ANTHROPIC_BASE_URL='http://localhost:8766'"),
    ],
)
def test_no_keys_renders_each_shell(clean_env, monkeypatch, capsys, shell, expected_fragment):
    _patch_isatty(monkeypatch, stdin=False, stdout=False)
    monkeypatch.setenv("TOKENPAK_SHELL", shell)

    _cli_core.cmd_setup(_stub_args())
    out = capsys.readouterr().out
    assert expected_fragment in out


# ---------------------------------------------------------------------------
# AC #7: no regression when env keys ARE present
# ---------------------------------------------------------------------------


def test_env_key_present_does_not_take_no_keys_branch(clean_env, monkeypatch, capsys):
    """When ANTHROPIC_API_KEY is set, the no-keys branch must not fire."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    _patch_isatty(monkeypatch, stdin=False, stdout=False)
    # Bypass the interactive wizard's `input()` calls by sending EOF via
    # an early exit: the wizard prompts for provider/port/profile *after*
    # the no-keys branch, and on non-TTY input() raises EOFError which
    # cmd_setup currently doesn't catch (the legacy wizard path). We
    # instead patch input() to raise SystemExit to abort cleanly *after*
    # asserting we're past the no-keys branch.
    monkeypatch.setattr("builtins.input", lambda *a, **kw: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        _cli_core.cmd_setup(_stub_args())

    out = capsys.readouterr().out
    assert "Found Anthropic API key" in out
    # The no-keys instruction block must NOT have printed.
    assert "Path A" not in out
    assert "No API keys detected" not in out
