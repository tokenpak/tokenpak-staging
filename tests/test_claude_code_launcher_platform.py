# SPDX-License-Identifier: Apache-2.0
"""Platform fallback tests for the Claude Code registry launcher."""

from __future__ import annotations

import types

import pytest

from tokenpak.core.registry.claude_code import launcher


def test_registry_launcher_windows_uses_subprocess(monkeypatch):
    monkeypatch.setattr(launcher.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        launcher.os,
        "execvpe",
        lambda *_a, **_k: pytest.fail("execvpe must not run on Windows"),
    )

    calls = {}

    def fake_run(argv, env, check):
        calls["argv"] = argv
        calls["env"] = env
        calls["check"] = check
        return types.SimpleNamespace(returncode=4)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher._exec_or_run("claude", ["claude"], {"Z": "1"}) == 4
    assert calls == {"argv": ["claude"], "env": {"Z": "1"}, "check": False}


def test_registry_launcher_posix_uses_execvpe(monkeypatch):
    monkeypatch.setattr(launcher.os, "name", "posix", raising=False)
    calls = {}

    def fake_exec(program, argv, env):
        calls["program"] = program
        calls["argv"] = argv
        calls["env"] = env
        raise RuntimeError("exec called")

    monkeypatch.setattr(launcher.os, "execvpe", fake_exec)

    with pytest.raises(RuntimeError, match="exec called"):
        launcher._exec_or_run("claude", ["claude"], {"Z": "2"})

    assert calls == {"program": "claude", "argv": ["claude"], "env": {"Z": "2"}}
