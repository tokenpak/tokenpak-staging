# SPDX-License-Identifier: Apache-2.0
"""Install TokenPak skills into the Codex skills directory.

Skills are copied from the bundled ``skills/`` directory to the user
skill-discovery path Codex actually scans: ``$HOME/.agents/skills``
(spec: https://developers.openai.com/docs/guides/tools-skills). The set
of skills is discovered at runtime by globbing for ``SKILL.md`` — no
hardcoded enumeration (discovery stays dynamic).  Uninstall
sweeps both the canonical path AND the pre-L3 legacy ``~/.codex/skills``
location so users upgrading from earlier installs don't leave orphans.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_BUNDLED_SKILLS = Path(__file__).parent / "skills"
# Canonical user-scope path per Codex skill-discovery spec.
_DEFAULT_TARGET = Path.home() / ".agents" / "skills"
# Pre-L3 install path. Kept for defensive uninstall + doctor orphan reporting.
_LEGACY_TARGET = Path.home() / ".codex" / "skills"


def bundled_skill_names() -> list[str]:
    """Return the names of all skills shipped with this package.

    A directory under :data:`_BUNDLED_SKILLS` counts as a skill only if
    it contains a ``SKILL.md`` file.
    """
    if not _BUNDLED_SKILLS.exists():
        return []
    return sorted(
        p.name
        for p in _BUNDLED_SKILLS.iterdir()
        if p.is_dir() and (p / "SKILL.md").exists()
    )


def install_skills(target_dir: Path | None = None) -> list[Path]:
    """Copy bundled skills to the Codex skills directory.

    Existing tokenpak skills are replaced so updates propagate; other
    skills in the target directory are untouched.
    """
    target = target_dir or _DEFAULT_TARGET
    target.mkdir(parents=True, exist_ok=True)

    installed: list[Path] = []
    for name in bundled_skill_names():
        src = _BUNDLED_SKILLS / name
        dst = target / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        installed.append(dst)

    return installed


def list_installed_skills(target_dir: Path | None = None) -> list[str]:
    """Return the bundled skills currently present in the target dir."""
    target = target_dir or _DEFAULT_TARGET
    return [name for name in bundled_skill_names() if (target / name).exists()]


def uninstall_skills(target_dir: Path | None = None) -> list[str]:
    """Remove every bundled tokenpak skill from the target dir(s).

    When ``target_dir`` is omitted, sweeps both the canonical
    ``~/.agents/skills`` path AND the pre-L3 legacy ``~/.codex/skills``
    location so users migrating off the old path are cleaned up in one
    pass.  Returns the names that were actually removed (deduped, in
    bundled order).
    """
    if target_dir is not None:
        targets = [target_dir]
    else:
        targets = [_DEFAULT_TARGET, _LEGACY_TARGET]

    removed: list[str] = []
    for name in bundled_skill_names():
        was_removed = False
        for target in targets:
            dst = target / name
            if dst.exists():
                shutil.rmtree(dst)
                was_removed = True
        if was_removed:
            removed.append(name)
    return removed


def orphaned_legacy_skills() -> list[str]:
    """Return bundled-skill names still installed at the pre-L3 legacy path.

    Doctor surfaces these so users can clean them up explicitly; we do
    not auto-migrate (a user may have customized a skill in place, and a
    silent overwrite would clobber the edit — see packet "Migration
    discipline").
    """
    return [
        name
        for name in bundled_skill_names()
        if (_LEGACY_TARGET / name).exists()
    ]
