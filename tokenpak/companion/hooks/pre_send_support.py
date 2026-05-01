# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the Claude Code pre-send hook.

Kept outside ``pre_send.py`` so the hook entrypoint stays a thin adapter
shim while preserving the existing journal/capsule/vault contracts.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from tokenpak.proxy.adapters import build_registry

_MODEL_RATES = {"opus": 15.00, "sonnet": 3.00, "haiku": 0.80}
_DEFAULT_INPUT_RATE = 3.00


def rate_for(model: str) -> float:
    m = (model or "").lower()
    for key, rate in _MODEL_RATES.items():
        if key in m:
            return rate
    return _DEFAULT_INPUT_RATE


def get_daily_total(budget_db: Path) -> float:
    try:
        if not budget_db.exists():
            return 0.0
        conn = sqlite3.connect(str(budget_db))
        row = conn.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0) FROM companion_costs WHERE date = ?",
            (datetime.date.today().isoformat(),),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


def record_daily_cost(budget_db: Path, cost_est: float) -> None:
    if cost_est <= 0:
        return
    try:
        budget_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(budget_db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS companion_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                timestamp REAL NOT NULL,
                estimated_cost REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO companion_costs (date, timestamp, estimated_cost) VALUES (?, ?, ?)",
            (datetime.date.today().isoformat(), time.time(), cost_est),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def notify_journal_row(row: dict) -> None:
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from tokenpak.core.contracts import tip_version as _tip_version
        from tokenpak.services.diagnostics import conformance as _conformance

        payload = {
            "timestamp": _dt.now(_tz.utc).isoformat().replace("+00:00", "Z"),
            "tip_version": _tip_version.CURRENT,
            **row,
        }
        _conformance.notify_companion_journal_row(payload)
    except Exception:
        pass


def journal_write(journal_db: Path, session_id: str, tokens_est: int, cost_est: float,
                  prompt_preview: str, route_class: str) -> None:
    try:
        journal_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(journal_db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                entry_type TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        conn.execute(
            "INSERT INTO entries (session_id, timestamp, entry_type, content, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, time.time(), "pre_send", prompt_preview,
             json.dumps({"tokens_est": tokens_est, "cost_est": round(cost_est, 6),
                         "route_class": route_class})),
        )
        conn.commit()
        conn.close()
        notify_journal_row({
            "session_id": session_id,
            "entry_type": "pre_send",
            "source": f"hook.pre_send:journal:{route_class}",
            "tokens_avoided": 0,
            "cost_avoided_usd": 0.0,
        })
    except Exception:
        pass


def journal_write_savings(journal_db: Path, session_id: str, tokens_avoided: int,
                          cost_avoided_usd: float, source: str) -> None:
    try:
        journal_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(journal_db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                entry_type TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        meta = {"source": source, "tokens_avoided": int(tokens_avoided),
                "cost_avoided_usd": round(cost_avoided_usd, 6)}
        conn.execute(
            "INSERT INTO entries (session_id, timestamp, entry_type, content, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, time.time(), "companion_savings", source, json.dumps(meta)),
        )
        conn.commit()
        conn.close()
        notify_journal_row({
            "session_id": session_id,
            "entry_type": "companion_savings",
            "source": f"hook.pre_send:{source}",
            "tokens_avoided": int(tokens_avoided),
            "cost_avoided_usd": round(cost_avoided_usd, 6),
        })
    except Exception:
        pass


def load_active_capsule(companion_dir: Path, session_id: str) -> str:
    for path in (companion_dir / "capsules" / f"{session_id}.md",
                 companion_dir / "capsules" / "active.md"):
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    return text
        except OSError:
            continue
    return ""


def query_vault_context(prompt: str, budget_chars: int) -> str:
    if not prompt or budget_chars <= 0:
        return ""
    try:
        from tokenpak.vault.blocks import BlockStore

        hits = BlockStore.default().search(prompt, top_k=5)  # type: ignore[attr-defined]
    except Exception:
        return ""
    pieces: list[str] = []
    used = 0
    for hit in hits or []:
        text = getattr(hit, "text", None) or getattr(hit, "content", None)
        if not text:
            continue
        remaining = budget_chars - used - 20
        if remaining <= 0:
            break
        snippet = text if len(text) <= remaining else text[:remaining]
        pieces.append(snippet)
        used += len(snippet) + 20
    return "\n---\n".join(pieces)


def headers(payload: Mapping[str, Any], session_id: str) -> dict[str, str]:
    raw = payload.get("headers") if isinstance(payload.get("headers"), Mapping) else {}
    out = {str(k).lower(): str(v) for k, v in raw.items()}
    out.setdefault("user-agent", os.environ.get("TOKENPAK_COMPANION_USER_AGENT", "claude-cli/pre-send"))
    out.setdefault("anthropic-version", "2023-06-01")
    if session_id:
        out.setdefault("x-claude-code-session-id", session_id)
    return out


def _body(payload: Mapping[str, Any], prompt_text: str, model: str) -> bytes:
    raw = payload.get("body") or payload.get("request_body")
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str) and raw.strip():
        return raw.encode("utf-8")
    return json.dumps({"model": model or "unknown", "messages": [{"role": "user", "content": prompt_text}]}).encode("utf-8")


def canonical_prompt(payload: Mapping[str, Any], req_headers: Mapping[str, str], model: str) -> tuple[str, str]:
    prompt_text = str(payload.get("prompt") or payload.get("message") or "")
    adapter = build_registry().detect("/v1/messages", req_headers, _body(payload, prompt_text, model))
    try:
        canonical = adapter.normalize(_body(payload, prompt_text, model))
    except Exception:
        canonical = adapter.normalize(_body({}, prompt_text, model))
    return canonical.last_user_message_text(), adapter.source_format


def token_estimate(transcript_path: str, prompt_text: str) -> int:
    total = len(prompt_text) // 4 if prompt_text else 0
    if transcript_path:
        try:
            total += os.path.getsize(transcript_path) // 4
        except OSError:
            pass
    return total


def budget_state(budget_db: Path, cost_est: float) -> tuple[bool, float, float]:
    try:
        budget = float(os.environ.get("TOKENPAK_COMPANION_BUDGET", "0"))
    except ValueError:
        budget = 0.0
    daily_total = get_daily_total(budget_db) if budget > 0 else 0.0
    return budget > 0 and daily_total + cost_est > budget, daily_total, budget


def enrichment(companion_dir: Path, journal_db: Path, session_id: str,
               prompt_text: str, rate: float) -> tuple[list[str], list[str]]:
    parts: list[str] = []
    credits: list[str] = []
    capsule = load_active_capsule(companion_dir, session_id)
    if capsule:
        parts.append("# tokenpak capsule (session memory)\n" + capsule)
        avoided = len(capsule) // 4
        journal_write_savings(journal_db, session_id, avoided, avoided * rate / 1_000_000, "capsule")
        credits.append(f"capsule +{avoided} tok")
    if len(prompt_text) // 4 >= int(os.environ.get("TOKENPAK_COMPANION_MIN_QUERY_TOKENS", "50")):
        vault_ctx = query_vault_context(prompt_text, int(os.environ.get("TOKENPAK_COMPANION_INJECT_BUDGET", "2000")))
        if vault_ctx:
            parts.append("# tokenpak vault context\n" + vault_ctx)
            added = len(vault_ctx) // 4
            journal_write_savings(journal_db, session_id, -added, -(added * rate / 1_000_000), "vault-enrichment")
            credits.append(f"vault +{added} tok")
    return parts, credits
