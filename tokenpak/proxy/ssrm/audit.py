"""SSRM audit log — append-only record of every decision.

Writes go to ``ssrm_audit.db.ssrm_decisions`` (separate file from
``monitor.db`` so decision bursts don't contend with the request-row
hot path).
"""

from __future__ import annotations

import time
from typing import Optional

from .contracts import Decision
from .state import open_audit_db


def record_decision(
    decision: Decision,
    *,
    audit_db_path: str = "~/.tokenpak/ssrm_audit.db",
) -> Optional[int]:
    """Insert a row for this decision and return its rowid.

    Best-effort: any sqlite error is swallowed and None is returned. The
    request flow MUST NOT depend on this succeeding.
    """
    try:
        conn = open_audit_db(audit_db_path)
        cur = conn.execute(
            """INSERT INTO ssrm_decisions
               (ts, session_id, agent_id, model, decision,
                effective_context_pct, cache_read_ratio, drift_score,
                fingerprint_repeat_count, progress_signal,
                signals_json, advisory_only, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(),
                decision.signals.session_id or None,
                decision.signals.agent_id or None,
                decision.signals.model or None,
                decision.action,
                decision.signals.effective_context_pct,
                decision.signals.cache_read_ratio,
                decision.signals.relevance_drift_score,
                decision.signals.prompt_fingerprint_repeat_count,
                decision.signals.progress_signal,
                decision.signals.to_json(),
                1 if decision.advisory_only else 0,
                decision.reason,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        return None


def tail(
    *,
    audit_db_path: str = "~/.tokenpak/ssrm_audit.db",
    session_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Read the most recent decisions for CLI / status surfaces."""
    try:
        import sqlite3
        conn = open_audit_db(audit_db_path)
        conn.row_factory = sqlite3.Row
        sql = (
            "SELECT id, ts, session_id, agent_id, model, decision, "
            "effective_context_pct, cache_read_ratio, drift_score, "
            "fingerprint_repeat_count, progress_signal, advisory_only, reason "
            "FROM ssrm_decisions "
        )
        params: tuple = ()
        if session_id:
            sql += "WHERE session_id = ? "
            params = (session_id,)
        sql += "ORDER BY id DESC LIMIT ?"
        params = params + (int(limit),)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def explain(decision_id: int, *, audit_db_path: str = "~/.tokenpak/ssrm_audit.db") -> Optional[dict]:
    """Return a single decision row with full signals_json. None if not found."""
    try:
        import sqlite3
        conn = open_audit_db(audit_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM ssrm_decisions WHERE id = ?", (int(decision_id),)
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def summary_stats(*, audit_db_path: str = "~/.tokenpak/ssrm_audit.db") -> dict:
    """Aggregate stats over the audit log for `tokenpak ssrm status`."""
    out = {
        "total_decisions": 0,
        "by_action": {},
        "top_sessions_by_repeat": [],
        "top_sessions_by_effective_context": [],
        "avg_effective_context_pct_24h": None,
    }
    try:
        conn = open_audit_db(audit_db_path)
        out["total_decisions"] = conn.execute(
            "SELECT COUNT(*) FROM ssrm_decisions"
        ).fetchone()[0]
        for action, count in conn.execute(
            "SELECT decision, COUNT(*) FROM ssrm_decisions GROUP BY decision ORDER BY COUNT(*) DESC"
        ):
            out["by_action"][action] = count
        out["top_sessions_by_repeat"] = [
            dict(zip(("session_id", "max_repeat"), r))
            for r in conn.execute(
                """SELECT session_id, MAX(fingerprint_repeat_count) FROM ssrm_decisions
                   WHERE session_id IS NOT NULL
                   GROUP BY session_id
                   ORDER BY MAX(fingerprint_repeat_count) DESC LIMIT 3"""
            )
        ]
        out["top_sessions_by_effective_context"] = [
            dict(zip(("session_id", "max_eff_ctx_pct"), r))
            for r in conn.execute(
                """SELECT session_id, MAX(effective_context_pct) FROM ssrm_decisions
                   WHERE session_id IS NOT NULL
                   GROUP BY session_id
                   ORDER BY MAX(effective_context_pct) DESC LIMIT 3"""
            )
        ]
        row = conn.execute(
            """SELECT AVG(effective_context_pct)
               FROM ssrm_decisions
               WHERE ts >= ?""",
            (time.time() - 86400,),
        ).fetchone()
        if row and row[0] is not None:
            out["avg_effective_context_pct_24h"] = round(float(row[0]), 2)
    except Exception:
        pass
    return out
