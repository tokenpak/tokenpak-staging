# SPDX-License-Identifier: Apache-2.0
"""Session-binding tests for the companion pre-send hook + MCP server.

The companion MCP server runs in a separate process from the ``UserPromptSubmit``
hook and never sees the hook's payload. The only bridge is a run-dir marker
(``run/current-session``): the live hook writes the session id there, and the
server binds ``state.session_id`` from it before each tool dispatch — which is
what makes ``session_info`` report a non-empty session id.

These tests drive the live bash hook (``companion/hooks/pre_send.sh``) and then
exercise the exact bind path the server uses (``current_session_id`` →
``state.session_id`` → ``_handle_session_info``). ``HOME`` is redirected to a
tmp dir so the host's real ``~/.tokenpak`` marker can never leak in.

Maps to p2-companion-budget-enforcement-and-session-id-runtime-fix-2026-06-19.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "companion" / "hooks" / "pre_send.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required to drive the live hook"
)


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    """Point HOME + the journal dir at tmp so no host marker leaks in."""
    monkeypatch.setenv("HOME", str(tmp_path))
    journal = tmp_path / "companion"
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(journal))
    monkeypatch.delenv("TOKENPAK_COMPANION_SESSION_ID", raising=False)
    monkeypatch.delenv("TOKENPAK_COMPANION_BUDGET", raising=False)  # fast path
    monkeypatch.delenv("TOKENPAK_COMPANION_ENABLED", raising=False)
    return journal


def _run_hook(input_obj: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(input_obj),
        text=True,
        capture_output=True,
        env=dict(os.environ),
        timeout=30,
    )


def _marker(journal: Path) -> Path:
    return journal / "run" / "current-session"


class TestSessionMarker:
    def test_live_hook_writes_session_marker(self, tmp_path, monkeypatch):
        """The live hook persists the session id to the run-dir marker so the
        separate-process MCP server can bind it."""
        journal = _isolate(tmp_path, monkeypatch)
        proc = _run_hook({"session_id": "sess-xyz-1", "prompt": "hi"})
        assert proc.returncode == 0, proc.stderr
        assert _marker(journal).read_text(encoding="utf-8").strip() == "sess-xyz-1"

    def test_no_session_id_writes_no_marker(self, tmp_path, monkeypatch):
        """A payload without a session id must not create a stale marker."""
        journal = _isolate(tmp_path, monkeypatch)
        proc = _run_hook({"prompt": "hi"})
        assert proc.returncode == 0, proc.stderr
        assert not _marker(journal).exists()

    def test_marker_overwritten_on_new_session(self, tmp_path, monkeypatch):
        """A new session id (e.g. after /clear) replaces the marker."""
        journal = _isolate(tmp_path, monkeypatch)
        _run_hook({"session_id": "first", "prompt": "hi"})
        _run_hook({"session_id": "second", "prompt": "hi"})
        assert _marker(journal).read_text(encoding="utf-8").strip() == "second"


class TestServerBinding:
    def test_current_session_id_reads_marker(self, tmp_path, monkeypatch):
        _isolate(tmp_path, monkeypatch)
        _run_hook({"session_id": "bind-me", "prompt": "hi"})

        from tokenpak.companion.mcp.tools import current_session_id

        assert current_session_id() == "bind-me"

    def test_session_info_reports_bound_id(self, tmp_path, monkeypatch):
        """session_info reports the id the server binds from the marker — the
        observable acceptance: session_id is non-empty for a normal session."""
        _isolate(tmp_path, monkeypatch)
        _run_hook({"session_id": "live-session-7", "prompt": "hi"})

        from tokenpak.companion.mcp import tools as tools_mod
        from tokenpak.companion.mcp.tools import (
            CompanionState,
            _handle_session_info,
            current_session_id,
        )

        # Keep the test offline: stub the proxy lookup session_info merges in.
        monkeypatch.setattr(
            tools_mod, "_proxy_get", lambda *a, **k: (0, {"detail": "offline"})
        )

        # Reproduce the server's per-dispatch bind (mcp/server.py).
        state = CompanionState()
        sid = current_session_id()
        if sid:
            state.session_id = sid

        info = json.loads(_handle_session_info(state, {}))
        assert info["session_id"] == "live-session-7"
        assert info["session_id"] != ""

    def test_session_info_empty_without_marker(self, tmp_path, monkeypatch):
        """No marker anywhere (HOME isolated) → bound id stays empty."""
        _isolate(tmp_path, monkeypatch)

        from tokenpak.companion.mcp import tools as tools_mod
        from tokenpak.companion.mcp.tools import (
            CompanionState,
            _handle_session_info,
            current_session_id,
        )

        monkeypatch.setattr(
            tools_mod, "_proxy_get", lambda *a, **k: (0, {"detail": "offline"})
        )

        state = CompanionState()
        sid = current_session_id()
        if sid:
            state.session_id = sid

        info = json.loads(_handle_session_info(state, {}))
        assert info["session_id"] == ""
