"""tokenpak.cli.commands.setup — setup wizard for tokenpak installation."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

PROXY_URL = os.environ.get("TOKENPAK_PROXY_URL", "http://127.0.0.1:8766")
OPENAI_PROXY_URL = os.environ.get("TOKENPAK_OPENAI_PROXY_URL", "http://127.0.0.1:8767")


def env_var_help(var: str, value: str = "...") -> str:
    """Render per-OS shell syntax for setting an environment variable.

    Returns a small multi-line block showing the platform-appropriate form
    first, followed by the alternates, so the guidance is correct on
    Windows (cmd / PowerShell) as well as macOS / Linux instead of being
    bash-only.
    """
    bash = f"export {var}={value}"
    powershell = f'$env:{var}="{value}"'
    cmd = f"set {var}={value}"

    if os.name == "nt":
        primary, alternates = powershell, (cmd, bash)
        labels = ("PowerShell", "cmd", "bash/zsh")
    else:
        primary, alternates = bash, (powershell, cmd)
        labels = ("bash/zsh", "PowerShell", "cmd")

    lines = [f"    {primary}    # {labels[0]}"]
    for form, label in zip(alternates, labels[1:]):
        lines.append(f"    {form}    # {label}")
    return "\n".join(lines)


def detect_claude_code() -> Optional[Path]:
    """Find the Claude Code settings directory."""
    p = Path.home() / ".claude"
    return p if p.exists() else None


def detect_openai() -> bool:
    """Detect if OpenAI API key is configured."""
    return bool(os.environ.get("OPENAI_API_KEY"))


def detect_google() -> bool:
    """Detect if Google API key is configured."""
    return bool(
        os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    )


def configure_claude_code(
    proxy_url: str = PROXY_URL,
    openai_proxy_url: str = OPENAI_PROXY_URL,
    claude_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write ANTHROPIC_BASE_URL into Claude Code settings.json."""
    base = claude_dir or detect_claude_code() or Path.home() / ".claude"
    base.mkdir(parents=True, exist_ok=True)
    settings_path = base / "settings.json"
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except Exception:
        settings = {}

    env = settings.setdefault("env", {})
    env["ANTHROPIC_BASE_URL"] = proxy_url
    if detect_openai():
        env["OPENAI_BASE_URL"] = openai_proxy_url

    with tempfile.NamedTemporaryFile("w", dir=base, delete=False, suffix=".tmp") as f:
        json.dump(settings, f, indent=2)
        tmp = f.name
    os.replace(tmp, settings_path)
    return settings


def run_setup_cmd(args) -> None:
    claude_dir = getattr(args, "claude_dir", None)
    if claude_dir:
        claude_dir = Path(claude_dir)
    configure_claude_code(claude_dir=claude_dir)


def _first_proof_help() -> str:
    """Return the no-key first-proof next-step block for the setup wizard.

    Points a freshly-configured (or still key-less) install at ``tokenpak demo``
    — a real, local compression receipt that needs no provider key, network
    call, or Pro license — then at the live ``serve``/``cost`` path once a key
    is set. Kept underscore-private (mirroring the small helper style of
    ``env_var_help``) so it stays off the public API surface.
    """
    return (
        "\n📊 See your first proof now — no API key required:\n"
        "  tokenpak demo     — run real compression on a bundled sample\n"
        "                      (prints tokens saved + est. cost/call; your\n"
        "                       savings vary with your own prompts)\n"
        "\nThen route your own traffic:\n"
        "  tokenpak serve    — start the proxy for your LLM client\n"
        "  tokenpak cost     — track your real savings\n"
    )


__all__ = [
    "PROXY_URL",
    "OPENAI_PROXY_URL",
    "configure_claude_code",
    "detect_claude_code",
    "detect_openai",
    "detect_google",
    "env_var_help",
    "run_setup_cmd",
]
