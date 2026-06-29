# SPDX-License-Identifier: Apache-2.0
"""First-receipt proof for the local proxy path."""

from __future__ import annotations

import http.client
import json
import socket
import sqlite3
from pathlib import Path
from typing import Any

from tokenpak.cli.aggregate import aggregate_records
from tokenpak.cli.aggregate import load_requests as load_aggregate_requests
from tokenpak.cli.request_explorer import get_request_by_id, load_requests, to_view
from tokenpak.proxy import monitor as monitor_mod
from tokenpak.proxy import server as proxy_server
from tokenpak.proxy import vault_bridge
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


class _SyncFallbackQueue:
    def put_nowait(self, _item: object) -> None:
        raise RuntimeError("force synchronous monitor write")


def _post_messages(port: int, body: bytes) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {
        "content-type": "application/json",
        "content-length": str(len(body)),
        "x-api-key": "test-anthropic-key",
        "anthropic-version": "2023-06-01",
        "x-tokenpak-session": "sess-first",
        "x-tokenpak-agent": "proxy-test",
    }
    conn.request("POST", "/v1/messages", body=body, headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload


def _last_request_row(db_path: Path) -> sqlite3.Row:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM requests ORDER BY id").fetchall()
    assert rows, "proxy request did not create a local monitor.db receipt row"
    return rows[-1]


def test_first_proxy_request_writes_measured_local_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    """A loopback proxy request creates a durable, locally-measured receipt."""
    home = tmp_path / "home"
    db_path = home / "monitor.db"
    pool = _FixturePool()
    port = _free_port()

    monkeypatch.setenv("TOKENPAK_HOME", str(home))
    monkeypatch.delenv("TOKENPAK_DB", raising=False)
    monkeypatch.delenv("TOKENPAK_MONITOR_DB", raising=False)
    monkeypatch.setattr(monitor_mod, "_init_db_write_queue", lambda: None)
    monkeypatch.setattr(monitor_mod, "_DB_WRITE_QUEUE", _SyncFallbackQueue(), raising=False)
    monkeypatch.setattr(monitor_mod, "_DB_CONNECTION", None, raising=False)
    monkeypatch.setattr(
        vault_bridge,
        "inject_vault_context",
        lambda body, adapter=None, request=None: (body, 0, []),
    )
    monkeypatch.setattr(proxy_server, "_DbMonitor", lambda _ignored: Monitor(str(db_path)))

    ps = proxy_server.ProxyServer(host="127.0.0.1", port=port, shutdown_timeout=1)
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

        row = _last_request_row(db_path)
        assert row["model"] == "claude-3-5-sonnet-20241022"
        assert row["status_code"] == 200
        assert row["input_tokens"] > 0
        assert row["output_tokens"] == 32
        assert row["cache_read_tokens"] == 20_000
        assert row["cache_origin"] == "proxy"
        assert row["estimated_cost"] > 0
        assert row["would_have_saved"] == 0
        assert row["session_id"] == "sess-first"
        assert row["agent_id"] == "proxy-test"
    finally:
        ps.stop()

    request_rows = load_requests()
    assert len(request_rows) == 1

    view = to_view(request_rows[0])
    assert view.model == "claude-3-5-sonnet-20241022"
    assert view.input_tokens > 0
    assert view.output_tokens == 32
    assert view.cache_read == 20_000
    assert view.saved_cost == 0
    assert view.session_id == "sess-first"
    assert get_request_by_id(view.request_id) == request_rows[0]

    aggregate_rows, totals = aggregate_records(load_aggregate_requests(), machine="test-host")
    assert totals["requests"] == 1
    assert totals["tokens"] == view.input_tokens + view.output_tokens
    assert totals["cost"] > 0
    assert totals["saved"] == view.saved_cost
    assert aggregate_rows[0].agent == "proxy-test"
