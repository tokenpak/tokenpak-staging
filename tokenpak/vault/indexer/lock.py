"""Advisory flock(2) helper + atomic-write helper.

Implements design §3.2 (lock primitive), §3.3 (contention/timeout), and
§3.5 (atomic-write pattern). The same lock coordinates three writers:
the async indexer, the nightly canonical rebuild, and the surgical patch-
mode CLI. Each acquires the lock once around a write batch — never once
per block.

The helper is a thin wrapper around ``fcntl.flock`` (no third-party
dependency). A blocking-with-timeout flavour is implemented with a small
polling loop because ``flock`` itself does not accept a timeout; that
keeps the design's 30 s / 5 min contention budgets honest without
introducing signals or thread shenanigans.
"""

from __future__ import annotations

import fcntl
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


def default_lock_path() -> Path:
    """Sentinel lockfile path per design §3.2."""
    return Path(os.path.expanduser("~/vault/.tokenpak/.index-write.lock"))


class LockTimeoutError(Exception):
    """The caller waited the budget and never acquired the lock."""

    def __init__(self, lock_path: Path, timeout_s: float) -> None:
        super().__init__(
            f"flock acquisition timed out after {timeout_s}s on {lock_path}"
        )
        self.lock_path = lock_path
        self.timeout_s = timeout_s


@dataclass(frozen=True)
class LockHandle:
    """File descriptor + path of an acquired lock. Returned for diagnostics."""

    fd: int
    path: Path


def _open_lock_fd(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)


def _try_acquire(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — still alive.
        return True
    return True


@contextmanager
def acquire(
    lock_path: Path | None = None,
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.1,
    write_pid: bool = True,
) -> Iterator[LockHandle]:
    """Acquire the §3.2 advisory lock with a polled timeout.

    Releases on context exit via OS fd close, satisfying §3.2's "crashed
    writer releases on process exit" invariant.

    ``write_pid`` truncates the sentinel and writes the holder's PID so a
    later acquirer can detect a stale lock by reading the PID and checking
    liveness with ``kill -0`` (design §3.2). The PID-hint mechanism is
    advisory only — correctness comes from ``flock`` itself.
    """
    path = lock_path or default_lock_path()
    deadline = time.monotonic() + timeout_s
    fd = _open_lock_fd(path)
    try:
        while True:
            if _try_acquire(fd):
                break
            if time.monotonic() >= deadline:
                raise LockTimeoutError(path, timeout_s)
            time.sleep(poll_interval_s)

        if write_pid:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            # Don't fsync — the PID is an advisory hint, not a durability
            # contract. A crash before fsync is fine; the next acquirer's
            # liveness check covers it.

        yield LockHandle(fd=fd, path=path)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def read_pid_hint(lock_path: Path | None = None) -> int | None:
    """Return the writer-PID hint from the sentinel, if parseable.

    A return value of ``None`` means "no hint available". A live caller
    should *not* trust this for mutual exclusion — use ``acquire`` for that.
    """
    path = lock_path or default_lock_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def stale_lock_detected(lock_path: Path | None = None) -> bool:
    """True if the sentinel names a PID that's not alive (advisory diag)."""
    pid = read_pid_hint(lock_path)
    if pid is None:
        return False
    return not _is_pid_alive(pid)


# --- Atomic-write helper (§3.5) -------------------------------------------


def atomic_replace(target: Path, content: bytes) -> None:
    """Write *content* to a sibling tempfile, then ``os.replace`` over target.

    The caller is expected to already hold the §3.2 lock for the batch.
    Use of ``os.replace`` (``rename(2)`` on Linux) on the same filesystem
    is atomic: any concurrent reader observes either the old or the new
    artifact, never a partial write (design §3.5).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.tmp.{uuid.uuid4().hex}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


__all__ = [
    "LockHandle",
    "LockTimeoutError",
    "acquire",
    "atomic_replace",
    "default_lock_path",
    "read_pid_hint",
    "stale_lock_detected",
]


# Keep imports tidy for ruff/mypy in environments where these names are
# referenced only via __all__ above.
_ = Any
