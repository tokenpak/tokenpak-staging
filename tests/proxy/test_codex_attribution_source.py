# SPDX-License-Identifier: Apache-2.0
"""Focused Codex Responses telemetry coverage."""

from __future__ import annotations

from pathlib import Path

from tokenpak.proxy.adapters.openai_codex_responses_adapter import (
    OpenAICodexResponsesAdapter,
    _extract_codex_responses_usage_from_sse_tail,
)


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_codex_responses_adapter_has_distinct_source_format() -> None:
    assert OpenAICodexResponsesAdapter.source_format == "openai-codex-responses"


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
    assert "dispatch_job_id=_cx_dispatch_job_id" in codex_block
    assert "dispatch_station_id=_cx_dispatch_station_id" in codex_block
