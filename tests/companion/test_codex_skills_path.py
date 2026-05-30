# SPDX-License-Identifier: Apache-2.0
"""Skills installer must write to the canonical $HOME/.agents/skills path.

Background: pre-L3 installs landed at ``~/.codex/skills``, which Codex
does not scan (its discovery paths are ``.agents/skills`` and
``$HOME/.agents/skills``).  Those installs were effectively dead.  This
test pins the canonical target and the defensive dual-path uninstall.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tokenpak.companion.codex import skills_installer as si

# ---------------------------------------------------------------------------
# _DEFAULT_TARGET — spec-canonical user path
# ---------------------------------------------------------------------------

def test_default_target_is_agents_skills_not_codex_skills():
    assert si._DEFAULT_TARGET == Path.home() / ".agents" / "skills"
    assert si._DEFAULT_TARGET != Path.home() / ".codex" / "skills"


def test_legacy_target_is_pre_l3_codex_skills_path():
    assert si._LEGACY_TARGET == Path.home() / ".codex" / "skills"


# ---------------------------------------------------------------------------
# install_skills uses the new path when no target_dir is provided
# ---------------------------------------------------------------------------

def test_install_skills_writes_to_explicit_target(tmp_path: Path):
    target = tmp_path / "agents_skills"
    installed = si.install_skills(target_dir=target)
    assert installed, "expected at least one bundled skill"
    for path in installed:
        assert path.parent == target
        assert (path / "SKILL.md").exists()


def test_install_skills_default_target_is_dot_agents(monkeypatch, tmp_path: Path):
    """When called with no target, install_skills() must land under the
    canonical ``$HOME/.agents/skills`` location."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # The module captured _DEFAULT_TARGET at import time; rebind it for
    # the assertion so we test the constant's *intent* (canonical path),
    # not just the import-time literal.
    new_default = fake_home / ".agents" / "skills"
    monkeypatch.setattr(si, "_DEFAULT_TARGET", new_default)
    installed = si.install_skills()
    assert installed
    for path in installed:
        assert str(path).startswith(str(new_default)), (
            f"skill {path} not under canonical default {new_default}"
        )


# ---------------------------------------------------------------------------
# uninstall_skills sweeps BOTH the new and legacy paths by default
# ---------------------------------------------------------------------------

def test_uninstall_skills_sweeps_both_targets(monkeypatch, tmp_path: Path):
    new_target = tmp_path / "agents" / "skills"
    legacy_target = tmp_path / "codex" / "skills"
    monkeypatch.setattr(si, "_DEFAULT_TARGET", new_target)
    monkeypatch.setattr(si, "_LEGACY_TARGET", legacy_target)

    # Plant skills at BOTH locations as if a user upgraded from pre-L3.
    si.install_skills(target_dir=new_target)
    si.install_skills(target_dir=legacy_target)
    assert any(new_target.iterdir())
    assert any(legacy_target.iterdir())

    removed = si.uninstall_skills()
    # Every bundled skill should appear exactly once in `removed`, and
    # both target trees should be empty of bundled skills.
    assert sorted(removed) == sorted(si.bundled_skill_names())
    for name in si.bundled_skill_names():
        assert not (new_target / name).exists()
        assert not (legacy_target / name).exists()


def test_uninstall_skills_with_explicit_target_does_not_sweep_legacy(
    monkeypatch, tmp_path: Path
):
    new_target = tmp_path / "agents" / "skills"
    legacy_target = tmp_path / "codex" / "skills"
    monkeypatch.setattr(si, "_DEFAULT_TARGET", new_target)
    monkeypatch.setattr(si, "_LEGACY_TARGET", legacy_target)

    si.install_skills(target_dir=new_target)
    si.install_skills(target_dir=legacy_target)

    # Explicit target_dir disables the defensive dual-sweep — the caller
    # asked for a specific directory and gets only that.
    si.uninstall_skills(target_dir=new_target)
    assert not any(new_target.iterdir())
    assert any(legacy_target.iterdir()), "legacy path should be untouched"


# ---------------------------------------------------------------------------
# orphaned_legacy_skills surfaces pre-L3 installs for doctor reporting
# ---------------------------------------------------------------------------

def test_orphaned_legacy_skills_empty_when_nothing_at_legacy_path(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(si, "_LEGACY_TARGET", tmp_path / "nowhere")
    assert si.orphaned_legacy_skills() == []


def test_orphaned_legacy_skills_lists_installed_pre_l3_skills(
    monkeypatch, tmp_path: Path
):
    legacy_target = tmp_path / "codex_skills"
    monkeypatch.setattr(si, "_LEGACY_TARGET", legacy_target)
    si.install_skills(target_dir=legacy_target)

    orphans = si.orphaned_legacy_skills()
    assert orphans == si.bundled_skill_names(), (
        "every bundled skill installed at the legacy path should appear "
        "as an orphan until uninstall + reinstall"
    )
