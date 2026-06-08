# SPDX-License-Identifier: Apache-2.0
"""Doctor lane: WARN-status surfacing + new checks (L3 audit deltas).

Covers:
- ``check_skills_installed`` reads the canonical ``$HOME/.agents/skills``
  path (not the pre-L3 ``~/.codex/skills`` hardcode).
- ``check_skills_legacy_orphans`` WARNs on pre-L3 leftovers.
- ``check_agents_md_size`` WARNs once AGENTS.md crosses the 80%-of-32-KiB
  threshold.
- ``check_hooks_feature`` parses the fixture-captured ``codex features
  list`` output and emits a WARN row for the ``under development`` label.
- ``run()`` exit code is 0 when the failure set is empty even if WARNs
  are present, and the summary line names the WARN count.
"""
from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest

from tokenpak.companion.codex import doctor
from tokenpak.companion.codex import skills_installer as si

_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "codex"
    / "codex_features_list.txt"
)


# ---------------------------------------------------------------------------
# check_skills_installed — canonical path
# ---------------------------------------------------------------------------

def test_check_skills_installed_uses_dot_agents_path(monkeypatch, tmp_path: Path):
    target = tmp_path / "agents_skills"
    si.install_skills(target_dir=target)
    monkeypatch.setattr(doctor, "SKILLS_TARGET", target)
    status, detail = doctor.check_skills_installed()
    assert status == "PASS"
    assert str(target) in detail


def test_check_skills_installed_fails_when_canonical_path_missing(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(doctor, "SKILLS_TARGET", tmp_path / "does_not_exist")
    status, detail = doctor.check_skills_installed()
    assert status == "FAIL"
    assert "missing" in detail.lower()


def test_check_skills_installed_does_not_lie_about_codex_discovery(
    monkeypatch, tmp_path: Path
):
    """The PASS detail must hint that Codex-side verification is L5 work.

    Pre-L3 the check stated "N skills present" with no indication that we
    were stat-ing a directory Codex doesn't even scan.  The detail string
    is part of the contract."""
    target = tmp_path / "agents_skills"
    si.install_skills(target_dir=target)
    monkeypatch.setattr(doctor, "SKILLS_TARGET", target)
    status, detail = doctor.check_skills_installed()
    assert status == "PASS"
    assert "pending" in detail.lower() or "L5" in detail


# ---------------------------------------------------------------------------
# check_skills_legacy_orphans
# ---------------------------------------------------------------------------

def test_check_skills_legacy_orphans_warns_when_legacy_path_populated(
    monkeypatch, tmp_path: Path
):
    legacy = tmp_path / "codex_skills"
    monkeypatch.setattr(si, "_LEGACY_TARGET", legacy)
    si.install_skills(target_dir=legacy)
    status, detail = doctor.check_skills_legacy_orphans()
    assert status == "WARN"
    assert "uninstall" in detail.lower()
    for name in si.bundled_skill_names():
        assert name in detail


def test_check_skills_legacy_orphans_passes_when_no_legacy_install(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(si, "_LEGACY_TARGET", tmp_path / "nope")
    status, _ = doctor.check_skills_legacy_orphans()
    assert status == "PASS"


# ---------------------------------------------------------------------------
# check_agents_md_size
# ---------------------------------------------------------------------------

def test_check_agents_md_size_passes_below_threshold(
    monkeypatch, tmp_path: Path
):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "AGENTS.md").write_text("x" * 1024)
    monkeypatch.setenv("HOME", str(home))
    # Path.home() consults HOME on POSIX; covers the default-Path lookup.
    status, detail = doctor.check_agents_md_size()
    assert status == "PASS"
    assert "1024" in detail


def test_check_agents_md_size_warns_at_or_above_80pct(
    monkeypatch, tmp_path: Path
):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    # 80% of 32 KiB = 26214 bytes; write at threshold exactly.
    threshold = int(32 * 1024 * 0.80)
    (home / ".codex" / "AGENTS.md").write_text("x" * threshold)
    monkeypatch.setenv("HOME", str(home))
    status, detail = doctor.check_agents_md_size()
    assert status == "WARN"
    assert "80%" in detail


def test_check_agents_md_size_skipped_when_file_missing(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    status, detail = doctor.check_agents_md_size()
    # Missing file is reported by the separate AGENTS.md presence check;
    # the size check must not double-fail.
    assert status == "PASS"
    assert "skipped" in detail.lower() or "missing" in detail.lower()


def test_check_agents_md_size_threshold_is_configurable(
    monkeypatch, tmp_path: Path
):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "AGENTS.md").write_text("x" * 100)
    monkeypatch.setenv("HOME", str(home))
    # Tiny cap → 100 bytes is over threshold.
    status, _ = doctor.check_agents_md_size(max_bytes=100, warn_fraction=0.5)
    assert status == "WARN"


# ---------------------------------------------------------------------------
# check_hooks_feature — fixture-driven (no live `codex features list`)
# ---------------------------------------------------------------------------

def _run_with_fixture_features_list(monkeypatch):
    """Make subprocess.run('codex features list') return the captured fixture."""
    fixture_text = _FIXTURE.read_text()

    class FakeCompleted:
        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["codex", "features", "list"]:
            return FakeCompleted(fixture_text)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)


def test_check_hooks_feature_warns_under_development(monkeypatch):
    _run_with_fixture_features_list(monkeypatch)
    status, detail = doctor.check_hooks_feature()
    assert status == "WARN"
    assert "under development" in detail.lower()
    assert "codex may break this" in detail.lower()


def test_check_hooks_feature_failure_when_row_absent(monkeypatch):
    class FakeCompleted:
        stdout = "other_feature  stable  true\n"
        stderr = ""
        returncode = 0

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["codex", "features", "list"]:
            return FakeCompleted()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    status, detail = doctor.check_hooks_feature()
    assert status == "FAIL"
    assert "hooks" in detail


def test_parse_hooks_maturity_multiword_label():
    sample = "hooks   under development   true\n"
    label_enabled = doctor._parse_hooks_maturity(sample)
    assert label_enabled == ("under development", True)


def test_parse_hooks_maturity_returns_none_when_missing():
    assert doctor._parse_hooks_maturity("other  stable  true\n") is None


# ---------------------------------------------------------------------------
# run() — exit code does not gate on WARN rows
# ---------------------------------------------------------------------------

def test_run_exit_zero_with_only_warn_rows(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor,
        "CHECKS",
        [
            ("dummy ok", lambda: ("PASS", "fine")),
            ("dummy warn", lambda: ("WARN", "advisory only")),
        ],
    )
    rc = doctor.run()
    assert rc == 0
    captured = capsys.readouterr()
    assert "[PASS]" in captured.out
    assert "[WARN]" in captured.out
    assert "1 WARN" in captured.out


def test_run_exit_one_when_any_fail(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor,
        "CHECKS",
        [
            ("dummy fail", lambda: ("FAIL", "bad")),
            ("dummy warn", lambda: ("WARN", "advisory")),
        ],
    )
    rc = doctor.run()
    assert rc == 1
    out = capsys.readouterr().out
    assert "1 FAIL" in out
    assert "some checks failed" in out
