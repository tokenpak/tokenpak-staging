"""SSRM signals — compute the 11 inputs Phase 1 records per request.

Reads from:
  - the request body (input tokens, prompt fingerprint, last-user-turn text)
  - monitor.db.requests (recent token totals for cache_read_ratio + burn rate
    + session_age)
  - ssrm_state.db.fingerprints (fingerprint repeat count)
  - ssrm_state.db.session_anchors (drift anchor, lazily set on first session
    observation)
  - no_progress_ledger.cycles (progress_signal join for the requesting agent)

All read paths are best-effort: if a DB is missing or unreadable, the
signal degrades to a safe default rather than raising.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .contracts import Signals
from .drift import drift_score
from .fingerprint import canonicalize_user_turn, record_and_count
from .state import open_state_db

# ----- model_max_context (reuse spend_guard helper if available) ---------

def _get_model_max_context(model: str) -> int:
    try:
        from tokenpak.proxy.spend_guard._context_window import get_model_max_context
        v = get_model_max_context(model)
        if v:
            return int(v)
    except Exception:
        pass
    # Fallback for unknown / missing model: assume 200K (Claude frontier default).
    return 200_000


# ----- body parsing ------------------------------------------------------

def _parse_body(body: bytes | str | dict | None) -> dict:
    if body is None:
        return {}
    if isinstance(body, dict):
        return body
    if isinstance(body, bytes):
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}
    return {}


def _estimate_input_tokens(body_obj: dict) -> int:
    """Approximate input tokens for the request body.

    Phase 1 uses a tokens-per-char heuristic (4 char ≈ 1 token) over the
    serialized messages list. Replaced by the real estimator in Phase 2
    if/when SSRM moves to behavior-changing mode.
    """
    if not isinstance(body_obj, dict):
        return 0
    msgs = body_obj.get("messages") or []
    sys_prompt = body_obj.get("system") or ""
    if isinstance(sys_prompt, list):
        sys_prompt = " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in sys_prompt
        )
    chars = len(str(sys_prompt))
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    t = part.get("text") or part.get("content") or ""
                    chars += len(str(t))
                else:
                    chars += len(str(part))
    return max(0, chars // 4)


# ----- monitor.db reads --------------------------------------------------

def _monitor_recent_for_session(
    monitor_db_path: str,
    session_id: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Return the last N monitor.db rows for this session_id (most recent first).

    Returns [] on any failure or unknown session.
    """
    if not session_id or not monitor_db_path:
        return []
    p = Path(os.path.expanduser(monitor_db_path))
    if not p.is_file():
        return []
    try:
        con = sqlite3.connect(str(p))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT id, timestamp, input_tokens, output_tokens,
                      cache_read_tokens, cache_creation_tokens
               FROM requests
               WHERE session_id = ?
               ORDER BY id DESC
               LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _compute_cache_read_ratio(recent_rows: list[dict]) -> float:
    """Aggregate cache_read / input across recent rows. NULL-safe; 0.0 if no input."""
    inp = sum((r.get("input_tokens") or 0) for r in recent_rows)
    cr = sum((r.get("cache_read_tokens") or 0) for r in recent_rows)
    if inp <= 0:
        return 0.0
    return cr / inp


def _compute_burn_rate_per_min(
    monitor_db_path: str, session_id: str, *, window_seconds: int = 600
) -> float:
    """Sum (input + output) tokens over the last `window_seconds` for this session,
    divide by window minutes. NULL-safe.
    """
    if not session_id or not monitor_db_path:
        return 0.0
    p = Path(os.path.expanduser(monitor_db_path))
    if not p.is_file():
        return 0.0
    try:
        con = sqlite3.connect(str(p))
        row = con.execute(
            """SELECT COALESCE(SUM(input_tokens),0) + COALESCE(SUM(output_tokens),0)
               FROM requests
               WHERE session_id = ?
                 AND timestamp >= datetime('now', ?)""",
            (session_id, f"-{int(window_seconds)} seconds"),
        ).fetchone()
        con.close()
        total = float(row[0] or 0)
        return total / max(1.0, window_seconds / 60.0)
    except sqlite3.Error:
        return 0.0


# ----- progress signal join ---------------------------------------------

def _progress_signal_for_agent(
    agent_id: str,
    ledger_path: str = "~/.tokenpak/no_progress_ledger.db",
    *,
    window_seconds: int = 1800,
) -> str:
    """Return 'progress' | 'neutral' | 'no_progress' from the most recent
    ledger row for this agent within the last `window_seconds`.

    'neutral' is returned for: unknown agent_id, missing ledger, error rows
    (claude_rc != 0), or no rows in window.
    """
    if not agent_id:
        return "neutral"
    p = Path(os.path.expanduser(ledger_path))
    if not p.is_file():
        return "neutral"
    cutoff = int(time.time()) - int(window_seconds)
    try:
        con = sqlite3.connect(str(p))
        row = con.execute(
            """SELECT no_progress, claude_rc
               FROM cycles
               WHERE agent = ? AND ended_at_epoch >= ?
               ORDER BY ended_at_epoch DESC LIMIT 1""",
            (agent_id.lower(), cutoff),
        ).fetchone()
        con.close()
    except sqlite3.Error:
        return "neutral"
    if not row:
        return "neutral"
    no_progress, claude_rc = row
    if claude_rc and int(claude_rc) != 0:
        return "neutral"
    return "no_progress" if int(no_progress) == 1 else "progress"


# ----- session anchor (for drift) ----------------------------------------

def _ensure_session_anchor(
    state_db_path: str, session_id: str, anchor_text: str
) -> str:
    """Return the stored anchor for this session, creating it (with `anchor_text`)
    if absent. Used so subsequent requests on the same session can compute
    drift against a stable reference.
    """
    if not session_id:
        return anchor_text
    conn = open_state_db(state_db_path)
    # Create the anchors table lazily here so state.py stays minimal.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_anchors (
            session_id TEXT PRIMARY KEY,
            anchor_text TEXT NOT NULL,
            created_at REAL NOT NULL
        )"""
    )
    row = conn.execute(
        "SELECT anchor_text FROM session_anchors WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row:
        return row[0]
    conn.execute(
        "INSERT INTO session_anchors (session_id, anchor_text, created_at) VALUES (?, ?, ?)",
        (session_id, anchor_text, time.time()),
    )
    conn.commit()
    return anchor_text


# ----- compute_signals top-level -----------------------------------------

def compute_signals(
    body: bytes | str | dict | None,
    model: str,
    session_id: str,
    headers: dict | None = None,
    *,
    monitor_db_path: str = "~/.tokenpak/monitor.db",
    state_db_path: str = "~/.tokenpak/ssrm_state.db",
    ledger_path: str = "~/.tokenpak/no_progress_ledger.db",
    drift_algorithm: str = "jaccard_word_set",
    fingerprint_ttl_seconds: int = 7200,
) -> Signals:
    """Compute the 11 SSRM signals for a request.

    All reads are best-effort — missing DBs / unparseable bodies degrade
    to safe defaults rather than raising. The returned Signals dataclass
    is JSON-serializable.
    """
    body_obj = _parse_body(body)
    headers = dict(headers or {})

    # Token / context computation
    input_tokens = _estimate_input_tokens(body_obj)
    model_max = _get_model_max_context(model)
    # cache_read for the CURRENT request isn't known pre-send. We use the
    # recent-aggregate as the working estimate (Phase 1 approximation).
    recent_rows = _monitor_recent_for_session(monitor_db_path, session_id)
    recent_cache_read = sum((r.get("cache_read_tokens") or 0) for r in recent_rows[:5])
    recent_input = sum((r.get("input_tokens") or 0) for r in recent_rows[:5])
    # Estimated per-request cache_read (the rolling mean of the last 5 rows).
    avg_cache_read = (recent_cache_read / 5.0) if recent_rows else 0.0
    effective_context_tokens = int(input_tokens + avg_cache_read)
    context_pct_now = (input_tokens / model_max * 100.0) if model_max else 0.0
    effective_context_pct = (
        (effective_context_tokens / model_max * 100.0) if model_max else 0.0
    )
    # Next-request projection: assume next user turn ~= current size; the
    # projection grows by current input + a tiny model-output reserve.
    context_pct_projected_next = (
        ((input_tokens * 2 + 1024) / model_max * 100.0) if model_max else 0.0
    )
    cache_read_ratio = (avg_cache_read / max(1, input_tokens)) if input_tokens else 0.0

    # Burn rate
    burn_rate = _compute_burn_rate_per_min(monitor_db_path, session_id)

    # Fingerprint + repeat count
    canonical = canonicalize_user_turn(body_obj)
    fingerprint = ""
    repeat_count = 0
    if canonical and session_id:
        from .fingerprint import hash_canonical
        fingerprint = hash_canonical(canonical)
        repeat_count = record_and_count(
            session_id,
            fingerprint,
            state_db_path,
            ttl_seconds=fingerprint_ttl_seconds,
        )

    # Drift vs session anchor
    drift = 1.0
    if canonical and session_id:
        anchor = _ensure_session_anchor(state_db_path, session_id, canonical)
        drift = drift_score(canonical, anchor, algorithm=drift_algorithm)

    # Task-id change detection (Phase 1 heuristic: parse out [TASK: ...] tags;
    # fall back to False when nothing parseable is present).
    task_id_change = False  # Phase 1: conservative default

    # Agent identifier — from X-Tokenpak-Agent header (set by worker
    # invocation) or fall back to the session_id prefix as a coarse proxy.
    agent_id = ""
    for k in headers.keys():
        if k.lower() == "x-tokenpak-agent":
            agent_id = str(headers[k]).strip().lower()
            break

    # Progress signal join
    progress = _progress_signal_for_agent(agent_id, ledger_path)

    # Session-age turns
    session_age = len(recent_rows)

    return Signals(
        context_pct_now=round(context_pct_now, 4),
        context_pct_projected_next=round(context_pct_projected_next, 4),
        token_burn_rate_per_min=round(burn_rate, 4),
        cache_read_ratio=round(cache_read_ratio, 4),
        prompt_fingerprint_repeat_count=int(repeat_count),
        relevance_drift_score=round(float(drift), 4),
        task_id_change_detected=bool(task_id_change),
        progress_signal=progress,
        session_age_turns=int(session_age),
        previous_recap_available=False,  # OSS Phase 1: always False
        projected_rotation_savings=0,    # OSS Phase 1: always 0
        effective_context_pct=round(effective_context_pct, 4),
        effective_context_tokens=int(effective_context_tokens),
        input_tokens=int(input_tokens),
        cache_read_tokens=int(round(avg_cache_read)),
        cache_creation_tokens=0,
        model=model,
        model_max_context=int(model_max),
        session_id=session_id or "",
        agent_id=agent_id,
        fingerprint=fingerprint,
        drift_algorithm=drift_algorithm,
    )
