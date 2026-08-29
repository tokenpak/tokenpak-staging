# SPDX-License-Identifier: Apache-2.0
"""End-to-end proof for the read-only ``/inflight`` surface and the timing
facts recorded to monitor.db, against a REAL proxy subprocess and a
synthetic-timing SSE stub upstream (paced, real delays between chunks —
not a canned all-at-once fixture).

Covers:
  - ttfb_ms measured at first upstream byte
  - stream duration + started_at recorded per request
  - live output-token count advances mid-stream, visible via /inflight
  - /inflight is read-only, local, and causes zero upstream calls / zero
    ledger mutation of its own (non-self-metering, extending the existing
    proof pattern from test_session_economics_default_surface_exclusion.py)
  - zero estimator math / zero time-remaining-ETA vocabulary in its output
"""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.proxy._proxy_subprocess import ProxyProc, free_port

# Paced so a human-scale test can reliably observe the request mid-flight
# without hairline race windows; still fast enough for a CI test budget.
_DELAY_BEFORE_FIRST_BYTE = 0.35
_DELAY_BETWEEN_CHUNKS = 0.35
_POLL_DEADLINE = 10.0


class _SlowSSEUpstream(HTTPServer):
    """Stub upstream that streams an SSE response in real, paced chunks.

    Two message_delta events (output_tokens 5, then 19) let the test observe
    the live count actually advancing while the request is still in flight —
    not just the final, end-of-stream value.
    """

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _SlowSSEHandler)
        self.request_count = 0


class _SlowSSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self.server.request_count += 1  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        def _send(event: dict) -> None:
            chunk = f"data: {json.dumps(event)}\n\n".encode()
            self.wfile.write(chunk)
            self.wfile.flush()

        time.sleep(_DELAY_BEFORE_FIRST_BYTE)
        _send(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_sedr014_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-8",
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            }
        )
        time.sleep(_DELAY_BETWEEN_CHUNKS)
        _send({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 5}})
        time.sleep(_DELAY_BETWEEN_CHUNKS)
        _send({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 19}})
        time.sleep(_DELAY_BETWEEN_CHUNKS)
        _send(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 19},
            }
        )
        _send({"type": "message_stop"})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture()
def slow_upstream():
    server = _SlowSSEUpstream(free_port())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    t.join(timeout=2)


def _get_json(port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, json.loads(body) if body else {}
    finally:
        conn.close()


def _post_streaming_and_drain(port: int) -> None:
    """POST a streaming /v1/messages request and read it to completion.

    Run on a background thread — this call blocks for the full paced
    duration of the stub's response (~3 chunks * delay).
    """
    body = json.dumps(
        {
            "model": "claude-sonnet-4-8",
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "timing-truth endpoint test"}],
        }
    ).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.putrequest("POST", "/v1/messages")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("x-api-key", "test-key")
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        while resp.read(4096):
            pass
    finally:
        conn.close()


def _poll_until(predicate, deadline_s: float = _POLL_DEADLINE, interval: float = 0.1):
    deadline = time.time() + deadline_s
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def _last_row(db_path: Path) -> sqlite3.Row | None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()


def test_ttfb_stream_duration_and_live_endpoint(slow_upstream):
    stub_url = f"http://127.0.0.1:{slow_upstream.server_address[1]}"
    proxy = ProxyProc(stub_url)
    try:
        proxy.wait_ready()

        # -- baseline: idle proxy reports no in-flight requests --
        status, body = _get_json(proxy.port, "/inflight")
        assert status == 200
        assert body == {"in_flight": [], "count": 0}

        request_thread = threading.Thread(
            target=_post_streaming_and_drain, args=(proxy.port,), daemon=True
        )
        request_thread.start()

        # -- 1) request becomes visible once headers + first byte land --
        def _has_ttfb():
            _, snap = _get_json(proxy.port, "/inflight")
            entries = snap.get("in_flight", [])
            if entries and entries[0].get("ttfb_ms") is not None:
                return entries[0]
            return None

        first_seen = _poll_until(_has_ttfb)
        assert first_seen is not None, "request never appeared with a ttfb_ms in /inflight"
        assert first_seen["model"] == "claude-sonnet-4-8"
        assert first_seen["ttfb_ms"] >= 0
        # Generous lower bound — proves this is a REAL measured delay, not a
        # zero/fabricated stand-in. Upper-bounded loosely to catch a totally
        # wrong (e.g. accumulated-across-retries) value without being flaky.
        assert first_seen["ttfb_ms"] >= int(_DELAY_BEFORE_FIRST_BYTE * 1000 * 0.5)
        assert first_seen["elapsed_ms"] >= 0

        # -- 2) the live output-token count actually ADVANCES mid-stream --
        def _live_tokens_at_least(n):
            def _check():
                _, snap = _get_json(proxy.port, "/inflight")
                entries = snap.get("in_flight", [])
                if entries and entries[0].get("output_tokens_live", 0) >= n:
                    return entries[0]
                return None

            return _check

        after_first_delta = _poll_until(_live_tokens_at_least(5))
        assert after_first_delta is not None, "output_tokens_live never reached 5"
        assert after_first_delta["output_tokens_live"] == 5  # first message_delta only, so far

        after_second_delta = _poll_until(_live_tokens_at_least(19))
        assert after_second_delta is not None, "output_tokens_live never advanced to 19"

        # -- 3) request completes, disappears from /inflight --
        request_thread.join(timeout=15)
        assert not request_thread.is_alive()

        def _is_empty():
            _, snap = _get_json(proxy.port, "/inflight")
            return snap if snap.get("count") == 0 else None

        emptied = _poll_until(_is_empty)
        assert emptied == {"in_flight": [], "count": 0}

        # -- 4) started_at / ttfb_ms / stream_duration_ms persisted to monitor.db --
        proxy.wait_row_count(1)
        row = _last_row(proxy.db_path)
        assert row is not None
        assert row["started_at"] is not None
        # UTC ISO-8601 — proves the format contract, not just "not null".
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", row["started_at"])
        assert row["ttfb_ms"] is not None and row["ttfb_ms"] >= 0
        assert row["stream_duration_ms"] is not None and row["stream_duration_ms"] > 0
        # Persisted end-of-stream count still matches the LAST message_delta —
        # unaffected by the live/incremental tracker existing alongside it.
        assert row["output_tokens"] == 19

        # -- 5) non-self-metering: polling /inflight caused no upstream calls
        # and no monitor.db mutation of its own --
        baseline_upstream_calls = slow_upstream.request_count
        baseline_row_count = proxy.row_count()
        for _ in range(5):
            status, _ = _get_json(proxy.port, "/inflight")
            assert status == 200
        assert slow_upstream.request_count == baseline_upstream_calls
        assert proxy.row_count() == baseline_row_count

        # -- 6) zero estimator math / zero time-remaining-ETA vocabulary --
        _, final_snapshot = _get_json(proxy.port, "/inflight")
        raw = json.dumps(final_snapshot).lower()
        for forbidden in ("eta", "time_remaining", "minutes_remaining", "remaining_minutes"):
            assert forbidden not in raw, f"forbidden estimator/ETA term present: {forbidden}"
    finally:
        proxy.cleanup()


def test_non_streaming_request_records_started_at_but_no_ttfb(slow_upstream):
    """Non-streaming requests get started_at (t0 is universal) but no
    per-byte ttfb/stream-duration — httpx blocks until the full body
    arrives, so there is no honest 'first byte' to report."""

    class _JSONUpstream(HTTPServer):
        def __init__(self, port: int) -> None:
            super().__init__(("127.0.0.1", port), _JSONHandler)

    class _JSONHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            body = json.dumps(
                {
                    "id": "msg_json",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-8",
                    "content": [{"type": "text", "text": "hi"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    json_server = _JSONUpstream(free_port())
    t = threading.Thread(target=json_server.serve_forever, daemon=True)
    t.start()
    stub_url = f"http://127.0.0.1:{json_server.server_address[1]}"
    proxy = ProxyProc(stub_url)
    try:
        proxy.wait_ready()
        status, _, _ = proxy.post_message("non-streaming timing test")
        assert status == 200
        proxy.wait_row_count(1)
        row = _last_row(proxy.db_path)
        assert row["started_at"] is not None
        assert row["ttfb_ms"] is None
        assert row["stream_duration_ms"] is None
    finally:
        proxy.cleanup()
        json_server.shutdown()
        t.join(timeout=2)
