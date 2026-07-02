# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from tokenpak.companion.codex import doctor, skills_installer


def test_default_target_is_agents_skills_not_codex_skills(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert skills_installer._default_target() == tmp_path / ".agents" / "skills"
    assert skills_installer._legacy_target() == tmp_path / ".codex" / "skills"


def test_install_skills_defaults_to_agents_skills(monkeypatch, tmp_path):
    home = tmp_path / "home"
    bundled = tmp_path / "bundled"
    skill = bundled / "tokenpak-example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: tokenpak-example\n---\n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(skills_installer, "_BUNDLED_SKILLS", bundled)

    installed = skills_installer.install_skills()

    assert installed == [home / ".agents" / "skills" / "tokenpak-example"]
    assert (home / ".agents" / "skills" / "tokenpak-example" / "SKILL.md").exists()
    assert not (home / ".codex" / "skills" / "tokenpak-example").exists()


def test_install_skills_replaces_skill_md_without_removing_directory(
    monkeypatch, tmp_path
):
    target = tmp_path / "target"
    bundled = tmp_path / "bundled"
    skill = bundled / "tokenpak-example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: tokenpak-example\n---\n")
    (skill / "notes.md").write_text("current\n")

    existing = target / "tokenpak-example"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("old\n")
    (existing / "stale.md").write_text("stale\n")

    monkeypatch.setattr(skills_installer, "_BUNDLED_SKILLS", bundled)

    before_inode = existing.stat().st_ino
    skills_installer.install_skills(target)

    assert existing.stat().st_ino == before_inode
    assert (existing / "SKILL.md").read_text().startswith("---\n")
    assert (existing / "notes.md").read_text() == "current\n"
    assert not (existing / "stale.md").exists()


def test_doctor_checks_canonical_agents_skills(monkeypatch, tmp_path):
    home = tmp_path / "home"
    skill = home / ".agents" / "skills" / "tokenpak-example"
    skill.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(doctor, "bundled_skill_names", lambda: ["tokenpak-example"])

    ok, detail = doctor.check_skills_installed()

    assert ok is True
    assert "1 skills present" in detail


def test_doctor_warns_on_legacy_codex_skill_orphans(monkeypatch, tmp_path):
    home = tmp_path / "home"
    legacy = home / ".codex" / "skills" / "tokenpak-example"
    legacy.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(doctor, "bundled_skill_names", lambda: ["tokenpak-example"])

    status, detail = doctor._check_legacy_skills_orphans()

    assert status == "WARN"
    assert "legacy .codex copy" in detail
    assert "duplicate non-atomic skill synchronization" in detail
