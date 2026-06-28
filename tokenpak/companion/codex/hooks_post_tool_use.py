#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Codex PostToolUse hook — Python-native token-out journal.

Windows-native replacement for ``hooks_post_tool_use.sh``. Reads the Codex
PostToolUse JSON payload from stdin (``session_id``, ``tool_name``,
``tool_use_id``, ``turn_id``, ``tool_response``, ...) and:

    - estimates output tokens from the serialized ``tool_response`` length,
    - inserts a best-effort per-tool token-out journal entry,
    - emits a structured ``additionalContext`` note (but still exits 0) when
      a response exceeds the optional hard cap.

Self-contained, stdlib-only — no ``bash``, ``jq``, ``sed``, or ``sqlite3``
CLI dependency. SQLite access is parameterized; the hard-cap note is built
with ``json.dumps``. PostToolUse never blocks — the tool result has already
happened — so it always returns success.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

__all__: list[str] = []


def _enabled() -> bool:
    return os.environ.get("TOKENPAK_COMPANION_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _journal_dir() -> Path:
    raw = os.environ.get("TOKENPAK_COMPANION_JOURNAL_DIR")
    return Path(raw) if raw else (Path.home() / ".tokenpak" / "companion")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def _response_tokens(tool_response: object) -> int:
    if isinstance(tool_response, str):
        text = tool_response
    elif tool_response is None:
        text = ""
    else:
        try:
            text = json.dumps(tool_response, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(tool_response)
    return len(text) // 4


def _journal_token_out(journal_db: Path, session_id: str, content: str) -> None:
    if not session_id or not journal_db.exists():
        return
    try:
        conn = _connect(journal_db)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO entries "
                "(session_id, timestamp, entry_type, content, metadata_json) "
                "VALUES (?, ?, 'auto', ?, '{}')",
                (session_id, int(time.time()), content),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _hardcap() -> int:
    try:
        return int(os.environ.get("TOKENPAK_COMPANION_RESPONSE_HARDCAP_TOKENS", "0"))
    except (TypeError, ValueError):
        return 0


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        return 0

    if not _enabled():
        return 0
    if not isinstance(hook_input, dict):
        return 0

    session_id = str(hook_input.get("session_id") or "")
    tool_name = str(hook_input.get("tool_name") or "")
    tool_use_id = str(hook_input.get("tool_use_id") or "")
    turn_id = str(hook_input.get("turn_id") or "")
    response_tokens = _response_tokens(hook_input.get("tool_response"))

    journal_db = _journal_dir() / "journal.db"
    _journal_token_out(
        journal_db,
        session_id,
        f"post_tool: {tool_name or 'unknown'} (~{response_tokens} tokens out, "
        f"turn={turn_id or '?'}, use_id={tool_use_id or '?'})",
    )

    hardcap = _hardcap()
    if hardcap > 0 and response_tokens > hardcap:
        msg = (
            f"tokenpak: {tool_name or 'tool'} response ~{response_tokens} tokens "
            f"exceeds hard cap {hardcap}"
        )
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": msg,
                    }
                }
            )
            + "\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
