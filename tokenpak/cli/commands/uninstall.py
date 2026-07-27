# SPDX-License-Identifier: Apache-2.0
"""tokenpak.cli.commands.uninstall — ``tokenpak uninstall --soft / --hard``.

Unifies the fragmented teardown primitives into one truthful,
reversible-by-default command.

  --soft  un-route only (reversible via ``tokenpak setup``): restores Claude
          Code settings, tears down the Codex companion install, stops a
          running proxy. Leaves config/state/dbs and the package intact.
  --hard  soft + purge the resolved TokenPak home (EXCEPT user data:
          companion/journal.db, companion/budget.db, companion/capsules/) +
          offer ``pip uninstall tokenpak`` as the final step.

Design contract (so dry-run output exactly mirrors the real run): every
side-effect is expressed as an ``Op`` in a single ordered plan. ``--dry-run``
prints the plan and touches nothing; a real run executes the same plan in the
same order and records the actual outcome of each step. The receipt reports
only operations that actually occurred — no fabricated "removed" lines.

Uses the standard library only (no new third-party dependencies).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional

# ANSI markers, gated on a TTY so piped/JSON output stays clean.
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
# Muted text uses a gray FOREGROUND, not the faint/dim SGR (\033[2m), which is a
# barred text-effect (CLI styling standard: foreground + bold only).
_DIM = "\033[90m"
_RESET = "\033[0m"


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if _use_color() else text


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------

# Outcome tags used in the receipt. "done" = the operation changed state,
# "noop" = nothing to do (already clean), "fail" = attempted and errored,
# "skip" = deliberately not run (e.g. user data retained).
_OUTCOME_DONE = "done"
_OUTCOME_NOOP = "noop"
_OUTCOME_FAIL = "fail"
_OUTCOME_SKIP = "skip"


@dataclass
class Op:
    """A single teardown operation.

    ``describe`` is the line shown in --dry-run (and the plan header of a real
    run). ``run`` performs the side-effect and returns (outcome, detail). It is
    never called in dry-run mode, so dry-run is structurally side-effect-free.
    """

    describe: str
    run: Callable[[], "tuple[str, str]"]
    # Cosmetic grouping label for the receipt ("soft" / "hard" / "package").
    phase: str = "soft"


@dataclass
class Receipt:
    soft: bool
    hard: bool
    dry_run: bool
    keep_data: bool
    home: str
    lines: list[dict[str, Any]] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    errors: int = 0
    purge: bool = False
    backup_path: Optional[str] = None

    @property
    def mode(self) -> str:
        if self.purge:
            return "purge"
        return "hard" if self.hard else "soft"

    def record(self, op: Op, outcome: str, detail: str) -> None:
        self.lines.append(
            {"phase": op.phase, "op": op.describe, "outcome": outcome, "detail": detail}
        )
        if outcome == _OUTCOME_FAIL:
            self.errors += 1

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "keep_data": self.keep_data,
            "home": self.home,
            "operations": self.lines,
            "retained": self.retained,
            "errors": self.errors,
            "backup_path": self.backup_path,
        }


# ---------------------------------------------------------------------------
# Low-level guarded primitives — each returns (outcome, detail)
# ---------------------------------------------------------------------------


def _unroute_settings() -> "tuple[str, str]":
    """Restore Claude Code settings.json (strip tokenpak routing keys).

    Prefers restoring the ``.json.bak`` written at install time; falls back to
    stripping the keys directly so it is idempotent even with no backup.
    Strips ``env.ANTHROPIC_BASE_URL`` / ``env.OPENAI_BASE_URL`` /
    ``env.TOKENPAK_PROFILE``.
    """
    from . import install as _install

    settings_path = _install._settings_path()
    bak = settings_path.with_suffix(".json.bak")

    if bak.exists():
        try:
            # restore_backup copies the backup over the live settings.
            _install.restore_backup(bak)
        except Exception as exc:  # pragma: no cover - defensive
            return _OUTCOME_FAIL, f"restore from backup failed: {exc}"
        # The backup itself may pre-date tokenpak (clean) or have been taken
        # after routing was written; strip the keys to guarantee un-routing.
        stripped = _strip_routing_keys(settings_path)
        bak_note = "restored backup"
        if stripped:
            return _OUTCOME_DONE, f"{bak_note} + stripped routing keys ({settings_path})"
        return _OUTCOME_DONE, f"{bak_note} ({settings_path})"

    # No backup: strip directly.
    if not settings_path.exists():
        return _OUTCOME_NOOP, f"no Claude Code settings to un-route ({settings_path})"
    stripped = _strip_routing_keys(settings_path)
    if stripped:
        return _OUTCOME_DONE, f"stripped routing keys ({settings_path})"
    return _OUTCOME_NOOP, f"already un-routed ({settings_path})"


_ROUTING_KEYS = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "TOKENPAK_PROFILE")


def _strip_routing_keys(settings_path: Path) -> bool:
    """Remove tokenpak routing keys from settings.json env. Returns True if any
    key was removed. Idempotent; tolerates absent / unreadable files."""
    from . import install as _install

    if not settings_path.exists():
        return False
    try:
        data = _install._read_settings()
    except Exception:
        return False
    env = data.get("env")
    if not isinstance(env, dict):
        return False
    removed = False
    for key in _ROUTING_KEYS:
        if key in env:
            del env[key]
            removed = True
    if removed:
        _install._atomic_write_settings(data)
    return removed


def _is_routed() -> bool:
    """True if Claude Code settings currently carry a tokenpak routing key.

    Used only to describe the planned action honestly in the receipt header.
    """
    from . import install as _install

    try:
        env = _install._read_settings().get("env", {})
    except Exception:
        return False
    return isinstance(env, dict) and any(k in env for k in _ROUTING_KEYS)


def _teardown_codex() -> "tuple[str, str]":
    """Reverse the Codex companion install via its own orchestrator.

    The orchestrator already retains user data (journal.db/budget.db) and is
    idempotent. It prints its own report; we capture the return code.
    """
    try:
        from ...companion.codex.uninstall import _run_global as codex_uninstall
    except Exception as exc:
        return _OUTCOME_NOOP, f"codex companion not present ({exc.__class__.__name__})"
    try:
        rc = codex_uninstall()
    except Exception as exc:
        return _OUTCOME_FAIL, f"codex teardown error: {exc}"
    if rc == 0:
        return _OUTCOME_DONE, "codex companion install reversed (user data retained)"
    return _OUTCOME_FAIL, f"codex teardown reported {rc} error(s)"


def _proxy_pid_paths() -> list[Path]:
    """Candidate proxy pid files: resolved home first, then legacy."""
    from ... import _paths

    seen: list[Path] = []
    for base in (_paths.home(), Path.home() / ".tokenpak"):
        p = base / "proxy.pid"
        if p not in seen:
            seen.append(p)
    return seen


def _stop_proxy() -> "tuple[str, str]":
    """Stop a running proxy via its pid file. Idempotent: no pid / dead pid is
    a clean no-op, mirroring cmd_stop's termination logic."""
    for pid_path in _proxy_pid_paths():
        if not pid_path.exists():
            continue
        try:
            pid = int(pid_path.read_text().strip())
        except Exception:
            # Unparsable pid file: remove it, treat as cleanup.
            pid_path.unlink(missing_ok=True)
            return _OUTCOME_DONE, f"removed unreadable pid file ({pid_path})"
        try:
            os.kill(pid, signal.SIGTERM)
            pid_path.unlink(missing_ok=True)
            return _OUTCOME_DONE, f"stopped proxy PID {pid} ({pid_path})"
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
            return _OUTCOME_DONE, f"removed stale pid file, proxy not running ({pid_path})"
        except Exception as exc:
            return _OUTCOME_FAIL, f"could not stop proxy PID {pid}: {exc}"
    return _OUTCOME_NOOP, "no running proxy"


def _remove_path(target: Path) -> "tuple[str, str]":
    """Delete a file or directory tree. Idempotent; reports permission errors."""
    if not target.exists() and not target.is_symlink():
        return _OUTCOME_NOOP, f"absent ({target})"
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    except Exception as exc:
        return _OUTCOME_FAIL, f"could not remove {target}: {exc}"
    return _OUTCOME_DONE, f"removed {target}"


# ---------------------------------------------------------------------------
# Purge target enumeration (--hard)
# ---------------------------------------------------------------------------

# Top-level entries deleted under --hard. Anything not listed (notably the
# companion/ subtree) is handled specially so user data survives.
_HARD_PURGE_NAMES = (
    "config.json",
    "config.yaml",
    "license.json",
    "index.json",
    "debug.log",
    "telemetry.db",
    "monitor.db",
    "requests.jsonl",
    "pinned_blocks.json",
    "update_check.json",
    "update_check.lock",
    "fleet.yaml",
    "cache",
    "dispatch",
    "pro",
    "templates",
    "paks",
    "cards",
)

# User data inside companion/ that is NEVER deleted by default (AC-S3).
_COMPANION_PROTECTED = ("journal.db", "budget.db", "capsules")


# ---------------------------------------------------------------------------
# Complete removal (--purge)
# ---------------------------------------------------------------------------
#
# --purge is deliberately a THIRD mode rather than a redefinition of --hard.
# Someone with `tokenpak uninstall --hard --yes` in a script relies on their
# journal and budget history surviving; changing that silently would destroy
# data on upgrade. --soft and --hard keep their exact meanings.

#: Data that survives --hard but is destroyed by --purge. Named in the warning
#: so nobody loses any of it by surprise. (relative path, human description)
_PURGE_NAMED_DATA: "tuple[tuple[str, str], ...]" = (
    ("companion/journal.db", "session journal"),
    ("companion/budget.db", "spend and budget history"),
    ("companion/capsules", "saved memory capsules"),
    ("monitor.db", "request ledger"),
    ("telemetry.db", "telemetry store"),
    ("license.json", "license store"),
)

#: Archive members that cannot be meaningfully restored — live sockets, pid
#: files and lock files. Excluded so the backup stays portable.
_BACKUP_SKIP_SUFFIXES = (".sock", ".pid", ".lock")


def _client_config_backup() -> Path:
    """The snapshot taken of the user's client config before we edited it."""
    from . import install as _install

    return _install._settings_path().with_suffix(".json.bak")


def _external_state_paths(include_client_backup: bool = True) -> "list[Path]":
    """TokenPak state written OUTSIDE the resolved home.

    These survive --hard today, which is why an uninstalled machine still
    carries a systemd unit and a permissions file. --purge removes them.

    ``include_client_backup`` covers the snapshot of the user's own client
    config. It is always archived, but it may only be *deleted* when its
    contents have been captured somewhere else: the removal contract keeps
    consciously-retained backups after a purge, and it is the user's last way
    back to their pre-install client settings.
    """
    from . import install as _install

    candidates = [
        Path.home() / ".config" / "tokenpak",  # permissions.toml
        Path.home() / ".config" / "tokenpak.env",  # env overrides
        _install._systemd_unit_path(),  # user service unit
    ]
    if include_client_backup:
        candidates.append(_client_config_backup())
    return [p for p in candidates if p.exists() or p.is_symlink()]


def _purge_data_inventory(home: Path) -> "list[str]":
    """Human-readable list of the named user data --purge will destroy."""
    found = []
    for rel, label in _PURGE_NAMED_DATA:
        if (home / rel).exists():
            found.append(f"{home / rel}  ({label})")
    return found


def _default_backup_path(now: Optional[Any] = None) -> Path:
    """Backup destination — deliberately outside every delete target."""
    import datetime as _dt

    stamp = (now or _dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    return Path.home() / f"tokenpak-backup-{stamp}.tar.gz"


def _create_backup(home: Path, dest: Path, extras: "list[Path]") -> "tuple[bool, str]":
    """Write a gzipped tar of everything --purge is about to delete.

    Verified before it is trusted: the archive must exist, be non-empty and
    list at least one member. A failed or empty backup aborts the uninstall
    rather than letting it proceed unprotected.
    """
    import tarfile

    def _filter(ti: "tarfile.TarInfo") -> "Optional[tarfile.TarInfo]":
        if Path(ti.name).name.endswith(_BACKUP_SKIP_SUFFIXES):
            return None
        parts = Path(ti.name).parts
        if "run" in parts or "tunnels" in parts:
            return None
        return ti

    def _arcname(p: Path) -> str:
        try:
            return f"external/{p.relative_to(Path.home())}"
        except ValueError:
            return f"external/{p.name}"

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dest, "w:gz") as tar:
            if home.exists():
                tar.add(home, arcname=home.name, filter=_filter)
            for p in extras:
                tar.add(p, arcname=_arcname(p), filter=_filter)
    except Exception as exc:
        try:
            dest.unlink(missing_ok=True)
        except Exception:  # pragma: no cover - defensive
            pass
        return False, f"backup failed: {exc}"

    try:
        if not dest.exists() or dest.stat().st_size == 0:
            return False, "backup archive is missing or empty"
        with tarfile.open(dest, "r:gz") as tar:
            if not tar.getnames():
                return False, "backup archive contains no entries"
    except Exception as exc:
        return False, f"backup verification failed: {exc}"

    return True, str(dest)


def _enumerate_hard_targets(home: Path, keep_data: bool) -> "tuple[list[Path], list[Path]]":
    """Return (to_delete, retained) absolute paths under *home* for --hard.

    Pure / read-only: it inspects the filesystem to list real targets but
    deletes nothing. The same enumeration drives both --dry-run and the real
    run, guaranteeing parity (AC-S2).

    --keep-data widens the retain set to the whole home (config + dbs kept);
    only the routing/companion teardown from --soft has run by then.
    """
    if keep_data:
        return [], [home]

    to_delete: list[Path] = []
    retained: list[Path] = []

    # Top-level files/dirs.
    for name in _HARD_PURGE_NAMES:
        p = home / name
        if p.exists() or p.is_symlink():
            to_delete.append(p)

    # Any other top-level *.db (monitor/telemetry variants we did not name).
    for child in sorted(home.glob("*.db")):
        if child not in to_delete:
            to_delete.append(child)

    # Companion subtree: delete everything EXCEPT protected user data.
    companion = home / "companion"
    if companion.exists():
        protected = {companion / n for n in _COMPANION_PROTECTED}
        retained.extend(sorted(p for p in protected if p.exists()))
        for child in sorted(companion.iterdir()):
            if child in protected:
                continue
            to_delete.append(child)

    return to_delete, retained


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def _build_plan(
    *,
    hard: bool,
    keep_data: bool,
    home: Path,
    purge: bool = False,
    archived: bool = True,
) -> "tuple[list[Op], list[Path]]":
    """Assemble the ordered operation plan and the retained-path list.

    ``archived`` says whether a verified backup archive was written. Without
    one, the snapshot of the user's client config is kept rather than deleted
    — it is their only way back to their pre-install settings.
    """
    ops: list[Op] = []

    routed_note = "active → un-route" if _is_routed() else "not routed (idempotent)"
    ops.append(
        Op(
            describe=f"Restore Claude Code settings.json [{routed_note}]",
            run=_unroute_settings,
            phase="soft",
        )
    )
    ops.append(
        Op(
            describe=(
                "Reverse Codex companion install"
                if purge  # the whole home goes next, so promising retention would lie
                else "Reverse Codex companion install (retains journal/budget/capsules)"
            ),
            run=_teardown_codex,
            phase="soft",
        )
    )
    ops.append(Op(describe="Stop running proxy (if any)", run=_stop_proxy, phase="soft"))

    retained: list[Path] = []
    if purge:
        # Nothing is retained. Remove the home wholesale — that covers the
        # protected companion data, the first-run sentinel, and anything a
        # future release writes without updating the named purge list — then
        # the state we scattered outside it.
        if home.exists():
            ops.append(
                Op(
                    describe=f"Delete {home} and everything in it",
                    run=partial(_remove_path, home),
                    phase="purge",
                )
            )
        for target in _external_state_paths(include_client_backup=archived):
            ops.append(
                Op(
                    describe=f"Delete {target}",
                    run=partial(_remove_path, target),
                    phase="purge",
                )
            )
        if not archived:
            kept = _client_config_backup()
            if kept.exists():
                retained.append(kept)
                ops.append(
                    Op(
                        describe=f"Keep {kept} (no archive was written — your way back)",
                        run=lambda: (_OUTCOME_SKIP, "retained: last route back to pre-install config"),
                        phase="purge",
                    )
                )
        return ops, retained

    if hard:
        to_delete, retained = _enumerate_hard_targets(home, keep_data)
        if keep_data:
            ops.append(
                Op(
                    describe=f"--keep-data: retain ALL user data under {home}",
                    run=lambda: (_OUTCOME_SKIP, f"retained {home}"),
                    phase="hard",
                )
            )
        else:
            for target in to_delete:
                ops.append(
                    Op(
                        describe=f"Delete {target}",
                        run=partial(_remove_path, target),
                        phase="hard",
                    )
                )
            if not to_delete:
                ops.append(
                    Op(
                        describe=f"Purge {home} (already clean — nothing to delete)",
                        run=lambda: (_OUTCOME_NOOP, f"{home} already clean"),
                        phase="hard",
                    )
                )

    return ops, retained


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def _confirm_hard(ops: list[Op], retained: list[Path], run_pip: bool) -> bool:
    """Show every path that will be deleted + the pip step, require explicit y."""
    print(_c("⚠️  HARD uninstall — this will permanently delete:", _YELLOW))
    deletes = [op.describe for op in ops if op.phase == "hard"]
    if deletes:
        for line in deletes:
            print(f"    {line}")
    else:
        print("    (no state files found to delete)")
    if retained:
        print(_c("  Retained (user data):", _DIM))
        for r in retained:
            print(f"    {r}")
    if run_pip:
        print(_c("  Then: pip uninstall tokenpak", _YELLOW))
    try:
        answer = input("Proceed? Type 'y' to confirm: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "y"


#: Complete removal is irreversible, so a bare "y" is too easy to hit by
#: accident. The user types the word out.
_PURGE_CONFIRM_WORD = "DELETE"


def _confirm_backup() -> bool:
    """Ask whether to write a backup first. Defaults to yes.

    Both EOF and Ctrl-C answer *yes*: every non-answer here has to fail
    toward keeping the user's data, never toward destroying it.
    """
    try:
        answer = input("\nGenerate a backup archive first? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return True
    return answer not in ("n", "no")


def _confirm_purge(ops: "list[Op]", home: Path, backup_note: str) -> bool:
    """Severe-tier confirmation: itemize the loss, then require the word."""
    print(_c("⚠️  COMPLETE uninstall — this CANNOT be undone.", _RED))
    print()
    print(_c("  These are removed permanently:", _YELLOW))
    for op in ops:
        if op.phase == "purge":
            print(f"    {op.describe}")

    inventory = _purge_data_inventory(home)
    if inventory:
        print()
        print(_c("  Including data that a --hard uninstall would have kept:", _YELLOW))
        for line in inventory:
            print(f"    {line}")

    print()
    print(_c("  The tokenpak package is removed as the final step.", _YELLOW))
    print(f"  {backup_note}")
    print()
    try:
        answer = input(f"Type {_PURGE_CONFIRM_WORD} to confirm, or anything else to cancel: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip() == _PURGE_CONFIRM_WORD


# ---------------------------------------------------------------------------
# Package removal (LAST — self-uninstall caveat)
# ---------------------------------------------------------------------------


def _pip_uninstall_cmd() -> list[str]:
    return [sys.executable, "-m", "pip", "uninstall", "-y", "tokenpak"]


def _run_pip_uninstall(receipt: Receipt) -> None:
    """Run pip uninstall as the final action. We do NOT import tokenpak modules
    afterward (the package is being removed out from under us)."""
    cmd = _pip_uninstall_cmd()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as exc:
        receipt.lines.append(
            {
                "phase": "package",
                "op": "pip uninstall tokenpak",
                "outcome": _OUTCOME_FAIL,
                "detail": f"could not invoke pip: {exc}",
            }
        )
        receipt.errors += 1
        return
    if proc.returncode == 0:
        receipt.lines.append(
            {
                "phase": "package",
                "op": "pip uninstall tokenpak",
                "outcome": _OUTCOME_DONE,
                "detail": "package removed",
            }
        )
    else:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
        receipt.lines.append(
            {
                "phase": "package",
                "op": "pip uninstall tokenpak",
                "outcome": _OUTCOME_FAIL,
                "detail": f"pip exited {proc.returncode}: {tail[0]}",
            }
        )
        receipt.errors += 1


# ---------------------------------------------------------------------------
# Receipt printing
# ---------------------------------------------------------------------------

_GLYPH = {
    _OUTCOME_DONE: ("✅", _GREEN),
    _OUTCOME_NOOP: ("—", _DIM),
    _OUTCOME_SKIP: ("•", _DIM),
    _OUTCOME_FAIL: ("❌", _RED),
}


def _print_receipt(receipt: Receipt) -> None:
    header = f"tokenpak uninstall --{receipt.mode}"
    if receipt.dry_run:
        header += "  (dry-run — nothing changed)"
    if receipt.purge:
        colour = _RED
    elif receipt.hard:
        colour = _YELLOW
    else:
        colour = _GREEN
    print(_c(header, colour))
    print()
    for line in receipt.lines:
        glyph, code = _GLYPH.get(line["outcome"], ("?", _DIM))
        label = "WOULD: " if receipt.dry_run else ""
        print(f"  {_c(glyph, code)}  {label}{line['op']}")
        if line.get("detail"):
            print(f"       {_c(line['detail'], _DIM)}")
    print()
    if receipt.retained:
        print(_c("Retained (user data — never deleted by default):", _DIM))
        for r in receipt.retained:
            print(f"  • {r}")
        print()
    if receipt.backup_path:
        print(_c(f"Backup written: {receipt.backup_path}", _GREEN))
        print(_c(f"  Restore with:  tar -xzf {receipt.backup_path} -C ~", _DIM))
        print()
    if not receipt.hard and not receipt.purge:
        print(_c("Re-route any time with: tokenpak setup", _GREEN))
    if receipt.errors:
        print(
            _c(f"{receipt.errors} operation(s) failed — see ❌ lines above.", _RED),
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _choose_mode() -> "Optional[str]":
    """Interactive chooser for a bare ``tokenpak uninstall``.

    Returns "soft", "purge", or None when the user cancels. EOF and Ctrl-C
    cancel — a non-answer must never select a destructive option.
    """
    print("How much do you want to remove?")
    print()
    print(f"  {_c('[1] Soft', _GREEN)} (recommended) — stop routing your clients through tokenpak.")
    print("      Your configuration, history and saved data are all kept.")
    print("      Reversible at any time with `tokenpak setup`.")
    print()
    print(f"  {_c('[2] Complete', _RED)} — remove everything: configuration, history,")
    print("      session journal, budget data, capsules, integrations, and the")
    print(f"      package itself. {_c('This cannot be undone.', _RED)}")
    print("      You will be offered a backup archive first.")
    print()
    print("  [q] Cancel")
    try:
        choice = input("\nChoose [1/2/q] (default 1): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if choice in ("", "1", "s", "soft"):
        return "soft"
    if choice in ("2", "c", "complete", "purge"):
        return "purge"
    return None


def run_uninstall(
    soft: bool = False,
    hard: bool = False,
    dry_run: bool = False,
    yes: bool = False,
    keep_data: bool = False,
    output_json: bool = False,
    purge: bool = False,
    backup: "Optional[str]" = None,
    no_backup: bool = False,
) -> int:
    """Run the uninstall flow. Returns a process exit code.

    Exit codes:
      0  success (incl. dry-run, incl. clean no-op)
      1  one or more operations failed
      2  ambiguous/unsafe invocation refused (no destructive default; non-TTY
         --hard/--purge without --yes; backup not resolved; backup failed)
    """
    from ... import _paths

    home = _paths.home()

    # ── Mode resolution (never guess a destructive default; AC-S1) ──────────
    if sum((bool(soft), bool(hard), bool(purge))) > 1:
        _emit_error("specify only one of --soft / --hard / --purge", output_json)
        return 2
    if backup is not None and no_backup:
        _emit_error("specify only one of --backup / --no-backup", output_json)
        return 2
    if (backup is not None or no_backup) and not purge:
        _emit_error("--backup / --no-backup apply to --purge only", output_json)
        return 2

    if not soft and not hard and not purge:
        interactive = sys.stdin.isatty() and sys.stdout.isatty() and not output_json
        if not interactive:
            _emit_error(
                "specify --soft (un-route, reversible), --hard (purge state), "
                "or --purge (complete, irreversible)",
                output_json,
            )
            return 2
        chosen = _choose_mode()
        if chosen is None:
            _emit_error("aborted — nothing was changed", output_json)
            return 2
        if chosen == "purge":
            purge = True
        else:
            soft = True

    # ── Destructive safety gate (AC-S1) ─────────────────────────────────────
    # --purge always removes the package; --hard still asks separately.
    run_pip = hard or purge
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if (hard or purge) and not dry_run and not yes and not tty:
        _emit_error(
            f"--{'purge' if purge else 'hard'} requires --yes in non-interactive "
            "use (refusing to delete)",
            output_json,
        )
        return 2

    # ── Resolve the backup decision BEFORE anything is deleted ──────────────
    # Scripts must state their intent: inheriting a default here would let
    # automation destroy user data silently.
    want_backup = False
    backup_dest: Optional[Path] = None
    if purge and not dry_run:
        if no_backup:
            want_backup = False
        elif backup is not None:
            want_backup = True
            backup_dest = Path(backup).expanduser() if backup else _default_backup_path()
        elif tty and not output_json:
            want_backup = _confirm_backup()
            backup_dest = _default_backup_path() if want_backup else None
        else:
            _emit_error(
                "--purge in non-interactive use requires an explicit "
                "--backup <path> or --no-backup",
                output_json,
            )
            return 2

    # ── Build the single ordered plan (drives dry-run AND real run) ─────────
    # In a dry run no backup is taken, so model the intent the flags express:
    # the offered default is yes, and only --no-backup opts out.
    archived = (not no_backup) if dry_run else want_backup
    ops, retained_paths = _build_plan(
        hard=hard, keep_data=keep_data, home=home, purge=purge, archived=archived
    )
    retained = [str(p) for p in retained_paths]

    receipt = Receipt(
        soft=soft,
        hard=hard,
        dry_run=dry_run,
        keep_data=keep_data,
        home=str(home),
        retained=retained,
        purge=purge,
    )

    # ── Dry-run: describe the exact plan, touch nothing ─────────────────────
    if dry_run:
        for op in ops:
            # Detail is empty: the op line already carries the full description,
            # so we avoid printing it twice in the receipt.
            receipt.record(op, _OUTCOME_SKIP, "")
        if run_pip:
            receipt.lines.append(
                {
                    "phase": "package",
                    "op": "pip uninstall tokenpak",
                    "outcome": _OUTCOME_SKIP,
                    "detail": (
                        "would run LAST; self-uninstall — no tokenpak imports "
                        "afterward (detached shell on Windows)"
                    ),
                }
            )
        return _finish(receipt, output_json, dry=True)

    # ── Confirmation for destructive modes ──────────────────────────────────
    if purge and not yes:
        note = (
            f"A backup will be written to {backup_dest} first."
            if want_backup
            else _c("No backup will be taken.", _RED)
        )
        if not _confirm_purge(ops, home, note):
            _emit_error("aborted — nothing was changed", output_json)
            return 2
    elif hard and not yes:
        # Interactive (already verified TTY above).
        if not _confirm_hard(ops, retained_paths, run_pip):
            _emit_error("aborted — nothing was changed", output_json)
            return 2

    # ── Backup BEFORE the first deletion; a failure aborts the uninstall ────
    if want_backup and backup_dest is not None:
        print("Creating backup…")
        ok, detail = _create_backup(home, backup_dest, _external_state_paths())
        receipt.lines.append(
            {
                "phase": "backup",
                "op": f"Write backup archive to {backup_dest}",
                "outcome": _OUTCOME_DONE if ok else _OUTCOME_FAIL,
                "detail": detail,
            }
        )
        if not ok:
            receipt.errors += 1
            _emit_error(
                f"{detail} — nothing was deleted. Re-run with --no-backup to "
                "proceed anyway, or free up space and retry.",
                output_json,
            )
            return 2
        receipt.backup_path = detail

    # ── Execute the plan in order ───────────────────────────────────────────
    # Codex prints its own report; suppress noise under --json so output stays
    # parseable.
    for op in ops:
        if output_json and op.run is _teardown_codex:
            outcome, detail = _silenced(op.run)
        else:
            outcome, detail = op.run()
        receipt.record(op, outcome, detail)

    # ── Package removal LAST (after all home purging) ───────────────────────
    if run_pip:
        # --purge means complete: the user already typed the confirmation word,
        # so removal is not asked a second time.
        if yes or purge:
            _run_pip_uninstall(receipt)
        else:
            # Interactive secondary confirm specifically for package removal.
            try:
                ans = (
                    input("Remove the tokenpak package now (pip uninstall)? [y/N] ").strip().lower()
                )
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans == "y":
                _run_pip_uninstall(receipt)
            else:
                receipt.lines.append(
                    {
                        "phase": "package",
                        "op": "pip uninstall tokenpak",
                        "outcome": _OUTCOME_SKIP,
                        "detail": "skipped (run `pip uninstall tokenpak` to finish)",
                    }
                )

    return _finish(receipt, output_json, dry=False)


def _silenced(fn: Callable[[], "tuple[str, str]"]) -> "tuple[str, str]":
    """Run *fn* with stdout redirected to /dev/null (keeps --json clean)."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn()


def _finish(receipt: Receipt, output_json: bool, *, dry: bool) -> int:
    if output_json:
        print(json.dumps(receipt.to_json(), indent=2))
    else:
        _print_receipt(receipt)
    return 1 if receipt.errors else 0


def _emit_error(msg: str, output_json: bool) -> None:
    if output_json:
        print(json.dumps({"error": msg}), file=sys.stderr)
    else:
        print(_c(f"uninstall: {msg}", _RED), file=sys.stderr)


__all__ = ["run_uninstall"]
