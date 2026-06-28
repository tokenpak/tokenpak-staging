#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Codex Stop hook — Python-native session closeout + cost record.

Windows-native replacement for ``hooks_stop.sh``. Reads the Codex Stop JSON
payload from stdin (``session_id``, ``transcript_path``, ``model``, ...) and:

    - writes a closeout journal entry and updates session totals,
    - records a final cost estimate into the budget tracker,
    - prints a closeout line to stderr,
    - always exits 0 (Stop never blocks).

Self-contained, stdlib-only — no ``bash``, ``jq``, ``sed``, ``sqlite3``
CLI, ``bc``, ``cut``, or ``date`` dependency. SQLite access is parameterized;
``session_id`` / ``model`` are never interpolated into SQL, and the decimal
cost is computed in Python (no shell ``bc``).
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

_DEFAULT_RATE = 3.0


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


def _rates_file() -> Path:
    raw = os.environ.get("TOKENPAK_COMPANION_RATES_FILE")
    if raw:
        return Path(raw)
    return Path.home() / ".tokenpak" / "companion" / "run" / "model_rates.tsv"


def _lookup_rate(model: str) -> float:
    """Resolve a rate from the shared TSV snapshot (see hooks_pre_send.py)."""
    if not model:
        return _DEFAULT_RATE
    rates_file = _rates_file()
    if not rates_file.exists():
        return _DEFAULT_RATE
    try:
        lines = rates_file.read_text().splitlines()
    except OSError:
        return _DEFAULT_RATE

    best_prefix = ""
    best_prefix_rate: float | None = None
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        key, raw_rate = parts[0], parts[1]
        try:
            rate = float(raw_rate)
        except ValueError:
            continue
        if key == model:
            return rate
        if model.startswith(key) and len(key) > len(best_prefix):
            best_prefix = key
            best_prefix_rate = rate
    return best_prefix_rate if best_prefix_rate is not None else _DEFAULT_RATE


def _transcript_tokens(transcript_path: str) -> int:
    if not transcript_path:
        return 0
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return 0
    return size // 4


def _write_closeout(journal_db: Path, session_id: str, model: str, content: str) -> None:
    if not journal_db.exists():
        return
    try:
        conn = _connect(journal_db)
        try:
            now = int(time.time())
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, started_at, model) "
                "VALUES (?, ?, ?)",
                (session_id, now, model or "unknown"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO entries "
                "(session_id, timestamp, entry_type, content, metadata_json) "
                "VALUES (?, ?, 'auto', ?, '{}')",
                (session_id, now, content),
            )
            conn.execute(
                "UPDATE sessions SET ended_at = ?, total_requests = ("
                "    SELECT COUNT(*) FROM entries "
                "    WHERE session_id = ? AND entry_type = 'auto'"
                ") WHERE session_id = ?",
                (now, session_id, session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _record_cost(
    budget_db: Path, session_id: str, model: str, tokens: int, cost: float
) -> None:
    if not budget_db.exists():
        return
    try:
        conn = _connect(budget_db)
        try:
            conn.execute(
                "INSERT INTO companion_costs "
                "(timestamp, date, session_id, model, input_tokens, "
                " cached_tokens, output_tokens, estimated_cost) "
                "VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
                (
                    int(time.time()),
                    date.today().isoformat(),
                    session_id,
                    model or "unknown",
                    tokens,
                    cost,
                ),
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
    if not session_id:
        return 0
    transcript_path = str(hook_input.get("transcript_path") or "")
    model = str(hook_input.get("model") or "")

    tokens = _transcript_tokens(transcript_path)
    tokens_fmt = f"{tokens:,}"

    journal_dir = _journal_dir()
    _write_closeout(
        journal_dir / "journal.db",
        session_id,
        model,
        f"session stopped (~{tokens_fmt} total tokens, model: {model or 'unknown'})",
    )

    rate = _lookup_rate(model)
    cost = round(tokens * rate / 1_000_000, 6)
    _record_cost(journal_dir / "budget.db", session_id, model, tokens, cost)

    if os.environ.get("TOKENPAK_COMPANION_SHOW_COST", "1") != "0":
        sys.stderr.write(f"tokenpak: session closeout (~{tokens_fmt} tokens)\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
