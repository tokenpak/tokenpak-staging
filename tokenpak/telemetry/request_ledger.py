"""request_ledger.py — Append-only per-request ledger for aggregation."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

REQUESTS_PATH = Path.home() / ".tokenpak" / "requests.jsonl"

MAX_REQUESTS = 1000


def _append_jsonl(payload: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(payload) + "\n")

    try:
        lines = path.read_text().splitlines()
        if len(lines) > MAX_REQUESTS:
            path.write_text("\n".join(lines[-MAX_REQUESTS:]) + "\n")
    except Exception:
        pass


def _status_code(value: Any) -> int:
    if isinstance(value, int):
        return value
    raw = str(value or "").strip().lower()
    if raw in ("", "ok", "success"):
        return 200
    try:
        return int(raw)
    except Exception:
        return 500


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _append_monitor_db(payload: Dict[str, Any]) -> None:
    from tokenpak import _paths
    from tokenpak.proxy.monitor import Monitor

    db_path = _paths.monitor_db(mode="write")
    if db_path is None:
        return
    Monitor(str(db_path))
    with sqlite3.connect(str(db_path), timeout=2) as conn:
        conn.execute(
            """
            INSERT INTO requests (
                timestamp, model, request_type, input_tokens, output_tokens,
                estimated_cost, latency_ms, status_code, endpoint,
                cache_read_tokens, cache_creation_tokens, would_have_saved,
                session_id, agent_id, cycle_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["timestamp"],
                str(payload.get("model", "")),
                str(payload.get("request_type", "chat")),
                _as_int(payload.get("input_tokens", payload.get("tokens_in"))),
                _as_int(payload.get("output_tokens", payload.get("tokens_out"))),
                _as_float(payload.get("cost", payload.get("estimated_cost"))),
                _as_int(payload.get("latency_ms")),
                _status_code(payload.get("status", payload.get("status_code"))),
                str(payload.get("endpoint", "")),
                _as_int(payload.get("cache_read", payload.get("cache_read_tokens"))),
                _as_int(payload.get("cache_creation_tokens")),
                _as_float(payload.get("saved_cost", payload.get("would_have_saved"))),
                str(payload.get("session_id", "")),
                str(payload.get("agent", payload.get("agent_id", ""))),
                str(payload.get("cycle_id", "")),
            ),
        )
        conn.commit()


def append_request(record: Dict[str, Any], path: Optional[Path] = None) -> None:
    payload = dict(record)
    if not payload.get("timestamp"):
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    if path is not None:
        _append_jsonl(payload, path)
        return
    _append_monitor_db(payload)


__all__ = ["append_request", "REQUESTS_PATH"]
