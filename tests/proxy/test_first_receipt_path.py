"""First-receipt proof for the local proxy path."""

from __future__ import annotations

import http.client
import json
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any

from tokenpak.cli.commands.status import _calculate_fleet_savings
from tokenpak.proxy import monitor as monitor_mod
from tokenpak.proxy import server as proxy_server
from tokenpak.proxy.monitor import Monitor


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FixtureResponse:
    status_code = 200
    headers = {"content-type": "application/json"}
    content = json.dumps(
        {
            "id": "msg_first_receipt_fixture",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "fixture response"}],
            "usage": {
                "input_tokens": 1200,
                "cache_read_input_tokens": 20_000,
                "cache_creation_input_tokens": 0,
                "output_tokens": 32,
            },
        },
        separators=(",", ":"),
    ).encode()

    def close(self) -> None:
        pass


class _FixturePool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        target_url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        session_key: str | None = None,
    ) -> _FixtureResponse:
        self.calls.append(
            {
                "method": method,
                "target_url": target_url,
                "content": content or b"",
                "headers": headers or {},
                "session_key": session_key,
            }
        )
        return _FixtureResponse()

    def close(self) -> None:
        pass


def _post_messages(port: int, body: bytes) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {
        "content-type": "application/json",
        "content-length": str(len(body)),
        "x-api-key": "test-anthropic-key",
        "anthropic-version": "2023-06-01",
    }
    conn.request("POST", "/v1/messages", body=body, headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload


def _read_request_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM requests ORDER BY id").fetchall()
    finally:
        conn.close()


def _drain_monitor_queue() -> None:
    queue = getattr(monitor_mod, "_DB_WRITE_QUEUE", None)
    if queue is not None:
        queue.join()


def _wait_for_request_row(db_path: Path, *, timeout_s: float = 2.0) -> sqlite3.Row:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _drain_monitor_queue()
        rows = _read_request_rows(db_path)
        if rows:
            return rows[-1]
        time.sleep(0.05)
    _drain_monitor_queue()
    rows = _read_request_rows(db_path)
    assert rows, "proxy request did not create a local monitor.db receipt row"
    return rows[-1]


def test_first_proxy_request_writes_measured_receipt(tmp_path: Path, monkeypatch) -> None:
    """A real loopback proxy request creates a local measured receipt artifact."""
    db_path = tmp_path / "monitor.db"
    pool = _FixturePool()
    port = _free_port()

    monkeypatch.setattr(proxy_server, "_DbMonitor", lambda _ignored: Monitor(db_path))

    ps = proxy_server.ProxyServer(host="127.0.0.1", port=port)
    ps._connection_pool = pool
    ps.compression_stats.flush_shutdown_record = lambda _record: None  # type: ignore[attr-defined]
    ps.start(blocking=False)
    try:
        body = json.dumps(
            {
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Summarize this recurring project context. "
                            "TokenPak should record measured local request attribution. "
                        )
                        * 80,
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()

        status, payload = _post_messages(port, body)

        assert status == 200
        assert json.loads(payload)["id"] == "msg_first_receipt_fixture"
        assert pool.calls
        assert pool.calls[0]["target_url"] == "https://api.anthropic.com/v1/messages"

        row = _wait_for_request_row(db_path)
        assert row["model"] == "claude-3-5-sonnet-20241022"
        assert row["status_code"] == 200
        assert row["input_tokens"] > 0
        assert row["output_tokens"] == 32
        assert row["cache_read_tokens"] == 20_000
        assert row["cache_origin"] == "proxy"
        assert row["estimated_cost"] > 0

        receipt = _calculate_fleet_savings(db_path=str(db_path), period=None)
        assert receipt["totals"]["requests"] == 1
        assert receipt["totals"]["saved"] > 0
        assert receipt["totals"]["cache_savings"] > 0
    finally:
        ps.stop()
