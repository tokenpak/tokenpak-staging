# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sqlite3


def _seed_monitor_db(tmp_path, monkeypatch) -> None:
    from tokenpak import _paths
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
                session_id, agent_id, cycle_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-25T18:00:00Z",
                "claude-sonnet",
                "chat",
                120,
                30,
                0.0123,
                44,
                200,
                "/v1/messages",
                60,
                12,
                0.0045,
                "sess-1",
                "worker-a",
                "cycle-1",
            ),
        )
        conn.commit()


def test_request_explorer_reads_monitor_db_by_default(tmp_path, monkeypatch):
    from tokenpak.cli.request_explorer import (
        _MONITOR_REQUEST_SCHEMA_VERSION,
        cache_pct,
        get_request_by_id,
        load_requests,
        status_label,
        to_view,
    )

    _seed_monitor_db(tmp_path, monkeypatch)

    rows = load_requests()
    assert len(rows) == 1
    assert rows[0]["schema_version"] == _MONITOR_REQUEST_SCHEMA_VERSION
    assert rows[0]["cache_read"] == 60
    assert rows[0]["saved_cost"] == 0.0045
    assert rows[0]["agent"] == "worker-a"

    view = to_view(rows[0])
    assert view.request_id == rows[0]["id"]
    assert view.session_id == "sess-1"
    assert cache_pct(view) == 50.0
    assert status_label(view) == "cached"
    assert get_request_by_id(rows[0]["id"]) == rows[0]


def test_aggregate_reads_monitor_db_by_default(tmp_path, monkeypatch):
    from tokenpak.cli.aggregate import aggregate_records, load_requests

    _seed_monitor_db(tmp_path, monkeypatch)

    records = load_requests()
    rows, totals = aggregate_records(records, machine="test-host")

    assert totals == {
        "requests": 1,
        "tokens": 150,
        "cost": 0.0123,
        "saved": 0.0045,
    }
    assert rows[0].agent == "worker-a"
    assert rows[0].machine == "test-host"
    assert rows[0].model == "claude-sonnet"
