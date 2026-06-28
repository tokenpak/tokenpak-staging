# SPDX-License-Identifier: Apache-2.0
"""Owner-only local data helpers for proxy runtime files."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _expand_path(path: os.PathLike[str] | str) -> Path:
    return Path(os.path.expanduser(os.fspath(path)))


def _is_special_sqlite_target(
    path: os.PathLike[str] | str,
    *,
    uri: bool = False,
) -> bool:
    raw = os.fspath(path)
    return raw in {"", ":memory:"} or (uri and raw.startswith("file:"))


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def ensure_private_dir(path: os.PathLike[str] | str) -> Path:
    """Create *path* as an owner-only directory and return it."""
    directory = _expand_path(path)
    directory.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    _chmod_best_effort(directory, PRIVATE_DIR_MODE)
    return directory


def ensure_private_parent(path: os.PathLike[str] | str) -> Path:
    """Ensure *path*'s parent is owner-only and return the expanded path."""
    expanded = _expand_path(path)
    ensure_private_dir(expanded.parent)
    return expanded


def secure_runtime_file(path: os.PathLike[str] | str) -> Path:
    """Best-effort chmod of an existing runtime file to owner-only."""
    expanded = _expand_path(path)
    if expanded.exists():
        _chmod_best_effort(expanded, PRIVATE_FILE_MODE)
    return expanded


def secure_sqlite_sidecars(db_path: os.PathLike[str] | str) -> None:
    """Best-effort owner-only chmod for SQLite sidecar files if present."""
    if _is_special_sqlite_target(db_path):
        return
    expanded = _expand_path(db_path)
    for suffix in ("", "-wal", "-shm", "-journal"):
        sidecar = Path(str(expanded) + suffix)
        if sidecar.exists():
            _chmod_best_effort(sidecar, PRIVATE_FILE_MODE)


def secure_sqlite_connect(
    db_path: os.PathLike[str] | str,
    *args,
    **kwargs,
) -> sqlite3.Connection:
    """Open a SQLite DB after securing its parent, then chmod the DB file."""
    if _is_special_sqlite_target(db_path, uri=bool(kwargs.get("uri"))):
        return sqlite3.connect(os.fspath(db_path), *args, **kwargs)
    path = ensure_private_parent(db_path)
    conn = sqlite3.connect(str(path), *args, **kwargs)
    secure_sqlite_sidecars(path)
    return conn


def sqlite_schema_cache_key(
    conn: sqlite3.Connection,
) -> tuple[str, int, int] | None:
    """Return a stable cache key for the connection's main DB file."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    path = row[2]
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return (str(path), 0, 0)
    return (str(path), int(stat.st_dev), int(stat.st_ino))


def default_spend_guard_db_path() -> str:
    """Return the canonical spend-guard DB path without creating it."""
    from tokenpak import _paths

    return str(_paths.under("spend_guard.db"))


def resolve_spend_guard_db_path(
    audit_db_path: Optional[os.PathLike[str] | str] = None,
) -> Path:
    """Resolve and secure the spend-guard DB parent directory."""
    path = audit_db_path or default_spend_guard_db_path()
    if _is_special_sqlite_target(path):
        return _expand_path(path)
    return ensure_private_parent(path)


def default_log_dir() -> Path:
    """Return the canonical proxy log directory, creating home if needed."""
    from tokenpak import _paths

    return _paths.ensure_home() / "logs"
