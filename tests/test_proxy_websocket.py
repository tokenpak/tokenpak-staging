# SPDX-License-Identifier: Apache-2.0
"""WebSocket upstream retry policy acceptance tests.

Covers the five acceptance criteria from
p1-tokenpak-websocket-upstream-retry-policy-coverage-2026-06-20:

1. Pre-output transient upstream failure retries safely.
2. 429 honors Retry-After.
3. Deterministic mode does not retry.
4. Failure after a WebSocket output frame produces a visible terminal recovery status.
5. Recovery metadata permissions and body persistence rules match the HTTP proxy path.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import unittest
from typing import Dict, List, Optional
from unittest.mock import MagicMock, call, patch

import pytest

pytest.importorskip(
    "websockets",
    reason="websockets library required for WebSocket retry policy tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sse_response(status: int = 200, body: bytes = b"data: {}\n\n", headers: Optional[Dict[str, str]] = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    _chunks = [body, b""]
    _iter = iter(_chunks)

    def _read(n=4096):
        try:
            return next(_iter)
        except StopIteration:
            return b""

    resp.read = _read
    resp.getheader = MagicMock(side_effect=lambda h: (headers or {}).get(h))
    return resp


def _start_ws_server(handler, port: int) -> threading.Thread:
    from websockets.asyncio.server import serve as ws_serve

    async def _serve():
        async with ws_serve(handler, "127.0.0.1", port):
            await asyncio.Future()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_serve())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(0.15)
    return t


def _noop_compact(body: bytes):
    return body, len(body), len(body), 0


# ---------------------------------------------------------------------------
# Acceptance test 1 — pre-output transient failure retries safely
# ---------------------------------------------------------------------------

class TestPreOutputRetry(unittest.TestCase):
    """503 on first attempt, 200 on second; verify stream completes."""

    def test_pre_output_transient_retry_succeeds(self):
        from tokenpak.proxy.upstream_retry import UpstreamRetryPolicy
        from tokenpak.proxy.websocket import _ws_handler

        port = _free_port()
        sse_body = b"data: {\"type\": \"message_stop\"}\n\n"

        call_count = [0]
        mock_conn_success = MagicMock()
        mock_conn_success.getresponse.return_value = _sse_response(200, sse_body)

        def _make_conn(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: connection-level exception (transient)
                raise OSError("upstream unreachable")
            return mock_conn_success

        policy = UpstreamRetryPolicy(max_retries=3, retry_delay_seconds=0.0)
        received: List[str] = []

        with patch("http.client.HTTPSConnection", side_effect=_make_conn):
            async def _handler(ws):
                await _ws_handler(ws, _noop_compact, retry_policy=policy)

            _start_ws_server(_handler, port)

            async def _client():
                import websockets
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                    await ws.send(json.dumps({"model": "claude-sonnet-4-6", "messages": []}))
                    async for msg in ws:
                        received.append(msg)

            asyncio.run(_client())

        self.assertEqual(call_count[0], 2, "Should have tried twice (1 fail + 1 success)")
        self.assertTrue(any("message_stop" in m for m in received), "Should receive SSE chunks after retry")


# ---------------------------------------------------------------------------
# Acceptance test 2 — 429 honors Retry-After
# ---------------------------------------------------------------------------

class TestRetryAfterHonored(unittest.TestCase):
    """429 with Retry-After header; verify sleep is called with the header value."""

    def test_429_honors_retry_after(self):
        from tokenpak.proxy.upstream_retry import UpstreamRetryPolicy
        from tokenpak.proxy.websocket import _ws_handler

        port = _free_port()
        sse_body = b"data: {\"type\": \"message_stop\"}\n\n"

        call_count = [0]
        mock_conn_429 = MagicMock()
        mock_conn_429.getresponse.return_value = _sse_response(
            429, b'{"error":"rate_limit"}', headers={"Retry-After": "2"}
        )
        mock_conn_success = MagicMock()
        mock_conn_success.getresponse.return_value = _sse_response(200, sse_body)

        def _make_conn(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_conn_429
            return mock_conn_success

        sleep_calls: List[float] = []
        policy = UpstreamRetryPolicy(max_retries=3, retry_delay_seconds=0.5)
        original_sleep = policy.sleep

        def _spy_sleep(s: float):
            sleep_calls.append(s)
            # Don't actually sleep in tests

        policy.sleep = _spy_sleep  # type: ignore[method-assign]

        received: List[str] = []

        with patch("http.client.HTTPSConnection", side_effect=_make_conn):
            async def _handler(ws):
                await _ws_handler(ws, _noop_compact, retry_policy=policy)

            _start_ws_server(_handler, port)

            async def _client():
                import websockets
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                    await ws.send(json.dumps({"model": "claude-sonnet-4-6", "messages": []}))
                    async for msg in ws:
                        received.append(msg)

            asyncio.run(_client())

        self.assertEqual(call_count[0], 2, "Should have connected twice (1×429 + 1×200)")
        self.assertTrue(len(sleep_calls) >= 1, "Should have slept at least once")
        self.assertAlmostEqual(sleep_calls[0], 2.0, places=0,
                               msg="Sleep duration should honor Retry-After: 2 header")


# ---------------------------------------------------------------------------
# Acceptance test 3 — deterministic mode does not retry
# ---------------------------------------------------------------------------

class TestDeterministicModeNoRetry(unittest.TestCase):
    """In deterministic mode, any failure closes immediately without retry."""

    def test_deterministic_mode_no_retry(self):
        from tokenpak.proxy.upstream_retry import UpstreamRetryPolicy
        from tokenpak.proxy.websocket import _ws_handler

        port = _free_port()

        call_count = [0]

        def _make_conn(*args, **kwargs):
            call_count[0] += 1
            raise OSError("upstream unreachable")

        # Deterministic mode: retries forbidden
        policy = UpstreamRetryPolicy(max_retries=3, retry_delay_seconds=0.0, deterministic=True)
        close_codes: List[int] = []

        with patch("http.client.HTTPSConnection", side_effect=_make_conn):
            async def _handler(ws):
                await _ws_handler(ws, _noop_compact, retry_policy=policy)

            _start_ws_server(_handler, port)

            async def _client():
                import websockets
                from websockets.exceptions import ConnectionClosedError
                try:
                    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                        await ws.send(json.dumps({"model": "claude-sonnet-4-6", "messages": []}))
                        async for _ in ws:
                            pass
                        close_codes.append(ws.close_code)
                except ConnectionClosedError as exc:
                    close_codes.append(exc.code)

            asyncio.run(_client())

        self.assertEqual(call_count[0], 1, "Deterministic mode must not retry — exactly 1 attempt")
        self.assertEqual(close_codes[0], 1011, "Should close with 1011 on failure")


# ---------------------------------------------------------------------------
# Acceptance test 4 — post-output failure produces visible recovery status
# ---------------------------------------------------------------------------

class TestPostOutputRecoveryFrame(unittest.TestCase):
    """After a WebSocket output frame, stream failure sends a recovery status frame."""

    def test_post_output_failure_sends_recovery_frame(self):
        from tokenpak.proxy.upstream_retry import UpstreamRetryPolicy
        from tokenpak.proxy.websocket import _ws_handler

        port = _free_port()

        # Build a response that yields one chunk then raises
        resp = MagicMock()
        resp.status = 200
        resp.getheader = MagicMock(return_value=None)
        _call_n = [0]

        def _read_raises(n=4096):
            _call_n[0] += 1
            if _call_n[0] == 1:
                return b"data: {\"type\": \"content_block_delta\"}\n\n"
            raise OSError("connection reset by peer")

        resp.read = _read_raises
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = resp

        policy = UpstreamRetryPolicy(max_retries=0, retry_delay_seconds=0.0)
        received: List[str] = []
        close_codes: List[int] = []

        write_record_calls: List[dict] = []

        def _capture_write_record(**kwargs):
            write_record_calls.append(kwargs)
            # Return a minimal mock record
            from tokenpak.proxy.upstream_retry import STATUS_TERMINAL, UpstreamRetryRecord
            return UpstreamRetryRecord(
                request_id=kwargs["request_id"],
                tip_plan_id=kwargs.get("tip_plan_id"),
                endpoint=kwargs["endpoint"],
                provider=kwargs.get("provider"),
                model=kwargs.get("model"),
                headers_redacted={},
                body_hash=None,
                body_preview=None,
                body_persisted=False,
                body_full=None,
                stream_started=kwargs.get("stream_started", False),
                terminal_recovery_status=kwargs.get("terminal_recovery_status", STATUS_TERMINAL),
                visible_continuation_required=kwargs.get("visible_continuation_required", True),
            )

        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            with patch("tokenpak.proxy.websocket.write_record", side_effect=_capture_write_record):
                async def _handler(ws):
                    await _ws_handler(ws, _noop_compact, retry_policy=policy)

                _start_ws_server(_handler, port)

                async def _client():
                    import websockets
                    from websockets.exceptions import ConnectionClosedError
                    try:
                        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                            await ws.send(json.dumps({
                                "model": "claude-sonnet-4-6",
                                "messages": [],
                                "request_id": "req-ws-post-output-test",
                            }))
                            async for msg in ws:
                                received.append(msg)
                            close_codes.append(ws.close_code)
                    except ConnectionClosedError as exc:
                        close_codes.append(exc.code)

                asyncio.run(_client())

        # The first message should be a real SSE chunk
        self.assertTrue(len(received) >= 1, "Should receive at least one SSE chunk before failure")
        sse_frames = [m for m in received if "content_block_delta" in m]
        self.assertTrue(len(sse_frames) >= 1, "First frame should be SSE content")

        # The recovery frame should appear among the received messages
        recovery_frames = [m for m in received if "tokenpak_recovery" in m]
        self.assertTrue(len(recovery_frames) >= 1, "Should receive a tokenpak_recovery frame")
        recovery_data = json.loads(recovery_frames[0])
        self.assertEqual(recovery_data["status"], "terminal_post_output")
        self.assertIn("request_id", recovery_data)
        self.assertIn("codex continue", recovery_data["message"])

        # write_record must have been called with stream_started=True
        self.assertTrue(len(write_record_calls) >= 1, "write_record should be called on post-output failure")
        wr = write_record_calls[0]
        self.assertTrue(wr["stream_started"], "stream_started must be True for post-output record")
        self.assertTrue(wr["visible_continuation_required"], "visible_continuation_required must be True")


# ---------------------------------------------------------------------------
# Acceptance test 5 — recovery metadata body persistence rules
# ---------------------------------------------------------------------------

class TestRecoveryMetadataBodyPersistence(unittest.TestCase):
    """Recovery records redact credential headers and obey TOKENPAK_RETRY_PERSIST_BODY."""

    def test_recovery_record_redacts_credentials(self):
        from tokenpak.proxy.upstream_retry import UpstreamRetryPolicy
        from tokenpak.proxy.websocket import _ws_handler

        port = _free_port()
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = _sse_response(503, b'{"error":"overloaded"}')

        policy = UpstreamRetryPolicy(max_retries=0, retry_delay_seconds=0.0)
        write_record_calls: List[dict] = []

        def _capture(**kwargs):
            write_record_calls.append(kwargs)
            from tokenpak.proxy.upstream_retry import STATUS_TERMINAL, UpstreamRetryRecord
            return UpstreamRetryRecord(
                request_id=kwargs["request_id"],
                tip_plan_id=kwargs.get("tip_plan_id"),
                endpoint=kwargs["endpoint"],
                provider=kwargs.get("provider"),
                model=kwargs.get("model"),
                headers_redacted={},
                body_hash=None,
                body_preview=None,
                body_persisted=False,
                body_full=None,
                stream_started=False,
                terminal_recovery_status=STATUS_TERMINAL,
                visible_continuation_required=True,
            )

        class _MockWebsocketRequest:
            path = "/ws"
            headers = {
                "x-api-key": "sk-supersecret",
                "authorization": "Bearer tok-abc123",
                "anthropic-version": "2023-06-01",
            }

        class _MockWebsocket:
            request = _MockWebsocketRequest()
            _closed = False
            _sent: List[str] = []

            async def recv(self):
                return json.dumps({
                    "model": "claude-sonnet-4-6",
                    "messages": [],
                    "request_id": "req-cred-check",
                    "tip_plan_id": "plan-xyz",
                })

            async def send(self, msg):
                self._sent.append(msg)

            async def close(self, code=1000, reason=""):
                self._closed = True

        ws_mock = _MockWebsocket()

        async def _run():
            with patch("http.client.HTTPSConnection", return_value=mock_conn):
                with patch("tokenpak.proxy.websocket.write_record", side_effect=_capture):
                    await _ws_handler(ws_mock, _noop_compact, retry_policy=policy)

        asyncio.run(_run())

        self.assertTrue(len(write_record_calls) >= 1, "write_record should be called on terminal failure")
        wr = write_record_calls[0]

        # Headers passed to write_record must contain the forwarded headers
        # (write_record itself redacts them — verify the call carries credentials)
        passed_headers = wr.get("headers", {})
        self.assertIn("x-api-key", passed_headers, "Forwarded headers should include x-api-key")
        # The credential values must not be pre-redacted before calling write_record
        # (redaction is write_record's responsibility, not _ws_handler's)
        self.assertEqual(passed_headers["x-api-key"], "sk-supersecret",
                         "Raw credential value must reach write_record for it to perform redaction")

        # request_id and tip_plan_id must be preserved
        self.assertEqual(wr["request_id"], "req-cred-check")
        self.assertEqual(wr["tip_plan_id"], "plan-xyz")

    def test_recovery_record_no_body_full_without_env(self):
        """Without TOKENPAK_RETRY_PERSIST_BODY=1, body_full must not be written."""
        import os
        import tempfile

        from tokenpak.proxy.upstream_retry import STATUS_TERMINAL, list_record_files, write_record

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["TOKENPAK_HOME"] = tmpdir
            os.environ.pop("TOKENPAK_RETRY_PERSIST_BODY", None)
            try:
                write_record(
                    request_id="req-ws-body-check",
                    endpoint="/v1/messages",
                    headers={"x-api-key": "sk-secret"},
                    body=b'{"messages":[{"role":"user","content":"hello"}]}',
                    stream_started=False,
                    terminal_recovery_status=STATUS_TERMINAL,
                )
                items = list_record_files()
                self.assertEqual(len(items), 1)
                _, rec = items[0]
                self.assertIsNone(rec.body_full, "body_full must be None without TOKENPAK_RETRY_PERSIST_BODY=1")
                self.assertEqual(rec.headers_redacted["x-api-key"], "[REDACTED]",
                                 "x-api-key must be redacted in stored record")
            finally:
                del os.environ["TOKENPAK_HOME"]


if __name__ == "__main__":
    unittest.main()
