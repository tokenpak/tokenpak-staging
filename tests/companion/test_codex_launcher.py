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
    session_home,
    skills_installer,
    state_lock,
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
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv(session_home.ENV_SESSION_MODE, raising=False)

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


def test_main_auto_falls_back_to_parallel_safe_session_when_codex_data_busy(
    monkeypatch,
    tmp_path,
    capsys,
):
    _patch_setup(monkeypatch, tmp_path)
    captured = {}
    probed_homes = []

    def fake_wait(home, *args, **kwargs):
        home = home if home is not None else tmp_path / ".codex"
        probed_homes.append(home)
        if len(probed_homes) == 1:
            return state_lock.LockStatus(
                home=home,
                db_path=home / "logs_2.sqlite",
                exists=True,
                locked=True,
                detail=(
                    "log database is busy/locked because another Codex process "
                    "is using it (holder PID unavailable)"
                ),
                db_label="log database",
            )
        return state_lock.LockStatus(
            home=home,
            db_path=home / "logs_2.sqlite",
            exists=False,
            locked=False,
            detail="no Codex database yet (uncontended)",
            db_label="log database",
        )

    def fake_exec(cmd, args, env):
        captured["cmd"] = cmd
        captured["args"] = args
        captured["env"] = env
        raise _ExecCalled

    monkeypatch.setattr(state_lock, "_wait_until_unlocked", fake_wait)
    monkeypatch.setattr(launcher.os, "execvpe", fake_exec)

    with pytest.raises(_ExecCalled):
        launcher.main([])

    assert probed_homes[0].parent == session_home.workspaces_root()
    assert probed_homes[1].parent == session_home.sessions_root()
    assert captured["cmd"] == "codex"
    assert captured["env"]["CODEX_HOME"] == str(probed_homes[1])

    err = capsys.readouterr().err
    assert "starting a fresh parallel-safe session" in err
    assert "TOKENPAK_CODEX_SESSION_MODE=workspace" not in err
    assert "Traceback" not in err


def test_main_auto_falls_back_when_workspace_is_already_claimed(
    monkeypatch,
    tmp_path,
    capsys,
):
    _patch_setup(monkeypatch, tmp_path)
    captured = {}
    claimed_homes = []

    def fake_claim(home):
        claimed_homes.append(home)
        return len(claimed_homes) > 1

    def fake_wait(home, *args, **kwargs):
        return state_lock.LockStatus(
            home=home,
            db_path=home / "logs_2.sqlite",
            exists=False,
            locked=False,
            detail="no Codex database yet (uncontended)",
            db_label="log database",
        )

    def fake_exec(cmd, args, env):
        captured["cmd"] = cmd
        captured["env"] = env
        raise _ExecCalled

    monkeypatch.setattr(session_home, "_claim_home", fake_claim)
    monkeypatch.setattr(state_lock, "_wait_until_unlocked", fake_wait)
    monkeypatch.setattr(launcher.os, "execvpe", fake_exec)

    with pytest.raises(_ExecCalled):
        launcher.main([])

    assert claimed_homes[0].parent == session_home.workspaces_root()
    assert claimed_homes[1].parent == session_home.sessions_root()
    assert captured["cmd"] == "codex"
    assert captured["env"]["CODEX_HOME"] == str(claimed_homes[1])

    err = capsys.readouterr().err
    assert "Codex workspace is already active" in err
    assert "starting a fresh parallel-safe session" in err


def test_main_explicit_shared_mode_reports_busy_codex_data(
    monkeypatch,
    tmp_path,
    capsys,
):
    _patch_setup(monkeypatch, tmp_path)
    monkeypatch.setenv(session_home.ENV_SESSION_MODE, session_home.MODE_SHARED)
    busy = state_lock.LockStatus(
        home=tmp_path / ".codex",
        db_path=tmp_path / ".codex" / "logs_2.sqlite",
        exists=True,
        locked=True,
        detail=(
            "log database is busy/locked because another Codex process is using it "
            "(holder PID unavailable)"
        ),
        db_label="log database",
    )
    monkeypatch.setattr(state_lock, "_wait_until_unlocked", lambda *a, **k: busy)

    assert launcher.main([]) == 1

    err = capsys.readouterr().err
    assert "tokenpak: connecting to Codex..." in err
    assert "tokenpak: still connecting to Codex; local data is busy:" in err
    assert "logs_2.sqlite" in err
    assert "TokenPak auto mode normally starts a parallel-safe session" in err
    assert "Traceback" not in err
    assert "Codex Companion" not in err


def test_interactive_loading_status_preserves_shell_command_line():
    stream = _TtyStringIO()
    status = launcher._LoadingStatus(stream)

    status.step("refreshing Codex companion rates")
    status.step("checking TokenPak proxy")
    status.clear()

    output = stream.getvalue()
    assert output.startswith("\n\r\033[2K")
    assert output.count("\n") == 1
