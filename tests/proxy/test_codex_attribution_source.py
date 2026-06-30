"""Codex Responses attribution-source and usage extraction coverage."""

from __future__ import annotations

import json
import sqlite3

from tokenpak.proxy import monitor as monitor_mod
from tokenpak.proxy.adapters.openai_codex_responses_adapter import (
    _codex_response_completed_usage,
)
from tokenpak.proxy.monitor import Monitor
from tokenpak.proxy.request_pipeline import _resolve_agent_id, _resolve_cycle_id


def _sse(payload: dict) -> bytes:
    return f"event: response.completed\ndata: {json.dumps(payload)}\n\n".encode()


def test_codex_response_completed_usage_extracts_model_tokens_and_cache_read():
    usage = _codex_response_completed_usage(
        _sse(
            {
                "response": {
                    "model": "gpt-5.5",
                    "usage": {
                        "input_tokens": 123,
                        "output_tokens": 45,
                        "input_tokens_details": {"cached_tokens": 67},
                    },
                }
            }
        )
    )

    assert usage == {
        "model": "gpt-5.5",
        "input_tokens": 123,
        "output_tokens": 45,
        "cache_read_tokens": 67,
    }


def test_codex_monitor_row_uses_header_attribution_source(tmp_path):
    db_path = tmp_path / "monitor.db"
    monitor = Monitor(str(db_path))
    headers = {
        "X-Tokenpak-Agent": "worker-a",
        "X-Tokenpak-Cycle": "governor-codex",
    }
    usage = _codex_response_completed_usage(
        _sse(
            {
                "response": {
                    "model": "gpt-5.5",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                }
            }
        )
    )

    monitor.log(
        model=str(usage["model"]),
        input_tokens=int(usage["input_tokens"]),
        output_tokens=int(usage["output_tokens"]),
        cost=0.0,
        latency_ms=20,
        status_code=200,
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        cache_origin="upstream",
        cache_read_tokens=int(usage["cache_read_tokens"]),
        agent_id=_resolve_agent_id(headers),
        cycle_id=_resolve_cycle_id(headers),
    )

    monitor_mod._DB_WRITE_QUEUE.join()
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT model, input_tokens, output_tokens, cache_origin, "
            "agent_id, cycle_id, attribution_source FROM requests"
        ).fetchone()
    finally:
        conn.close()

    assert row == ("gpt-5.5", 10, 4, "upstream", "worker-a", "governor-codex", "header")
