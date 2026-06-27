# SPDX-License-Identifier: Apache-2.0
"""Append-only audit log for spend guard decisions.

One row per decision point. Lives in the same SQLite DB as the pending
store (``~/.tokenpak/spend_guard.db``) so a single ``sqlite3`` query can
slice the full history.

The writer is best-effort and non-blocking: the orchestrator never waits
on the audit row, and any IO error is swallowed (logged at DEBUG).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .._local_data import (
    resolve_spend_guard_db_path as _resolve_spend_guard_db_path,
)
from .._local_data import (
    secure_sqlite_connect as _secure_sqlite_connect,
)
from .._local_data import (
    sqlite_schema_cache_key as _sqlite_schema_cache_key,
)

_log = logging.getLogger(__name__)
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[tuple[str, int, int]] = set()

# Single source of truth for event_type values (used by analytics queries).
EVENT_TYPES = frozenset({
    "block",            # initial block stored as pending
    "hard_block",       # immutable hard ceiling
    "warn",             # advisory warn band
    "allow",            # allow path with audit-worthy context (e.g. TIP bypass)
    "tip_bypass",       # TIP-authorized allow
    "approve_yes",      # POSITIVE intent → replay
    "cancel_no",        # NEGATIVE intent
    "cancel",           # [TIP: cancel] explicit
    "replay",           # request re-emitted to provider
    "reprompt",         # AMBIGUOUS intent
    "estimate",         # [TIP: estimate=on]
    "expire",           # TTL expired
    "anti_loop_hit",    # cached block returned without re-estimation
    "pending_waiting",  # subsequent request while pending exists
    "replay_race",      # race on double-consume
    "yes_grant_created",     # session-scoped grant written on POSITIVE/allow=session
    "yes_grant_bypass",      # held-band block bypassed by an active grant (per redemption)
    "yes_grant_exhausted",   # grant's max=$ budget spent → fell back to block band
    "yes_grant_expired",     # grant past expires_at → fell back to block band
    "yes_grant_read_error",  # grant-table read failed → fail-closed to block band
    "yes_grant_discarded",   # NEGATIVE intent / [TIP: cancel] discarded an active grant
    "reservation_block",       # concurrent admission denied (Standard 29 §15)
    "reservation_tip_bypass",  # TIP-approved send recorded as a forced hold
    "reservation_settled",     # hold released after provider settlement
})


def _db_path(audit_db_path: Optional[str]) -> Path:
    return _resolve_spend_guard_db_path(audit_db_path)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    schema_key = _sqlite_schema_cache_key(conn)
    if schema_key in _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if schema_key in _SCHEMA_READY:
            return
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spend_guard_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                projected_tokens INTEGER NOT NULL DEFAULT 0,
                projected_cost_usd REAL NOT NULL DEFAULT 0.0,
                pending_id TEXT NOT NULL DEFAULT '',
                request_hash TEXT NOT NULL DEFAULT '',
                tip_directive_json TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_session_ts "
            "ON spend_guard_audit(session_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_event_ts "
            "ON spend_guard_audit(event_type, ts)"
        )
        # Traffic classification columns (Standard 29 §13.4). Additive +
        # idempotent — PRAGMA table_info guard so re-running is a no-op. Every
        # audit row carries both the §13.1 class and the §13.2 detection reason;
        # the class alone is not sufficient for forensic reconstruction. Pre-§13
        # rows default to 'external_untagged' / 'no_marker'.
        _audit_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(spend_guard_audit)")
        }
        if "request_class" not in _audit_cols:
            conn.execute(
                "ALTER TABLE spend_guard_audit ADD COLUMN request_class "
                "TEXT NOT NULL DEFAULT 'external_untagged'"
            )
        if "request_class_reason" not in _audit_cols:
            conn.execute(
                "ALTER TABLE spend_guard_audit ADD COLUMN request_class_reason "
                "TEXT NOT NULL DEFAULT 'no_marker'"
            )
        conn.commit()
        if schema_key is not None:
            _SCHEMA_READY.add(schema_key)


def write_audit(
    audit_db_path: Optional[str] = None,
    *,
    event_type: str,
    session_id: str,
    decision_str: str = "",
    pending_id: Optional[str] = None,
    projected_cost: Optional[float] = None,
    projected_tokens: Optional[int] = None,
    request_hash: Optional[str] = None,
    tip=None,                   # TIPDirective | None
    extra: Optional[dict] = None,
    request_class: str = "external_untagged",
    request_class_reason: str = "no_marker",
) -> None:
    """Insert one audit row. Best-effort — never raises into caller.

    ``request_class`` / ``request_class_reason`` are the Standard 29 §13.1/§13.2
    traffic class + detection reason of the request that produced this decision;
    they default to the pre-classifier sentinels so legacy callers stay valid.
    """
    try:
        path = _db_path(audit_db_path)
        conn = _secure_sqlite_connect(path, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            _ensure_schema(conn)
            tip_json = ""
            if tip is not None:
                try:
                    tip_json = json.dumps({k: v for k, v in asdict(tip).items() if v not in (None, False, "", [])})
                except Exception:
                    tip_json = ""
            extra_json = ""
            if extra:
                try:
                    extra_json = json.dumps(extra, default=str)
                except Exception:
                    extra_json = ""
            conn.execute(
                """INSERT INTO spend_guard_audit
                       (ts, session_id, event_type, decision, reason,
                        projected_tokens, projected_cost_usd,
                        pending_id, request_hash, tip_directive_json, extra_json,
                        request_class, request_class_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    time.time(),
                    session_id or "",
                    event_type,
                    decision_str or "",
                    "",  # reason — populated by caller via extra if needed
                    int(projected_tokens or 0),
                    float(projected_cost or 0.0),
                    pending_id or "",
                    request_hash or "",
                    tip_json,
                    extra_json,
                    request_class or "external_untagged",
                    request_class_reason or "no_marker",
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log.debug("spend_guard.audit: write failed: %s: %s", type(e).__name__, e)


def query_recent(
    audit_db_path: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Read the most recent audit rows. Used by tests and `tokenpak doctor`."""
    path = _db_path(audit_db_path)
    conn = _secure_sqlite_connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        _ensure_schema(conn)
        if session_id:
            rows = conn.execute(
                "SELECT * FROM spend_guard_audit WHERE session_id = ? "
                "ORDER BY ts DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM spend_guard_audit ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


__all__ = ["EVENT_TYPES", "write_audit", "query_recent"]
