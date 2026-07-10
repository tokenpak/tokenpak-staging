"""SSRM state databases — sqlite handles for fingerprint memory + audit log.

Two databases:
  - ssrm_state.db.fingerprints — per-session canonical-prompt hash memory
  - ssrm_audit.db.ssrm_decisions — append-only log of every decision

Kept separate from monitor.db so dense decision bursts don't contend with
the request-row hot path.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


_OPEN_LOCK = threading.Lock()
_HANDLES: dict[str, sqlite3.Connection] = {}


def open_state_db(path: str) -> sqlite3.Connection:
    """Open (or reopen) the fingerprint-memory database with the schema initialized.

    Returned connection has WAL mode + busy_timeout configured. Caller
    must NOT close the connection unless cleaning up the whole process —
    it's cached per-path for the lifetime of the proxy.
    """
    p = str(_expand(path))
    with _OPEN_LOCK:
        if p in _HANDLES:
            return _HANDLES[p]
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS fingerprints (
                session_id  TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                first_seen  REAL NOT NULL,
                last_seen   REAL NOT NULL,
                seen_count  INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (session_id, prompt_hash)
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS ix_fp_session_last_seen
               ON fingerprints(session_id, last_seen)"""
        )
        conn.commit()
        _HANDLES[p] = conn
        return conn


def open_audit_db(path: str) -> sqlite3.Connection:
    """Open (or reopen) the decision-audit database with schema initialized."""
    p = str(_expand(path))
    handle_key = f"audit::{p}"
    with _OPEN_LOCK:
        if handle_key in _HANDLES:
            return _HANDLES[handle_key]
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ssrm_decisions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL NOT NULL,
                session_id      TEXT,
                agent_id        TEXT,
                model           TEXT,
                decision        TEXT NOT NULL,
                effective_context_pct REAL,
                cache_read_ratio REAL,
                drift_score     REAL,
                fingerprint_repeat_count INTEGER,
                progress_signal TEXT,
                signals_json    TEXT,
                advisory_only   INTEGER NOT NULL DEFAULT 1,
                reason          TEXT
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS ix_ssrm_decisions_session_ts
               ON ssrm_decisions(session_id, ts)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS ix_ssrm_decisions_decision
               ON ssrm_decisions(decision)"""
        )
        conn.commit()
        _HANDLES[handle_key] = conn
        return conn


def reset_handles_for_testing() -> None:
    """Test-only: close cached handles so subsequent opens use new paths."""
    with _OPEN_LOCK:
        for h in _HANDLES.values():
            try:
                h.close()
            except Exception:
                pass
        _HANDLES.clear()
