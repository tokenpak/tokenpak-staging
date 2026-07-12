# SPDX-License-Identifier: Apache-2.0
"""Focused tests for read-only Codex SQLite holder diagnostics."""

from __future__ import annotations

import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from tokenpak.companion.codex import state_lock as sl


@pytest.fixture
def codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / ".codex"
    home.mkdir(parents=True)
    return home


def _make_db(home: Path, name: str = sl.STATE_DB_NAME) -> Path:
    db = home / name
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER)")
    connection.commit()
    connection.close()
    return db


_HOLDER_SCRIPT = r"""
import sqlite3
import sys

database, mode = sys.argv[1:]
connection = sqlite3.connect(database, isolation_level=None)
if mode == "wal":
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("BEGIN IMMEDIATE")
else:
    connection.execute("BEGIN EXCLUSIVE")
print("ready", flush=True)
sys.stdin.readline()
connection.execute("ROLLBACK")
connection.close()
"""


@contextmanager
def _sqlite_holder(db: Path, mode: str = "exclusive"):
    """Start and fully reap a test-owned SQLite holder process."""
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(db), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 10)
        if not readable:
            stderr = process.stderr.read() if process.poll() is not None and process.stderr else ""
            pytest.fail(f"SQLite holder did not become ready: {stderr}")
        assert process.stdout.readline().strip() == "ready"
        yield process
    finally:
        if process.poll() is None and sl._pid_stopped(process.pid):
            os.kill(process.pid, signal.SIGCONT)
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write("\n")
                process.stdin.flush()
            except BrokenPipeError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _wait_for_state(pid: int, *, stopped: bool) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sl._pid_stopped(pid) is stopped:
            return
        time.sleep(0.01)
    pytest.fail(f"test holder PID {pid} did not reach expected stopped={stopped} state")


def _write_synthetic_process(
    proc_root: Path,
    pid: int,
    *,
    uid: int,
    state: str = "S",
    name: str = "codex",
    with_fd_dir: bool = True,
) -> Path:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    if with_fd_dir:
        (process / "fd").mkdir()
        (process / "fdinfo").mkdir()
    (process / "status").write_text(
        f"Name:\t{name}\nTgid:\t{pid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
    )
    fields = [state, *(["0"] * 18), "9001"]
    (process / "stat").write_text(f"{pid} (fixture holder) {' '.join(fields)}\n")
    (process / "comm").write_text(name + "\n")
    (process / "cmdline").write_bytes(name.encode() + b"\0")
    return process


def _complete_empty_proc(tmp_path: Path) -> Path:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "locks").write_text("")
    return proc_root


def test_probe_absent_db_is_unlocked(codex_home):
    status = sl.probe(codex_home)

    assert status.exists is False
    assert status.locked is False
    assert status.diagnostics_complete is True
    assert "uncontended" in status.detail


def test_probe_free_db_is_unlocked_without_sqlite_client(codex_home, tmp_path):
    _make_db(codex_home)

    status = sl.probe(codex_home, proc_root=_complete_empty_proc(tmp_path))

    assert status.exists is True
    assert status.locked is False
    assert status.diagnostics_complete is True
    assert "sqlite3" not in sl.__dict__


def test_missing_proc_fails_closed_when_database_exists(codex_home, tmp_path):
    db = _make_db(codex_home)

    status = sl.probe(codex_home, proc_root=tmp_path / "missing-proc")

    assert status.exists is True
    assert status.db_path == db
    assert status.locked is True
    assert status.diagnostics_complete is False
    assert status.holder_pids == []
    assert "/proc holder inspection is incomplete" in status.detail
    assert "refusing unsafe access" in status.detail


def test_unreadable_database_discovery_fails_closed(codex_home, tmp_path, monkeypatch):
    _make_db(codex_home)
    proc_root = _complete_empty_proc(tmp_path)
    original_iterdir = Path.iterdir

    def guarded_iterdir(path):
        if path == codex_home:
            raise PermissionError("fixture denies database discovery")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    status = sl.probe(codex_home, proc_root=proc_root)

    assert status.exists is True
    assert status.locked is True
    assert status.diagnostics_complete is False
    assert "database discovery is incomplete" in status.detail
    assert "refusing unsafe access" in status.detail


def test_unreadable_database_sidecar_target_fails_closed(codex_home, tmp_path, monkeypatch):
    db = _make_db(codex_home)
    shm = Path(f"{db}-shm")
    shm.write_bytes(b"\0" * 256)
    proc_root = _complete_empty_proc(tmp_path)
    original_stat = Path.stat

    def guarded_stat(path, *args, **kwargs):
        if path == shm:
            raise PermissionError("fixture denies sidecar inspection")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)

    status = sl.probe(codex_home, proc_root=proc_root)

    assert status.exists is True
    assert status.locked is True
    assert status.diagnostics_complete is False
    assert "database target inspection is incomplete" in status.detail
    assert "refusing unsafe access" in status.detail


def test_same_owner_unreadable_fd_scan_fails_closed(codex_home, tmp_path):
    db = _make_db(codex_home)
    proc_root = _complete_empty_proc(tmp_path)
    _write_synthetic_process(
        proc_root,
        4243,
        uid=db.stat().st_uid,
        with_fd_dir=False,
    )

    status = sl.probe(codex_home, proc_root=proc_root)

    assert status.locked is True
    assert status.diagnostics_complete is False
    assert status.holder_pids == []
    assert "refusing unsafe access" in status.detail


def test_foreign_owner_unreadable_fd_scan_does_not_fail_closed(codex_home, tmp_path):
    db = _make_db(codex_home)
    proc_root = _complete_empty_proc(tmp_path)
    _write_synthetic_process(
        proc_root,
        4244,
        uid=db.stat().st_uid + 1,
        with_fd_dir=False,
    )

    status = sl.probe(codex_home, proc_root=proc_root)

    assert status.locked is False
    assert status.diagnostics_complete is True


def test_same_owner_known_non_codex_unreadable_fd_is_ignored(codex_home, tmp_path):
    db = _make_db(codex_home)
    proc_root = _complete_empty_proc(tmp_path)
    _write_synthetic_process(
        proc_root,
        4245,
        uid=db.stat().st_uid,
        name="sd-pam",
        with_fd_dir=False,
    )

    status = sl.probe(codex_home, proc_root=proc_root)

    assert status.locked is False
    assert status.diagnostics_complete is True


@pytest.mark.parametrize("name", ["node", "python", "sh", "env", "npx", "arbitrary-helper"])
def test_same_owner_unreadable_wrapper_process_fails_closed(codex_home, tmp_path, name):
    db = _make_db(codex_home)
    proc_root = _complete_empty_proc(tmp_path)
    _write_synthetic_process(
        proc_root,
        4247,
        uid=db.stat().st_uid,
        name=name,
        with_fd_dir=False,
    )

    status = sl.probe(codex_home, proc_root=proc_root)

    assert status.locked is True
    assert status.diagnostics_complete is False


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_known_dead_codex_process_does_not_make_scan_incomplete(codex_home, tmp_path, state):
    db = _make_db(codex_home)
    proc_root = _complete_empty_proc(tmp_path)
    _write_synthetic_process(
        proc_root,
        4246,
        uid=db.stat().st_uid,
        state=state,
        with_fd_dir=False,
    )

    status = sl.probe(codex_home, proc_root=proc_root)

    assert status.locked is False
    assert status.diagnostics_complete is True


def test_probe_defaults_to_codex_home_env(codex_home, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _make_db(codex_home)

    status = sl.probe(proc_root=_complete_empty_proc(tmp_path))

    assert status.home == codex_home
    assert status.locked is False


@pytest.mark.skipif(not Path("/proc/locks").exists(), reason="requires Linux /proc")
def test_dynamic_state_database_reports_running_holder_pid(codex_home):
    db = _make_db(codex_home, "state_99.sqlite")

    with _sqlite_holder(db) as holder:
        status = sl.probe(codex_home)

        assert status.locked is True
        assert status.db_path == db
        assert status.holder_pids == [holder.pid]
        assert status.running_pids == [holder.pid]
        assert status.stopped_pids == []
        assert f"PID {holder.pid} (running)" in status.detail
        assert "holder PID unavailable" not in status.detail


@pytest.mark.skipif(not Path("/proc/locks").exists(), reason="requires Linux /proc")
def test_dynamic_log_database_reports_running_holder_pid(codex_home):
    _make_db(codex_home, "state_12.sqlite")
    db = _make_db(codex_home, "logs_17.sqlite")

    with _sqlite_holder(db) as holder:
        status = sl.probe(codex_home)

        assert status.locked is True
        assert status.db_path == db
        assert status.holder_pids == [holder.pid]
        assert f"PID {holder.pid} (running)" in status.detail
        assert str(db) in sl.remediation_hint(status)


@pytest.mark.skipif(not Path("/proc/locks").exists(), reason="requires Linux /proc")
def test_stopped_test_holder_is_named_and_classified(codex_home):
    db = _make_db(codex_home)

    with _sqlite_holder(db) as holder:
        os.kill(holder.pid, signal.SIGSTOP)
        _wait_for_state(holder.pid, stopped=True)
        status = sl.probe(codex_home)

        assert status.locked is True
        assert status.holder_pids == [holder.pid]
        assert status.running_pids == []
        assert status.stopped_pids == [holder.pid]
        assert f"PID {holder.pid} (stopped)" in status.detail
        hint = sl.remediation_hint(status)
        assert "resumed and exited normally" in hint
        assert "kill" not in hint.lower()

        os.kill(holder.pid, signal.SIGCONT)
        _wait_for_state(holder.pid, stopped=False)


@pytest.mark.skipif(not Path("/proc/locks").exists(), reason="requires Linux /proc")
def test_wal_shm_attachment_is_detected(codex_home):
    db = _make_db(codex_home)

    with _sqlite_holder(db, mode="wal") as holder:
        assert Path(f"{db}-shm").exists()
        status = sl.probe(codex_home)

        assert status.locked is True
        assert status.holder_pids == [holder.pid]
        assert f"PID {holder.pid} (running)" in status.detail


def test_sqlite_main_and_shm_lock_byte_ranges(codex_home):
    db = _make_db(codex_home)
    shm = Path(f"{db}-shm")
    shm.touch()
    targets = {target.role: target for target in sl._target_files(db)[0]}

    def lock_for(role: str, start: int, end: int):
        target = targets[role]
        major, minor = target.proc_device
        return sl._ProcLock(123, major, minor, target.inode, start, end)

    assert sl._lock_targets_file(
        lock_for("main", sl._SQLITE_PENDING_BYTE, sl._SQLITE_PENDING_BYTE),
        targets["main"],
    )
    assert sl._lock_targets_file(lock_for("shm", 120, 127), targets["shm"])
    assert sl._lock_targets_file(lock_for("shm", 128, 128), targets["shm"])
    assert not sl._lock_targets_file(lock_for("main", 0, 100), targets["main"])
    assert not sl._lock_targets_file(lock_for("shm", 0, 100), targets["shm"])


@pytest.mark.parametrize(
    ("state", "expected_running", "expected_stopped"),
    [("S", [4242], []), ("T", [], [4242])],
)
def test_synthetic_proc_shm_only_lock_names_holder_state(
    codex_home, tmp_path, state, expected_running, expected_stopped
):
    """Prove SHM-byte attribution without a main-database descriptor."""
    db = _make_db(codex_home, "logs_88.sqlite")
    shm = Path(f"{db}-shm")
    shm.write_bytes(b"\0" * 256)
    target = next(item for item in sl._target_files(db)[0] if item.role == "shm")
    major, minor = target.proc_device

    proc_root = tmp_path / "proc"
    process = _write_synthetic_process(
        proc_root,
        4242,
        uid=db.stat().st_uid,
        state=state,
    )
    (process / "fd" / "7").symlink_to(shm)
    lock_row = f"9: POSIX ADVISORY WRITE 4242 {major:02x}:{minor:02x}:{target.inode} 120 127"
    (process / "fdinfo" / "7").write_text(f"lock:\t{lock_row}\n")
    (proc_root / "locks").write_text(lock_row + "\n")

    status = sl.probe(codex_home, proc_root=proc_root)

    assert status.locked is True
    assert status.db_path == db
    assert status.holder_pids == [4242]
    assert status.diagnostics_complete is True
    assert status.running_pids == expected_running
    assert status.stopped_pids == expected_stopped
    assert f"PID 4242 ({'stopped' if state == 'T' else 'running'})" in status.detail


def test_proc_lock_parser_accepts_global_and_fdinfo_rows():
    global_row = "7: POSIX ADVISORY READ 4321 08:02:99 120 127"
    fdinfo_row = "lock:\t7: POSIX ADVISORY WRITE 4321 08:02:99 128 128"

    global_lock = sl._parse_proc_lock_line(global_row)
    fdinfo_lock = sl._parse_proc_lock_line(fdinfo_row)

    assert global_lock == sl._ProcLock(4321, 8, 2, 99, 120, 127)
    assert fdinfo_lock == sl._ProcLock(4321, 8, 2, 99, 128, 128)


def test_codex_pid_file_is_not_trusted_as_lock_attribution(codex_home, tmp_path):
    _make_db(codex_home)
    (codex_home / "codex.pid").write_text(f"{os.getpid()}\n")

    status = sl.probe(codex_home, proc_root=_complete_empty_proc(tmp_path))

    assert status.locked is False
    assert status.holder_pids == []


def test_malformed_state_file_without_attachment_is_not_locked(codex_home, tmp_path):
    (codex_home / "state_42.sqlite").write_text("not a SQLite database")

    status = sl.probe(codex_home, proc_root=_complete_empty_proc(tmp_path))

    assert status.exists is True
    assert status.locked is False
