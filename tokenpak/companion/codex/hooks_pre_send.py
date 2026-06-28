#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Codex UserPromptSubmit hook — Python-native cost estimate + budget gate.

Windows-native replacement for ``hooks_pre_send.sh``. Reads the Codex
UserPromptSubmit JSON payload from stdin (``session_id``,
``transcript_path``, ``model``, ...) and:

    - estimates prompt-context tokens from the transcript file size,
    - resolves a per-1M-token rate from the alias-table TSV snapshot,
    - prints a cost estimate to stderr (visible in the Codex TUI),
    - blocks with the structured ``hookSpecificOutput`` decision JSON and
      exit code 2 when the daily budget would be exceeded,
    - records a best-effort journal entry.

Self-contained, stdlib-only — no ``bash``, ``jq``, ``sed``, ``sqlite3``
CLI, ``bc``, ``cut``, or ``date`` dependency. SQLite access is parameterized
and uses ``busy_timeout`` + WAL; the budget JSON block is built with
``json.dumps`` so values cannot break the output or inject SQL.
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

_DEFAULT_RATE = 3.0  # matches registry _UNKNOWN_DEFAULT for unknown models


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
    """Resolve a per-1M-token rate from the TSV snapshot.

    Exact match first, then the longest TSV key that is a prefix of the
    model name (e.g. ``gpt-4o-2026-01-01`` → ``gpt-4o``). Falls back to
    :data:`_DEFAULT_RATE`. Mirrors the awk logic in the .sh reference but
    needs no external tools.
    """
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


def _parse_budget(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _journal_prompt(journal_db: Path, session_id: str, model: str, content: str) -> None:
    if not session_id or not journal_db.exists():
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

    transcript_path = str(hook_input.get("transcript_path") or "")
    session_id = str(hook_input.get("session_id") or "")
    model = str(hook_input.get("model") or "")

    tokens = _transcript_tokens(transcript_path)
    if tokens == 0:
        return 0

    tokens_fmt = f"{tokens:,}"
    rate = _lookup_rate(model)
    cost_dollars = f"{tokens * rate / 1_000_000:.6f}"

    journal_dir = _journal_dir()
    budget_db = journal_dir / "budget.db"
    journal_db = journal_dir / "journal.db"

    budget = _parse_budget(os.environ.get("TOKENPAK_COMPANION_BUDGET", "0"))
    budget_tag = ""

    if budget > 0:
        daily_total = _daily_total(budget_db)
        budget_micro = int(round(budget * 1_000_000))
        daily_micro = int(round(daily_total * 1_000_000))
        est_micro = int(round(tokens * rate))

        if daily_micro + est_micro > budget_micro:
            msg = f"tokenpak: budget exceeded (${daily_total:.4f} / ${budget} daily)"
            sys.stderr.write(msg + "\n")
            sys.stdout.write(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "decision": "block",
                            "reason": msg,
                        }
                    }
                )
                + "\n"
            )
            return 2

        if budget_micro > 0:
            pct = daily_micro * 100 // budget_micro
            if pct > 50:
                budget_tag = f"  budget {pct}%"

    if os.environ.get("TOKENPAK_COMPANION_SHOW_COST", "1") != "0":
        model_tag = f" ({model})" if model else ""
        sys.stderr.write(
            f"tokenpak: ~{tokens_fmt} tokens  est ${cost_dollars}{model_tag}{budget_tag}\n"
        )

    _journal_prompt(
        journal_db,
        session_id,
        model,
        f"prompt submitted (~{tokens_fmt} tokens, est ${cost_dollars}, "
        f"model: {model or 'unknown'})",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
