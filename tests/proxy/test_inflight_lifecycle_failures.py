# SPDX-License-Identifier: Apache-2.0
"""Regression tests for in-flight timing order and exceptional cleanup."""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.needs_proxy

from tests.proxy._proxy_subprocess import free_port
from tokenpak.proxy import inflight_registry
from tokenpak.proxy import server as proxy_server_module
from tokenpak.proxy.server import ProxyServer, get_upstream_inflight_snapshot


class _SingleChunkSSEHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        body = b'data: {"type":"message_delta","usage":{"output_tokens":1}}\n\ndata: [DONE]\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _wait_ready(port: int) -> None:
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"proxy did not open port {port}")


def _wait_inflight_empty() -> None:
    deadline = time.time() + 2
    while time.time() < deadline:
        if inflight_registry.snapshot() == []:
            return
        time.sleep(0.01)
    assert inflight_registry.snapshot() == []


def _post_stream(port: int, target_url: str) -> tuple[int, bytes]:
    body = json.dumps(
        {
            "model": "claude-sonnet-4-8",
            "max_tokens": 16,
            "stream": True,
            "messages": [{"role": "user", "content": "inflight lifecycle regression"}],
        }
    ).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(
            "POST",
            target_url,
            body=body,
            headers={"Content-Type": "application/json", "x-api-key": "test-key"},
        )
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_inflight_registry():
    inflight_registry.reset_for_testing()
    yield
    inflight_registry.reset_for_testing()


def test_ttfb_is_observed_before_first_downstream_body_write(monkeypatch):
    """Regression: first upstream-byte timing precedes downstream body I/O."""
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _SingleChunkSSEHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    observed: list[bool] = []
    body_write_seen = threading.Event()
    original_setup = proxy_server_module._ProxyHandler.setup

    class _InspectingWriter:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def write(self, data):
            if b'data: {"type":"message_delta"' in data and not body_write_seen.is_set():
                snapshot = inflight_registry.snapshot()
                observed.append(bool(snapshot and snapshot[0]["ttfb_ms"] is not None))
                body_write_seen.set()
            return self._wrapped.write(data)

    def _observing_setup(handler):
        original_setup(handler)
        handler.wfile = _InspectingWriter(handler.wfile)

    monkeypatch.setattr(proxy_server_module._ProxyHandler, "setup", _observing_setup)
    monkeypatch.setattr(
        proxy_server_module,
        "INTERCEPT_HOSTS",
        set(proxy_server_module.INTERCEPT_HOSTS) | {"127.0.0.1"},
    )
    monkeypatch.setenv("TOKENPAK_SPEND_GUARD_ENABLED", "0")

    proxy = ProxyServer(host="127.0.0.1", port=free_port())
    proxy.start(blocking=False)
    try:
        _wait_ready(proxy.port)
        target = f"http://127.0.0.1:{upstream.server_address[1]}/v1/messages"
        status, _ = _post_stream(proxy.port, target)
        assert status == 200
        assert body_write_seen.wait(timeout=2)
        assert observed == [True]
        _wait_inflight_empty()
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)


def test_upstream_exception_finishes_inflight_registration(monkeypatch):
    """Regression: a provider exception cannot strand a live registration."""

    class _FailingPool:
        @contextlib.contextmanager
        def stream(self, *args, **kwargs):
            raise RuntimeError("injected upstream stream failure")
            yield  # pragma: no cover

        def close(self) -> None:
            pass

    monkeypatch.setenv("TOKENPAK_SPEND_GUARD_ENABLED", "0")
    proxy = ProxyServer(host="127.0.0.1", port=free_port())
    proxy._connection_pool.close()
    proxy._connection_pool = _FailingPool()
    proxy.start(blocking=False)
    try:
        _wait_ready(proxy.port)
        status, _ = _post_stream(proxy.port, "https://api.anthropic.com/v1/messages")
        assert status == 502
        _wait_inflight_empty()
        assert get_upstream_inflight_snapshot() == {}
    finally:
        proxy.stop()
