#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Codex SessionStart hook — Python-native capsule auto-load + banner.

Windows-native replacement for ``hooks_session_start.sh``. Reads the Codex
SessionStart JSON payload from stdin (``session_id``, ``transcript_path``,
``cwd``, ``hook_event_name``, ``model``, ``source``) and:

    - records the live session id under ``run/current-session`` (the bridge
      the separate-process MCP server binds to),
    - inserts a best-effort ``session started`` journal entry,
    - emits a branded banner to stderr,
    - surfaces a prior capsule for this ``cwd`` via SessionStart
      ``systemMessage`` JSON,
    - always exits 0 (SessionStart cannot block).

Self-contained, stdlib-only (``json``/``os``/``sqlite3``) — no ``bash``,
``jq``, ``sed``, ``sqlite3`` CLI, or ``date`` dependency. All SQLite writes
are parameterized; no value (``session_id``, ``cwd``, ``model``, ``source``)
is ever string-interpolated into SQL. Journal/budget writes are best-effort
and fail open so a hook problem never aborts the user's session.
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
    """Open a companion DB with the cross-platform concurrency posture.

    ``busy_timeout`` + WAL match the .sh reference and the
    :class:`JournalStore` / :class:`BudgetTracker` stores so concurrent
    hooks and the MCP server do not raise ``database is locked``.
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def _write_current_session(run_dir: Path, session_id: str) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    marker = run_dir / "current-session"
    try:
        old = marker.read_text().strip() if marker.exists() else ""
    except OSError:
        old = ""
    if old != session_id:
        try:
            marker.write_text(session_id + "\n")
        except OSError:
            pass


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        return 0  # fail-open: unparseable stdin → allow

    if not _enabled():
        return 0
    if not isinstance(hook_input, dict):
        return 0

    session_id = str(hook_input.get("session_id") or "")
    cwd = str(hook_input.get("cwd") or "")
    model = str(hook_input.get("model") or "")
    source = str(hook_input.get("source") or "startup")

    journal_dir = _journal_dir()
    journal_db = journal_dir / "journal.db"
    run_dir = journal_dir / "run"

    if session_id:
        _write_current_session(run_dir, session_id)

    if session_id and journal_db.exists():
        try:
            conn = _connect(journal_db)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO entries "
                    "(session_id, timestamp, entry_type, content, metadata_json) "
                    "VALUES (?, ?, 'auto', ?, '{}')",
                    (
                        session_id,
                        int(time.time()),
                        f"session started (source: {source}, model: {model or 'unknown'})",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            pass  # best-effort journal write fails open

    capsule_path = ""
    if cwd and journal_db.exists():
        try:
            conn = _connect(journal_db)
            try:
                row = conn.execute(
                    "SELECT capsule_path FROM sessions "
                    "WHERE project_dir = ? AND capsule_path IS NOT NULL "
                    "AND capsule_path != '' ORDER BY started_at DESC LIMIT 1",
                    (cwd,),
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                capsule_path = str(row[0])
        except sqlite3.Error:
            capsule_path = ""

    if os.environ.get("TOKENPAK_COMPANION_SHOW_BANNER", "1") != "0":
        model_tag = f" — {model}" if model else ""
        sys.stderr.write(
            f"tokenpak: session {session_id[:8]} ({source}){model_tag}\n"
        )

    if capsule_path:
        # json.dumps handles all escaping — no manual quoting, no shell.
        sys.stdout.write(
            json.dumps(
                {
                    "systemMessage": f"tokenpak: prior capsule available at {capsule_path}",
                    "continue": True,
                }
            )
            + "\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
