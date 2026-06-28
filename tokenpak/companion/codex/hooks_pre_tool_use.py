#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Codex PreToolUse hook — Python-native per-tool budget gate + trace stamp.

Windows-native replacement for ``hooks_pre_tool_use.sh``. Reads the Codex
PreToolUse JSON payload from stdin (``session_id``, ``tool_name``,
``tool_use_id``, ``turn_id``, ...) and:

    - stamps the tool call into the journal (best-effort, non-blocking),
    - denies the tool with the structured ``hookSpecificOutput``
      ``permissionDecision`` JSON and exit code 2 when the daily budget is
      already exhausted,
    - otherwise exits 0.

Self-contained, stdlib-only — no ``bash``, ``jq``, ``sed``, ``sqlite3``
CLI, ``bc``, or ``cut`` dependency. SQLite access is parameterized; tool
names and ids are never interpolated into SQL or shell.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import date
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


def _parse_budget(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _daily_total(budget_db: Path) -> float:
    if not budget_db.exists():
        return 0.0
    try:
        conn = _connect(budget_db)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost), 0) FROM companion_costs WHERE date = ?",
                (date.today().isoformat(),),
            ).fetchone()
        finally:
            conn.close()
        return float(row[0]) if row and row[0] is not None else 0.0
    except (sqlite3.Error, ValueError):
        return 0.0


def _trace_stamp(journal_db: Path, session_id: str, content: str) -> None:
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

    journal_dir = _journal_dir()
    journal_db = journal_dir / "journal.db"
    budget_db = journal_dir / "budget.db"

    _trace_stamp(
        journal_db,
        session_id,
        f"pre_tool: {tool_name or 'unknown'} "
        f"(turn={turn_id or '?'}, use_id={tool_use_id or '?'})",
    )

    budget = _parse_budget(os.environ.get("TOKENPAK_COMPANION_BUDGET", "0"))
    if budget > 0 and budget_db.exists():
        daily_total = _daily_total(budget_db)
        budget_micro = int(round(budget * 1_000_000))
        daily_micro = int(round(daily_total * 1_000_000))
        if budget_micro > 0 and daily_micro >= budget_micro:
            msg = (
                f"tokenpak: budget exceeded (${daily_total:.4f} / ${budget} daily) "
                f"— blocking {tool_name or 'tool'}"
            )
            sys.stderr.write(msg + "\n")
            sys.stdout.write(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": msg,
                        }
                    }
                )
                + "\n"
            )
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
