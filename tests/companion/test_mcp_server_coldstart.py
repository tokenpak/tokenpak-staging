"""Regression: the companion MCP server entry point must import cheaply.

Background
----------
``tokenpak claude`` / ``tokenpak codex`` spawn the MCP server as
``python3 -m tokenpak.companion.mcp.server`` and the client (Claude Code /
Codex) expects an ``initialize`` response inside its MCP-connect window. If the
server's import chain (server -> tools -> ... -> vault retrieval) eagerly pulled
in the heavy optional ML stack (``sentence_transformers`` / ``transformers`` /
``torch``, a ~13-18s cold import), the first launch tripped Claude Code's
timeout and surfaced as "⚠ N setup issues: MCP" even though nothing was broken.

The durable fix makes those backends lazy: availability is detected cheaply at
import and the model is loaded only when a retrieval tool is actually invoked.
``test_vector_local_coldstart.py`` locks that contract at the
``vault.retrieval.vector_local`` boundary; by its own scope note it does **not**
import the MCP server module. This test closes that gap by asserting the
property at the *canonical server entry point itself* — the exact module the
launcher spawns and the docs (``docs/companion-mcp.md``) describe.

Each assertion runs in a fresh subprocess from a neutral working directory so
``sys.modules`` is clean and the repo-root cwd does not shadow ``tokenpak`` as a
bare namespace package. (We avoid the ``-P`` flag, which is Python 3.11+ and
would break the 3.10 CI leg, and which also drops the user-site editable
install used in local dev.)
"""
from __future__ import annotations

import subprocess
import sys
import tempfile

# Heavy, slow-to-import optional ML deps that must NOT load as a side effect of
# importing the MCP server module.
HEAVY_MODULES = ("sentence_transformers", "transformers", "torch")

_SUBPROC_TIMEOUT = 120  # generous: a regressed cold torch import is ~13s


def _run_py(code: str) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter from a neutral working directory."""
    with tempfile.TemporaryDirectory() as neutral_cwd:
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=neutral_cwd,
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )


def test_mcp_server_import_does_not_load_heavy_ml_stack():
    """Importing the MCP server entry point must stay free of the ML stack."""
    code = (
        "import sys\n"
        "import tokenpak.companion.mcp.server  # noqa: F401\n"
        f"loaded = [m for m in {HEAVY_MODULES!r} if m in sys.modules]\n"
        "print('LOADED:' + ','.join(loaded))\n"
    )
    proc = _run_py(code)
    assert proc.returncode == 0, f"MCP server import raised:\n{proc.stderr}"
    loaded_line = [
        ln for ln in proc.stdout.splitlines() if ln.startswith("LOADED:")
    ][-1]
    loaded = [m for m in loaded_line[len("LOADED:"):].split(",") if m]
    assert loaded == [], (
        "importing tokenpak.companion.mcp.server pulled in heavy ML modules "
        f"{loaded}; these must stay lazy so the server answers `initialize` "
        "inside the client's MCP-connect window (see docs/companion-mcp.md)."
    )


def test_mcp_server_exposes_canonical_tool_registry():
    """The server's tool registry must import without the heavy stack and
    expose the documented tool names — the same set the docs and the Codex
    ``enabled_tools`` allowlist are generated from."""
    code = (
        "import sys\n"
        "from tokenpak.companion.mcp.tools import TOOLS\n"
        f"loaded = [m for m in {HEAVY_MODULES!r} if m in sys.modules]\n"
        "print('LOADED:' + ','.join(loaded))\n"
        "print('TOOLS:' + ','.join(t.name for t in TOOLS))\n"
    )
    proc = _run_py(code)
    assert proc.returncode == 0, f"tools import raised:\n{proc.stderr}"
    loaded_line = [
        ln for ln in proc.stdout.splitlines() if ln.startswith("LOADED:")
    ][-1]
    loaded = [m for m in loaded_line[len("LOADED:"):].split(",") if m]
    assert loaded == [], f"importing the tool registry loaded heavy modules {loaded}"

    tools_line = [
        ln for ln in proc.stdout.splitlines() if ln.startswith("TOOLS:")
    ][-1]
    names = set(tools_line[len("TOOLS:"):].split(","))
    # The mutating tools that the docs say require approval must be present.
    assert {"journal_write", "prune_context"} <= names, names
    # A representative read-shaped tool the docs describe must be present.
    assert {"check_budget", "vault_search"} <= names, names
