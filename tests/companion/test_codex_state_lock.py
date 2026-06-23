# SPDX-License-Identifier: Apache-2.0
"""Tests for the Codex state-database lock preflight.

Re-authored as part of the isolated-CODEX_HOME work (absorbs the prior
state-lock diagnostics + lock-message rework): a fresh/free home preflights
clean, a database held by another connection is reported ``locked``, and a
stopped (job-control-suspended) holder produces a distinct, actionable
remediation message.
"""

from __future__ import annotations

import sqlite3
import threading
import time
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


def _make_state_db(home: Path) -> Path:
    db = home / sl.STATE_DB_NAME
    return _make_db(db)


def _make_log_db(home: Path) -> Path:
    db = home / "logs_2.sqlite"
    return _make_db(db)


def _make_db(db: Path) -> Path:
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
    conn.commit()
    conn.close()
    return db


# ── absent / free database ────────────────────────────────────────────

def test_probe_absent_db_is_unlocked(codex_home):
    status = sl.probe(codex_home)
    assert status.exists is False
    assert status.locked is False
    assert "uncontended" in status.detail


def test_probe_free_db_is_unlocked(codex_home):
    _make_state_db(codex_home)
    status = sl.probe(codex_home)
    assert status.exists is True
    assert status.locked is False


def test_probe_defaults_to_codex_home_env(codex_home, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _make_state_db(codex_home)
    status = sl.probe()  # no arg → reads $CODEX_HOME
    assert status.home == codex_home
    assert status.locked is False


# ── contended database ────────────────────────────────────────────────

def test_probe_detects_locked_db(codex_home):
    db = _make_state_db(codex_home)
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")  # hold an exclusive lock
    try:
        status = sl.probe(codex_home)
        assert status.locked is True
        assert status.exists is True
        assert "locked" in status.detail.lower()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_probe_detects_locked_log_db(codex_home):
    db = _make_log_db(codex_home)
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        status = sl.probe(codex_home)
        assert status.locked is True
        assert status.db_path == db
        assert status.db_label == "log database"
        assert "busy/locked" in status.detail
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_internal_wait_until_unlocked_retries_until_free(monkeypatch, codex_home):
    locked = sl.LockStatus(
        home=codex_home,
        db_path=codex_home / "logs_2.sqlite",
        exists=True,
        locked=True,
        detail="log database is busy/locked",
        db_label="log database",
    )
    free = sl.LockStatus(
        home=codex_home,
        db_path=codex_home / "logs_2.sqlite",
        exists=True,
        locked=False,
        detail="log database is free",
        db_label="log database",
    )
    statuses = [locked, free]
    sleeps = []

    def fake_probe(home):
        assert home == codex_home
        return statuses.pop(0)

    monkeypatch.setattr(sl, "probe", fake_probe)
    monkeypatch.setattr(sl.time, "sleep", lambda seconds: sleeps.append(seconds))

    status = sl._wait_until_unlocked(
        codex_home,
        timeout_s=1.0,
        poll_interval_s=0.01,
    )

    assert status.locked is False
    assert sleeps


def test_locked_status_with_no_holder_pid_still_locked(codex_home):
    db = _make_state_db(codex_home)
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        status = sl.probe(codex_home)
        # no codex.pid sentinel written → holder_pids empty, but locked holds
        assert status.locked is True
        assert status.holder_pids == []
        assert "holder PID unavailable" in status.detail
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# ── holder PID context + remediation message ──────────────────────────

def test_remediation_hint_for_locked_shared_home(codex_home):
    db = _make_state_db(codex_home)
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        status = sl.probe(codex_home)
        hint = sl.remediation_hint(status)
        assert str(db) in hint
        # message points the user back to the automatic parallel-safe path
        assert "TokenPak auto mode normally starts a parallel-safe session" in hint
        assert "unset the forced session mode" in hint
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_locked_with_live_holder_pid_names_it(codex_home, monkeypatch):
    db = _make_state_db(codex_home)
    pid = 12345
    (codex_home / "codex.pid").write_text(f"{pid}\n")
    monkeypatch.setattr(sl, "_pid_alive", lambda p: p == pid)
    monkeypatch.setattr(sl, "_pid_stopped", lambda pid: False)
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        status = sl.probe(codex_home)
        assert status.locked is True
        assert pid in status.holder_pids
        assert "active Codex process" in status.detail
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_locked_with_stopped_holder_pid_distinct_message(codex_home, monkeypatch):
    db = _make_state_db(codex_home)
    pid = 12345
    (codex_home / "codex.pid").write_text(f"{pid}\n")
    monkeypatch.setattr(sl, "_pid_alive", lambda p: p == pid)
    monkeypatch.setattr(sl, "_pid_stopped", lambda p: True)
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        status = sl.probe(codex_home)
        assert status.locked is True
        assert pid in status.stopped_pids
        assert "stopped Codex process" in status.detail
        hint = sl.remediation_hint(status)
        assert "kill -CONT" in hint
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_dead_holder_pid_dropped_from_candidates(codex_home):
    db = _make_state_db(codex_home)
    (codex_home / "codex.pid").write_text("2147480000\n")  # implausible/dead
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        status = sl.probe(codex_home)
        assert status.locked is True
        assert status.holder_pids == []  # dead PID filtered out
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# ── non-database / malformed file is not a lock ───────────────────────

def test_malformed_state_file_is_not_a_lock(codex_home):
    (codex_home / sl.STATE_DB_NAME).write_text("not a sqlite db at all")
    status = sl.probe(codex_home)
    assert status.locked is False
