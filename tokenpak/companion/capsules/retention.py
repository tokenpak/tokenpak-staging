"""
tokenpak.companion.capsules.retention
======================================

Local helper functions for companion capsule build gates, retention pruning, and
``active.md`` fallback maintenance.

These helpers are intentionally filesystem-only: they never inspect provider,
model, or platform configuration and never make network calls. They mirror the
TIP cache operations contract from Standard 26 for daily companion capsule
builds: default-off build gate, 14-day/50-file retention, and preserving the
``active.md`` fallback path outside normal capsule pruning.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
DEFAULT_CAPSULE_RETENTION_DAYS = 14
DEFAULT_CAPSULE_RETENTION_COUNT = 50
DEFAULT_ACTIVE_MIN_BYTES = 800
ACTIVE_CAPSULE_NAME = "active.md"


@dataclass(frozen=True)
class CapsuleRetentionResult:
    """Result from applying capsule retention to a directory."""

    pruned: tuple[Path, ...]
    remaining: tuple[Path, ...]


def capsule_build_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether daily companion capsule generation is enabled.

    The production gate is intentionally default-off. Only explicit truthy
    values for ``TOKENPAK_TIP_CAPSULE_BUILD_ENABLED`` enable builds.
    """

    values = os.environ if env is None else env
    return values.get("TOKENPAK_TIP_CAPSULE_BUILD_ENABLED", "").strip().lower() in TRUTHY_ENV_VALUES


def apply_capsule_retention(
    capsule_dir: Path | str,
    *,
    now: float | None = None,
    max_age_days: int = DEFAULT_CAPSULE_RETENTION_DAYS,
    max_files: int = DEFAULT_CAPSULE_RETENTION_COUNT,
) -> CapsuleRetentionResult:
    """Prune old companion capsules while preserving ``active.md``.

    Retention first removes non-active capsule files older than ``max_age_days``
    and then keeps the newest ``max_files`` remaining non-active capsule files.
    ``active.md`` is never counted against the file limit and is never removed
    by this function, even if it is stale or a symlink.
    """

    directory = Path(capsule_dir).expanduser()
    current_time = time.time() if now is None else now
    prune_cutoff = current_time - (max_age_days * 86400)
    pruned: list[Path] = []

    if not directory.exists():
        return CapsuleRetentionResult(pruned=(), remaining=())

    for capsule_file in _iter_non_active_capsules(directory):
        try:
            if capsule_file.stat().st_mtime < prune_cutoff:
                capsule_file.unlink()
                pruned.append(capsule_file)
        except OSError:
            continue

    remaining = sorted(
        _iter_non_active_capsules(directory),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for capsule_file in remaining[max_files:]:
        try:
            capsule_file.unlink()
            pruned.append(capsule_file)
        except OSError:
            continue

    kept = tuple(
        sorted(
            _iter_non_active_capsules(directory),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    )
    return CapsuleRetentionResult(pruned=tuple(pruned), remaining=kept)


def refresh_active_capsule(
    capsule_dir: Path | str,
    *,
    min_bytes: int = DEFAULT_ACTIVE_MIN_BYTES,
) -> Path | None:
    """Point ``active.md`` at the largest substantive capsule.

    Returns the selected capsule path, or ``None`` when no non-active capsule is
    large enough. If no candidate exists, an existing ``active.md`` fallback is
    left untouched so callers do not lose the last usable fallback.
    """

    directory = Path(capsule_dir).expanduser()
    if not directory.exists():
        return None

    candidates = sorted(
        (path for path in _iter_non_active_capsules(directory) if path.stat().st_size >= min_bytes),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )
    if not candidates:
        return None

    selected = candidates[0]
    active = directory / ACTIVE_CAPSULE_NAME
    if active.is_symlink() or active.exists():
        active.unlink()
    active.symlink_to(selected.name)
    return selected


def _iter_non_active_capsules(directory: Path) -> tuple[Path, ...]:
    return tuple(path for path in directory.glob("*.md") if path.name != ACTIVE_CAPSULE_NAME)
