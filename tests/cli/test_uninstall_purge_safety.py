# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the destructive-path defects found by adversarial review.

Each test names the input that previously caused unrecoverable data loss. They
exercise the real primitives rather than mocking the decision under test — the
original suite mocked exactly the function that was wrong, and passed.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from tokenpak.cli.commands import uninstall as U


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / ".tpk"
    (h / "companion" / "capsules" / "run").mkdir(parents=True)
    (h / "companion" / "run").mkdir(parents=True)
    (h / "config.yaml").write_text("profile: balanced")
    (h / "companion" / "journal.db").write_text("journal")
    (h / "companion" / "capsules" / "run" / "notes.json").write_text("{}")
    (h / "companion" / "run" / "agent.sock").write_text("")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return h


# H1 — a non-answer must never authorise deletion.
def test_backup_prompt_returns_none_on_eof_not_true(monkeypatch):
    """EOF is a stop. Returning True here let Ctrl-C complete the uninstall."""

    def _eof(*_a):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert U._confirm_backup() is None


def test_backup_prompt_returns_none_on_interrupt(monkeypatch):
    def _int(*_a):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _int)
    assert U._confirm_backup() is None


@pytest.mark.parametrize("answer,expected", [("", True), ("n", False), ("no", False), ("y", True)])
def test_backup_prompt_real_answers_unchanged(answer, expected, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a: answer)
    assert U._confirm_backup() is expected


def test_purge_without_tty_requires_confirm_word_not_just_yes(home, monkeypatch, capsys):
    """--yes is assent to run unattended; it is not assent to destroy."""
    monkeypatch.setattr(U.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(U.sys.stdout, "isatty", lambda: False, raising=False)
    rc = U.run_uninstall(purge=True, yes=True, no_backup=True)
    assert rc == 2
    assert "--confirm DELETE" in capsys.readouterr().err
    assert home.exists(), "refusal must not delete"


# H2 — the archive must not live inside what the run deletes.
def test_backup_destination_inside_delete_target_is_refused(home):
    reason = U.validate_backup_dest(home / "b.tar.gz", [home])
    assert reason and "inside" in reason


def test_backup_destination_outside_targets_is_allowed(home, tmp_path):
    assert U.validate_backup_dest(tmp_path / "b.tar.gz", [home]) is None


# H3 — an unidentifiable home is not a delete target.
@pytest.mark.parametrize(
    "factory",
    [
        lambda tmp: tmp,  # the user's home itself
        lambda tmp: tmp / "empty_unrelated",  # no TokenPak markers
    ],
)
def test_purge_refuses_a_home_it_cannot_identify(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    target = factory(tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    (target / "thesis.txt").write_text("irreplaceable")
    assert U.validate_purge_home(target) is not None


def test_purge_accepts_a_real_tokenpak_home(home):
    assert U.validate_purge_home(home) is None


# H5 — a symlinked home defeats both removal and archiving.
def test_purge_refuses_a_symlinked_home(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    (real / "config.yaml").write_text("x")
    link = tmp_path / ".tpk"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    reason = U.validate_purge_home(link)
    assert reason and "symlink" in reason
    assert (real / "config.yaml").exists()


# H4 — exclusions are prefix-scoped, and verification compares against the plan.
def test_capsule_under_a_run_directory_is_archived(home, tmp_path):
    dest = tmp_path / "b.tar.gz"
    ok, detail = U._create_backup(home, dest, [])
    assert ok, detail
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("capsules/run/notes.json") for n in names), (
        "a capsule stored under a directory named 'run' was silently dropped"
    )
    # The genuine runtime dir and the socket suffix are still excluded.
    assert not any(n.endswith("companion/run/agent.sock") for n in names)


def test_verification_rejects_an_archive_missing_deletable_files(home, tmp_path, monkeypatch):
    """The archive must be checked against the delete plan, not just be non-empty."""
    real_add = tarfile.TarFile.add

    def _add_almost_nothing(self, name, arcname=None, recursive=True, filter=None):
        # Archive only the top-level config, dropping the rest.
        return real_add(self, Path(name) / "config.yaml", arcname=f"{arcname}/config.yaml")

    monkeypatch.setattr(tarfile.TarFile, "add", _add_almost_nothing)
    ok, detail = U._create_backup(home, tmp_path / "b.tar.gz", [])
    assert ok is False
    assert "missing" in detail


# M1 — a split install leaves a second home; purging one left the other whole.
def test_legacy_home_is_included_when_distinct(home, tmp_path, monkeypatch):
    legacy = tmp_path / ".tokenpak"
    (legacy / "companion").mkdir(parents=True)
    (legacy / "config.json").write_text("{}")
    (legacy / "companion" / "journal.db").write_text("legacy journal")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    paths = U._external_state_paths()
    assert legacy in paths, "the legacy home survived a complete uninstall"


def test_legacy_home_not_double_listed_when_it_is_the_resolved_home(tmp_path, monkeypatch):
    legacy = tmp_path / ".tokenpak"
    (legacy).mkdir()
    (legacy / "config.json").write_text("{}")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(U, "_external_state_paths", U._external_state_paths)
    from tokenpak import _paths

    monkeypatch.setattr(_paths, "home", lambda: legacy)
    assert legacy not in U._external_state_paths()


# M3 — the printed restore command must actually restore.
def test_archive_round_trips_to_the_documented_location(home, tmp_path):
    """`tar -xzf <archive> -C ~` must land every member back where it came from."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json.bak").write_text('{"env": {"SENTINEL": "x"}}')
    dest = tmp_path / "b.tar.gz"

    ok, _ = U._create_backup(home, dest, U._external_state_paths())
    assert ok

    restore_root = tmp_path / "restored"
    restore_root.mkdir()
    with tarfile.open(dest, "r:gz") as tar:
        tar.extractall(restore_root, filter="data")

    # Exactly the layout `-C ~` produces: no synthetic prefix directory.
    assert (restore_root / ".tpk" / "config.yaml").read_text() == "profile: balanced"
    assert (restore_root / ".claude" / "settings.json.bak").exists()
    assert not (restore_root / "external").exists(), (
        "an 'external/' prefix makes the documented restore command wrong"
    )
