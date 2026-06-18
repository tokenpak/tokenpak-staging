# SPDX-License-Identifier: Apache-2.0
"""Register / unregister the tokenpak companion MCP server with Codex.

Uses ``codex mcp add`` / ``codex mcp remove`` so the config lives in
Codex's own config store (~/.codex/config.toml) and is visible to
``codex mcp list``.

The MCP server binary is the same stdio JSON-RPC server used by Claude Code
(``python3 -m tokenpak.companion.mcp.server``).  Only the discovery
mechanism differs.

After registration the companion also writes an explicit policy block into
the ``[mcp_servers.tokenpak-companion]`` table:

- ``startup_timeout_sec = 30`` — Python cold-start imports can exceed the
  Codex default startup timeout; 30s keeps slow first-spawns from flapping.
- ``tool_timeout_sec = 60`` — per-call ceiling for tool handlers.
- ``enabled_tools`` — explicit allowlist generated from the canonical MCP
  TOOLS registry (never hand-maintained).
- ``default_tools_approval_mode = "auto"`` — read-shaped tools run without
  a prompt.
- ``tool_approvals`` — mutating tools (``journal_write``, ``prune_context``)
  require explicit approval.

``verify_policy`` checks the resolved config; the doctor surfaces drift.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tokenpak.companion.config import CompanionConfig

SERVER_NAME = "tokenpak-companion"

# ── MCP policy (explicit timeouts / allowlist / approval modes) ──────

STARTUP_TIMEOUT_SEC = 30
TOOL_TIMEOUT_SEC = 60
DEFAULT_TOOLS_APPROVAL_MODE = "auto"
MUTATING_TOOL_APPROVAL_MODE = "prompt"
# Tools that mutate companion state (journal rows / context rewriting) and
# therefore require explicit approval rather than auto-run.
MUTATING_TOOLS: "tuple[str, ...]" = ("journal_write", "prune_context")

# Keys inside [mcp_servers.tokenpak-companion] that the companion owns and
# rewrites on every policy application.  Everything else in the table
# (command, args, env, ...) is preserved verbatim.
_POLICY_KEYS: "tuple[str, ...]" = (
    "startup_timeout_sec",
    "tool_timeout_sec",
    "enabled_tools",
    "default_tools_approval_mode",
    "tool_approvals",
)


def codex_home() -> Path:
    """Resolved Codex home directory (honors ``CODEX_HOME``).

    Single resolver for every Codex-home surface the companion touches
    (config.toml, AGENTS.md, AGENTS.override.md, doctor checks,
    uninstall) so the writer and the verifier can never disagree about
    which tree Codex actually reads.
    """
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"])
    return Path.home() / ".codex"


def codex_config_path() -> Path:
    """Resolved Codex ``config.toml`` path (honors ``CODEX_HOME``)."""
    return codex_home() / "config.toml"


def enabled_tool_names() -> "list[str]":
    """Allowlist content: every tool in the MCP registry, registry order."""
    from tokenpak.companion.mcp.tools import TOOLS  # local: keep import light

    return [tool.name for tool in TOOLS]


def expected_policy() -> "dict[str, object]":
    """The policy table the companion expects in Codex ``config.toml``."""
    return {
        "startup_timeout_sec": STARTUP_TIMEOUT_SEC,
        "tool_timeout_sec": TOOL_TIMEOUT_SEC,
        "enabled_tools": enabled_tool_names(),
        "default_tools_approval_mode": DEFAULT_TOOLS_APPROVAL_MODE,
        "tool_approvals": {
            name: MUTATING_TOOL_APPROVAL_MODE for name in MUTATING_TOOLS
        },
    }


def render_policy_lines() -> "list[str]":
    """Render the policy keys as deterministic TOML lines."""
    tools = ", ".join(f'"{name}"' for name in enabled_tool_names())
    approvals = ", ".join(
        f'{name} = "{MUTATING_TOOL_APPROVAL_MODE}"' for name in MUTATING_TOOLS
    )
    return [
        f"startup_timeout_sec = {STARTUP_TIMEOUT_SEC}",
        f"tool_timeout_sec = {TOOL_TIMEOUT_SEC}",
        f"enabled_tools = [{tools}]",
        f'default_tools_approval_mode = "{DEFAULT_TOOLS_APPROVAL_MODE}"',
        f"tool_approvals = {{ {approvals} }}",
    ]


def _loads_toml(text: str) -> "dict | None":
    """Parse TOML text; None when unparseable or no TOML reader exists."""
    try:
        import tomllib as _toml  # 3.11+
    except ModuleNotFoundError:  # 3.10
        try:
            import tomli as _toml  # type: ignore
        except ModuleNotFoundError:  # pragma: no cover - graceful degrade
            return None
    try:
        return _toml.loads(text)
    except Exception:
        return None


def apply_policy(config_path: Optional[Path] = None) -> "tuple[bool, str]":
    """Write/refresh the policy keys inside ``[mcp_servers.tokenpak-companion]``.

    Returns ``(ok, detail)``.  Refuses to create the server table itself —
    registration (``codex mcp add``) owns table creation, so a policy-only
    write can never orphan a command-less server entry.  All non-policy
    keys in the table are preserved; the rewrite is aborted (nothing
    written) if the edited text would no longer parse as TOML.
    """
    path = config_path or codex_config_path()
    if not path.exists():
        return False, f"{path} missing — register the MCP server first"

    text = path.read_text(encoding="utf-8")
    if _loads_toml(text) is None:
        return False, f"{path} is not parseable TOML — refusing to edit"

    lines = text.splitlines()
    header = f"[mcp_servers.{SERVER_NAME}]"
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == header), None
    )
    if start is None:
        return False, f"{header} not found in {path} — register the MCP server first"

    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = j
            break

    managed = tuple(f"{key} " for key in _POLICY_KEYS) + tuple(
        f"{key}=" for key in _POLICY_KEYS
    )
    kept = [
        line
        for line in lines[start + 1 : end]
        if not line.strip().startswith(managed)
    ]
    while kept and not kept[-1].strip():
        kept.pop()

    new_section = kept + render_policy_lines()
    if end < len(lines):
        new_section.append("")

    new_lines = lines[: start + 1] + new_section + lines[end:]
    new_text = "\n".join(new_lines) + "\n"

    if _loads_toml(new_text) is None:
        return False, f"policy edit would corrupt {path} — aborted, nothing written"

    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
    return True, f"policy applied to {path}"


def verify_policy(config_path: Optional[Path] = None) -> "tuple[bool, list[str]]":
    """Check the resolved config against the expected policy.

    Returns ``(ok, problems)`` where ``problems`` lists every deviation.
    Timeouts higher than the floor are accepted (user raised them); a
    missing allowlist, an allowlist that drifted from the registry, or a
    mutating tool without an approval prompt are not.
    """
    path = config_path or codex_config_path()
    if not path.exists():
        return False, [f"{path} missing"]
    data = _loads_toml(path.read_text(encoding="utf-8"))
    if data is None:
        return False, [f"{path} unparseable (or no TOML reader available)"]

    servers = data.get("mcp_servers")
    table = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    if not isinstance(table, dict):
        return False, [f"[mcp_servers.{SERVER_NAME}] table missing"]

    problems: "list[str]" = []

    startup = table.get("startup_timeout_sec")
    if not isinstance(startup, int) or isinstance(startup, bool) or startup < STARTUP_TIMEOUT_SEC:
        problems.append(
            f"startup_timeout_sec must be an int >= {STARTUP_TIMEOUT_SEC} (found {startup!r})"
        )

    tool_timeout = table.get("tool_timeout_sec")
    if not isinstance(tool_timeout, int) or isinstance(tool_timeout, bool) or tool_timeout < TOOL_TIMEOUT_SEC:
        problems.append(
            f"tool_timeout_sec must be an int >= {TOOL_TIMEOUT_SEC} (found {tool_timeout!r})"
        )

    expected_names = enabled_tool_names()
    enabled = table.get("enabled_tools")
    if not isinstance(enabled, list):
        problems.append("enabled_tools allowlist missing")
    else:
        missing = [n for n in expected_names if n not in enabled]
        unknown = [n for n in enabled if n not in expected_names]
        if missing:
            problems.append(f"enabled_tools missing registry tools: {missing}")
        if unknown:
            problems.append(f"enabled_tools lists unknown tools: {unknown}")

    if table.get("default_tools_approval_mode") != DEFAULT_TOOLS_APPROVAL_MODE:
        problems.append(
            f'default_tools_approval_mode must be "{DEFAULT_TOOLS_APPROVAL_MODE}" '
            f"(found {table.get('default_tools_approval_mode')!r})"
        )

    approvals = table.get("tool_approvals")
    if not isinstance(approvals, dict):
        problems.append("tool_approvals (mutating-tool approval modes) missing")
    else:
        for name in MUTATING_TOOLS:
            if approvals.get(name) != MUTATING_TOOL_APPROVAL_MODE:
                problems.append(
                    f'tool_approvals.{name} must be "{MUTATING_TOOL_APPROVAL_MODE}" '
                    f"(found {approvals.get(name)!r})"
                )

    return (not problems), problems


# ── Registration ─────────────────────────────────────────────────────

def is_registered() -> bool:
    """Check whether the companion MCP server is already registered."""
    try:
        result = subprocess.run(
            ["codex", "mcp", "get", SERVER_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def register(
    env_vars: Optional[dict[str, str]] = None,
) -> bool:
    """Register the companion MCP server via ``codex mcp add``.

    Returns True if registration succeeded (or was already registered).
    On success the explicit policy block (timeouts / allowlist / approval
    modes) is written into the server's ``config.toml`` table; a policy
    write failure does not undo registration but is reported to stderr
    (the doctor flags it durably).
    """
    if is_registered():
        ok, detail = apply_policy()
        if not ok:
            print(f"tokenpak: MCP policy not applied: {detail}", file=sys.stderr)
        return True

    # ``-P`` (PYTHONSAFEPATH) keeps the spawn cwd off sys.path.  Codex starts
    # MCP servers from the user's cwd, where a ``tokenpak`` symlink to a repo
    # root can shadow the installed package as a namespace package and break
    # ``from ... import __version__`` at server import time.
    cmd = [
        "codex", "mcp", "add", SERVER_NAME,
        "--",
        sys.executable, "-P", "-m", "tokenpak.companion.mcp.server",
    ]

    # Pass companion env vars to the MCP server process.
    # Codex documents `--env KEY=VALUE` (space-separated); insert before "--".
    if env_vars:
        insert_at = cmd.index("--")
        for k, v in env_vars.items():
            cmd[insert_at:insert_at] = ["--env", f"{k}={v}"]
            insert_at += 2

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            print(
                f"tokenpak: failed to register MCP server: {result.stderr.strip()}",
                file=sys.stderr,
            )
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"tokenpak: codex not available: {e}", file=sys.stderr)
        return False

    ok, detail = apply_policy()
    if not ok:
        print(
            f"tokenpak: MCP registered but policy not applied: {detail}",
            file=sys.stderr,
        )
    return True


def unregister() -> bool:
    """Remove the companion MCP server registration."""
    if not is_registered():
        return True

    try:
        result = subprocess.run(
            ["codex", "mcp", "remove", SERVER_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_env_vars(config: "CompanionConfig") -> dict[str, str]:
    """Build env vars to forward to the MCP server subprocess."""

    env: dict[str, str] = {}
    if getattr(config, "session_id", "") and getattr(config, "session_id_source", "") == "env":
        env["TOKENPAK_COMPANION_SESSION_ID"] = config.session_id
    if getattr(config, "project_dir", ""):
        env["TOKENPAK_COMPANION_PROJECT_DIR"] = config.project_dir
    if config.budget_daily_usd > 0:
        env["TOKENPAK_COMPANION_BUDGET"] = str(config.budget_daily_usd)
    if config.profile != "balanced":
        env["TOKENPAK_COMPANION_PROFILE"] = config.profile
    if str(config.journal_dir) != str(config.journal_dir.__class__.home() / ".tokenpak" / "companion"):
        env["TOKENPAK_COMPANION_JOURNAL_DIR"] = str(config.journal_dir)
    return env
