# SPDX-License-Identifier: Apache-2.0
"""Tests for the complete-removal (``--purge``) uninstall mode.

``--purge`` is the only mode that destroys user data, so the invariants that
matter are: it refuses to guess, it never deletes on an unverified backup, and
it removes the state that ``--hard`` leaves orphaned.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from tokenpak.cli.commands import uninstall as U


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A populated TokenPak home plus the state written outside it."""
    h = tmp_path / ".tpk"
    (h / "companion" / "capsules").mkdir(parents=True)
    (h / "companion" / "run").mkdir(parents=True)
    (h / "companion" / "journal.db").write_text("journal")
    (h / "companion" / "budget.db").write_text("budget")
    (h / "companion" / "capsules" / "c1.json").write_text("{}")
    (h / "companion" / "run" / "agent.sock").write_text("")
    (h / "monitor.db").write_text("ledger")
    (h / ".seen_intro").write_text("")
    (tmp_path / ".config" / "tokenpak").mkdir(parents=True)
    (tmp_path / ".config" / "tokenpak" / "permissions.toml").write_text("t=1")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return h


def test_purge_plan_removes_home_and_external_state(home):
    ops, retained = U._build_plan(hard=False, keep_data=False, home=home, purge=True)
    described = " ".join(op.describe for op in ops if op.phase == "purge")
    assert str(home) in described
    assert "permissions.toml" in described or ".config/tokenpak" in described
    # Nothing is protected under a complete removal.
    assert retained == []


def test_hard_still_protects_user_data(home):
    """--hard must keep its old meaning; --purge is additive, not a redefinition."""
    _ops, retained = U._build_plan(hard=True, keep_data=False, home=home, purge=False)
    names = {p.name for p in retained}
    assert {"journal.db", "budget.db", "capsules"} <= names


def test_backup_contains_protected_data_and_skips_sockets(home, tmp_path):
    dest = tmp_path / "backup.tar.gz"
    ok, detail = U._create_backup(home, dest, U._external_state_paths())
    assert ok, detail
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("companion/journal.db") for n in names)
    assert any(n.endswith("companion/budget.db") for n in names)
    assert any(n.endswith("capsules/c1.json") for n in names)
    assert any("permissions.toml" in n for n in names)
    # Live sockets are not restorable and must not be archived.
    assert not any(n.endswith(".sock") for n in names)


def test_backup_failure_is_reported_not_swallowed(home, tmp_path):
    """A backup that cannot be written must report failure, so the caller aborts."""
    dest = tmp_path / "nope" / "x.tar.gz"
    dest.parent.mkdir()
    dest.parent.chmod(0o500)  # read+execute only — cannot create inside
    try:
        ok, detail = U._create_backup(home, dest, [])
    finally:
        dest.parent.chmod(0o700)
    assert ok is False
    assert "backup" in detail.lower()


def test_purge_non_interactive_requires_explicit_backup_choice(home, monkeypatch, capsys):
    monkeypatch.setattr(U.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(U.sys.stdout, "isatty", lambda: False, raising=False)
    rc = U.run_uninstall(purge=True, yes=True)
    assert rc == 2
    assert "--backup" in capsys.readouterr().err


def test_mutually_exclusive_modes_refused(capsys):
    assert U.run_uninstall(soft=True, purge=True) == 2
    assert "only one of" in capsys.readouterr().err


def test_backup_flags_rejected_without_purge(capsys):
    assert U.run_uninstall(hard=True, no_backup=True) == 2
    assert "--purge only" in capsys.readouterr().err


@pytest.mark.parametrize("answer,expected", [("DELETE", True), ("delete", False), ("y", False)])
def test_purge_confirmation_requires_the_exact_word(answer, expected, home, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    ops, _ = U._build_plan(hard=False, keep_data=False, home=home, purge=True)
    assert U._confirm_purge(ops, home, "no backup") is expected


def test_confirmations_fail_safe_on_eof(monkeypatch):
    def _eof(*_args):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    # A non-answer must keep data: back up yes, delete no, choose nothing.
    assert U._confirm_backup() is True
    assert U._confirm_purge([], Path("/nonexistent"), "") is False
    assert U._choose_mode() is None
