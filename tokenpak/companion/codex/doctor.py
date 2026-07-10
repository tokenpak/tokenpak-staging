# SPDX-License-Identifier: Apache-2.0
"""End-to-end verification for ``tokenpak codex`` installation.

Run via ``tokenpak codex doctor``. Exits 0 if no check FAILs; WARN rows
flag risk surfaces (e.g. Codex features marked "under development") but
do not gate the exit code.  Each check is a callable returning
``(status, detail)`` where ``status`` is ``"PASS" | "FAIL" | "WARN"``.
The module stays self-contained — no cross-cutting framework — so
adding a check is "define function, append to CHECKS list".
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Literal

from ..config import CompanionConfig
from .mcp_config import SERVER_NAME
from .rates_snapshot import DEFAULT_SNAPSHOT_PATH
from .rates_snapshot import count as rates_count
from .skills_installer import (
    _DEFAULT_TARGET as SKILLS_TARGET,
)
from .skills_installer import (
    bundled_skill_names,
    orphaned_legacy_skills,
)

Status = Literal["PASS", "FAIL", "WARN"]
CheckFn = Callable[[], "tuple[Status, str]"]

# Codex default project_doc_max_bytes is 32 KiB; WARN once AGENTS.md
# climbs above 80% so users can shed content before truncation kicks in.
_AGENTS_MD_MAX_BYTES_DEFAULT = 32 * 1024
_AGENTS_MD_WARN_FRACTION = 0.80


# ── Individual checks ────────────────────────────────────────────────

def check_codex_binary() -> "tuple[Status, str]":
    path = shutil.which("codex")
    if not path:
        return "FAIL", "codex not on PATH"
    try:
        result = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return "FAIL", f"codex --version failed: {exc}"
    if result.returncode != 0:
        return "FAIL", result.stderr.strip() or "codex --version exited nonzero"
    return "PASS", result.stdout.strip() or result.stderr.strip()


def check_hooks_feature() -> "tuple[Status, str]":
    """Surface codex_hooks maturity as WARN — Codex labels it "under development".

    The companion still installs hooks (the launcher needs them), but we
    refuse to tell the user "all green" while Codex itself marks the
    feature unstable.  L5 will add the min_codex_version probe that lets
    us tighten this once Codex stabilizes the surface.
    """
    try:
        result = subprocess.run(
            ["codex", "features", "list"], capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return "FAIL", f"codex features list failed: {exc}"

    maturity = _parse_codex_hooks_maturity(result.stdout)
    if maturity is None:
        return "FAIL", "codex_hooks feature not found in `codex features list`"

    label, enabled = maturity
    if label == "under development":
        return (
            "WARN",
            f"codex_hooks={label} (enabled={enabled}) — Codex may break this; "
            "consider unpinning hooks plane if encountering failures",
        )
    # Once Codex bumps the label to experimental/beta/stable, treat
    # enabled=true as PASS, enabled=false as FAIL (companion needs it on).
    if enabled:
        return "PASS", f"codex_hooks={label} (enabled=true)"
    return "FAIL", f"codex_hooks={label} (enabled=false)"


def _parse_codex_hooks_maturity(stdout: str) -> "tuple[str, bool] | None":
    """Parse ``codex features list`` output for the codex_hooks row.

    Returns ``(maturity_label, enabled)`` or ``None`` if the row is
    absent.  Maturity labels can be multi-word ("under development"), so
    we tokenize from both ends: first column is the feature name, last
    column is the boolean, middle columns are the label.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if not parts or parts[0] != "codex_hooks":
            continue
        if len(parts) < 3:
            return None
        enabled = parts[-1].lower() == "true"
        label = " ".join(parts[1:-1])
        return label, enabled
    return None


def check_mcp_registered() -> "tuple[Status, str]":
    try:
        result = subprocess.run(
            ["codex", "mcp", "get", SERVER_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return "FAIL", f"codex mcp get failed: {exc}"
    if result.returncode != 0:
        return "FAIL", f"{SERVER_NAME} not registered"
    return "PASS", f"{SERVER_NAME} registered"


def check_hooks_json() -> "tuple[Status, str]":
    path = Path.home() / ".codex" / "hooks.json"
    if not path.exists():
        return "FAIL", f"{path} missing"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return "FAIL", f"{path} invalid JSON: {exc}"

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return "FAIL", "top-level 'hooks' is not a dict (schema mismatch)"

    missing = [e for e in ("UserPromptSubmit", "Stop") if e not in hooks]
    if missing:
        return "FAIL", f"events missing from hooks.json: {missing}"

    for event in ("UserPromptSubmit", "Stop"):
        groups = hooks.get(event, [])
        found = any(
            "tokenpak" in cmd.get("command", "")
            for group in groups
            if isinstance(group, dict)
            for cmd in group.get("hooks", [])
            if isinstance(cmd, dict)
        )
        if not found:
            return "FAIL", f"no tokenpak hook registered for {event}"

    return "PASS", "UserPromptSubmit + Stop both wired"


def check_agents_md() -> "tuple[Status, str]":
    path = Path.home() / ".codex" / "AGENTS.md"
    if not path.exists():
        return "FAIL", f"{path} missing"
    content = path.read_text()
    if "# TokenPak Companion" not in content:
        return "FAIL", "TokenPak section missing from AGENTS.md"
    return "PASS", f"{path} ({len(content)} bytes)"


def check_agents_md_size(
    max_bytes: int = _AGENTS_MD_MAX_BYTES_DEFAULT,
    warn_fraction: float = _AGENTS_MD_WARN_FRACTION,
) -> "tuple[Status, str]":
    """WARN when AGENTS.md approaches Codex's project_doc_max_bytes cap.

    Codex truncates AGENTS.md silently at the cap; users only notice
    when guidance stops landing.  Surfacing the size early lets them
    shed content before they hit it.
    """
    path = Path.home() / ".codex" / "AGENTS.md"
    if not path.exists():
        # A missing AGENTS.md is already flagged by check_agents_md;
        # don't double-fail. Treat as PASS for the size check.
        return "PASS", f"{path} missing (size check skipped)"

    size = path.stat().st_size
    threshold = int(max_bytes * warn_fraction)
    if size >= threshold:
        return (
            "WARN",
            f"AGENTS.md is {size} bytes "
            f"(≥{int(warn_fraction * 100)}% of {max_bytes}-byte Codex cap; trim it)",
        )
    return "PASS", f"AGENTS.md {size}/{max_bytes} bytes"


def check_skills_installed() -> "tuple[Status, str]":
    """Verify every bundled skill is present at the canonical user path.

    NOTE: this stats the canonical install path (``$HOME/.agents/skills``)
    rather than asking Codex itself. L5 adds a Codex-discovery probe so
    we can confirm Codex actually sees them, not just that the files
    exist on disk.
    """
    target = SKILLS_TARGET
    if not target.exists():
        return "FAIL", f"{target} missing (run `tokenpak codex install`)"
    bundled = bundled_skill_names()
    missing = [name for name in bundled if not (target / name).exists()]
    if missing:
        return "FAIL", f"missing skills at {target}: {missing}"
    return (
        "PASS",
        f"{len(bundled)} skills present at {target} "
        "(Codex-discovery verification pending L5)",
    )


def check_skills_legacy_orphans() -> "tuple[Status, str]":
    """WARN when pre-L3 installs left skills at ``~/.codex/skills``.

    Doctor flags the orphan rather than auto-migrating: a user may have
    customized a tokenpak skill in place, and a silent overwrite would
    clobber the edit.  Recommended cleanup: ``tokenpak codex uninstall
    && tokenpak codex install``.
    """
    orphans = orphaned_legacy_skills()
    if not orphans:
        return "PASS", "no legacy ~/.codex/skills orphans"
    return (
        "WARN",
        f"legacy skills at ~/.codex/skills: {orphans} — "
        "run `tokenpak codex uninstall && tokenpak codex install` to clean up",
    )


def check_databases() -> "tuple[Status, str]":
    config = CompanionConfig.from_env()
    journal = config.journal_dir / "journal.db"
    budget = config.journal_dir / "budget.db"
    missing = [p.name for p in (journal, budget) if not p.exists()]
    if missing:
        return "FAIL", f"missing dbs: {missing} (run `tokenpak codex --install-only`)"
    return "PASS", f"journal.db + budget.db in {config.journal_dir}"


def check_rates_snapshot() -> "tuple[Status, str]":
    n = rates_count()
    if n == 0:
        return "FAIL", f"{DEFAULT_SNAPSHOT_PATH} missing or empty"
    if n < 10:
        return "FAIL", f"only {n} rate entries — registry load may have failed"
    return "PASS", f"{n} model rates in snapshot"


def check_mcp_import() -> "tuple[Status, str]":
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import tokenpak.companion.mcp.server"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "FAIL", "MCP server import hung"
    if result.returncode != 0:
        return "FAIL", f"import failed: {result.stderr.strip()}"
    return "PASS", "tokenpak.companion.mcp.server importable"


def check_mcp_ping() -> "tuple[Status, str]":
    """Spawn the MCP server and send a JSON-RPC initialize. Short timeout."""
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "tokenpak-doctor", "version": "0.1.0"},
        },
    }
    payload = (json.dumps(req) + "\n").encode()

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "tokenpak.companion.mcp.server"],
            input=payload,
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        # Server keeps running after initialize — that's fine, we got a reply.
        stdout = (exc.stdout or b"").decode(errors="replace")
        return _parse_initialize_reply(stdout)

    stdout = proc.stdout.decode(errors="replace")
    return _parse_initialize_reply(stdout)


def _parse_initialize_reply(stdout: str) -> "tuple[Status, str]":
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = msg.get("result")
        if isinstance(result, dict):
            server_info = result.get("serverInfo", {})
            name = server_info.get("name", "")
            return "PASS", f"MCP initialize OK (server={name or 'unnamed'})"
    return "FAIL", "no JSON-RPC response from MCP server"


# ── Runner ──────────────────────────────────────────────────────────

CHECKS: list["tuple[str, CheckFn]"] = [
    ("codex binary", check_codex_binary),
    ("codex_hooks feature", check_hooks_feature),
    ("MCP registration", check_mcp_registered),
    ("hooks.json schema", check_hooks_json),
    ("AGENTS.md", check_agents_md),
    ("AGENTS.md size", check_agents_md_size),
    ("skills installed", check_skills_installed),
    ("skills legacy orphans", check_skills_legacy_orphans),
    ("storage dbs", check_databases),
    ("rates snapshot", check_rates_snapshot),
    ("MCP import", check_mcp_import),
    ("MCP initialize ping", check_mcp_ping),
]


def run(refresh_rates: bool = False) -> int:
    """Run all checks, print a report, return an exit code.

    Exit code is 0 unless one or more checks FAIL.  WARN rows are
    advisory and surfaced prominently but do not gate the exit code.
    """
    if refresh_rates:
        from .rates_snapshot import refresh

        path = refresh()
        print(f"refreshed rates snapshot: {path}")

    results: list["tuple[str, Status, str]"] = []
    for name, fn in CHECKS:
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = "FAIL", f"check raised: {exc.__class__.__name__}: {exc}"
        results.append((name, status, detail))

    name_width = max(len(n) for n, _, _ in results)
    any_fail = False
    warn_count = 0
    for name, status, detail in results:
        print(f"  [{status}] {name.ljust(name_width)}  {detail}")
        if status == "FAIL":
            any_fail = True
        elif status == "WARN":
            warn_count += 1

    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    total = len(results)
    parts = [f"{passed}/{total} PASS"]
    if warn_count:
        parts.append(f"{warn_count} WARN")
    if failed:
        parts.append(f"{failed} FAIL")
    summary = ", ".join(parts)
    print()
    if any_fail:
        print(f"{summary} — some checks failed")
    elif warn_count:
        print(f"{summary} — review WARN rows above")
    else:
        print(summary)
    return 0 if not any_fail else 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    refresh_rates = "--refresh-rates" in args
    return run(refresh_rates=refresh_rates)


if __name__ == "__main__":
    raise SystemExit(main())
