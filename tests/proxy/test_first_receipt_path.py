# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sqlite3


def test_first_proxy_receipt_is_visible_to_requests_and_aggregate(tmp_path, monkeypatch):
    from tokenpak import _paths
    from tokenpak.cli.aggregate import aggregate_records
    from tokenpak.cli.aggregate import load_requests as load_aggregate_requests
    from tokenpak.cli.request_explorer import get_request_by_id, load_requests, to_view
    from tokenpak.proxy.monitor import Monitor

    home = tmp_path / "home"
    monkeypatch.setenv("TOKENPAK_HOME", str(home))
    monkeypatch.delenv("TOKENPAK_DB", raising=False)
    monkeypatch.delenv("TOKENPAK_MONITOR_DB", raising=False)

    db = _paths.monitor_db(mode="write")
    Monitor(str(db))
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO requests (
                timestamp, model, request_type, input_tokens, output_tokens,
                estimated_cost, latency_ms, status_code, endpoint,
                cache_read_tokens, cache_creation_tokens, would_have_saved,
                session_id, agent_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-25T18:05:00Z",
                "claude-sonnet",
                "chat",
                200,
                50,
                0.018,
                71,
                200,
                "/v1/messages",
                80,
                20,
                0.006,
                "sess-first",
                "proxy-test",
            ),
        )
        conn.commit()

    request_rows = load_requests()
    assert len(request_rows) == 1

    view = to_view(request_rows[0])
    assert view.model == "claude-sonnet"
    assert view.input_tokens == 200
    assert view.output_tokens == 50
    assert view.cache_read == 80
    assert view.saved_cost == 0.006
    assert view.session_id == "sess-first"
    assert get_request_by_id(view.request_id) == request_rows[0]

    aggregate_rows, totals = aggregate_records(load_aggregate_requests(), machine="test-host")
    assert totals["requests"] == 1
    assert totals["tokens"] == 250
    assert totals["cost"] == 0.018
    assert totals["saved"] == 0.006
    assert aggregate_rows[0].agent == "proxy-test"
