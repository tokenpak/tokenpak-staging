# SPDX-License-Identifier: Apache-2.0
"""Durable persistence + path resolution for Runtime Hygiene manifests.

The registry is the on-disk half of the foundation: it resolves every path
through TokenPak's canonical :mod:`tokenpak._paths` policy (never a hardcoded
``~/.tpk``/``~/.tokenpak``), creates state-home directories with mode 0700,
and writes manifest files with mode 0600 using an atomic temp-write → fsync →
rename sequence so a crash mid-write can never leave a half-written manifest
that a later cleanup pass might trust.

Layout::

    <_paths.home()>/runtime/sessions/<session_id>/
        manifest.json     (0600)
        heartbeat         (0600, written by a later packet)

Snapshot note: ``__all__ = []`` — internal plumbing, not released public API.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from tokenpak import _paths

from .hygiene_schema import (
    HygieneError,
    ManifestValidationError,
    SessionManifest,
    assert_transition,
    utc_now_iso,
)

# Directory / file permission policy (contract "Registry Requirements").
_DIR_MODE = 0o700
_FILE_MODE = 0o600

_MANIFEST_NAME = "manifest.json"
_HEARTBEAT_NAME = "heartbeat"
_SESSIONS_SUBDIR = "sessions"


class ManifestWriteError(HygieneError):
    """A manifest could not be durably persisted (temp-write/fsync/rename failed)."""


class ManifestReadError(HygieneError):
    """A manifest exists on disk but is unreadable or corrupt."""


# ── Path resolution (always through _paths) ──────────────────────────────


def sessions_root() -> Path:
    """Root directory holding every per-session manifest directory."""
    return _paths.runtime_home() / _SESSIONS_SUBDIR


def session_dir(session_id: str) -> Path:
    """Directory holding one session's manifest + heartbeat."""
    if not session_id or "/" in session_id or session_id in (".", ".."):
        raise ValueError(f"invalid session_id for path resolution: {session_id!r}")
    return sessions_root() / session_id


def manifest_path(session_id: str) -> Path:
    """Path to a session's ``manifest.json``."""
    return session_dir(session_id) / _MANIFEST_NAME


def heartbeat_path(session_id: str) -> Path:
    """Path to a session's heartbeat sentinel (written by a later packet)."""
    return session_dir(session_id) / _HEARTBEAT_NAME


# ── Directory provisioning (0700 at every level) ─────────────────────────


def _ensure_dir_0700(path: Path) -> None:
    """Create ``path`` and any missing ancestors up to ``_paths.home()`` at 0700.

    ``Path.mkdir(parents=True)`` only applies ``mode`` to the leaf, so we walk
    the chain explicitly to guarantee the whole runtime subtree is 0700 — the
    manifests carry license-adjacent ownership evidence and must not be
    world-readable. ``_paths.home()`` itself is created via
    :func:`tokenpak._paths.ensure_home` (also 0700). Existing directories are
    chmod'd back to 0700 defensively; an operator who intentionally widened a
    parent is re-narrowed here because the contract is explicit.
    """
    home = _paths.ensure_home(mode=_DIR_MODE)
    try:
        rel = path.resolve().relative_to(home.resolve())
        chain_parts = rel.parts
        base = home.resolve()
    except ValueError:
        # path is not under home (e.g. TOKENPAK_HOME points elsewhere) — fall
        # back to creating the full chain at 0700 without a home anchor.
        chain_parts = path.parts
        base = Path(path.anchor)

    cur = base
    for part in chain_parts:
        cur = cur / part
        cur.mkdir(mode=_DIR_MODE, exist_ok=True)
        try:
            cur.chmod(_DIR_MODE)
        except OSError:
            pass


def ensure_session_dir(session_id: str) -> Path:
    """Create (0700) and return a session's manifest directory."""
    d = session_dir(session_id)
    _ensure_dir_0700(d)
    return d


# ── Atomic manifest write ────────────────────────────────────────────────


def write_manifest(manifest: SessionManifest) -> Path:
    """Validate + durably persist ``manifest``; return the manifest path.

    Atomic temp-write → fsync(file) → rename → fsync(dir) so the manifest is
    either fully present or absent, never partial. The file lands at mode
    0600. Any failure raises :class:`ManifestWriteError` so a cleanup-capable
    caller can abort *before* spawning (contract: manifest failure must never
    create cleanup authority).
    """
    manifest.validate()
    target = manifest_path(manifest.tokenpak_session_id)
    d = target.parent
    try:
        _ensure_dir_0700(d)
    except OSError as exc:
        raise ManifestWriteError(f"could not create session dir {d}: {exc}") from exc

    body = json.dumps(manifest.to_dict(), indent=2, sort_keys=False) + "\n"
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=str(d))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _FILE_MODE)
        os.replace(tmp_path, target)
        _fsync_dir(d)
    except OSError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise ManifestWriteError(f"could not write manifest {target}: {exc}") from exc
    return target


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it is durable across a crash."""
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


# ── Read ─────────────────────────────────────────────────────────────────


def read_manifest(session_id: str) -> "SessionManifest | None":
    """Load a session's manifest, or ``None`` if it does not exist.

    Fail-closed: a present-but-corrupt or schema-mismatched manifest raises
    :class:`ManifestReadError` rather than returning a half-trusted object.
    Absence (the session was never registered) is the benign ``None`` case.
    """
    path = manifest_path(session_id)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestReadError(f"could not read manifest {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestReadError(f"manifest {path} is not valid JSON: {exc}") from exc
    try:
        return SessionManifest.from_dict(data).validate()
    except ManifestValidationError as exc:
        raise ManifestReadError(f"manifest {path} failed validation: {exc}") from exc


# ── Lifecycle transition (persisted) ─────────────────────────────────────


def transition(session_id: str, new_state: str) -> SessionManifest:
    """Atomically move a persisted manifest to ``new_state``.

    Reads the current manifest, validates the transition against
    :data:`tokenpak.runtime.hygiene_schema.ALLOWED_TRANSITIONS` (raising
    :class:`tokenpak.runtime.hygiene_schema.InvalidLifecycleTransition` on an
    illegal edge), stamps ``lifecycle_updated_at``, and rewrites the manifest
    with the same atomic durability guarantees as :func:`write_manifest`.
    """
    current = read_manifest(session_id)
    if current is None:
        raise ManifestReadError(f"no manifest to transition for session {session_id!r}")
    assert_transition(current.lifecycle, new_state)
    current.lifecycle = new_state
    current.lifecycle_updated_at = utc_now_iso()
    write_manifest(current)
    return current


__all__: list[str] = []
