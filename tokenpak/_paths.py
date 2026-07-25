# SPDX-License-Identifier: Apache-2.0
"""Canonical on-disk path resolver for TokenPak.

Single source of truth for where TokenPak stores user state, system
state, and Pro daemon coordination files. New code MUST route through
this module rather than building ``Path.home() / ".tokenpak"`` ad hoc.

Resolution order:
    1. ``TOKENPAK_HOME`` env var (operator override, e.g. for sandboxes)
    2. ``~/.tpk/`` (canonical default — Glossary 08 §TPK)
    3. ``~/.tokenpak/`` (legacy fallback, only when ``~/.tpk/`` is absent
       AND the legacy directory exists — preserves zero-touch upgrade)

Layout:
    <home>/
        config.{json,yaml}      user config (config commands)
        license.json            license store (licensing module)
        debug.log               doctor/diagnostics log
        index.json              vault index
        templates/              user templates
        fleet.yaml              fleet manifest
        pinned_blocks.json      retain pins
        requests.jsonl          request log
        telemetry.db            telemetry store
        monitor.db              request ledger
        tunnels/                dashboard SSH tunnel state (control sockets, pids)
        companion/              companion subsystem state
        pro/                    Pro daemon coordination (sock-info, state)

The resolver is deliberately read-only — it does not create directories.
Callsites that need a directory must call ``ensure_home()`` (creates
``<home>/`` with mode 0700) or build their own ensure-step.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Optional

CANONICAL_DIRNAME = ".tpk"
LEGACY_DIRNAME = ".tokenpak"
ENV_VAR = "TOKENPAK_HOME"

# Canonical TokenPak home layout subdirectories. ``under()`` fail-loud rejects
# any extension-less first segment that is not in this set (or _ADOPTED_SUBDIRS),
# per the always-dynamic fail-loud principle: do not silently accept unknown
# subdirs (a typo'd subdir would otherwise create junk state). New subdirs are
# added here when the canonical layout is amended.
_STD_33_SUBDIRS: frozenset[str] = frozenset(
    {
        "templates",
        "companion",
        "pro",
        # dispatch/ — TokenPak Dispatch runtime state (runs.db, artifacts/,
        # tmp/, overlays/). Added per the canonical layout amendment of
        # 2026-05-20.
        "dispatch",
        "tunnels",
    }
)

# Subdirs in active use that are not (yet) enumerated in the canonical layout.
# Tracked separately so the drift is visible and can be reconciled into the
# canonical layout rather than silently blessed. ``paks/`` is written by
# tokenpak/cli/commands/pak.py and predates the resolver's fail-loud contract.
_ADOPTED_SUBDIRS: frozenset[str] = frozenset(
    {
        "paks",
    }
)


def _known_subdirs() -> frozenset[str]:
    """All subdir names ``under()`` will accept (canonical layout + adopted)."""
    return _STD_33_SUBDIRS | _ADOPTED_SUBDIRS


def _is_top_level_file(name: str) -> bool:
    """True when the first segment names a top-level file (e.g. ``config.json``).

    The resolver contract explicitly sanctions ``under("file")`` for top-level
    files ("always ... call ``_paths.under(\"file\")``"). Every top-level file
    carries an extension (``config.json``, ``license.json``, ``telemetry.db``
    ...), so the presence of a ``.`` distinguishes a file target from a
    (typo'd) subdir target.
    """
    return "." in name


_MONITOR_DB_ENV = "TOKENPAK_DB"
_MONITOR_DB_ENV_COMPAT = "TOKENPAK_MONITOR_DB"
_MONITOR_TABLE = "requests"


def home() -> Path:
    """Return the resolved TokenPak home directory for **reads**.

    Read resolution is compatibility-first so existing installs keep working:
    ``TOKENPAK_HOME`` → ``~/.tpk`` if present → ``~/.tokenpak`` if present →
    ``~/.tpk``.

    Do **not** use this to decide where to create new state. Writes go through
    :func:`write_home`, which never selects the legacy directory. Mixing the
    two is what produced the defect this split fixes: a first-run flag written
    to ``~/.tokenpak`` made ``home()`` resolve to the legacy path for every
    later call, so no new install ever used the canonical home.
    """
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    canonical = Path.home() / CANONICAL_DIRNAME
    if canonical.exists():
        return canonical
    legacy = Path.home() / LEGACY_DIRNAME
    if legacy.exists():
        return legacy
    return canonical


def write_home() -> Path:
    """Return the directory that new state MUST be written to.

    Only ever ``TOKENPAK_HOME`` or ``~/.tpk`` — never the legacy
    ``~/.tokenpak``. When a legacy tree exists it stays readable via
    :func:`home` and is migrated explicitly by ``tokenpak config migrate``;
    nothing writes to it implicitly.
    """
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / CANONICAL_DIRNAME


def read_candidates() -> list[Path]:
    """Homes to search when reading existing state, most-preferred first.

    Lets a caller find state written by an older version without ever
    implying that the legacy location is a valid write target.
    """
    seen: list[Path] = []
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        seen.append(Path(override).expanduser())
    for candidate in (Path.home() / CANONICAL_DIRNAME, Path.home() / LEGACY_DIRNAME):
        if candidate not in seen:
            seen.append(candidate)
    return seen


def resolve_existing(*parts: str) -> Optional[Path]:
    """First existing path for *parts* across :func:`read_candidates`.

    Returns ``None`` when the file exists in no home — the caller decides
    whether that is ``no_data`` or ``unavailable``; this function does not
    invent a default.
    """
    for base in read_candidates():
        candidate = base.joinpath(*parts)
        if candidate.exists():
            return candidate
    return None


#: Env override for companion state. Read here rather than in the companion
#: package because the proxy and the CLI both need to resolve this path, and
#: the architecture forbids either of them importing ``tokenpak.companion``.
COMPANION_DIR_ENV = "TOKENPAK_COMPANION_JOURNAL_DIR"
_COMPANION_SUBDIR = "companion"


def companion_write_dir() -> Path:
    """Directory new companion state is written to.

    Canonical-only: the env override if set, else ``<write_home>/companion``.
    Never the legacy tree — an install that predates the canonical home keeps
    its journals readable (see :func:`companion_read_dirs`) without
    accumulating new ones there.

    Every companion process — hooks, MCP server, launcher, proxy endpoints —
    must agree on this path or they silently write to different databases.
    """
    override = os.environ.get(COMPANION_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return write_home() / _COMPANION_SUBDIR


def companion_read_dirs() -> list[Path]:
    """Existing companion directories to read, most-preferred first.

    Readers must search across homes: a user who installed before the
    canonical home existed has real journal data in the legacy tree, and
    reporting $0.00 because we only looked in the new location would be the
    absent-rendered-as-zero defect in a different costume.
    """
    override = os.environ.get(COMPANION_DIR_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        return [path] if path.exists() else []
    dirs: list[Path] = []
    for base in read_candidates():
        candidate = base / _COMPANION_SUBDIR
        if candidate.exists() and candidate not in dirs:
            dirs.append(candidate)
    return dirs


def companion_file(name: str) -> Optional[Path]:
    """First existing companion file called *name*, or ``None``.

    ``None`` means "no data anywhere", which is a different answer from
    "zero"; callers must not substitute one for the other.
    """
    for base in companion_read_dirs():
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


def companion_run_dir() -> Path:
    """Runtime coordination directory (session markers, generated config)."""
    return companion_write_dir() / "run"


def legacy_home() -> Path:
    """Return the legacy ``~/.tokenpak/`` path (always — for migration probes)."""
    return Path.home() / LEGACY_DIRNAME


def canonical_home() -> Path:
    """Return the canonical ``~/.tpk/`` path (always — for migration targets)."""
    return Path.home() / CANONICAL_DIRNAME


def has_legacy() -> bool:
    """True if ``~/.tokenpak/`` exists on disk (migration trigger)."""
    return legacy_home().exists()


def has_canonical() -> bool:
    """True if ``~/.tpk/`` exists on disk."""
    return canonical_home().exists()


def needs_migration() -> bool:
    """True when the legacy directory exists and the canonical does not.

    This is the migration trigger condition. ``tokenpak config migrate``
    backs up the legacy tree, copies it to the canonical location, and
    leaves the legacy tree in place (rename-after-soak, not delete) so
    no user state is destroyed.
    """
    return has_legacy() and not has_canonical()


def ensure_home(*, mode: int = 0o700) -> Path:
    """Create the write home if absent, with secure permissions. Returns it.

    Mode 0700 is enforced because the directory holds license keys, API
    credentials and Pro daemon coordination state.

    Two behaviours differ from the original implementation, both because the
    original guarantee did not hold in practice:

    * The target is :func:`write_home`, not :func:`home`. Otherwise the first
      call on a machine with a legacy tree would keep provisioning the legacy
      directory forever.
    * A group/world-accessible mode **is** repaired. The previous version
      documented "existing directories are not re-chmoded", which meant that
      whichever code path created the directory first decided its permissions
      — and the first-run flag created it at 0775 under the default umask, so
      the 0700 guarantee was defeated on every fresh install. Bits *beyond*
      the requested mode are cleared; nothing is loosened.
    """
    h = write_home()
    h.mkdir(mode=mode, parents=True, exist_ok=True)
    _harden_dir(h, mode=mode)
    return h


def _harden_dir(path: Path, *, mode: int = 0o700) -> None:
    """Clear group/other bits on *path* when present. Never loosens."""
    try:
        current = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if current & 0o077:
        try:
            path.chmod(current & mode)
        except OSError:
            pass


#: Canonical user-config filename. YAML is what ``tokenpak setup`` writes and
#: what the proxy reads, so it is the format of record.
CONFIG_CANONICAL = "config.yaml"

#: Read-compatibility config filenames, in preference order after the
#: canonical one. ``config.json`` is accepted for existing installs but is
#: never written by new code.
CONFIG_COMPAT = ("config.yml", "config.json")


def config_write_path() -> Path:
    """Where a new user config MUST be written: ``<write_home>/config.yaml``."""
    return write_home() / CONFIG_CANONICAL


def config_read_path() -> Optional[Path]:
    """The user config in effect, or ``None`` if there is none.

    Searches every read home for the canonical name first, then the
    compatibility names. This is the single resolver for "is TokenPak
    configured?" — ``doctor`` previously answered that question by looking for
    ``config.json`` alone while ``setup`` wrote ``config.yaml``, so completing
    setup left doctor reporting "no config → Run: tokenpak setup".
    """
    for name in (CONFIG_CANONICAL, *CONFIG_COMPAT):
        found = resolve_existing(name)
        if found is not None:
            return found
    return None


def is_configured() -> bool:
    """True when a user config exists in any supported location or format."""
    return config_read_path() is not None


def secure_file(path: Path, *, mode: int = 0o600) -> None:
    """Restrict a state file to the owner. Best effort; never raises.

    Applied to files that carry secrets or user configuration (``config.yaml``,
    ``license.json``, credential stores). Windows ignores POSIX bits, so this
    is a no-op there rather than an error.
    """
    try:
        current = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if current & 0o077:
        try:
            path.chmod(current & mode)
        except OSError:
            pass


def under(*parts: str) -> Path:
    """Build a path under the resolved home: ``under("companion", "journal.db")``.

    Pure-path helper — does not create parents. Equivalent to
    ``home().joinpath(*parts)`` but spelled to encourage callsites to
    say what they want at the import site, not assemble strings.

    Fail-loud per the resolver contract: the first segment must be either a
    known layout subdir (``_STD_33_SUBDIRS`` / ``_ADOPTED_SUBDIRS``) or a
    top-level file (a name containing an extension). An unknown
    extension-less first segment raises ``ValueError`` rather than
    silently resolving — this catches typo'd subdirs (``under("compaion")``)
    and not-yet-enumerated subdirs before they create junk state.
    """
    if not parts:
        raise ValueError("under() requires at least one path segment")
    first = parts[0]
    if first in _known_subdirs() or _is_top_level_file(first):
        return home().joinpath(*parts)
    raise ValueError(
        f"unknown TokenPak home subdir {first!r}: allowed subdirs are "
        f"{sorted(_known_subdirs())}, or a top-level file (name with an "
        f"extension, e.g. 'config.json'). Add new subdirs to "
        f"_STD_33_SUBDIRS per a canonical layout amendment."
    )


def is_legacy_active() -> bool:
    """True when the *resolved* home is the legacy directory.

    Used by doctor/setup to surface a "you're on legacy paths — run
    ``tokenpak config migrate`` to move to ``~/.tpk/``" advisory.
    """
    return home() == legacy_home() and not has_canonical()


# Resolver-contract API names: ``resolved_home`` / ``is_legacy`` are the names
# the public path API uses. They alias the module's existing ``home`` /
# ``is_legacy_active`` so both spellings resolve identically and no existing
# callsite breaks.
def resolved_home() -> Path:
    """Alias for :func:`home`."""
    return home()


def is_legacy() -> bool:
    """Alias for :func:`is_legacy_active`."""
    return is_legacy_active()


# ---------------------------------------------------------------------------
# Monitor DB resolver
# ---------------------------------------------------------------------------


def _is_valid_monitor_db(p: Path) -> bool:
    """Check whether *p* is a usable monitor DB (exists, SQLite, has schema)."""
    try:
        resolved = p.resolve() if p.is_symlink() else p
        if not resolved.is_file():
            return False
        if resolved.stat().st_size < 100:
            return False
        conn = sqlite3.connect(str(resolved), timeout=2)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (_MONITOR_TABLE,),
        )
        found = cur.fetchone() is not None
        conn.close()
        return found
    except Exception:
        return False


def _monitor_db_candidates() -> list[Path]:
    """Ordered candidate paths for the monitor DB (read resolution order).

    When ``TOKENPAK_HOME`` is set, the scoped home is the sole home-derived
    candidate so every consumer of this list (the proxy writer, the
    spend-guard readers, doctor) converges on the same scoped file and a
    scoped run can never read or write the default home's DB. Explicit DB
    overrides (``TOKENPAK_DB`` / compat) still win first in both shapes.
    When unset, the historical read-migration order is unchanged.
    """
    candidates: list[Path] = []
    env_val = os.environ.get(_MONITOR_DB_ENV, "").strip()
    if env_val:
        candidates.append(Path(env_val).expanduser())
    else:
        env_compat = os.environ.get(_MONITOR_DB_ENV_COMPAT, "").strip()
        if env_compat:
            candidates.append(Path(env_compat).expanduser())
    if os.environ.get(ENV_VAR, "").strip():
        scoped = home() / "monitor.db"
        if scoped not in candidates:
            candidates.append(scoped)
        return candidates
    candidates.append(Path.home() / CANONICAL_DIRNAME / "monitor.db")
    candidates.append(Path.home() / LEGACY_DIRNAME / "monitor.db")
    candidates.append(Path.home() / "tokenpak" / "monitor.db")
    return candidates


def monitor_db(mode: str = "read") -> Optional[Path]:
    """Resolve the monitor DB path.

    mode="read":  Return the first valid active DB, or None if no
                  valid DB exists. Does not create anything.
    mode="write": Return the existing active DB if found, otherwise the
                  fresh-install path: the scoped home when
                  ``TOKENPAK_HOME`` is set, else the canonical
                  ``~/.tpk/monitor.db`` (never the legacy dir, even when
                  the legacy dir is the resolved home). Creates the
                  parent directory if needed, but does NOT create the DB
                  file itself.
    """
    for candidate in _monitor_db_candidates():
        if _is_valid_monitor_db(candidate):
            return candidate
    if mode == "write":
        if os.environ.get(ENV_VAR, "").strip():
            target = home() / "monitor.db"
        else:
            target = Path.home() / CANONICAL_DIRNAME / "monitor.db"
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return target
    return None


def monitor_db_candidates() -> list[dict[str, Any]]:
    """Return diagnostic info for each candidate path (for doctor/split-brain).

    Each entry: {path, exists, valid, rows, selected}.
    """
    results: list[dict[str, Any]] = []
    selected_path = monitor_db(mode="read")
    for candidate in _monitor_db_candidates():
        entry: dict[str, Any] = {
            "path": str(candidate),
            "exists": candidate.exists(),
            "valid": False,
            "rows": 0,
            "selected": False,
        }
        if _is_valid_monitor_db(candidate):
            entry["valid"] = True
            entry["selected"] = (
                selected_path is not None and candidate.resolve() == selected_path.resolve()
            )
            try:
                conn = sqlite3.connect(str(candidate.resolve()), timeout=2)
                cur = conn.execute(f"SELECT COUNT(*) FROM {_MONITOR_TABLE}")
                entry["rows"] = cur.fetchone()[0]
                conn.close()
            except Exception:
                pass
        results.append(entry)
    return results


__all__ = [
    "CANONICAL_DIRNAME",
    "LEGACY_DIRNAME",
    "ENV_VAR",
    "home",
    "resolved_home",
    "is_legacy",
    "legacy_home",
    "canonical_home",
    "has_legacy",
    "has_canonical",
    "needs_migration",
    "companion_file",
    "companion_read_dirs",
    "companion_run_dir",
    "companion_write_dir",
    "ensure_home",
    "under",
    "is_legacy_active",
    "monitor_db",
    "monitor_db_candidates",
]
