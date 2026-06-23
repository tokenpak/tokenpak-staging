# SPDX-License-Identifier: Apache-2.0
"""Isolated ``CODEX_HOME`` provisioning for parallel Codex sessions.

Codex stores interactive state in a single SQLite database
(``state_5.sqlite``) under ``CODEX_HOME``.  Two Codex processes sharing the
same home contend over that database, and a suspended process holds the
lock indefinitely (see :mod:`tokenpak.companion.codex.state_lock`).  This
module gives each session a *home of its own* so the contention cannot
arise, while keeping authentication and user preferences shared.

Isolation modes (env var ``TOKENPAK_CODEX_SESSION_MODE``):

==========  ============================================  ==========================
mode        CODEX_HOME                                    semantics
==========  ============================================  ==========================
``auto``    ``~/.tokenpak/codex-workspaces/<hash>/``      default; parallel-safe
                                                             with launcher fallback
``shared``  ``~/.codex/`` (or existing ``$CODEX_HOME``)   legacy/debug behavior
``workspace`` ``~/.tokenpak/codex-workspaces/<hash>/``    per-project; durable state
``isolated``  ``~/.tokenpak/codex-sessions/<uuid>/``      per-session; ephemeral
``attach``  (deferred)                                     not implemented
==========  ============================================  ==========================

``auto`` is the shipped user-facing default. It starts with the durable
per-project workspace home, and the launcher falls back to a fresh
``isolated`` home if that workspace is already owned by another live Codex
session. ``shared`` remains available only as an explicit legacy/debug mode.

Auth/config propagation uses the never-mirror-credentials rule: we
**symlink** ``auth.json`` and ``config.toml`` into the isolated home rather
than copying them, so Codex's own refresh-token rotation is never forked
into a divergent copy.  Only ``state_5.sqlite`` is *not* propagated — that
is the whole point of an isolated home.

Lifecycle / zombie-avoidance: this module provisions homes and records a
``codex.pid`` sentinel; it does **not** spawn or supervise Codex itself and
does **not** add any background reaper thread (a reaper that mis-fires would
recreate the very zombie-state pattern the packet warns against).  Cleanup
is explicit (``tokenpak codex clean`` / doctor surfacing) plus a bounded
retention sweep run only at provisioning time.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# ── Mode constants ───────────────────────────────────────────────────
MODE_SHARED = "shared"
_MODE_AUTO = "auto"
MODE_WORKSPACE = "workspace"
MODE_ISOLATED = "isolated"
MODE_ATTACH = "attach"  # deferred — see module docstring

ENV_SESSION_MODE = "TOKENPAK_CODEX_SESSION_MODE"
ENV_CODEX_HOME = "CODEX_HOME"

# Modes that resolve to a provisioned home under ~/.tokenpak.
_ISOLATING_MODES = frozenset({_MODE_AUTO, MODE_WORKSPACE, MODE_ISOLATED})

# Files symlinked from the canonical ~/.codex into a provisioned home.
# auth.json: shared so Codex's external refresh is never forked.
# config.toml: shared so user preferences carry over.
_PROPAGATED = ("auth.json", "config.toml")

# Retention thresholds for ephemeral `isolated` homes (per packet §3).
RETENTION_MAX_HOMES = 5
RETENTION_MAX_AGE_S = 7 * 24 * 60 * 60  # 7 days
RETENTION_MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB


# ── Path roots ───────────────────────────────────────────────────────

def _tokenpak_root() -> Path:
    return Path.home() / ".tokenpak"


def sessions_root() -> Path:
    """Root for ephemeral per-session (``isolated``) homes."""
    return _tokenpak_root() / "codex-sessions"


def workspaces_root() -> Path:
    """Root for durable per-project (``workspace``) homes."""
    return _tokenpak_root() / "codex-workspaces"


def canonical_codex_home() -> Path:
    """The user's real Codex home — auth/config source of truth.

    This is ``~/.codex`` and is intentionally *not* derived from
    ``$CODEX_HOME``: when we provision an isolated home we will have
    already set ``$CODEX_HOME`` to the isolated path, but auth/config must
    still be sourced from the user's canonical home.
    """
    return Path.home() / ".codex"


def workspace_hash(workspace_dir: "Path | str") -> str:
    """Stable short hash of a resolved workspace directory.

    Same project directory ⇒ same hash ⇒ same home, which is what makes
    ``workspace`` mode reuse state across invocations from the same
    project.  We resolve symlinks/``..`` first so equivalent paths collapse
    to one home.
    """
    resolved = str(Path(workspace_dir).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return digest[:16]


# ── Mode resolution ──────────────────────────────────────────────────

def resolve_mode(raw: "str | None" = None) -> str:
    """Normalize the session mode from the env (or an explicit value).

    Unknown values fall back to ``auto`` rather than erroring — a typo
    in the env var must never break ``tokenpak codex`` for a user who just
    wants the default behavior.
    """
    value = (raw if raw is not None else os.environ.get(ENV_SESSION_MODE) or _MODE_AUTO)
    value = value.strip().lower()
    if value in (_MODE_AUTO, MODE_SHARED, MODE_WORKSPACE, MODE_ISOLATED):
        return value
    if value == MODE_ATTACH:
        # Deferred — degrade to shared with the same not-implemented signal
        # the launcher surfaces. Resolved here so callers have one code path.
        return MODE_ATTACH
    return _MODE_AUTO


# ── Provisioning ─────────────────────────────────────────────────────

@dataclass
class ProvisionedHome:
    """Outcome of :func:`provision_codex_home`."""

    mode: str
    home: Path
    created: bool  # True if this call created the home directory
    propagated: list[str]  # names symlinked in (auth.json/config.toml)


def provision_codex_home(
    mode: "str | None" = None,
    workspace_dir: "Path | str | None" = None,
    session_id: "str | None" = None,
) -> ProvisionedHome:
    """Provision the ``CODEX_HOME`` for ``mode`` and return its location.

    Does NOT mutate ``os.environ`` — the launcher decides when to export
    ``CODEX_HOME`` (see :func:`apply_to_env`).  For ``shared`` mode this is
    a no-op that returns the canonical home, so callers can route every
    mode through one code path.

    ``attach`` raises :class:`NotImplementedError` — it is deferred until
    Codex provides a session-attach API.
    """
    resolved = resolve_mode(mode)

    if resolved == MODE_SHARED:
        home = Path(os.environ.get(ENV_CODEX_HOME) or canonical_codex_home())
        return ProvisionedHome(mode=MODE_SHARED, home=home, created=False, propagated=[])

    if resolved == MODE_ATTACH:
        raise NotImplementedError(
            "TOKENPAK_CODEX_SESSION_MODE=attach is deferred — it requires a "
            "Codex upstream session-attach API. Use 'workspace' or 'isolated'."
        )

    if resolved in (_MODE_AUTO, MODE_WORKSPACE):
        ws = workspace_dir if workspace_dir is not None else Path.cwd()
        home = workspaces_root() / workspace_hash(ws)
        # workspace homes are durable; run a retention sweep on the
        # ephemeral sessions root only (never on workspaces).
        _retention_sweep(sessions_root())
    else:  # MODE_ISOLATED
        sid = session_id or uuid.uuid4().hex
        home = sessions_root() / sid
        _retention_sweep(sessions_root(), reserve=home)

    created = not home.exists()
    home.mkdir(parents=True, exist_ok=True)
    propagated = _propagate_auth_config(home)
    return ProvisionedHome(
        mode=resolved, home=home, created=created, propagated=propagated
    )


def _propagate_auth_config(home: Path) -> list[str]:
    """Symlink ``auth.json`` / ``config.toml`` from the canonical home.

    Symlink (never copy) per the never-mirror-credentials rule: a copy of
    ``auth.json`` would fork Codex's externally-owned refresh token and
    diverge.  ``state_5.sqlite`` is deliberately NOT propagated — isolated
    state is the entire purpose.

    Idempotent: an existing correct symlink is left alone; a stale link or
    a real file at the target is replaced by the symlink so the home always
    points at the live canonical credential.
    """
    src_home = canonical_codex_home()
    linked: list[str] = []
    for name in _PROPAGATED:
        src = src_home / name
        if not src.exists():
            continue
        dst = home / name
        try:
            if dst.is_symlink():
                if os.readlink(dst) == str(src):
                    linked.append(name)
                    continue
                dst.unlink()
            elif dst.exists():
                # A real file shadowing the canonical credential — remove so
                # we never serve a divergent copy.
                dst.unlink()
            dst.symlink_to(src)
            linked.append(name)
        except OSError:
            # Symlinks unsupported (rare) — skip rather than copy. A copy
            # would violate the no-mirror credential rule; better to let
            # Codex fall back to its own auth discovery than fork the token.
            continue
    return linked


def apply_to_env(home: Path, env: "dict[str, str] | None" = None) -> dict[str, str]:
    """Return ``env`` (default ``os.environ`` copy) with ``CODEX_HOME`` set.

    The launcher uses this immediately before ``execvpe`` so the child
    Codex process sees the provisioned home.  Returning a dict (rather than
    mutating in place) keeps the launcher's existing ``env = os.environ.copy()``
    flow intact.
    """
    out = dict(env) if env is not None else os.environ.copy()
    out[ENV_CODEX_HOME] = str(home)
    return out


def record_pid(home: Path, pid: "int | None" = None) -> None:
    """Record the launching PID in ``<home>/codex.pid`` (best-effort).

    Lets :mod:`state_lock` and the doctor name a holder process and lets
    cleanup distinguish a home with a live session from an orphan.  We
    record the launcher's own PID because ``execvpe`` reuses it for the
    Codex process (same PID), so the sentinel stays accurate across the
    exec without supervising anything.
    """
    pid = pid if pid is not None else os.getpid()
    try:
        (home / "codex.pid").write_text(f"{pid}\n", encoding="utf-8")
    except OSError:
        pass


def _claim_home(home: Path, pid: "int | None" = None) -> bool:
    """Atomically claim a provisioned Codex home for the launching process.

    This is an internal lease, not a user-facing lock. It closes the launch
    race where two ``tokenpak codex`` processes could both find a workspace
    database free before either Codex process opens it.
    """
    pid = pid if pid is not None else os.getpid()
    sentinel = home / "codex.pid"
    home.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(sentinel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{pid}\n")
            return True
        except FileExistsError:
            existing = _home_pid(home)
            if existing == pid:
                return True
            if _pid_alive(existing):
                return False
            try:
                sentinel.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                return False
        except OSError:
            return False


def _release_home_claim(home: Path, pid: "int | None" = None) -> None:
    """Release a claim written by this process, best-effort."""
    pid = pid if pid is not None else os.getpid()
    sentinel = home / "codex.pid"
    if _home_pid(home) != pid:
        return
    try:
        sentinel.unlink()
    except OSError:
        pass


# ── Cleanup / retention ──────────────────────────────────────────────

def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = Path(root) / f
                try:
                    total += fp.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _home_pid(home: Path) -> "int | None":
    try:
        raw = (home / "codex.pid").read_text().strip()
        return int(raw) if raw.isdigit() else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: "int | None") -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_orphaned(home: Path) -> bool:
    """True if ``home`` has no live Codex process recorded against it."""
    return not _pid_alive(_home_pid(home))


@dataclass
class HomeInfo:
    path: Path
    mode: str  # "isolated" | "workspace"
    pid: "int | None"
    alive: bool
    age_s: float
    size_bytes: int


def list_homes(include_workspaces: bool = True) -> list[HomeInfo]:
    """Enumerate provisioned homes with liveness/size/age, for doctor/clean."""
    now = time.time()
    infos: list[HomeInfo] = []
    roots = [(sessions_root(), MODE_ISOLATED)]
    if include_workspaces:
        roots.append((workspaces_root(), MODE_WORKSPACE))
    for root, mode in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            pid = _home_pid(child)
            try:
                mtime = child.stat().st_mtime
            except OSError:
                mtime = now
            infos.append(
                HomeInfo(
                    path=child,
                    mode=mode,
                    pid=pid,
                    alive=_pid_alive(pid),
                    age_s=max(0.0, now - mtime),
                    size_bytes=_dir_size(child),
                )
            )
    return infos


def _retention_sweep(root: Path, reserve: "Path | None" = None) -> list[Path]:
    """Bound the ephemeral sessions root (count/age/size). Returns removed.

    Only ever called on the *sessions* root — ``workspace`` homes are
    durable and never swept automatically.  Live homes (PID alive) and the
    ``reserve`` (the home about to be provisioned) are never removed, so a
    sweep can never delete a home in active use.
    """
    if not root.is_dir():
        return []
    reserve = reserve.resolve() if reserve is not None else None
    homes = [h for h in list_homes_in(root) if not h.alive]
    if reserve is not None:
        homes = [h for h in homes if h.path.resolve() != reserve]

    removed: list[Path] = []

    # 1) age: drop anything older than the max age.
    survivors = []
    for h in homes:
        if h.age_s > RETENTION_MAX_AGE_S:
            if _rm_home(h.path):
                removed.append(h.path)
        else:
            survivors.append(h)

    # 2) count: keep the newest RETENTION_MAX_HOMES, drop the rest.
    survivors.sort(key=lambda h: h.age_s)  # youngest first
    keep = survivors[: max(0, RETENTION_MAX_HOMES - 1)]  # -1 reserves a slot
    for h in survivors[len(keep) :]:
        if _rm_home(h.path):
            removed.append(h.path)

    # 3) size: while total exceeds the cap, drop the oldest survivors.
    keep.sort(key=lambda h: h.age_s, reverse=True)  # oldest first
    total = sum(h.size_bytes for h in keep)
    while total > RETENTION_MAX_TOTAL_BYTES and keep:
        victim = keep.pop(0)
        if _rm_home(victim.path):
            removed.append(victim.path)
            total -= victim.size_bytes

    return removed


def list_homes_in(root: Path) -> list[HomeInfo]:
    """``list_homes`` restricted to a single root (helper for sweeps)."""
    mode = MODE_WORKSPACE if root == workspaces_root() else MODE_ISOLATED
    now = time.time()
    infos: list[HomeInfo] = []
    if not root.is_dir():
        return infos
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        pid = _home_pid(child)
        try:
            mtime = child.stat().st_mtime
        except OSError:
            mtime = now
        infos.append(
            HomeInfo(
                path=child,
                mode=mode,
                pid=pid,
                alive=_pid_alive(pid),
                age_s=max(0.0, now - mtime),
                size_bytes=_dir_size(child),
            )
        )
    return infos


def _rm_home(home: Path) -> bool:
    """Remove a provisioned home directory. Never follows symlinks out.

    Safe because the propagated ``auth.json`` / ``config.toml`` are
    symlinks: ``shutil.rmtree`` removes the links, not their canonical
    targets, so the user's real credentials are untouched.
    """
    try:
        shutil.rmtree(home)
        return True
    except OSError:
        return False


def clean(
    mode: "str | None" = None,
    include_workspaces: bool = False,
    force: bool = False,
) -> list[Path]:
    """Remove orphaned provisioned homes. Returns the removed paths.

    Default scope: orphaned ``isolated`` homes only (ephemeral by design).
    ``include_workspaces=True`` extends to orphaned ``workspace`` homes
    (the ``--workspace`` flag).  ``force=True`` also removes homes with a
    live PID — used only by an explicit destructive flag, never the sweep.
    """
    removed: list[Path] = []
    for info in list_homes(include_workspaces=include_workspaces):
        if info.mode == MODE_WORKSPACE and not include_workspaces:
            continue
        if info.alive and not force:
            continue
        if _rm_home(info.path):
            removed.append(info.path)
    return removed
