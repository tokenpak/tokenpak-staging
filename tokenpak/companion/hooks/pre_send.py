#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Claude Code UserPromptSubmit hook for tokenpak companion pre-send work."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from tokenpak.companion.hooks import pre_send_support as support
from tokenpak.services.routing_service.platform import detect_platform

_COMPANION_DIR = Path(os.environ.get("TOKENPAK_COMPANION_DIR", str(Path.home() / ".tokenpak" / "companion")))
_JOURNAL_DB = _COMPANION_DIR / "journal.db"
_BUDGET_DB = _COMPANION_DIR / "budget.db"


def _read_input() -> dict[str, Any]:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _session_id(payload: dict[str, Any]) -> str:
    return str(payload.get("session_id") or "").strip() or f"anon-{os.getpid()}-{int(time.time())}"


def _budget_block(daily_total: float, cost_est: float, budget: float) -> int:
    msg = f"tokenpak: budget exceeded (${daily_total:.2f} + ${cost_est:.4f} projected > ${budget:.2f} daily)"
    print(msg, file=sys.stderr)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "decision": "block", "reason": msg}}))
    return 2


def _emit_context(parts: list[str]) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n\n".join(parts)}}))


def _emit_status(tokens_est: int, cost_est: float, daily_total: float, budget: float,
                 credit_summary: list[str]) -> None:
    if os.environ.get("TOKENPAK_COMPANION_SHOW_COST", "1") == "0":
        return
    parts = [f"tokenpak: ~{tokens_est:,} tokens"]
    if cost_est > 0:
        parts.append(f"est ${cost_est:.4f}")
    if budget > 0 and (daily_total / budget) * 100 >= 50:
        parts.append(f"budget {(daily_total / budget) * 100:.0f}%")
    print("  ".join(parts + credit_summary), file=sys.stderr)


def run(payload: dict[str, Any]) -> int:
    if os.environ.get("TOKENPAK_COMPANION_ENABLED", "1").lower() in {"0", "false", "no", "off"}:
        return 0
    session_id = _session_id(payload)
    model = os.environ.get("TOKENPAK_COMPANION_MODEL", "")
    headers = support.headers(payload, session_id)
    platform = detect_platform(headers, os.environ)
    prompt_text, source_format = support.canonical_prompt(payload, headers, model)
    tokens_est = support.token_estimate(str(payload.get("transcript_path") or ""), prompt_text)
    rate = support.rate_for(model)
    cost_est = tokens_est * rate / 1_000_000
    blocked, daily_total, budget = support.budget_state(_BUDGET_DB, cost_est)
    if blocked:
        return _budget_block(daily_total, cost_est, budget)
    route_class = f"{getattr(platform, 'platform_name', 'generic')}:{source_format}"
    support.journal_write(_JOURNAL_DB, session_id, tokens_est, cost_est, prompt_text[:200], route_class)
    support.record_daily_cost(_BUDGET_DB, cost_est)
    credit_summary: list[str] = []
    if os.environ.get("TOKENPAK_COMPANION_ENRICH", "1") != "0":
        enriched_parts, credit_summary = support.enrichment(_COMPANION_DIR, _JOURNAL_DB, session_id, prompt_text, rate)
        if enriched_parts:
            _emit_context(enriched_parts)
    _emit_status(tokens_est, cost_est, daily_total, budget, credit_summary)
    return 0


def main() -> None:
    sys.exit(run(_read_input()))


if __name__ == "__main__":
    main()
