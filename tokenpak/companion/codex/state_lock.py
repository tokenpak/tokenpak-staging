# SPDX-License-Identifier: Apache-2.0
"""Read-only Codex SQLite holder diagnostics for ``CODEX_HOME``.

The diagnostic path must not join, copy, checkpoint, or otherwise touch a
Codex runtime database.  On Linux, the kernel already exposes the evidence we
need: database file identities, open file descriptors, and advisory byte-range
locks.  This module correlates those sources without importing ``sqlite3`` or
opening a database file.

SQLite's rollback-mode locks live in the main database's pending/reserved/
shared byte range.  WAL coordination locks live in bytes 120 through 127 of
the ``-shm`` file, with byte 128 used for dead-man-switch coordination.  An
open descriptor to a Codex database or sidecar is also treated as an unsafe
shared-home attachment, even if the process happens to be between lock
operations when sampled.
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path

# Companion-internal launcher helper (probe/remediation_hint are called by
# launcher.py, not by end users): export nothing as released public API.
__all__: list[str] = []

# Retained for callers and older installations.  Discovery itself is dynamic.
STATE_DB_NAME = "state_5.sqlite"
_LOG_DB_NAME = "logs_2.sqlite"

_PROC_ROOT = Path("/proc")

# SQLite locking bytes from sqlite3.h/os_unix.c.
_SQLITE_PENDING_BYTE = 0x40000000
_SQLITE_SHARED_FIRST = _SQLITE_PENDING_BYTE + 2
_SQLITE_SHARED_SIZE = 510
_SQLITE_MAIN_LOCK_LAST = _SQLITE_SHARED_FIRST + _SQLITE_SHARED_SIZE - 1
_SQLITE_SHM_LOCK_FIRST = 120
_SQLITE_SHM_LOCK_LAST = 127
_SQLITE_SHM_DMS = 128

_DEAD_STATES = frozenset({"X", "x", "Z"})
_STOPPED_STATES = frozenset({"T", "t"})
_BENIGN_UNREADABLE_PROCESSES = frozenset({"sd-pam"})


@dataclass
class LockStatus:
    """Result of a read-only state-lock preflight on one ``CODEX_HOME``."""

    home: Path
    db_path: Path
    exists: bool
    locked: bool
    holder_pids: list[int] = field(default_factory=list)
    stopped_pids: list[int] = field(default_factory=list)
    detail: str = ""
    running_pids: list[int] = field(default_factory=list)
    diagnostics_complete: bool = True


@dataclass(frozen=True)
class _FileIdentity:
    path: Path
    device: int
    inode: int
    owner_uid: int
    role: str
    db_path: Path

    @property
    def proc_device(self) -> tuple[int, int]:
        return os.major(self.device), os.minor(self.device)


@dataclass(frozen=True)
class _ProcLock:
    pid: int
    device_major: int
    device_minor: int
    inode: int
    start: int
    end: int | None


@dataclass(frozen=True)
class _ProcessInfo:
    pid: int
    uid: int
    state: str
    start_time: int

    @property
    def stopped(self) -> bool:
        return self.state in _STOPPED_STATES


def _db_path(home: Path) -> Path:
    """Compatibility fallback used when a home has no database yet."""
    return home / STATE_DB_NAME


def _db_paths(home: Path) -> tuple[list[Path], bool]:
    """Discover Codex databases without assuming a schema generation."""
    try:
        entries = list(home.iterdir())
    except FileNotFoundError:
        return [], True
    except OSError:
        return [], False
    candidates = [
        path
        for path in entries
        if path.name.endswith(".sqlite") and path.name.startswith(("state_", "logs_"))
    ]
    result: list[Path] = []
    complete = True
    for path in sorted(
        candidates, key=lambda item: (not item.name.startswith("state_"), item.name)
    ):
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
            continue
        if stat_module.S_ISREG(mode):
            result.append(path)
    return result, complete


def _target_files(db_path: Path) -> tuple[list[_FileIdentity], bool]:
    targets: list[_FileIdentity] = []
    complete = True
    for path, role in (
        (db_path, "main"),
        (Path(f"{db_path}-wal"), "wal"),
        (Path(f"{db_path}-shm"), "shm"),
    ):
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
            continue
        if stat_module.S_ISREG(st.st_mode):
            targets.append(
                _FileIdentity(
                    path=path,
                    device=st.st_dev,
                    inode=st.st_ino,
                    owner_uid=st.st_uid,
                    role=role,
                    db_path=db_path,
                )
            )
    return targets, complete


def _parse_proc_lock_line(line: str) -> _ProcLock | None:
    """Parse one ``/proc/locks`` or ``fdinfo`` lock row."""
    line = line.strip()
    if line.startswith("lock:"):
        line = line[5:].strip()
    fields = line.split()
    if len(fields) < 8:
        return None
    if len(fields) > 1 and fields[1] == "->":
        fields.pop(1)
    if len(fields) < 8:
        return None
    try:
        pid = int(fields[4])
        major_raw, minor_raw, inode_raw = fields[5].split(":", 2)
        device_major = int(major_raw, 16)
        device_minor = int(minor_raw, 16)
        inode = int(inode_raw)
        start = int(fields[6])
        end = None if fields[7] == "EOF" else int(fields[7])
    except (TypeError, ValueError):
        return None
    return _ProcLock(
        pid=pid,
        device_major=device_major,
        device_minor=device_minor,
        inode=inode,
        start=start,
        end=end,
    )


def _read_proc_locks(proc_root: Path = _PROC_ROOT) -> tuple[list[_ProcLock], bool]:
    try:
        rows = (proc_root / "locks").read_text(errors="replace").splitlines()
    except OSError:
        return [], False
    locks: list[_ProcLock] = []
    complete = True
    for row in rows:
        lock = _parse_proc_lock_line(row)
        if lock is None:
            if row.strip():
                complete = False
            continue
        locks.append(lock)
    return locks, complete


def _read_process_info(pid: int, proc_root: Path = _PROC_ROOT) -> _ProcessInfo | None:
    """Return a TGID-normalized process identity, resistant to PID reuse."""
    if pid <= 0:
        return None
    process_dir = proc_root / str(pid)
    try:
        status = (process_dir / "status").read_text(errors="replace")
        raw_stat = (process_dir / "stat").read_text(errors="replace")
    except OSError:
        return None

    tgid: int | None = None
    uid: int | None = None
    for line in status.splitlines():
        if line.startswith("Tgid:"):
            try:
                tgid = int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
        elif line.startswith("Uid:"):
            try:
                uid = int(line.split(":", 1)[1].split()[0])
            except (IndexError, ValueError):
                return None
    if tgid is None or uid is None:
        return None
    if tgid != pid:
        return _read_process_info(tgid, proc_root)

    # /proc/<pid>/stat is ``pid (comm) state ...``.  comm may contain spaces
    # and parentheses, so only the final right parenthesis is structural.
    rparen = raw_stat.rfind(")")
    if rparen < 0:
        return None
    tail = raw_stat[rparen + 1 :].split()
    if len(tail) <= 19:
        return None
    state = tail[0]
    try:
        start_time = int(tail[19])  # field 22 in proc_pid_stat(5)
    except ValueError:
        return None
    return _ProcessInfo(pid=tgid, uid=uid, state=state, start_time=start_time)


def _same_process(info: _ProcessInfo, proc_root: Path = _PROC_ROOT) -> _ProcessInfo | None:
    current = _read_process_info(info.pid, proc_root)
    if current is None or current.state in _DEAD_STATES or current.start_time != info.start_time:
        return None
    return current


def _identity_key(device: int, inode: int) -> tuple[int, int]:
    return device, inode


def _codex_process_kind(pid: int, proc_root: Path = _PROC_ROOT) -> bool | None:
    """Classify Codex, explicitly benign, or unknown process wrappers."""
    process_dir = proc_root / str(pid)
    names: list[str] = []
    try:
        comm = (process_dir / "comm").read_text(errors="replace").strip()
    except OSError:
        comm = ""
    if comm:
        names.append(comm)
    try:
        command = (process_dir / "cmdline").read_bytes()
    except OSError:
        command = b""
    names.extend(token.decode(errors="replace") for token in command.split(b"\0") if token)
    if not names:
        return None
    normalized = {Path(name).name.casefold() for name in names}
    if any("codex" in name for name in normalized):
        return True
    # Generic interpreters and arbitrary readable process names are not proof
    # that the process cannot own a Codex database.  Only a deliberately tiny
    # benign allowlist avoids the known non-dumpable desktop-session false
    # blocker; every other readable name remains unknown and fails closed.
    if normalized and normalized <= _BENIGN_UNREADABLE_PROCESSES:
        return False
    return None


def _inspection_failure_is_unsafe(
    info: _ProcessInfo,
    owner_uids: set[int],
    proc_root: Path,
) -> bool:
    """Fail closed only for live same-owner Codex or unclassified processes."""
    current = _same_process(info, proc_root)
    if current is None or current.uid not in owner_uids:
        return False
    return _codex_process_kind(current.pid, proc_root) is not False


def _scan_fd_holders(
    targets: list[_FileIdentity], proc_root: Path = _PROC_ROOT
) -> tuple[dict[int, _ProcessInfo], list[_ProcLock], bool]:
    """Find TGIDs with descriptors for a target and inspect their fdinfo."""
    identities = {_identity_key(target.device, target.inode) for target in targets}
    owner_uids = {target.owner_uid for target in targets if target.role == "main"}
    holders: dict[int, _ProcessInfo] = {}
    fd_locks: list[_ProcLock] = []
    complete = True
    try:
        process_dirs = list(proc_root.iterdir())
    except OSError:
        return holders, fd_locks, False

    for process_dir in process_dirs:
        if not process_dir.name.isdigit():
            continue
        info = _read_process_info(int(process_dir.name), proc_root)
        if info is None:
            # A same-owner process that remains present but cannot be parsed
            # cannot safely be excluded from the holder scan.  Use the proc
            # directory owner only as a fallback because status is unavailable.
            try:
                process_uid = process_dir.stat().st_uid
            except OSError:
                continue
            process_kind = _codex_process_kind(int(process_dir.name), proc_root)
            if process_uid in owner_uids and process_dir.exists() and process_kind is not False:
                complete = False
            continue
        if info.state in _DEAD_STATES:
            continue
        if info.pid in holders:
            continue
        fd_dir = proc_root / str(info.pid) / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except OSError:
            if _inspection_failure_is_unsafe(info, owner_uids, proc_root):
                complete = False
            continue
        matching_fds: list[str] = []
        for descriptor in descriptors:
            try:
                st = descriptor.stat()
            except FileNotFoundError:
                continue
            except OSError:
                if _inspection_failure_is_unsafe(info, owner_uids, proc_root):
                    complete = False
                continue
            if _identity_key(st.st_dev, st.st_ino) in identities:
                matching_fds.append(descriptor.name)
        if not matching_fds:
            continue

        # Re-read stat after descriptor matching so a recycled PID cannot be
        # attributed using observations from two different processes.
        current = _same_process(info, proc_root)
        if current is None:
            continue
        holders[current.pid] = current

        for fd_name in matching_fds:
            try:
                rows = (
                    (proc_root / str(current.pid) / "fdinfo" / fd_name)
                    .read_text(errors="replace")
                    .splitlines()
                )
            except FileNotFoundError:
                continue
            except OSError:
                if _inspection_failure_is_unsafe(current, owner_uids, proc_root):
                    complete = False
                continue
            for row in rows:
                if not row.lstrip().startswith("lock:"):
                    continue
                lock = _parse_proc_lock_line(row)
                if lock is not None:
                    fd_locks.append(lock)

    return holders, fd_locks, complete


def _ranges_overlap(
    first_start: int, first_end: int | None, second_start: int, second_end: int
) -> bool:
    effective_end = (1 << 63) - 1 if first_end is None else first_end
    return first_start <= second_end and effective_end >= second_start


def _lock_targets_file(lock: _ProcLock, target: _FileIdentity) -> bool:
    major, minor = target.proc_device
    if (lock.device_major, lock.device_minor, lock.inode) != (major, minor, target.inode):
        return False
    if target.role == "main":
        return _ranges_overlap(
            lock.start,
            lock.end,
            _SQLITE_PENDING_BYTE,
            _SQLITE_MAIN_LOCK_LAST,
        )
    if target.role == "shm":
        return _ranges_overlap(
            lock.start,
            lock.end,
            _SQLITE_SHM_LOCK_FIRST,
            _SQLITE_SHM_DMS,
        )
    return False


def _lock_pid_info(
    lock: _ProcLock,
    holders: dict[int, _ProcessInfo],
    proc_root: Path = _PROC_ROOT,
) -> _ProcessInfo | None:
    """Map a kernel lock owner to a validated TGID with a matching FD."""
    # OFD locks use PID -1 in /proc/locks; fdinfo plus the descriptor scan
    # provides the owning TGID in that case.
    if lock.pid <= 0:
        return None
    info = _read_process_info(lock.pid, proc_root)
    if info is None:
        return None
    held = holders.get(info.pid)
    if held is None or held.start_time != info.start_time:
        return None
    return _same_process(held, proc_root)


def _evidence_for_db(
    db_path: Path,
    proc_locks: list[_ProcLock],
    proc_root: Path = _PROC_ROOT,
) -> tuple[bool, dict[int, _ProcessInfo], bool, bool]:
    targets, targets_complete = _target_files(db_path)
    holders, fd_locks, proc_complete = _scan_fd_holders(targets, proc_root)
    relevant = [
        lock
        for lock in (*proc_locks, *fd_locks)
        if any(_lock_targets_file(lock, target) for target in targets)
    ]

    # Descriptor attachment is deliberately sufficient.  SQLite locks are
    # transient, so a shared-home check that sampled only /proc/locks could
    # incorrectly declare an active runtime safe between transactions.
    unsafe = bool(holders or relevant)
    for lock in relevant:
        info = _lock_pid_info(lock, holders, proc_root)
        if info is not None:
            holders[info.pid] = info
    holders = {
        pid: current
        for pid, info in holders.items()
        if (current := _same_process(info, proc_root)) is not None
    }
    return unsafe, holders, targets_complete, proc_complete


def _pid_alive(pid: int) -> bool:
    """Compatibility helper implemented as a read-only ``/proc`` lookup."""
    info = _read_process_info(pid)
    return bool(info and info.state not in _DEAD_STATES)


def _pid_stopped(pid: int) -> bool:
    """Return whether a live process is stopped or being traced."""
    info = _read_process_info(pid)
    return bool(info and info.state not in _DEAD_STATES and info.stopped)


def _incomplete_component(
    *,
    discovery_complete: bool,
    targets_complete: bool,
    proc_complete: bool,
) -> str:
    components: list[str] = []
    if not discovery_complete:
        components.append("Codex database discovery")
    if not targets_complete:
        components.append("Codex database target inspection")
    if not proc_complete:
        components.append("/proc holder inspection")
    return " and ".join(components)


def probe(home: "Path | str | None" = None, *, proc_root: Path = _PROC_ROOT) -> LockStatus:
    """Inspect Codex database attachments under ``home`` without opening them."""
    if home is None:
        home = os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    home = Path(home)
    dbs, discovery_complete = _db_paths(home)
    fallback = _db_path(home)
    if not dbs:
        if not discovery_complete:
            return LockStatus(
                home=home,
                db_path=fallback,
                exists=True,
                locked=True,
                detail=("Codex database discovery is incomplete; refusing unsafe access"),
                diagnostics_complete=False,
            )
        return LockStatus(
            home=home,
            db_path=fallback,
            exists=False,
            locked=False,
            detail="no Codex local database yet (uncontended)",
        )

    proc_locks, locks_complete = _read_proc_locks(proc_root)
    incomplete_db: Path | None = None
    incomplete_component: str | None = None
    for db_path in dbs:
        unsafe, holders, targets_complete, fd_scan_complete = _evidence_for_db(
            db_path, proc_locks, proc_root
        )
        proc_complete = locks_complete and fd_scan_complete
        diagnostics_complete = discovery_complete and targets_complete and proc_complete
        if not unsafe:
            if not diagnostics_complete and incomplete_db is None:
                incomplete_db = db_path
                incomplete_component = _incomplete_component(
                    discovery_complete=discovery_complete,
                    targets_complete=targets_complete,
                    proc_complete=proc_complete,
                )
            continue
        running = sorted(pid for pid, info in holders.items() if not info.stopped)
        stopped = sorted(pid for pid, info in holders.items() if info.stopped)
        all_holders = sorted(holders)
        return LockStatus(
            home=home,
            db_path=db_path,
            exists=True,
            locked=True,
            holder_pids=all_holders,
            stopped_pids=stopped,
            running_pids=running,
            detail=_format_lock_detail(
                running,
                stopped,
                db_path.name,
                diagnostics_complete=diagnostics_complete,
                incomplete_component=_incomplete_component(
                    discovery_complete=discovery_complete,
                    targets_complete=targets_complete,
                    proc_complete=proc_complete,
                ),
            ),
            diagnostics_complete=diagnostics_complete,
        )

    if incomplete_db is not None:
        return LockStatus(
            home=home,
            db_path=incomplete_db,
            exists=True,
            locked=True,
            detail=_format_lock_detail(
                [],
                [],
                incomplete_db.name,
                diagnostics_complete=False,
                incomplete_component=incomplete_component or "/proc holder inspection",
            ),
            diagnostics_complete=False,
        )

    names = ", ".join(db.name for db in dbs)
    return LockStatus(
        home=home,
        db_path=dbs[0],
        exists=True,
        locked=False,
        detail=f"Codex local databases have no live attachments: {names}",
    )


def _format_lock_detail(
    running: list[int],
    stopped: list[int],
    db_name: str,
    *,
    diagnostics_complete: bool = True,
    incomplete_component: str = "/proc holder inspection",
) -> str:
    """Describe every validated holder and its current process state."""
    if not running and not stopped:
        detail = f"{db_name} has SQLite lock evidence, but no holder passed /proc validation"
    else:
        states = [
            *(f"PID {pid} (running)" for pid in running),
            *(f"PID {pid} (stopped)" for pid in stopped),
        ]
        detail = f"{db_name} has live Codex database holder(s): {', '.join(states)}"
    if not diagnostics_complete:
        detail += f"; {incomplete_component} is incomplete, refusing unsafe access"
    return detail


def remediation_hint(status: LockStatus) -> str:
    """Return mode-aware guidance for a contended shared home."""
    lines = [
        f"tokenpak: Codex local database is locked: {status.db_path}",
        f"          {status.detail}",
    ]
    if status.stopped_pids:
        lines.append(
            "          a stopped holder must be resumed and exited normally "
            "before this shared home is safe"
        )
    elif status.running_pids:
        lines.append("          finish or close the running session normally before retrying")
    lines.append(
        "          to run a parallel session without contention, set "
        "TOKENPAK_CODEX_SESSION_MODE=workspace (per-project home) or "
        "=isolated (fresh per-session home)."
    )
    return "\n".join(lines)
