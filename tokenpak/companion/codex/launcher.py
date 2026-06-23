# SPDX-License-Identifier: Apache-2.0
"""Launcher for ``tokenpak codex`` — thin bootstrap for Codex with companion.

Does setup (rate snapshot, MCP registration, hooks install, AGENTS.md,
skills) and either exec-replaces into ``codex`` (default) or exits
after install (``--install-only``).

Companion features work without the launcher if the user manually
configures MCP, hooks, and AGENTS.md — the launcher is convenience.
"""

from __future__ import annotations

import os
import random
import sys
from typing import TextIO

from ..config import CompanionConfig

_TEAL = "\033[38;2;0;180;170m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_CLEAR_LINE = "\033[2K"
_TOKENPAK_CHATGPT_BASE_URL = "http://127.0.0.1:8766/v1"

_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
_BYPASS_ENV_VAR = "TOKENPAK_CODEX_BYPASS_APPROVALS_AND_SANDBOX"
_TRUTHY = {"1", "true", "yes"}
_CODEX_LOCK_WAIT_S = 8.0
_CODEX_LOCK_POLL_S = 0.5


def _bypass_env_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True if the bypass env var is set to a truthy value (case-insensitive)."""
    src = env if env is not None else os.environ
    raw = src.get(_BYPASS_ENV_VAR, "")
    return raw.strip().lower() in _TRUTHY


def _fleet_state_enabled() -> bool:
    """True when TokenPak launcher fleet mode is enabled. Never raises.

    Fleet mode is the runtime unattended-bypass knob stored in
    TokenPak-owned state (~/.config/tokenpak/permissions.toml, set via
    `tokenpak permissions set fleet`). It is launcher-scoped only and
    never persists into ~/.codex/config.toml — the persistent trust level
    (tier) lives there and is managed by `tokenpak permissions`.
    """
    try:
        from tokenpak.cli.commands.permissions import fleet_mode_enabled

        return fleet_mode_enabled()
    except Exception:
        return False


def _maybe_inject_bypass_flag(
    args: list[str], env: dict[str, str] | None = None, fleet: bool = False
) -> list[str]:
    """Return a new arg list with the Codex bypass flag injected when opted in.

    Two opt-in surfaces, both launcher-scoped:

    - ``fleet=True`` — TokenPak launcher fleet mode (canonical path; the
      caller reads it from TokenPak-owned state via
      :func:`_fleet_state_enabled`).
    - the env var ``TOKENPAK_CODEX_BYPASS_APPROVALS_AND_SANDBOX``
      (accepts ``1`` / ``true`` / ``yes``) — the Codex-side back-compat
      alias of fleet mode, kept for automation scripts that predate the
      permission-tier system. Same effect, same banner.

    The flag is a no-op if the user already passed it on the command line
    (no duplication). Never mutates the input list.
    """
    if not (fleet or _bypass_env_enabled(env)):
        return list(args)
    if _BYPASS_FLAG in args:
        return list(args)
    return [_BYPASS_FLAG, *args]


def _fleet_banner(env: dict[str, str] | None = None, fleet: bool = False) -> str | None:
    """Mandatory stderr banner text for fleet-mode launches (None when off).

    Canonical user-visible guardrail — do not remove or soften it.
    """
    if fleet or _bypass_env_enabled(env):
        return f"tokenpak: fleet mode — bypass flags injected ({_BYPASS_FLAG})"
    return None


def main(args: list[str] | None = None) -> int:
    """Entry point for ``tokenpak codex``."""
    args = list(args if args is not None else sys.argv[1:])

    install_only = False
    if "--install-only" in args:
        install_only = True
        args = [a for a in args if a != "--install-only"]

    # Opt-in: route codex through the local TokenPak proxy via a dedicated
    # named profile. Stripped from the args before they reach codex. Default
    # (no --proxy) behaviour is byte-identical to before this flag existed.
    use_proxy = False
    if "--proxy" in args:
        use_proxy = True
        args = [a for a in args if a != "--proxy"]

    config = CompanionConfig.from_env()
    config.profile_overrides()

    config.journal_dir.mkdir(parents=True, exist_ok=True)
    progress = _LoadingStatus(sys.stderr)

    # ── Step 0a: Resolve CODEX_HOME isolation mode ───────────
    # auto = default user contract: start with the per-project workspace
    # home, then fall back to a fresh isolated home if it is busy.
    # shared/workspace/isolated remain explicit advanced/debug modes.
    from . import session_home, state_lock

    codex_home = None
    claimed_home = False
    mode = "auto"
    try:
        mode = session_home.resolve_mode()
        if mode == session_home.MODE_ATTACH:
            progress.clear()
            print(
                "tokenpak: TOKENPAK_CODEX_SESSION_MODE=attach is deferred; "
                "falling back to auto mode",
                file=sys.stderr,
            )
            mode = "auto"
        provisioned = session_home.provision_codex_home(mode)
        codex_home = provisioned.home
    except Exception as exc:  # never let isolation block the launcher
        progress.clear()
        print(
            f"tokenpak: codex home provisioning failed ({exc}); "
            "using default ~/.codex",
            file=sys.stderr,
        )
        provisioned = None

    # ── Step 0: Refresh model-rate snapshot for shell hooks ──
    from .rates_snapshot import refresh as refresh_rates

    progress.step("refreshing Codex companion rates")
    refresh_rates()

    # ── Step 1: Register MCP server ──────────────────────────
    from .mcp_config import get_env_vars, register

    progress.step("registering Codex MCP server")
    env_vars = get_env_vars(config)
    if not register(env_vars=env_vars):
        progress.clear()
        print("tokenpak: MCP registration failed (continuing)", file=sys.stderr)

    # ── Step 2: Install hooks ────────────────────────────────
    if config.hooks_enabled:
        from .hooks import ensure_hooks_feature_enabled, install_hooks

        progress.step("installing Codex hooks")
        if ensure_hooks_feature_enabled():
            install_hooks(target="global")
        else:
            progress.clear()
            print(
                "tokenpak: hooks feature could not be enabled",
                file=sys.stderr,
            )
    else:
        progress.step("skipping Codex hooks")

    # ── Step 3: Install AGENTS.md ────────────────────────────
    from .agents_md import install_agents_md

    progress.step("installing Codex AGENTS.md")
    install_agents_md(target="global")

    # ── Step 4: Install skills ───────────────────────────────
    from .skills_installer import install_skills

    progress.step("installing TokenPak skills")
    install_skills()

    # ── Step 5: Banner ───────────────────────────────────────
    proxy_url = ""
    if use_proxy and not install_only:
        progress.step("installing TokenPak proxy profile")
        try:
            _install_tokenpak_chatgpt_profile()
            proxy_url = _TOKENPAK_CHATGPT_BASE_URL
        except Exception as exc:
            progress.clear()
            print(
                f"tokenpak: --proxy profile install failed ({exc}); "
                "launching codex without proxy profile",
                file=sys.stderr,
            )

    if not install_only:
        progress.step("connecting to Codex")
        if provisioned is not None and provisioned.mode != session_home.MODE_SHARED:
            claimed_home = session_home._claim_home(provisioned.home)
        if not claimed_home and provisioned is not None and provisioned.mode != session_home.MODE_SHARED:
            if mode == "auto":
                try:
                    fallback = session_home.provision_codex_home(
                        session_home.MODE_ISOLATED
                    )
                    claimed_home = session_home._claim_home(fallback.home)
                except Exception as exc:
                    progress.clear()
                    print(
                        f"tokenpak: parallel-safe Codex session setup failed ({exc})",
                        file=sys.stderr,
                    )
                    return 1
                if not claimed_home:
                    progress.clear()
                    print(
                        "tokenpak: parallel-safe Codex session setup failed "
                        "(could not claim an isolated home)",
                        file=sys.stderr,
                    )
                    return 1
                progress.clear()
                print(
                    "tokenpak: Codex workspace is already active; starting a "
                    "fresh parallel-safe session.",
                    file=sys.stderr,
                )
                provisioned = fallback
                codex_home = fallback.home
            else:
                progress.clear()
                print(
                    "tokenpak: Codex session home is already active; close the "
                    "existing session or use the default auto mode.",
                    file=sys.stderr,
                )
                return 1
        lock = state_lock._wait_until_unlocked(
            codex_home,
            timeout_s=_CODEX_LOCK_WAIT_S,
            poll_interval_s=_CODEX_LOCK_POLL_S,
        )
        if lock.locked:
            if claimed_home and provisioned is not None:
                session_home._release_home_claim(provisioned.home)
                claimed_home = False
            if mode == "auto":
                try:
                    fallback = session_home.provision_codex_home(
                        session_home.MODE_ISOLATED
                    )
                    claimed_home = session_home._claim_home(fallback.home)
                    if not claimed_home:
                        raise RuntimeError("could not claim an isolated home")
                    fallback_lock = state_lock._wait_until_unlocked(
                        fallback.home,
                        timeout_s=_CODEX_LOCK_WAIT_S,
                        poll_interval_s=_CODEX_LOCK_POLL_S,
                    )
                except Exception as exc:
                    progress.clear()
                    print(
                        f"tokenpak: parallel-safe Codex session setup failed ({exc})",
                        file=sys.stderr,
                    )
                    return 1
                if fallback_lock.locked:
                    if claimed_home:
                        session_home._release_home_claim(fallback.home)
                        claimed_home = False
                    progress.clear()
                    print(state_lock.remediation_hint(fallback_lock), file=sys.stderr)
                    return 1
                progress.clear()
                print(
                    "tokenpak: local Codex data is busy; starting a fresh "
                    "parallel-safe session.",
                    file=sys.stderr,
                )
                provisioned = fallback
                codex_home = fallback.home
            else:
                progress.clear()
                print(state_lock.remediation_hint(lock), file=sys.stderr)
                return 1

    progress.clear()
    _print_ready_banner(config, proxy_url, sys.stderr)

    if install_only:
        print(
            "tokenpak: setup complete — run `tokenpak codex doctor` to verify",
            file=sys.stderr,
        )
        return 0

    # ── Step 6: Exec into codex ──────────────────────────────
    if config.budget_daily_usd > 0:
        os.environ["TOKENPAK_COMPANION_BUDGET"] = str(config.budget_daily_usd)

    env = os.environ.copy()
    env.update(env_vars)

    # Point the child Codex process at the provisioned home and record the
    # PID sentinel (same PID survives execvpe, so it stays accurate). Do this
    # before the bypass-flag injection so the bypass logic sees the final env.
    if provisioned is not None and provisioned.mode != session_home.MODE_SHARED:
        env = session_home.apply_to_env(provisioned.home, env)
        if not claimed_home:
            session_home.record_pid(provisioned.home)

    fleet = _fleet_state_enabled()
    forwarded = _maybe_inject_bypass_flag(args, env, fleet=fleet)
    banner = _fleet_banner(env, fleet=fleet)
    if banner:
        print(banner, file=sys.stderr)
    codex_args = ["codex", *forwarded]
    if proxy_url:
        codex_args = ["codex", "-p", "tokenpak-chatgpt", *forwarded]
    os.execvpe("codex", codex_args, env)

    print("tokenpak: failed to launch codex", file=sys.stderr)
    return 1


class _LoadingStatus:
    """Transient setup status for interactive terminals, plain lines for logs."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.interactive = bool(getattr(stream, "isatty", lambda: False)())
        self._active = False
        self._started = False

    def step(self, message: str) -> None:
        if self.interactive:
            if not self._started:
                self.stream.write("\n")
                self._started = True
            self.stream.write(f"\r{_CLEAR_LINE}{_DIM}tokenpak: {message}...{_RESET}")
            self.stream.flush()
            self._active = True
            return
        print(f"tokenpak: {message}...", file=self.stream)

    def clear(self) -> None:
        if not self.interactive or not self._active:
            return
        self.stream.write(f"\r{_CLEAR_LINE}")
        self.stream.flush()
        self._active = False


def _print_ready_banner(
    config: CompanionConfig,
    proxy_url: str,
    stream: TextIO,
) -> None:
    """Print the Codex companion banner using the Claude launcher style."""
    from tokenpak.cli.commands.status import MEME_LINES, _get_version

    mode = config.profile.capitalize()
    budget = (
        f"${config.budget_daily_usd:.2f}/day"
        if config.budget_daily_usd > 0
        else "Unlimited"
    )
    meme = random.choice(MEME_LINES)
    version = _get_version()

    print(file=stream)
    print(f"  \U0001f4e6 Token{_TEAL}Pak{_RESET} Codex Companion", file=stream)
    print(f"     {_DIM}TokenPak {version}{_RESET}", file=stream)
    print(f"     {_DIM}Ready \u2022 Mode: {mode} \u2022 Budget: {budget}{_RESET}", file=stream)
    if proxy_url:
        print(f"     {_DIM}Proxy active \u2192 {proxy_url}{_RESET}", file=stream)
    print(file=stream)
    print(f"     {_DIM}{meme}{_RESET}", file=stream)
    print(file=stream)


# Exact contents of the dedicated named profile written under --proxy. This is
# the ONLY file the launcher writes into ~/.codex, and only when --proxy is
# passed. The global ~/.codex/config.toml and ~/.codex/AGENTS.md are never
# touched.
_TOKENPAK_CHATGPT_PROFILE_TOML = f"""model_provider = "tokenpak-chatgpt"

[model_providers.tokenpak-chatgpt]
name = "TokenPak ChatGPT"
base_url = "{_TOKENPAK_CHATGPT_BASE_URL}"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
stream_idle_timeout_ms = 300000
"""


def _install_tokenpak_chatgpt_profile() -> str:
    """Write the named ``tokenpak-chatgpt`` profile file. Returns its path.

    Creates ``~/.codex`` if missing and writes
    ``~/.codex/tokenpak-chatgpt.config.toml`` with the exact provider block.
    Never edits ``~/.codex/config.toml`` or ``~/.codex/AGENTS.md``.
    """
    from pathlib import Path

    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    profile_path = codex_dir / "tokenpak-chatgpt.config.toml"
    profile_path.write_text(_TOKENPAK_CHATGPT_PROFILE_TOML, encoding="utf-8")
    return str(profile_path)
