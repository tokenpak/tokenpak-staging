"""request_explorer.py — Utilities for live request exploration."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tokenpak import _paths

REQUESTS_PATH = Path.home() / ".tokenpak" / "requests.jsonl"
_MONITOR_REQUEST_SCHEMA_VERSION = "monitor-requests/v1"


@dataclass
class RequestView:
    request_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    saved_cost: float
    status: str
    timestamp: str
    session_id: str = ""


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _safe_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _safe_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _load_jsonl_requests(path: Path, limit: Optional[int] = None) -> list[dict[str, Any]]:
    p = path
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit:
        return rows[-limit:]
    return rows


def _monitor_db_path() -> Optional[Path]:
    return _paths.monitor_db(mode="read")


def _has_request_store() -> bool:
    return _monitor_db_path() is not None


def _select_expr(columns: set[str], column: str, default_sql: str, alias: str) -> str:
    if column in columns:
        return f"COALESCE({column}, {default_sql}) AS {alias}"
    return f"{default_sql} AS {alias}"


def _status_from_code(value: Any) -> str:
    try:
        code = int(value)
    except Exception:
        return str(value or "")
    if 200 <= code < 400:
        return "success"
    return str(code)


def _load_monitor_requests(limit: Optional[int] = None) -> list[dict[str, Any]]:
    db_path = _monitor_db_path()
    if db_path is None:
        return []

    with sqlite3.connect(str(db_path), timeout=2) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
        if not columns:
            return []

        select_parts = [
            _select_expr(columns, "id", "''", "id"),
            _select_expr(columns, "timestamp", "''", "timestamp"),
            _select_expr(columns, "model", "''", "model"),
            _select_expr(columns, "request_type", "''", "request_type"),
            _select_expr(columns, "endpoint", "''", "endpoint"),
            _select_expr(columns, "input_tokens", "0", "input_tokens"),
            _select_expr(columns, "output_tokens", "0", "output_tokens"),
            _select_expr(columns, "cache_read_tokens", "0", "cache_read"),
            _select_expr(columns, "cache_read_tokens", "0", "cache_read_tokens"),
            _select_expr(columns, "cache_creation_tokens", "0", "cache_creation_tokens"),
            _select_expr(columns, "estimated_cost", "0.0", "cost"),
            _select_expr(columns, "estimated_cost", "0.0", "estimated_cost"),
            _select_expr(columns, "would_have_saved", "0.0", "saved_cost"),
            _select_expr(columns, "would_have_saved", "0.0", "would_have_saved"),
            _select_expr(columns, "status_code", "0", "status_code"),
            _select_expr(columns, "session_id", "''", "session_id"),
            _select_expr(columns, "agent_id", "''", "agent"),
            _select_expr(columns, "agent_id", "''", "agent_id"),
            _select_expr(columns, "cycle_id", "''", "cycle_id"),
            _select_expr(columns, "dispatch_job_id", "''", "dispatch_job_id"),
            _select_expr(columns, "dispatch_station_id", "''", "dispatch_station_id"),
        ]
        order_column = "id" if "id" in columns else "timestamp"
        sql = f"SELECT {', '.join(select_parts)} FROM requests ORDER BY {order_column} DESC"
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (int(limit),)
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

    rows.reverse()
    for row in rows:
        row["id"] = str(row.get("id", ""))
        row["status"] = _status_from_code(row.get("status_code"))
        row["schema_version"] = _MONITOR_REQUEST_SCHEMA_VERSION
    return rows


def load_requests(path: Optional[Path] = None, limit: Optional[int] = None) -> list[dict]:
    if path is not None:
        return _load_jsonl_requests(path, limit=limit)
    return _load_monitor_requests(limit=limit)


def get_request_by_id(request_id: str, path: Optional[Path] = None) -> Optional[dict]:
    for row in load_requests(path=path):
        if str(row.get("id", "")) == str(request_id):
            return row
    return None


def to_view(row: dict) -> RequestView:
    return RequestView(
        request_id=str(row.get("id") or row.get("request_id") or ""),
        model=str(row.get("model", "")),
        input_tokens=_safe_int(row.get("input_tokens")),
        output_tokens=_safe_int(row.get("output_tokens")),
        cache_read=_safe_int(row.get("cache_read", row.get("cache_read_tokens"))),
        saved_cost=_safe_float(row.get("saved_cost", row.get("would_have_saved"))),
        status=str(row.get("status", "")),
        timestamp=str(row.get("timestamp", "")),
        session_id=str(row.get("session_id", "")),
    )


def cache_pct(view: RequestView) -> float:
    if view.input_tokens <= 0:
        return 0.0
    return round((view.cache_read / view.input_tokens) * 100, 1)


def status_label(view: RequestView) -> str:
    if view.status and view.status != "success":
        return "error"
    if view.cache_read > 0:
        return "cached"
    return "fresh"


def age_label(timestamp: str) -> str:
    dt = _parse_iso(timestamp)
    if not dt:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


__all__ = [
    "REQUESTS_PATH",
    "RequestView",
    "load_requests",
    "get_request_by_id",
    "to_view",
    "cache_pct",
    "status_label",
    "age_label",
    "format_tokens",
]
