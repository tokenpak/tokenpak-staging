# SPDX-License-Identifier: Apache-2.0
"""Resolve and provision Codex homes for parallel launcher sessions.

``TOKENPAK_CODEX_SESSION_MODE`` accepts exactly three values:

``shared``
    Use the existing user Codex home.  This preserves the pre-isolation
    behavior and is intentionally single-session when local state is in use.
``workspace``
    Use a deterministic home derived from the resolved project directory.
``isolated``
    Use a fresh, unique home for each launcher invocation.

Provisioning is allowlist-only.  It never walks or copies the source Codex
home.  A new home may receive a private copy of ``config.toml`` and a symlink
to the externally refreshed ``auth.json`` credential; databases, WAL/SHM
sidecars, history, logs, sessions, and every other runtime file stay behind.

The ``codex.pid`` file is a lifecycle lease, not lock-attribution evidence.
Actual SQLite holders are discovered from kernel lock and file-descriptor
state by :mod:`tokenpak.companion.codex.state_lock`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 backport
    import tomli as tomllib  # type: ignore[no-redef]

# Launcher-internal implementation.  Keeping the module out of ``import *``
# also keeps these lifecycle helpers out of the released API snapshot.
__all__: list[str] = []

MODE_SHARED = "shared"
MODE_WORKSPACE = "workspace"
MODE_ISOLATED = "isolated"
VALID_MODES = (MODE_SHARED, MODE_WORKSPACE, MODE_ISOLATED)

ENV_SESSION_MODE = "TOKENPAK_CODEX_SESSION_MODE"
ENV_CODEX_HOME = "CODEX_HOME"

PID_SENTINEL_NAME = "codex.pid"
_LEASE_GUARD_NAME = ".tokenpak-codex-home.lock"
_SENTINEL_SCHEMA = "tokenpak.codex.pid.v1"

# Closed seed allowlist.  Adding a filename here is a security-sensitive
# decision; provisioning deliberately has no glob or recursive-copy path.
SAFE_CONFIG_FILES = ("config.toml",)
SAFE_CREDENTIAL_LINKS = ("auth.json",)
_ISOLATION_BREAKING_CONFIG_KEYS = ("sqlite_home", "log_dir")

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_THREAD_LEASE_LOCK = threading.RLock()
_MAX_SEED_BYTES = 4 * 1024 * 1024
_MAX_SENTINEL_BYTES = 16 * 1024


class InvalidSessionMode(ValueError):
    """Raised when the session-mode environment value is not supported."""


class HomeInUseError(RuntimeError):
    """Raised when a validated live lease already owns a selected home."""


@dataclass(frozen=True)
class SessionPaths:
    """Every path selected for one Codex launcher invocation."""

    mode: str
    home: Path
    source_home: Path
    workspace: Path
    config: Path
    auth: Path
    mcp_config: Path
    hooks: Path
    agents: Path
    skills_root: Path
    pid_sentinel: Path

    def environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Return a child environment pointing Codex at this home."""
        env = dict(os.environ if base is None else base)
        env[ENV_CODEX_HOME] = str(self.home)
        env[ENV_SESSION_MODE] = self.mode
        return env

    def report_rows(self) -> list[tuple[str, str]]:
        """Return stable labels used by launcher and doctor output."""
        return [
            ("session mode", self.mode),
            ("workspace", str(self.workspace)),
            ("CODEX_HOME", str(self.home)),
            ("source home", str(self.source_home)),
            ("config", str(self.config)),
            ("auth", str(self.auth)),
            ("MCP config", str(self.mcp_config)),
            ("hooks", str(self.hooks)),
            ("AGENTS.md", str(self.agents)),
            ("skills", str(self.skills_root)),
            ("PID sentinel", str(self.pid_sentinel)),
        ]


@dataclass(frozen=True)
class ProvisionedHome:
    """Result of allowlist-only home provisioning."""

    paths: SessionPaths
    created: bool
    seeded: tuple[str, ...]
    linked_credentials: tuple[str, ...]


@dataclass(frozen=True)
class PidSentinel:
    """Validated lifecycle lease stored in ``codex.pid``."""

    schema: str
    pid: int
    start_time_ticks: int
    session_id: str
    mode: str
    home: str


def resolve_mode(raw: str | None = None) -> str:
    """Return an exact advertised mode token, failing closed on bad input."""
    value = raw if raw is not None else os.environ.get(ENV_SESSION_MODE, MODE_SHARED)
    if value not in VALID_MODES:
        allowed = "|".join(VALID_MODES)
        shown = value or "<empty>"
        raise InvalidSessionMode(f"invalid {ENV_SESSION_MODE}={shown!r}; expected {allowed}")
    return value


def canonical_codex_home() -> Path:
    """Return the source-of-truth user Codex home."""
    return Path.home() / ".codex"


def _tokenpak_home() -> Path:
    from tokenpak import _paths

    return _paths.home()


def sessions_root(tokenpak_home: Path | None = None) -> Path:
    """Root containing unique per-session Codex homes."""
    return (tokenpak_home or _tokenpak_home()) / "companion" / "codex" / "sessions"


def workspaces_root(tokenpak_home: Path | None = None) -> Path:
    """Root containing deterministic per-project Codex homes."""
    return (tokenpak_home or _tokenpak_home()) / "companion" / "codex" / "workspaces"


def workspace_hash(workspace_dir: Path | str) -> str:
    """Return a stable short digest for an equivalent resolved directory."""
    resolved = str(Path(workspace_dir).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:20]


def project_root(workspace_dir: Path | str) -> Path:
    """Resolve a stable project root, using the nearest Git boundary."""
    resolved = Path(workspace_dir).expanduser().resolve()
    start = resolved if resolved.is_dir() else resolved.parent
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


def _validated_session_id(session_id: str | None) -> str:
    value = session_id or uuid.uuid4().hex
    if not _SESSION_ID_RE.fullmatch(value):
        raise ValueError("session_id must be a safe filename component")
    return value


def select_paths(
    mode: str | None = None,
    *,
    workspace_dir: Path | str | None = None,
    session_id: str | None = None,
    tokenpak_home: Path | None = None,
    source_home: Path | None = None,
    selected_home: Path | None = None,
) -> SessionPaths:
    """Resolve all paths without creating or modifying the filesystem.

    ``selected_home`` is intended for doctor/uninstall inspection of an
    already-running isolated session.  Normal launcher calls omit it so an
    ``isolated`` mode invocation always receives a new UUID home.
    """
    resolved_mode = resolve_mode(mode)
    workspace = project_root(workspace_dir or Path.cwd())
    source = Path(
        source_home or os.environ.get(ENV_CODEX_HOME) or canonical_codex_home()
    ).expanduser()

    if selected_home is not None:
        home = Path(selected_home).expanduser()
    elif resolved_mode == MODE_SHARED:
        home = Path(os.environ.get(ENV_CODEX_HOME) or source).expanduser()
    elif resolved_mode == MODE_WORKSPACE:
        home = workspaces_root(tokenpak_home) / workspace_hash(workspace)
    else:
        home = sessions_root(tokenpak_home) / _validated_session_id(session_id)

    return SessionPaths(
        mode=resolved_mode,
        home=home,
        source_home=source,
        workspace=workspace,
        config=home / "config.toml",
        auth=home / "auth.json",
        mcp_config=home / "config.toml",
        hooks=home / "hooks.json",
        agents=home / "AGENTS.md",
        # User skills remain at Codex's documented user discovery root.  The
        # selected config records explicit per-skill entries separately.
        skills_root=Path.home() / ".agents" / "skills",
        pid_sentinel=home / PID_SENTINEL_NAME,
    )


def current_paths(
    mode: str | None = None,
    *,
    workspace_dir: Path | str | None = None,
    tokenpak_home: Path | None = None,
    source_home: Path | None = None,
) -> SessionPaths:
    """Resolve the active home for doctor/uninstall without creating one."""
    resolved_mode = resolve_mode(mode)
    selected = os.environ.get(ENV_CODEX_HOME)
    if resolved_mode == MODE_ISOLATED and not selected:
        raise InvalidSessionMode(
            "isolated mode has no selected home outside a launch; set CODEX_HOME "
            "to the session home before running doctor or uninstall"
        )
    if resolved_mode == MODE_ISOLATED and selected:
        selected_path = Path(selected).expanduser().resolve()
        expected_parent = sessions_root(tokenpak_home).resolve()
        if selected_path.parent != expected_parent:
            raise InvalidSessionMode(
                "isolated CODEX_HOME is outside the TokenPak sessions root; "
                "refusing unsafe inspection or cleanup"
            )
    return select_paths(
        resolved_mode,
        workspace_dir=workspace_dir,
        tokenpak_home=tokenpak_home,
        source_home=source_home,
        # A workspace home is a deterministic function of the project.  An
        # inherited CODEX_HOME must not silently redirect it.  Isolated mode,
        # by contrast, can only be inspected when its selected home is passed
        # through the environment by the launcher.
        selected_home=(
            Path(selected) if selected and resolved_mode in {MODE_SHARED, MODE_ISOLATED} else None
        ),
    )


def _entry_stat(name: str, dir_fd: int) -> os.stat_result | None:
    """Return an entry's no-follow stat relative to a validated home."""
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_selected_home(paths: SessionPaths) -> int:
    """Create/open the selected home and pin its directory inode.

    Non-shared homes are TokenPak-owned private runtime directories.  Their
    immediate container and leaf are therefore forced to 0700 before any
    credential link or configuration is installed.  ``O_NOFOLLOW`` closes
    the leaf-symlink race; callers retain the returned descriptor for the
    entire lease so sentinel operations stay bound to this exact inode.
    """
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if paths.mode == MODE_SHARED:
        paths.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(paths.home), directory_flags)
    else:
        # Normal generated homes have this shape:
        # <tokenpak-home>/companion/codex/{sessions,workspaces}/<id>.  Create
        # the TokenPak-owned chain one component at a time and pin every
        # component with O_NOFOLLOW before creating the next.  Tests and
        # explicit internal callers with another shape use the immediate
        # parent as their private root.
        parents = paths.home.parents
        generated = (
            len(parents) >= 4
            and parents[0].name in {"sessions", "workspaces"}
            and parents[1].name == "codex"
            and parents[2].name == "companion"
        )
        private_root = parents[3] if generated else paths.home.parent
        root_created = not private_root.exists()
        private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        current_fd = os.open(
            str(private_root),
            directory_flags | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            root_stat = os.fstat(current_fd)
            if root_created:
                os.fchmod(current_fd, 0o700)
            elif root_stat.st_mode & 0o077:
                raise HomeInUseError(
                    f"selected CODEX_HOME private root is not 0700: {private_root}"
                )

            relative_parent = paths.home.parent.relative_to(private_root)
            for component in relative_parent.parts:
                created = False
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    created = True
                except FileExistsError:
                    pass
                next_fd = os.open(
                    component,
                    directory_flags | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
                component_stat = os.fstat(current_fd)
                if created:
                    os.fchmod(current_fd, 0o700)
                elif component_stat.st_mode & 0o077:
                    raise HomeInUseError(
                        f"selected CODEX_HOME private parent is not 0700: {paths.home.parent}"
                    )

            try:
                os.mkdir(paths.home.name, 0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            try:
                fd = os.open(
                    paths.home.name,
                    directory_flags | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise HomeInUseError(
                    f"selected CODEX_HOME is not a private directory: {paths.home}"
                ) from exc
            # The leaf is always TokenPak-owned, including a durable workspace
            # home from an earlier launch, so repair its privacy in place.
            try:
                os.fchmod(fd, 0o700)
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(current_fd)
    try:
        pinned = os.fstat(fd)
        current = os.stat(paths.home, follow_symlinks=paths.mode == MODE_SHARED)
        if not stat.S_ISDIR(pinned.st_mode) or (pinned.st_dev, pinned.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise HomeInUseError(f"selected CODEX_HOME changed during validation: {paths.home}")
        if paths.mode != MODE_SHARED and pinned.st_mode & 0o077:
            raise HomeInUseError(f"selected CODEX_HOME is not private (0700): {paths.home}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_bounded_regular(
    path: Path,
    *,
    dir_fd: int | None = None,
    private: bool = False,
    max_bytes: int = _MAX_SEED_BYTES,
) -> bytes | None:
    """Read a bounded regular file without following its final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path.name if dir_fd is not None else str(path), flags, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        source_stat = os.fstat(fd)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size > max_bytes:
            return None
        if private:
            getuid = getattr(os, "geteuid", None)
            if getuid is not None and source_stat.st_uid != getuid():
                return None
            if source_stat.st_mode & 0o077:
                return None
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, min(1024 * 1024, max_bytes + 1 - total)):
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _sanitized_config(data: bytes) -> bytes | None:
    """Validate one TOML config and remove isolation-breaking root keys."""
    try:
        text = data.decode("utf-8")
        tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None

    lines = text.splitlines()
    top_level_end = next(
        (index for index, line in enumerate(lines) if re.match(r"^\s*\[", line)),
        len(lines),
    )
    key_pattern = re.compile(
        r"^\s*(?:"
        + "|".join(
            rf"(?:{re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')"
            for key in _ISOLATION_BREAKING_CONFIG_KEYS
        )
        + r")\s*="
    )
    filtered = [
        line
        for index, line in enumerate(lines)
        if not (index < top_level_end and key_pattern.match(line))
    ]
    sanitized = "\n".join(filtered)
    if text.endswith("\n"):
        sanitized += "\n"
    try:
        parsed = tomllib.loads(sanitized)
    except tomllib.TOMLDecodeError:
        return None
    if any(key in parsed for key in _ISOLATION_BREAKING_CONFIG_KEYS):
        return None
    return sanitized.encode("utf-8")


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte, treating a zero-length write as an I/O failure."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting Codex session metadata")
        view = view[written:]


def _replace_private_at(dir_fd: int, name: str, data: bytes) -> None:
    """Atomically replace one regular file inside a pinned directory."""
    tmp_name = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name, dir_fd=dir_fd)


def _provision_config(src: Path, dir_fd: int, name: str) -> bool:
    """Seed or revalidate the selected home's private config."""
    existing = _entry_stat(name, dir_fd)
    if existing is None:
        source = _read_bounded_regular(src)
        if source is None:
            return False
        sanitized = _sanitized_config(source)
        if sanitized is None:
            return False
        _replace_private_at(dir_fd, name, sanitized)
        return True

    getuid = getattr(os, "geteuid", None)
    if (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_nlink != 1
        or (getuid is not None and existing.st_uid != getuid())
    ):
        raise RuntimeError(f"selected config is not a regular file: {name}")
    current = _read_bounded_regular(Path(name), dir_fd=dir_fd)
    if current is None:
        raise RuntimeError(f"selected config is unreadable or oversized: {name}")
    sanitized = _sanitized_config(current)
    if sanitized is None:
        raise RuntimeError(f"selected config is not safe UTF-8 TOML: {name}")
    if sanitized != current or existing.st_mode & 0o777 != 0o600:
        _replace_private_at(dir_fd, name, sanitized)
    return False


def _safe_credential_source(src: Path) -> Path | None:
    """Validate an externally refreshed credential without copying it."""
    raw = _read_bounded_regular(src, private=True)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return Path(os.path.abspath(src))


def _link_credential_once(src: Path, dir_fd: int, name: str) -> bool:
    """Install or validate one exact allowlisted credential symlink."""
    safe_source = _safe_credential_source(src)
    existing = _entry_stat(name, dir_fd)
    if existing is not None:
        if not stat.S_ISLNK(existing.st_mode):
            raise RuntimeError(f"selected credential is not a managed symlink: {name}")
        try:
            target = os.readlink(name, dir_fd=dir_fd)
        except OSError as exc:
            raise RuntimeError(f"selected credential link is unreadable: {name}") from exc
        if safe_source is None or target != str(safe_source):
            raise RuntimeError(f"selected credential link has an unsafe target: {name}")
        return False
    if safe_source is None:
        return False
    os.symlink(str(safe_source), name, dir_fd=dir_fd)
    return True


def _validate_selected_entries(dir_fd: int) -> None:
    """Reject redirectable managed files and aliased runtime databases."""
    for name in ("hooks.json", "AGENTS.md"):
        entry = _entry_stat(name, dir_fd)
        if entry is None:
            continue
        getuid = getattr(os, "geteuid", None)
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_nlink != 1
            or entry.st_size > _MAX_SEED_BYTES
            or (getuid is not None and entry.st_uid != getuid())
        ):
            raise RuntimeError(f"selected managed file is unsafe: {name}")

    runtime_pattern = re.compile(r"^(?:state|logs)_[^/]+\.sqlite(?:-(?:wal|wal2|shm|journal))?$")
    for name in os.listdir(dir_fd):
        if not runtime_pattern.fullmatch(name):
            continue
        entry = _entry_stat(name, dir_fd)
        if entry is None:
            continue
        getuid = getattr(os, "geteuid", None)
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_nlink != 1
            or (getuid is not None and entry.st_uid != getuid())
        ):
            raise RuntimeError(f"selected runtime database is aliased or unsafe: {name}")


def provision(paths: SessionPaths, *, home_fd: int | None = None) -> ProvisionedHome:
    """Create ``paths.home`` and seed only the closed safe-file allowlist."""
    if paths.mode == MODE_SHARED:
        return ProvisionedHome(paths, False, (), ())
    created = not paths.home.exists()
    owned_fd = home_fd is None
    fd = _open_selected_home(paths) if home_fd is None else home_fd

    seeded: list[str] = []
    linked: list[str] = []
    try:
        _validate_selected_entries(fd)
        for name in SAFE_CONFIG_FILES:
            if _provision_config(paths.source_home / name, fd, name):
                seeded.append(name)
        for name in SAFE_CREDENTIAL_LINKS:
            if _link_credential_once(paths.source_home / name, fd, name):
                linked.append(name)
        return ProvisionedHome(paths, created, tuple(seeded), tuple(linked))
    finally:
        if owned_fd:
            os.close(fd)


def _portable_process_identity(pid: int) -> tuple[str, int] | None:
    """Best-effort process incarnation identity when Linux procfs is absent."""
    if os.name == "posix":
        try:
            result = subprocess.run(
                ["ps", "-o", "stat=,lstart=", "-p", str(pid)],
                capture_output=True,
                env={**os.environ, "LC_ALL": "C"},
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        row = result.stdout.strip()
        if result.returncode != 0 or not row:
            return None
        fields = row.split(maxsplit=1)
        if len(fields) != 2:
            return None
        fingerprint = int.from_bytes(hashlib.sha256(fields[1].encode("utf-8")).digest()[:8], "big")
        return fields[0][0], fingerprint or 1

    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        )
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            if exit_code.value != 259:  # STILL_ACTIVE
                return None
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return "R", (created.high << 32) | created.low
        finally:
            kernel32.CloseHandle(handle)
    return None


def _proc_identity(pid: int, proc_root: Path = Path("/proc")) -> tuple[str, int] | None:
    """Return ``(state, start-time ticks)`` for one process incarnation."""
    if pid <= 0:
        return None
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        if proc_root == Path("/proc"):
            return _portable_process_identity(pid)
        return None
    rparen = raw.rfind(")")
    if rparen < 0:
        return None
    fields = raw[rparen + 1 :].split()
    if len(fields) <= 19:
        return None
    try:
        return fields[0], int(fields[19])
    except ValueError:
        return None


def _sentinel_from_data(data: object) -> PidSentinel | None:
    if not isinstance(data, dict):
        return None
    try:
        sentinel = PidSentinel(
            schema=str(data["schema"]),
            pid=int(data["pid"]),
            start_time_ticks=int(data["start_time_ticks"]),
            session_id=str(data["session_id"]),
            mode=str(data["mode"]),
            home=str(data["home"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if (
        sentinel.schema != _SENTINEL_SCHEMA
        or sentinel.pid <= 0
        or sentinel.start_time_ticks <= 0
        or sentinel.mode not in VALID_MODES
        or not _SESSION_ID_RE.fullmatch(sentinel.session_id)
    ):
        return None
    return sentinel


def read_pid_sentinel(path: Path, *, dir_fd: int | None = None) -> PidSentinel | None:
    """Parse a bounded private regular sentinel without following links."""
    raw = _read_bounded_regular(
        path,
        dir_fd=dir_fd,
        private=True,
        max_bytes=_MAX_SENTINEL_BYTES,
    )
    if raw is None:
        return None
    try:
        return _sentinel_from_data(json.loads(raw))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def sentinel_is_live(
    sentinel: PidSentinel,
    *,
    expected_home: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Validate process incarnation and selected-home binding."""
    if expected_home is not None:
        try:
            if Path(sentinel.home).resolve() != expected_home.resolve():
                return False
        except OSError:
            return False
    identity = _proc_identity(sentinel.pid, proc_root)
    if identity is None:
        return False
    state, start_ticks = identity
    return state not in {"Z", "X", "x"} and start_ticks == sentinel.start_time_ticks


@contextlib.contextmanager
def _lease_guard(home: Path, *, home_fd: int | None = None) -> Iterator[None]:
    """Serialize lease mutation without deleting or signalling processes."""
    guard = home / _LEASE_GUARD_NAME
    guard_stat = _entry_stat(_LEASE_GUARD_NAME, home_fd) if home_fd is not None else None
    if home_fd is None and guard.is_symlink():
        raise HomeInUseError(f"invalid lease guard: {guard}")
    if guard_stat is not None and not stat.S_ISREG(guard_stat.st_mode):
        raise HomeInUseError(f"invalid lease guard: {guard}")
    fd = os.open(
        _LEASE_GUARD_NAME if home_fd is not None else str(guard),
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=home_fd,
    )
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise HomeInUseError(f"invalid lease guard: {guard}")
    with _THREAD_LEASE_LOCK:
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - non-POSIX fallback
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):  # pragma: no cover - fallback
                pass
            os.close(fd)


def _write_sentinel_exclusive(
    path: Path, sentinel: PidSentinel, *, dir_fd: int | None = None
) -> None:
    payload = (json.dumps(asdict(sentinel), sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(
        path.name if dir_fd is not None else str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=dir_fd,
    )
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


class SessionLease:
    """Owner-checked ``codex.pid`` lifecycle lease."""

    def __init__(
        self,
        paths: SessionPaths,
        sentinel: PidSentinel,
        *,
        proc_root: Path = Path("/proc"),
        home_fd: int,
    ) -> None:
        self.paths = paths
        self.sentinel = sentinel
        self.proc_root = proc_root
        self.home_fd = home_fd
        self._released = False

    def assert_home_binding(self) -> None:
        """Fail if the selected pathname no longer names the pinned home."""
        if self.paths.mode == MODE_SHARED:
            return
        try:
            current = os.stat(self.paths.home, follow_symlinks=False)
            pinned = os.fstat(self.home_fd)
        except OSError as exc:
            raise HomeInUseError("selected CODEX_HOME disappeared during launch") from exc
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
            pinned.st_dev,
            pinned.st_ino,
        ):
            raise HomeInUseError("selected CODEX_HOME changed during launch")

    @classmethod
    def acquire(
        cls,
        paths: SessionPaths,
        *,
        pid: int | None = None,
        session_id: str | None = None,
        proc_root: Path = Path("/proc"),
    ) -> "SessionLease":
        home_fd = _open_selected_home(paths)
        owner_pid = pid if pid is not None else os.getpid()
        identity = _proc_identity(owner_pid, proc_root)
        if identity is None:
            os.close(home_fd)
            raise RuntimeError(f"cannot validate launcher PID {owner_pid}")
        state, start_ticks = identity
        if state in {"Z", "X", "x"}:
            os.close(home_fd)
            raise RuntimeError(f"launcher PID {owner_pid} is not running")
        sentinel = PidSentinel(
            schema=_SENTINEL_SCHEMA,
            pid=owner_pid,
            start_time_ticks=start_ticks,
            session_id=_validated_session_id(session_id),
            mode=paths.mode,
            home=str(paths.home.resolve()),
        )

        try:
            with _lease_guard(paths.home, home_fd=home_fd):
                if _entry_stat(PID_SENTINEL_NAME, home_fd) is not None:
                    existing = read_pid_sentinel(paths.pid_sentinel, dir_fd=home_fd)
                    if existing is None:
                        raise HomeInUseError(
                            f"invalid {paths.pid_sentinel}; refusing unsafe replacement"
                        )
                    if sentinel_is_live(existing, proc_root=proc_root):
                        if not sentinel_is_live(
                            existing, expected_home=paths.home, proc_root=proc_root
                        ):
                            raise HomeInUseError(
                                f"live {paths.pid_sentinel} is bound to another home; "
                                "refusing unsafe replacement"
                            )
                        raise HomeInUseError(
                            f"{paths.home} is already claimed by PID {existing.pid}"
                        )
                    os.unlink(PID_SENTINEL_NAME, dir_fd=home_fd)
                _write_sentinel_exclusive(paths.pid_sentinel, sentinel, dir_fd=home_fd)
            return cls(paths, sentinel, proc_root=proc_root, home_fd=home_fd)
        except BaseException:
            os.close(home_fd)
            raise

    def transfer_to(self, pid: int) -> None:
        """Transfer the lease to the spawned child process incarnation."""
        identity = _proc_identity(pid, self.proc_root)
        if identity is None:
            raise RuntimeError(f"cannot validate Codex child PID {pid}")
        state, start_ticks = identity
        if state in {"Z", "X", "x"}:
            raise RuntimeError(f"Codex child PID {pid} is not running")
        replacement = PidSentinel(
            schema=_SENTINEL_SCHEMA,
            pid=pid,
            start_time_ticks=start_ticks,
            session_id=self.sentinel.session_id,
            mode=self.sentinel.mode,
            home=self.sentinel.home,
        )
        with _lease_guard(self.paths.home, home_fd=self.home_fd):
            current = read_pid_sentinel(self.paths.pid_sentinel, dir_fd=self.home_fd)
            if current != self.sentinel:
                raise HomeInUseError("PID sentinel ownership changed during launch")
            tmp_name = f".{PID_SENTINEL_NAME}.{uuid.uuid4().hex}.tmp"
            fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self.home_fd,
            )
            try:
                payload = (json.dumps(asdict(replacement), sort_keys=True) + "\n").encode("utf-8")
                _write_all(fd, payload)
                os.fsync(fd)
                os.close(fd)
                fd = -1
                os.replace(
                    tmp_name,
                    PID_SENTINEL_NAME,
                    src_dir_fd=self.home_fd,
                    dst_dir_fd=self.home_fd,
                )
            finally:
                if fd >= 0:
                    os.close(fd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_name, dir_fd=self.home_fd)
        self.sentinel = replacement

    def release(self) -> bool:
        """Remove only the still-matching sentinel owned by this session."""
        if self._released:
            return False
        removed = False
        with _lease_guard(self.paths.home, home_fd=self.home_fd):
            current = read_pid_sentinel(self.paths.pid_sentinel, dir_fd=self.home_fd)
            if current == self.sentinel:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(PID_SENTINEL_NAME, dir_fd=self.home_fd)
                removed = True
        self._released = True
        os.close(self.home_fd)
        return removed

    def __enter__(self) -> "SessionLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()
