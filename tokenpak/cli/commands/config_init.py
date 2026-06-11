# SPDX-License-Identifier: Apache-2.0
"""config init — scaffold a valid, minimal, comment-rich user config.

The config-file analogue of onboarding: gives a new user a known-good
``<tpk-home>/config.yaml`` starting point. It is NOT the credential-
acquisition flow and NOT the guided first-run TUI (``tokenpak setup``).

Design: vault ``01_PROJECTS/tokenpak/design/
centralized-env-packet-b-config-doctor-init-loadorder-design-2026-06-06.md``
(§2).

Hard non-goals (design §2.1):

* **No secrets.** Never prompts for, reads, writes, or stores an API key
  or any high/medium secret-class value. The scaffold references secrets
  only by env-var *name*. The optional ``--with-env-stub`` template is
  placeholders-only — secret-class keys are rendered commented with no
  value after ``=``. ``<tpk-home>/.env`` itself is never created.
* **No global mutation.** Writes only under ``<tpk-home>`` (user-owned
  tier, Std 36 §4.1). Never touches ``~/.claude/settings.json``, shell rc
  files, or system env — proxy attach stays ``tokenpak integrate``'s job.
* **No ``migrate-from-openclaw``** (HELD scope; Kevin-gated).

Idempotency (design §2.3): a second run is a safe no-op; overwrite needs
an explicit ``--force`` (backup-first to ``config.yaml.bak``) and, on a
TTY, a [y/N] confirmation. Non-interactive runs never prompt (Std 03 §5).

Exit codes (design §2.5): 0 scaffolded or no-op; 1 would-overwrite in
non-interactive mode without --force, or unwritable home; 4 generated
default fails its own parse guard; 2 usage (argparse).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from tokenpak import _paths

_ENV_STUB_NAME = ".env.example"

# Placeholders-only template for `--with-env-stub`. Secret-class (high /
# medium) keys are commented with NOTHING after `=` — the user copies this
# to <tpk-home>/.env (mode 0600) and fills values there. Canonical
# inventory: vault 01_PROJECTS/tokenpak/config/env-schema.md (Packet A).
_ENV_STUB_TEMPLATE = """\
# TokenPak environment template — placeholders only, never real values.
# Copy to <tpk-home>/.env and `chmod 600` it, then fill in your values:
#   cp .env.example .env && chmod 600 .env
# Secret values live ONLY in your private .env (gitignored, mode 0600).
# Full inventory + load order: docs/configuration/env-load-order.md

# ── Core runtime (secret class: low) ─────────────────────────────────
# TOKENPAK_PORT=8766
# TOKENPAK_MODE=hybrid            # strict|hybrid|aggressive
# TOKENPAK_LOG_LEVEL=info
# TOKENPAK_HOME=                  # operator override for <tpk-home>

# ── Provider keys (secret class: HIGH — fill in .env only, never here) ─
# ANTHROPIC_API_KEY=
# OPENAI_API_KEY=
# GOOGLE_API_KEY=
# GEMINI_API_KEY=

# ── Proxy auth (secret class: HIGH) ──────────────────────────────────
# TOKENPAK_API_KEY=
# TOKENPAK_PROXY_KEY=

# ── Integrations (secret class: HIGH unless noted) ───────────────────
# GITHUB_TOKEN=
# NOTION_API_TOKEN=
# TOKENPAK_TELEGRAM_BOT_TOKEN=
# TOKENPAK_TELEGRAM_CHAT_ID=      # medium — chat target, not a credential
# TOKENPAK_SLACK_WEBHOOK=

# ── Tuning (secret class: low) ───────────────────────────────────────
# TOKENPAK_COMPACT=1
# TOKENPAK_INJECT_BUDGET=4000
# TOKENPAK_VAULT_INDEX=~/vault/.tokenpak
"""


def _is_interactive() -> bool:
    """True on a real TTY unless TOKENPAK_NONINTERACTIVE=1 (Std 03 §5)."""
    if os.environ.get("TOKENPAK_NONINTERACTIVE", "").strip() in ("1", "true", "yes"):
        return False
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _confirm_overwrite(target: Path) -> bool:
    """TTY-only [y/N] confirmation, default no (Std 03 §5)."""
    try:
        answer = input(f"Overwrite existing {target}? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def run_config_init(
    force: bool = False,
    with_env_stub: bool = False,
    print_only: bool = False,
    json_output: bool = False,
    quiet: bool = False,
) -> int:
    """Scaffold ``<tpk-home>/config.yaml`` (and optionally an .env stub).

    Returns the process exit code (see module docstring).
    """
    from tokenpak.core.config_loader import generate_default_yaml

    home = _paths.home()
    target = home / "config.yaml"
    stub_target = home / _ENV_STUB_NAME

    created: list[str] = []
    skipped: list[str] = []
    backed_up: list[str] = []

    def _emit(code: int) -> int:
        if json_output:
            print(json.dumps({
                "created": created,
                "skipped": skipped,
                "backed_up": backed_up,
                "exit_code": code,
            }, indent=2))
        return code

    content = generate_default_yaml()

    # Internal config-error guard: the generated default must parse before
    # anything is written (design §2.5 exit 4).
    try:
        import yaml as _yaml

        _yaml.safe_load(content)
    except ImportError:
        pass  # no PyYAML — skip the guard rather than block scaffolding
    except Exception as exc:
        if not json_output:
            print(f"✗ generated default config failed its parse guard: {exc}",
                  file=sys.stderr)
        return _emit(4)

    def _print_plan() -> None:
        # Planned-changes block first (Std 03 §5 dry-run-first discipline).
        if json_output or quiet:
            return
        verb = "overwrite" if target.exists() else "create"
        print("Planned changes:")
        print(f"  {verb}  {target}")
        if with_env_stub:
            print(f"  create  {stub_target}  (placeholders only)")

    if print_only:
        _print_plan()
        skipped.extend([str(target)] + ([str(stub_target)] if with_env_stub else []))
        if not json_output and not quiet:
            print("(--print: nothing written)")
        return _emit(0)

    interactive = _is_interactive()

    if target.exists():
        if not force:
            skipped.append(str(target))
            if interactive:
                # Default no-op + report (design §2.3; Std 03 §3 exit 0
                # includes "no-op with nothing to report").
                if not json_output and not quiet:
                    print(f"config already present at {target}; nothing changed")
                    print("(pass --force to overwrite — backup-first)")
                return _emit(0)
            # Non-interactive would-be overwrite: bail naming the flag
            # (design §2.4 / TC-I-04).
            print(f"config already present at {target}; pass --force to overwrite",
                  file=sys.stderr)
            return _emit(1)
        # --force: backup-first (mirrors integrate's discipline), then on a
        # TTY confirm [y/N] default no. Non-interactive --force never prompts.
        _print_plan()
        if interactive and not _confirm_overwrite(target):
            skipped.append(str(target))
            if not json_output and not quiet:
                print("aborted; nothing changed")
            return _emit(0)
        backup = target.with_name(target.name + ".bak")
        try:
            shutil.copy2(target, backup)
        except OSError as exc:
            print(f"✗ could not back up existing config: {exc}", file=sys.stderr)
            return _emit(1)
        backed_up.append(str(backup))
    else:
        _print_plan()

    try:
        _paths.ensure_home()  # mode 0700, idempotent (Std 33 §3)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        print(f"✗ could not write {target}: {exc}", file=sys.stderr)
        return _emit(1)
    created.append(str(target))

    if with_env_stub:
        if stub_target.exists() and not force:
            skipped.append(str(stub_target))
        else:
            try:
                stub_target.write_text(_ENV_STUB_TEMPLATE, encoding="utf-8")
                created.append(str(stub_target))
            except OSError as exc:
                print(f"✗ could not write {stub_target}: {exc}", file=sys.stderr)
                return _emit(1)

    if not json_output and not quiet:
        for path in created:
            print(f"✔ created {path}")
        for path in backed_up:
            print(f"  (previous config backed up to {path})")
        for path in skipped:
            print(f"  skipped {path} (already present)")
        print("\nNext: `tokenpak config show` to view, "
              "`tokenpak config doctor` to diagnose.")
    return _emit(0)
