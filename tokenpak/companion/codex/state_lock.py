# SPDX-License-Identifier: Apache-2.0
"""Detect Codex SQLite state database lock holders.

Multiple Codex processes sharing the same CODEX_HOME will contend over
``state_5.sqlite``. This module detects holders, classifies them as
stale (stopped/suspended) vs likely-active (foreground), and formats
guidance — without modifying or deleting any Codex-owned state files.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _StateLockHolder:
    """Process currently holding the Codex state database open."""

    pid: int
    ppid: int | None = None
    stat: str = "?"
    tty: str = "?"
    started: str = "?"
    command: str = "?"


def _codex_state_db_path(codex_home: Path | None = None) -> Path:
    """Return the Codex state database path for the active Codex home."""
    home = codex_home or (Path.home() / ".codex")
    return home / "state_5.sqlite"


def _state_lock_holder_pids(path: Path | None = None) -> list[int]:
    """Return PIDs holding the Codex state DB open, if the host can report them."""
    target = path or _codex_state_db_path()
    if not target.exists():
        return []

    pids = _holder_pids_from_lsof(target)
    if pids:
        return pids
    return _holder_pids_from_fuser(target)


def _state_lock_holders(path: Path | None = None) -> list[_StateLockHolder]:
    """Return process details for Codex state DB lock holders."""
    target = path or _codex_state_db_path()
    pids = _state_lock_holder_pids(target)
    if not pids:
        return []

    details = _process_details(pids)
    return [details.get(pid, _StateLockHolder(pid=pid)) for pid in pids]


def _classify_holders(
    holders: list[_StateLockHolder],
) -> tuple[list[_StateLockHolder], list[_StateLockHolder]]:
    """Split holders into (stale, active).

    Stale = process stat contains ``T`` (stopped/suspended by job control).
    Active = everything else (running, sleeping, foreground group ``+``).
    """
    stale: list[_StateLockHolder] = []
    active: list[_StateLockHolder] = []
    for h in holders:
        if "T" in h.stat:
            stale.append(h)
        else:
            active.append(h)
    return stale, active


def _format_lock_report(path: Path, holders: list[_StateLockHolder]) -> str:
    """Format a user-facing state-lock diagnostic."""
    stale, active = _classify_holders(holders)

    lines = [
        f"Codex state database is locked: {path}",
        "Lock holders:",
    ]
    for holder in holders:
        command = _shorten(holder.command)
        tag = "(stopped)" if "T" in holder.stat else "(active)"
        lines.append(
            f"  pid={holder.pid} ppid={holder.ppid or '?'} "
            f"stat={holder.stat} tty={holder.tty} {tag} cmd={command}"
        )

    lines.append("Next steps:")
    if stale:
        pids = " ".join(str(h.pid) for h in stale)
        lines.append(
            f"  {len(stale)} stopped/suspended holder(s) detected — "
            f"release with: kill {pids}"
        )
    if active:
        lines.append(
            "  Active Codex session detected — quit its TUI or use a "
            "separate CODEX_HOME for parallel sessions."
        )
    if not stale and not active:
        lines.append(f"  fuser -v {path}")
        lines.append(f"  lsof {path}")

    lines.append(
        "Parallel Codex sessions require an isolated CODEX_HOME or "
        "releasing the existing holder."
    )
    lines.append(f"Do not delete {path.name}.")
    return "\n".join(lines)


def _holder_pids_from_lsof(path: Path) -> list[int]:
    if not shutil.which("lsof"):
        return []
    try:
        result = subprocess.run(
            ["lsof", "-t", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode not in (0, 1):
        return []
    return _dedupe_ints(
        int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()
    )


def _holder_pids_from_fuser(path: Path) -> list[int]:
    if not shutil.which("fuser"):
        return []
    try:
        result = subprocess.run(
            ["fuser", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode not in (0, 1):
        return []

    text = (result.stdout or "") + " " + (result.stderr or "")
    text = text.replace(str(path), " ")
    return _dedupe_ints(int(match) for match in re.findall(r"\b\d+\b", text))


def _process_details(pids: list[int]) -> dict[int, _StateLockHolder]:
    if not pids:
        return {}
    try:
        result = subprocess.run(
            [
                "ps",
                "-o",
                "pid=",
                "-o",
                "ppid=",
                "-o",
                "stat=",
                "-o",
                "tty=",
                "-o",
                "lstart=",
                "-o",
                "args=",
                "-p",
                ",".join(str(pid) for pid in pids),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}

    holders: dict[int, _StateLockHolder] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=9)
        if len(parts) < 9:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        command = parts[9] if len(parts) > 9 else "?"
        holders[pid] = _StateLockHolder(
            pid=pid,
            ppid=ppid,
            stat=parts[2],
            tty=parts[3],
            started=" ".join(parts[4:9]),
            command=command,
        )
    return holders


def _dedupe_ints(values: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for value in values:
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _shorten(command: str, limit: int = 100) -> str:
    command = command.strip() or "?"
    if len(command) <= limit:
        return command
    return command[: limit - 3].rstrip() + "..."
