# SPDX-License-Identifier: Apache-2.0
"""Tests for the Codex companion launcher."""

from __future__ import annotations

import io
import json
import stat

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


def test_launch_registers_runtime_hygiene_manifest(monkeypatch, tmp_path):
    """A codex launch writes a non-cleanup (never_touch) session manifest."""
    _patch_setup(monkeypatch, tmp_path)
    monkeypatch.delenv("TOKENPAK_HOME", raising=False)

    def fake_exec(cmd, args, env):
        raise _ExecCalled

    monkeypatch.setattr(launcher.os, "execvpe", fake_exec)

    with pytest.raises(_ExecCalled):
        launcher.main([])

    from tokenpak.runtime import hygiene_registry as reg

    manifests = list(reg.sessions_root().glob("codex-*/manifest.json"))
    assert len(manifests) == 1
    data = json.loads(manifests[0].read_text())
    assert data["launch_mode"] == "codex"
    assert data["cleanup_policy"] == "never_touch"
    assert data["containment_method"] == "none"
    assert data["containment_created_by_tokenpak"] is False
    assert stat.S_IMODE(manifests[0].stat().st_mode) == 0o600


def test_manifest_registration_failure_never_blocks_launch(monkeypatch, tmp_path):
    """A deep registration failure is swallowed; the launcher still execs codex."""
    _patch_setup(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("registry exploded")

    # Make the underlying registration raise; the launcher's best-effort
    # wrapper must swallow it (non-cleanup launch continues regardless).
    monkeypatch.setattr("tokenpak.runtime.hygiene.register_session", boom)

    captured = {}

    def fake_exec(cmd, args, env):
        captured["cmd"] = cmd
        raise _ExecCalled

    monkeypatch.setattr(launcher.os, "execvpe", fake_exec)

    with pytest.raises(_ExecCalled):
        launcher.main([])
    assert captured["cmd"] == "codex"


def test_interactive_loading_status_preserves_shell_command_line():
    stream = _TtyStringIO()
    status = launcher._LoadingStatus(stream)

    status.step("refreshing Codex companion rates")
    status.step("checking TokenPak proxy")
    status.clear()

    output = stream.getvalue()
    assert output.startswith("\n\r\033[2K")
    assert output.count("\n") == 1
