# SPDX-License-Identifier: Apache-2.0
"""Tests for the Codex companion launcher."""

from __future__ import annotations

import io

import pytest

import tokenpak
from tokenpak.companion.codex import (
    agents_md,
    hooks,
    launcher,
    mcp_config,
    rates_snapshot,
    skills_installer,
)


class _ExecCalled(Exception):
    pass


class _TtyStringIO(io.StringIO):
    def isatty(self):
        return True


def _patch_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("TOKENPAK_COMPANION_PROFILE", "balanced")
    monkeypatch.delenv("TOKENPAK_COMPANION_BUDGET", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    monkeypatch.setattr(rates_snapshot, "refresh", lambda: tmp_path / "rates.tsv")
    monkeypatch.setattr(mcp_config, "get_env_vars", lambda config: {})
    monkeypatch.setattr(mcp_config, "register", lambda env_vars=None: True)
    monkeypatch.setattr(hooks, "ensure_hooks_feature_enabled", lambda: True)
    monkeypatch.setattr(hooks, "install_hooks", lambda target="global": tmp_path / "hooks.json")
    monkeypatch.setattr(
        agents_md,
        "install_agents_md",
        lambda target="global": tmp_path / "AGENTS.md",
    )
    monkeypatch.setattr(
        skills_installer,
        "install_skills",
        lambda: [tmp_path / "skill-a", tmp_path / "skill-b"],
    )
    monkeypatch.setattr(
        launcher.random,
        "choice",
        lambda lines: "Cache is king. Tokens are pawns.",
    )


def test_install_only_prints_loading_status_then_codex_banner(
    monkeypatch,
    tmp_path,
    capsys,
):
    _patch_setup(monkeypatch, tmp_path)

    assert launcher.main(["--install-only"]) == 0

    captured = capsys.readouterr()
    err = captured.err

    assert "tokenpak: refreshing Codex companion rates..." in err
    assert "tokenpak: registering Codex MCP server..." in err
    assert "tokenpak: installing Codex hooks..." in err
    assert "Codex Companion" in err
    assert f"TokenPak v{tokenpak.__version__}" in err
    assert "Ready • Mode: Balanced • Budget: Unlimited" in err
    assert "Proxy active" not in err
    assert "Cache is king. Tokens are pawns." in err
    assert "MCP server registered" not in err
    assert "hooks installed" not in err
    assert "companion ready for codex" not in err


def test_main_proxy_flag_routes_codex_through_named_profile(
    monkeypatch,
    tmp_path,
    capsys,
):
    _patch_setup(monkeypatch, tmp_path)
    captured = {}

    def fake_exec(cmd, args, env):
        captured["cmd"] = cmd
        captured["args"] = args
        captured["env"] = env
        raise _ExecCalled

    monkeypatch.setattr(launcher.os, "execvpe", fake_exec)

    with pytest.raises(_ExecCalled):
        launcher.main(["--proxy", "--model", "gpt-5-codex"])

    profile = tmp_path / ".codex" / "tokenpak-chatgpt.config.toml"
    assert profile.exists()
    assert 'base_url = "http://127.0.0.1:8766/v1"' in profile.read_text()
    assert captured["cmd"] == "codex"
    assert captured["args"] == [
        "codex",
        "-p",
        "tokenpak-chatgpt",
        "--model",
        "gpt-5-codex",
    ]
    assert "OPENAI_BASE_URL" not in captured["env"]
    assert "Proxy active → http://127.0.0.1:8766/v1" in capsys.readouterr().err


def test_interactive_loading_status_preserves_shell_command_line():
    stream = _TtyStringIO()
    status = launcher._LoadingStatus(stream)

    status.step("refreshing Codex companion rates")
    status.step("checking TokenPak proxy")
    status.clear()

    output = stream.getvalue()
    assert output.startswith("\n\r\033[2K")
    assert output.count("\n") == 1
