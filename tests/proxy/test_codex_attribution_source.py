# SPDX-License-Identifier: Apache-2.0
"""Focused Codex Responses telemetry coverage."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import tokenpak.proxy.monitor as _monitor_module
from tokenpak.proxy.adapters.openai_codex_responses_adapter import (
    OpenAICodexResponsesAdapter,
    _extract_codex_responses_usage_from_sse_tail,
    codex_responses_payload_fixup,
)
from tokenpak.proxy.monitor import Monitor
from tokenpak.proxy.request_pipeline import _resolve_session_id


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _wait_for_row(db_path: Path, model: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT session_id, agent_id, cycle_id, attribution_source "
                "FROM requests WHERE model = ?",
                (model,),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return row
        time.sleep(0.02)
    return None


def _reset_monitor_writer_connection() -> None:
    conn = getattr(_monitor_module, "_DB_CONNECTION", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _monitor_module._DB_CONNECTION = None
    _monitor_module._DB_CONNECTION_PATH = None


def test_codex_responses_adapter_has_distinct_source_format() -> None:
    assert OpenAICodexResponsesAdapter.source_format == "openai-codex-responses"


def test_codex_payload_fixup_emits_valid_streaming_request_shape() -> None:
    fixed = codex_responses_payload_fixup(
        b'{"model":"gpt-5-codex","input":"Reply with exactly: ok"}'
    )

    assert b'"stream": true' in fixed
    assert b'"store": false' in fixed
    assert b'"role": "user"' in fixed
    assert b'"type": "input_text"' in fixed


def test_session_resolver_uses_canonical_headers_only() -> None:
    assert (
        _resolve_session_id({"X-TokenPak-Session": "session-tokenpak"}, "")
        == "session-tokenpak"
    )
    assert (
        _resolve_session_id(
            {"X-Claude-Code-Session-Id": "session-claude-code"},
            "",
        )
        == "session-claude-code"
    )
    assert (
        _resolve_session_id(
            {
                "X-Claude-Code-Session-Id": "session-claude-code",
                "X-TokenPak-Session": "session-tokenpak",
            },
            "",
        )
        == "session-claude-code"
    )
    assert _resolve_session_id({"X-Session-ID": "unsupported"}, "") == ""


def test_codex_sse_usage_extraction_reads_completion_usage_only() -> None:
    tail = b"\n".join(
        [
            b"event: response.in_progress",
            b'data: {"type":"response.in_progress","response":{"id":"resp_fixture"}}',
            b"",
            b"event: response.completed",
            (
                b'data: {"type":"response.completed","response":{"usage":'
                b'{"input_tokens":123,"output_tokens":45,'
                b'"input_tokens_details":{"cached_tokens":67}}}}'
            ),
            b"",
            b"data: [DONE]",
        ]
    )

    usage = _extract_codex_responses_usage_from_sse_tail(tail)

    assert usage == {
        "input_tokens": 123,
        "output_tokens": 45,
        "cache_read_tokens": 67,
    }


def test_codex_sse_usage_extraction_fails_closed_to_zeroes() -> None:
    assert _extract_codex_responses_usage_from_sse_tail(b"data: not-json") == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
    }


def test_sync_codex_monitor_row_uses_adapter_usage_and_header_attribution() -> None:
    server_source = _source("tokenpak/proxy/server.py")
    codex_block = server_source[
        server_source.index("# \u2500\u2500 Codex /v1/responses monitor row")
        : server_source.index("# \u2500\u2500 Circuit breaker: record outcome")
    ]

    assert "_extract_codex_responses_usage_from_sse_tail" in codex_block
    assert 'cache_origin="upstream"' in codex_block
    assert "session_id=_cx_session_id" in codex_block
    assert "agent_id=_cx_agent_id" in codex_block
    assert "cycle_id=_cx_cycle_id" in codex_block
    assert '"header" if (_cx_agent_id or _cx_cycle_id) else "unknown"' in codex_block
    assert "attribution_source=_cx_attribution_source" in codex_block
    assert "dispatch_job_id=_cx_dispatch_job_id" in codex_block
    assert "dispatch_station_id=_cx_dispatch_station_id" in codex_block


def test_codex_monitor_row_persists_session_and_attribution_source(tmp_path) -> None:
    db_path = tmp_path / "monitor.db"
    mon = Monitor(db_path=str(db_path))

    try:
        session_id = _resolve_session_id({"X-TokenPak-Session": "session-tokenpak"}, "")
        mon.log(
            model="gpt-5-codex",
            input_tokens=1,
            output_tokens=1,
            cost=0.0,
            latency_ms=10,
            status_code=200,
            endpoint="https://chatgpt.com/backend-api/codex/responses",
            cache_origin="upstream",
            session_id=session_id,
            agent_id="worker-a",
            cycle_id="manual",
            attribution_source="header",
        )

        row = _wait_for_row(db_path, "gpt-5-codex")
        assert row == ("session-tokenpak", "worker-a", "manual", "header")
    finally:
        _reset_monitor_writer_connection()
