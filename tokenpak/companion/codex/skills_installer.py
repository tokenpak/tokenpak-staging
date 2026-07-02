# SPDX-License-Identifier: Apache-2.0
"""Install TokenPak skills into the canonical Codex skills directory.

Skills are copied from the bundled ``skills/`` directory to
``~/.agents/skills/``.  The set of skills is discovered at runtime by
globbing for ``SKILL.md`` — no hardcoded enumeration (see
``feedback_always_dynamic.md``).  Uninstall uses the same glob so the
two halves can never drift.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_BUNDLED_SKILLS = Path(__file__).parent / "skills"


def _default_target() -> Path:
    return Path.home() / ".agents" / "skills"


def _legacy_target() -> Path:
    return Path.home() / ".codex" / "skills"


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
    """Copy bundled skills to the canonical Codex skills directory.

    Existing tokenpak skills are replaced so updates propagate; other
    skills in the target directory are untouched. Existing skill files are
    updated with same-directory atomic replaces so a TUI reload never observes
    an empty or partially-written ``SKILL.md``.
    """
    target = target_dir or _default_target()
    target.mkdir(parents=True, exist_ok=True)

    installed: list[Path] = []
    for name in bundled_skill_names():
        src = _BUNDLED_SKILLS / name
        dst = target / name
        _sync_skill_tree(src, dst)
        installed.append(dst)

    return installed


def list_installed_skills(target_dir: Path | None = None) -> list[str]:
    """Return the bundled skills currently present in the target dir."""
    target = target_dir or _default_target()
    return [name for name in bundled_skill_names() if (target / name).exists()]


def uninstall_skills(target_dir: Path | None = None) -> list[str]:
    """Remove every bundled tokenpak skill from the target dir.

    Returns the names that were actually removed.
    """
    target = target_dir or _default_target()
    removed: list[str] = []
    for name in bundled_skill_names():
        dst = target / name
        if dst.exists():
            shutil.rmtree(dst)
            removed.append(name)
    return removed


def _sync_skill_tree(src: Path, dst: Path) -> None:
    if not dst.exists():
        _copy_new_tree(src, dst)
        return
    if not dst.is_dir():
        dst.unlink()
        _copy_new_tree(src, dst)
        return

    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            _copy_file_atomic(path, target)

    for path in sorted(dst.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = path.relative_to(dst)
        if (src / rel).exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _copy_new_tree(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(src, tmp)
    try:
        try:
            os.replace(tmp, dst)
        except OSError:
            if dst.exists() and dst.is_dir():
                _sync_skill_tree(tmp, dst)
            else:
                raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def _copy_file_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
    shutil.copy2(src, tmp)
    try:
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()
