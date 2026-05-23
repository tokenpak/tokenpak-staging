# SPDX-License-Identifier: Apache-2.0
"""Regression tests for Codex state database lock diagnostics."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokenpak.companion.codex import doctor, launcher, state_lock
from tokenpak.companion.codex.state_lock import _StateLockHolder


def test_state_lock_holder_pids_uses_lsof(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "state_5.sqlite"
    db.write_text("")

    monkeypatch.setattr(state_lock.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(args, **kwargs):
        assert args == ["lsof", "-t", str(db)]
        return subprocess.CompletedProcess(args, 0, stdout="123\n456\n123\n", stderr="")

    monkeypatch.setattr(state_lock.subprocess, "run", fake_run)

    assert state_lock._state_lock_holder_pids(db) == [123, 456]


def test_state_lock_holder_pids_falls_back_to_fuser(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "state_5.sqlite"
    db.write_text("")

    monkeypatch.setattr(
        state_lock.shutil,
        "which",
        lambda name: None if name == "lsof" else f"/usr/bin/{name}",
    )

    def fake_run(args, **kwargs):
        assert args == ["fuser", str(db)]
        return subprocess.CompletedProcess(
            args, 0, stdout=f" 123 456{db}:", stderr=""
        )

    monkeypatch.setattr(state_lock.subprocess, "run", fake_run)

    assert state_lock._state_lock_holder_pids(db) == [123, 456]


def test_doctor_reports_state_db_lock(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "state_5.sqlite"
    holder = _StateLockHolder(
        pid=123,
        ppid=45,
        stat="Tl",
        tty="pts/16",
        started="Sat May 23 07:37:07 2026",
        command="codex",
    )
    monkeypatch.setattr(doctor, "_codex_state_db_path", lambda: db)
    monkeypatch.setattr(doctor, "_state_lock_holders", lambda path: [holder])

    ok, detail = doctor.check_state_db_lock()

    assert ok is False
    assert "pid=123" in detail
    assert "stat=Tl" in detail
    assert "tty=pts/16" in detail
    assert "Do not delete state_5.sqlite" in detail


def test_launcher_preflight_blocks_when_state_db_locked(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    db = tmp_path / "state_5.sqlite"
    holder = _StateLockHolder(pid=123, stat="Tl", tty="pts/16", command="codex")
    monkeypatch.setattr(launcher, "_codex_state_db_path", lambda: db)
    monkeypatch.setattr(launcher, "_state_lock_holders", lambda path: [holder])

    assert launcher._preflight_state_db_lock() is False

    captured = capsys.readouterr()
    assert "Codex cannot start" in captured.err
    assert "pid=123" in captured.err
    assert "Do not delete state_5.sqlite" in captured.err


def test_launcher_preflight_allows_when_no_state_db_lock(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        launcher, "_codex_state_db_path", lambda: tmp_path / "state_5.sqlite"
    )
    monkeypatch.setattr(launcher, "_state_lock_holders", lambda path: [])

    assert launcher._preflight_state_db_lock() is True


def test_cmd_codex_propagates_launcher_return_code(monkeypatch) -> None:
    from tokenpak import _cli_core
    from tokenpak.companion import codex as codex_module

    def fake_launch(args):
        assert args == ["exec", "--help"]
        return 7

    monkeypatch.setattr(codex_module, "launch", fake_launch)

    with pytest.raises(SystemExit) as exc:
        _cli_core.cmd_codex(
            SimpleNamespace(args=["exec", "--help"], budget=None, install_only=False)
        )

    assert exc.value.code == 7
