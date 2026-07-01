# SPDX-License-Identifier: Apache-2.0
"""Platform fallback tests for the Codex companion launcher."""

from __future__ import annotations

import pytest

from tokenpak.companion.codex import launcher


class _ExecCalled(Exception):
    pass


def test_exec_or_run_windows_uses_subprocess(monkeypatch):
    monkeypatch.setattr(launcher.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        launcher.os,
        "execvpe",
        lambda *_args, **_kwargs: pytest.fail("execvpe must not run on Windows"),
    )

    calls = {}

    class FakeProcess:
        pid = 4321

        def wait(self):
            return 9

    def fake_popen(argv, env):
        calls["argv"] = argv
        calls["env"] = env
        return FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    recorded = []

    assert (
        launcher._exec_or_run(
            "codex",
            ["codex", "--version"],
            {"A": "B"},
            record_child_pid=recorded.append,
        )
        == 9
    )
    assert calls == {"argv": ["codex", "--version"], "env": {"A": "B"}}
    assert recorded == [4321]


def test_exec_or_run_posix_uses_execvpe(monkeypatch):
    monkeypatch.setattr(launcher.os, "name", "posix", raising=False)
    calls = {}

    def fake_exec(program, argv, env):
        calls["program"] = program
        calls["argv"] = argv
        calls["env"] = env
        raise _ExecCalled

    monkeypatch.setattr(launcher.os, "execvpe", fake_exec)

    with pytest.raises(_ExecCalled):
        launcher._exec_or_run("codex", ["codex"], {"C": "D"})

    assert calls == {"program": "codex", "argv": ["codex"], "env": {"C": "D"}}
