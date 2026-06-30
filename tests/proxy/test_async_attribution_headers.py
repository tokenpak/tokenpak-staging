# SPDX-License-Identifier: Apache-2.0
"""Focused async-proxy attribution coverage."""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

from starlette.requests import Request

from tokenpak.proxy.server_async import (
    _build_forward_headers,
    _record_telemetry,
    _resolve_async_monitor_attribution,
)


class _Monitor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _CompressionStats:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_compression(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _ps() -> SimpleNamespace:
    return SimpleNamespace(
        _session_lock=threading.Lock(),
        _compression_lock=threading.Lock(),
        _last_lock=threading.Lock(),
        _compression_ratios=[],
        session={
            "requests": 0,
            "input_tokens": 0,
            "sent_input_tokens": 0,
            "saved_tokens": 0,
            "protected_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
            "cost_saved": 0.0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        compression_stats=_CompressionStats(),
        trace_storage=SimpleNamespace(store=lambda _trace: None),
        monitor=_Monitor(),
        compilation_mode="fixture",
    )


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "query_string": b"",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in headers.items()
        ],
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request(scope, receive)


def test_async_monitor_write_threads_valid_header_attribution(monkeypatch) -> None:
    monkeypatch.setattr("tokenpak.proxy.router.estimate_cost", lambda *args: 0.01)
    ps = _ps()

    _record_telemetry(
        ps,
        None,
        "claude-sonnet-4-6",
        100,
        80,
        12,
        5,
        7,
        3,
        42,
        status_code=200,
        endpoint="https://api.anthropic.com/v1/messages",
        request_headers={
            "X-TokenPak-Session": "session-a",
            "X-Tokenpak-Agent": "Worker-A",
            "X-Tokenpak-Cycle": "cycle-7",
        },
    )

    assert len(ps.monitor.calls) == 1
    row = ps.monitor.calls[0]
    assert row["session_id"] == "session-a"
    assert row["agent_id"] == "worker-a"
    assert row["cycle_id"] == "cycle-7"
    assert row["status_code"] == 200
    assert row["endpoint"] == "https://api.anthropic.com/v1/messages"
    assert row["cache_read_tokens"] == 7
    assert row["cache_creation_tokens"] == 3
    assert row["would_have_saved"] == 20


def test_async_monitor_write_uses_empty_sentinels_when_attribution_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr("tokenpak.proxy.router.estimate_cost", lambda *args: 0.01)
    ps = _ps()

    _record_telemetry(
        ps,
        None,
        "claude-haiku-4-5",
        10,
        10,
        1,
        0,
        0,
        0,
        5,
        status_code=200,
        endpoint="https://api.anthropic.com/v1/messages",
        request_headers={},
    )

    row = ps.monitor.calls[0]
    assert row["session_id"] == ""
    assert row["agent_id"] == ""
    assert row["cycle_id"] == ""


def test_invalid_attribution_header_is_sentinel_and_warning_omits_value(
    caplog,
) -> None:
    raw_secretish_value = "bad\nvalue"

    with caplog.at_level(logging.WARNING, logger="tokenpak.proxy.server_async"):
        session_id, agent_id, cycle_id = _resolve_async_monitor_attribution(
            {
                "X-Tokenpak-Agent": raw_secretish_value,
                "X-Tokenpak-Cycle": "cycle-ok",
            }
        )

    assert session_id == ""
    assert agent_id == ""
    assert cycle_id == "cycle-ok"
    assert "X-Tokenpak-Agent" in caplog.text
    assert raw_secretish_value not in caplog.text


def test_internal_attribution_headers_are_not_forwarded_upstream() -> None:
    req = _request(
        {
            "Host": "127.0.0.1",
            "Content-Type": "application/json",
            "X-Tokenpak-Agent": "worker-a",
            "X-Tokenpak-Cycle": "cycle-7",
        }
    )

    forwarded = _build_forward_headers(req, "https://api.anthropic.com/v1/messages")
    lowered = {key.lower(): value for key, value in forwarded.items()}

    assert "x-tokenpak-agent" not in lowered
    assert "x-tokenpak-cycle" not in lowered
    assert lowered["content-type"] == "application/json"
    assert lowered["host"] == "api.anthropic.com"
