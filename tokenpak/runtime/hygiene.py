# SPDX-License-Identifier: Apache-2.0
"""Registry-aware session registration for Runtime Hygiene.

The launch-time orchestration layer: gather best-effort process identity,
build a :class:`~tokenpak.runtime.hygiene_schema.SessionManifest`, and persist
it through :mod:`tokenpak.runtime.hygiene_registry` *before* a session is
spawned. This is what makes a TokenPak launcher "registry-aware".

Write-failure policy (contract "Registry Requirements"):

* **cleanup-capable launch** (``cleanup_policy=term_allowed``) — if the
  manifest cannot be durably written, :func:`register_session` raises so the
  caller aborts before spawning. A session TokenPak cannot prove it owns must
  never be spawned with TERM authority.
* **non-cleanup launch** (``report_only`` / ``never_touch``) — a write failure
  is downgraded to ``never_touch`` and the launch may proceed; it simply
  forfeits any future cleanup eligibility.

Authority rule (contract acceptance #7): ``term_allowed`` is only legitimate
when TokenPak created the containment. A matching process group alone is *not*
authority, so requesting TERM authority without
``containment_created_by_tokenpak=True`` is a programming error and raises.

Snapshot note: ``__all__ = []`` — internal plumbing, not released public API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from . import hygiene_registry as _registry
from .hygiene_registry import ManifestWriteError
from .hygiene_schema import (
    CleanupPolicy,
    ContainmentMethod,
    SessionManifest,
    redact_command_shape,
)

# Launcher schema version this module emits. Distinct from the manifest
# SCHEMA_VERSION (on-disk layout) — this tracks the launcher's own behavior so
# a manifest can be attributed to the code that wrote it.
LAUNCHER_VERSION = "1"


@dataclass
class ProcessIdentity:
    """Best-effort process identity snapshot for the manifest.

    Every field except ``pid``/``uid`` is optional because it is platform- or
    permission-dependent; a missing field weakens later cleanup eligibility
    rather than blocking registration.
    """

    pid: int
    uid: int
    pid_start_time: Optional[str] = None
    boot_id: Optional[str] = None
    process_group_id: Optional[int] = None
    os_sid: Optional[int] = None


@dataclass
class RegistrationResult:
    """Outcome of :func:`register_session`."""

    session_id: str
    manifest: SessionManifest
    registered: bool  # True if the manifest was durably written
    cleanup_policy: str  # effective policy (may be downgraded to never_touch)
    manifest_path: Optional[Path] = None


def collect_identity(pid: "int | None" = None) -> ProcessIdentity:
    """Snapshot the current (or given) process's identity, best-effort.

    On non-Linux platforms or when ``/proc`` is unavailable, the Linux-only
    fields degrade to ``None`` — the manifest still records ``pid``/``uid`` and
    whatever else the OS exposes.
    """
    pid = pid if pid is not None else os.getpid()
    uid = _safe(lambda: os.getuid(), default=0)  # type: ignore[arg-type]
    return ProcessIdentity(
        pid=pid,
        uid=uid if isinstance(uid, int) else 0,
        pid_start_time=_pid_start_time(pid),
        boot_id=_boot_id(),
        process_group_id=_safe(lambda: os.getpgid(pid)),
        os_sid=_safe(lambda: os.getsid(pid)),
    )


def register_session(
    *,
    session_id: str,
    launch_mode: str,
    cleanup_policy: str,
    state_home: "str | Path",
    command: "Sequence[str] | str | None" = None,
    tokenpak_version: str,
    agent_id: "str | None" = None,
    containment_created_by_tokenpak: bool = False,
    containment_method: str = ContainmentMethod.NONE,
    containment_id: "str | None" = None,
    identity: "ProcessIdentity | None" = None,
) -> RegistrationResult:
    """Build + persist a session manifest, honoring the write-failure policy.

    Returns a :class:`RegistrationResult`. Raises :class:`ManifestWriteError`
    only for a *cleanup-capable* launch whose manifest could not be written
    (the caller must abort before spawn); non-cleanup launches never raise on
    write failure — they come back with ``registered=False`` and an effective
    ``cleanup_policy`` of ``never_touch``.

    Raises :class:`ValueError` if ``cleanup_policy=term_allowed`` is requested
    without TokenPak-created containment (a process-group match alone is not
    cleanup authority — contract acceptance #7).
    """
    term_capable = cleanup_policy == CleanupPolicy.TERM_ALLOWED
    if term_capable and not (
        containment_created_by_tokenpak
        and containment_method != ContainmentMethod.NONE
    ):
        raise ValueError(
            "cleanup_policy=term_allowed requires TokenPak-created containment "
            "(containment_created_by_tokenpak=True and a non-'none' "
            "containment_method); a matching process group alone is not authority"
        )

    ident = identity if identity is not None else collect_identity()
    manifest = SessionManifest(
        tokenpak_session_id=session_id,
        pid=ident.pid,
        uid=ident.uid,
        state_home=str(state_home),
        heartbeat_path=str(_registry.heartbeat_path(session_id)),
        launch_mode=launch_mode,
        cleanup_policy=cleanup_policy,
        containment_created_by_tokenpak=containment_created_by_tokenpak,
        containment_method=containment_method,
        containment_id=containment_id,
        tokenpak_version=tokenpak_version,
        launcher_version=LAUNCHER_VERSION,
        command_shape=redact_command_shape(command),
        agent_id=agent_id,
        pid_start_time=ident.pid_start_time,
        boot_id=ident.boot_id,
        process_group_id=ident.process_group_id,
        os_sid=ident.os_sid,
    )

    try:
        path = _registry.write_manifest(manifest)
    except ManifestWriteError:
        if term_capable:
            # Cleanup-capable launch with no durable manifest: re-raise so the
            # caller aborts before spawning a TERM-authorized child.
            raise
        # Non-cleanup launch: fail safe to never_touch and continue.
        manifest.cleanup_policy = CleanupPolicy.NEVER_TOUCH
        return RegistrationResult(
            session_id=session_id,
            manifest=manifest,
            registered=False,
            cleanup_policy=CleanupPolicy.NEVER_TOUCH,
            manifest_path=None,
        )

    return RegistrationResult(
        session_id=session_id,
        manifest=manifest,
        registered=True,
        cleanup_policy=manifest.cleanup_policy,
        manifest_path=path,
    )


# ── Best-effort identity helpers (Linux-first, degrade elsewhere) ─────────


def _safe(fn, default=None):
    """Call ``fn`` returning ``default`` on any OSError/AttributeError."""
    try:
        return fn()
    except (OSError, AttributeError):
        return default


def _boot_id() -> Optional[str]:
    """Linux boot identity from ``/proc/sys/kernel/random/boot_id`` (else None).

    The boot id lets a later cleanup pass detect a recycled PID across a
    reboot: a manifest whose ``boot_id`` differs from the live system can never
    be matched to a live process.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _pid_start_time(pid: int) -> Optional[str]:
    """Process start time (field 22 of ``/proc/<pid>/stat``) as a string.

    Linux-only and best-effort. Combined with the PID this distinguishes the
    original process from a later one that reused the same PID number.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The comm field (field 2) is parenthesized and may contain spaces/')';
    # split on the LAST ')' so field offsets after it are stable.
    rparen = stat.rfind(")")
    if rparen == -1:
        return None
    rest = stat[rparen + 1 :].split()
    # rest[0] is field 3 (state); field 22 (starttime) is rest[19].
    if len(rest) < 20:
        return None
    return rest[19] or None


__all__: list[str] = []
