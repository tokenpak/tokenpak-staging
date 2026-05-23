# SPDX-License-Identifier: Apache-2.0
"""Regression tests for Codex state database lock diagnostics."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokenpak.companion.codex import doctor, launcher, state_lock
from tokenpak.companion.codex.state_lock import _classify_holders, _StateLockHolder


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


# ── Holder classification ──────────────────────────────────────────


def test_classify_holders_splits_stale_and_active() -> None:
    stale_holder = _StateLockHolder(pid=100, stat="Tl", command="codex")
    active_holder = _StateLockHolder(pid=200, stat="Sl+", command="codex")
    sleeping = _StateLockHolder(pid=300, stat="Sl", command="codex")

    stale, active = _classify_holders([stale_holder, active_holder, sleeping])

    assert stale == [stale_holder]
    assert active == [active_holder, sleeping]


def test_classify_holders_all_stale() -> None:
    h1 = _StateLockHolder(pid=1, stat="Tl", command="codex")
    h2 = _StateLockHolder(pid=2, stat="T", command="codex")

    stale, active = _classify_holders([h1, h2])

    assert len(stale) == 2
    assert active == []


def test_classify_holders_empty() -> None:
    stale, active = _classify_holders([])
    assert stale == []
    assert active == []


# ── Report formatting ──────────────────────────────────────────────


def test_format_report_stale_holders_shows_kill_guidance(tmp_path: Path) -> None:
    path = tmp_path / "state_5.sqlite"
    holders = [
        _StateLockHolder(pid=111, ppid=10, stat="Tl", tty="pts/1", command="codex"),
        _StateLockHolder(pid=222, ppid=10, stat="T", tty="pts/1", command="codex"),
    ]
    report = state_lock._format_lock_report(path, holders)

    assert "(stopped)" in report
    assert "kill 111 222" in report
    assert "Parallel Codex sessions require an isolated CODEX_HOME" in report
    assert "Do not delete state_5.sqlite" in report


def test_format_report_active_holder_shows_quit_guidance(tmp_path: Path) -> None:
    path = tmp_path / "state_5.sqlite"
    holders = [
        _StateLockHolder(pid=333, ppid=10, stat="Sl+", tty="pts/2", command="codex"),
    ]
    report = state_lock._format_lock_report(path, holders)

    assert "(active)" in report
    assert "Active Codex session detected" in report
    assert "separate CODEX_HOME" in report


def test_format_report_mixed_holders(tmp_path: Path) -> None:
    path = tmp_path / "state_5.sqlite"
    holders = [
        _StateLockHolder(pid=111, stat="Tl", tty="pts/1", command="codex"),
        _StateLockHolder(pid=333, stat="Sl+", tty="pts/2", command="codex"),
    ]
    report = state_lock._format_lock_report(path, holders)

    assert "kill 111" in report
    assert "Active Codex session detected" in report


# ── Doctor check ───────────────────────────────────────────────────


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


# ── Launcher preflight ─────────────────────────────────────────────


def test_launcher_preflight_blocks_when_state_db_locked(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    db = tmp_path / "state_5.sqlite"
    holder = _StateLockHolder(pid=123, stat="Tl", tty="pts/16", command="codex")
    monkeypatch.setattr(launcher, "_codex_state_db_path", lambda: db)
    monkeypatch.setattr(launcher, "_state_lock_holders", lambda path: [holder])
    monkeypatch.delenv("TOKENPAK_CODEX_SKIP_STATE_LOCK_PREFLIGHT", raising=False)

    assert launcher._preflight_state_db_lock() is False

    captured = capsys.readouterr()
    assert "locked by another process" in captured.err
    assert "pid=123" in captured.err
    assert "Do not delete state_5.sqlite" in captured.err
    assert "TOKENPAK_CODEX_SKIP_STATE_LOCK_PREFLIGHT" in captured.err


def test_launcher_preflight_allows_when_no_state_db_lock(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        launcher, "_codex_state_db_path", lambda: tmp_path / "state_5.sqlite"
    )
    monkeypatch.setattr(launcher, "_state_lock_holders", lambda path: [])
    monkeypatch.delenv("TOKENPAK_CODEX_SKIP_STATE_LOCK_PREFLIGHT", raising=False)

    assert launcher._preflight_state_db_lock() is True


def test_launcher_preflight_skipped_by_env_var(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "state_5.sqlite"
    holder = _StateLockHolder(pid=999, stat="Sl+", tty="pts/5", command="codex")
    monkeypatch.setattr(launcher, "_codex_state_db_path", lambda: db)
    monkeypatch.setattr(launcher, "_state_lock_holders", lambda path: [holder])
    monkeypatch.setenv("TOKENPAK_CODEX_SKIP_STATE_LOCK_PREFLIGHT", "1")

    assert launcher._preflight_state_db_lock() is True


def test_skip_lock_preflight_truthy_values() -> None:
    for val in ("1", "true", "yes", "TRUE", " Yes "):
        assert launcher._skip_lock_preflight_enabled(
            {"TOKENPAK_CODEX_SKIP_STATE_LOCK_PREFLIGHT": val}
        ) is True


def test_skip_lock_preflight_falsy_values() -> None:
    for val in ("", "0", "false", "no", "off"):
        assert launcher._skip_lock_preflight_enabled(
            {"TOKENPAK_CODEX_SKIP_STATE_LOCK_PREFLIGHT": val}
        ) is False
    assert launcher._skip_lock_preflight_enabled({}) is False


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
