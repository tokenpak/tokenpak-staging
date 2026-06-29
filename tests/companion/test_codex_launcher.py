# SPDX-License-Identifier: Apache-2.0
"""Tests for the Codex companion launcher."""

from __future__ import annotations

import io

import pytest

from tokenpak.cli.commands import status as status_cmds
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
    monkeypatch.setattr(status_cmds, "_get_version", lambda: "v1.8.0")
    monkeypatch.setattr(
        launcher.random,
        "choice",
        lambda lines: "Cache is king. Tokens are pawns.",
    )


def _write_current_codex_config(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    config_path = codex_dir / "config.toml"
    policy = "\n".join(mcp_config.render_policy_lines())
    config_path.write_text(
        "[features]\n"
        "hooks = true\n"
        "\n"
        f"[mcp_servers.{mcp_config.SERVER_NAME}]\n"
        'command = "python3"\n'
        'args = ["-P", "-m", "tokenpak.companion.mcp.server"]\n'
        f"{policy}\n",
        encoding="utf-8",
    )
    return config_path


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
    assert "TokenPak v1.8.0" in err
    assert "Ready • Mode: Balanced • Budget: Unlimited" in err
    assert "Proxy active" not in err
    assert "Cache is king. Tokens are pawns." in err
    assert "MCP server registered" not in err
    assert "hooks installed" not in err
    assert "companion ready for codex" not in err


def test_main_skips_codex_setup_subprocesses_when_config_current(
    monkeypatch,
    tmp_path,
    capsys,
):
    """Regression: normal launch avoids Codex CLI probes when setup is current."""
    _patch_setup(monkeypatch, tmp_path)
    _write_current_codex_config(tmp_path)

    def register_must_not_run(env_vars=None):
        raise AssertionError("register should not run on current hot path")

    def enable_must_not_run():
        raise AssertionError("features enable should not run on current hot path")

    captured = {}

    def fake_exec(cmd, args, env):
        captured["cmd"] = cmd
        captured["args"] = args
        raise _ExecCalled

    monkeypatch.setattr(mcp_config, "register", register_must_not_run)
    monkeypatch.setattr(hooks, "ensure_hooks_feature_enabled", enable_must_not_run)
    monkeypatch.setattr(launcher.os, "execvpe", fake_exec)

    with pytest.raises(_ExecCalled):
        launcher.main(["--model", "gpt-5-codex"])

    assert captured["cmd"] == "codex"
    assert captured["args"] == ["codex", "--model", "gpt-5-codex"]
    err = capsys.readouterr().err
    assert "tokenpak: Codex MCP server already configured..." in err
    assert "tokenpak: registering Codex MCP server..." not in err


def test_install_only_repairs_setup_even_when_config_current(
    monkeypatch,
    tmp_path,
):
    _patch_setup(monkeypatch, tmp_path)
    _write_current_codex_config(tmp_path)
    calls = {"register": 0, "hooks": 0}

    def count_register(env_vars=None):
        calls["register"] += 1
        return True

    def count_hooks():
        calls["hooks"] += 1
        return True

    monkeypatch.setattr(mcp_config, "register", count_register)
    monkeypatch.setattr(hooks, "ensure_hooks_feature_enabled", count_hooks)

    assert launcher.main(["--install-only"]) == 0
    assert calls == {"register": 1, "hooks": 1}


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
