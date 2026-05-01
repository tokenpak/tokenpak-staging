"""Edge-case coverage for local companion capsule retention helpers."""

from __future__ import annotations

from pathlib import Path

from tokenpak.companion.capsules.retention import (
    ACTIVE_CAPSULE_NAME,
    apply_capsule_retention,
    capsule_build_enabled,
    refresh_active_capsule,
)


def _write_capsule(path: Path, *, size: int = 900, mtime: float) -> Path:
    path.write_text("# capsule\n" + ("x" * size), encoding="utf-8")
    path.touch()
    # Explicit mtime keeps the retention order deterministic and independent of
    # the test host clock granularity.
    import os

    os.utime(path, (mtime, mtime))
    return path


def test_capsule_build_gate_is_default_off_and_truthy_only() -> None:
    assert capsule_build_enabled({}) is False
    assert capsule_build_enabled({"TOKENPAK_TIP_CAPSULE_BUILD_ENABLED": "0"}) is False
    assert capsule_build_enabled({"TOKENPAK_TIP_CAPSULE_BUILD_ENABLED": "false"}) is False

    for value in ("1", "true", "TRUE", "yes", "YES", "on", "ON"):
        assert capsule_build_enabled({"TOKENPAK_TIP_CAPSULE_BUILD_ENABLED": value}) is True


def test_retention_prunes_by_age_and_preserves_active_md(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    old = _write_capsule(tmp_path / "old-session.md", mtime=now - (15 * 86400))
    recent = _write_capsule(tmp_path / "recent-session.md", mtime=now - 60)
    active = _write_capsule(tmp_path / ACTIVE_CAPSULE_NAME, size=64, mtime=now - (90 * 86400))

    result = apply_capsule_retention(tmp_path, now=now, max_age_days=14, max_files=50)

    assert old in result.pruned
    assert not old.exists()
    assert recent.exists()
    assert active.exists()
    assert active.name not in {path.name for path in result.remaining}


def test_retention_keeps_newest_count_without_counting_active_md(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    active = _write_capsule(tmp_path / ACTIVE_CAPSULE_NAME, size=64, mtime=now - 1000)
    for idx in range(55):
        _write_capsule(tmp_path / f"session-{idx:02d}.md", mtime=now + idx)

    result = apply_capsule_retention(tmp_path, now=now, max_age_days=14, max_files=50)

    remaining_names = {path.name for path in result.remaining}
    assert active.exists()
    assert len(remaining_names) == 50
    assert "session-54.md" in remaining_names
    assert "session-05.md" in remaining_names
    assert "session-04.md" not in remaining_names
    assert {path.name for path in result.pruned} == {f"session-{idx:02d}.md" for idx in range(5)}


def test_refresh_active_capsule_selects_largest_substantive_capsule(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    _write_capsule(tmp_path / ACTIVE_CAPSULE_NAME, size=32, mtime=now)
    small = _write_capsule(tmp_path / "small.md", size=100, mtime=now + 1)
    large = _write_capsule(tmp_path / "large.md", size=1200, mtime=now + 2)
    medium = _write_capsule(tmp_path / "medium.md", size=950, mtime=now + 3)

    selected = refresh_active_capsule(tmp_path, min_bytes=800)

    assert selected == large
    active = tmp_path / ACTIVE_CAPSULE_NAME
    assert active.is_symlink()
    assert active.readlink() == Path("large.md")
    assert small.exists()
    assert medium.exists()


def test_refresh_active_capsule_preserves_existing_active_when_no_candidate(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    active = _write_capsule(tmp_path / ACTIVE_CAPSULE_NAME, size=32, mtime=now)
    before = active.read_text(encoding="utf-8")
    _write_capsule(tmp_path / "tiny.md", size=100, mtime=now + 1)

    selected = refresh_active_capsule(tmp_path, min_bytes=800)

    assert selected is None
    assert active.exists()
    assert not active.is_symlink()
    assert active.read_text(encoding="utf-8") == before
