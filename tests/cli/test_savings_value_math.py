from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from tokenpak import _cli_core


def _args(*, days: int = 30, as_json: bool = False, minimal: bool = False):
    return SimpleNamespace(
        days=days,
        as_json=as_json,
        minimal=minimal,
        output="raw" if as_json else "normal",
    )


def _create_monitor_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            compressed_tokens INTEGER NOT NULL DEFAULT 0,
            protected_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost REAL NOT NULL DEFAULT 0,
            cache_origin TEXT,
            status_code INTEGER NOT NULL DEFAULT 200
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_request(
    path,
    *,
    timestamp_sql: str = "datetime('now')",
    input_tokens: int = 800_000,
    output_tokens: int = 100_000,
    cache_read_tokens: int = 200_000,
    compressed_tokens: int = 400_000,
    estimated_cost: float = 3.96,
    cache_origin: str = "proxy",
):
    conn = sqlite3.connect(path)
    conn.execute(
        f"""
        INSERT INTO requests (
            timestamp,
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            compressed_tokens,
            protected_tokens,
            estimated_cost,
            cache_origin,
            status_code
        )
        VALUES (
            {timestamp_sql},
            'claude-sonnet-4-6',
            ?,
            ?,
            ?,
            0,
            ?,
            0,
            ?,
            ?,
            200
        )
        """,
        (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            compressed_tokens,
            estimated_cost,
            cache_origin,
        ),
    )
    conn.commit()
    conn.close()


def test_cmd_savings_renders_live_value_math_and_estimate_caveat(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "monitor.db"
    _create_monitor_db(db_path)
    _insert_request(db_path)
    _insert_request(
        db_path,
        timestamp_sql="datetime('now', '-60 days')",
        input_tokens=8_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=2_000_000,
        compressed_tokens=4_000_000,
        estimated_cost=39.60,
    )
    monkeypatch.setenv("TOKENPAK_DB", str(db_path))

    _cli_core.cmd_savings(_args(days=1))

    out = capsys.readouterr().out
    assert "Window" in out and "1d" in out
    assert "Savings" in out and "$1.76" in out
    assert "Savings %" in out and "30.8%" in out
    assert "Actual Cost" in out and "$3.96" in out
    assert "Baseline" in out and "$5.72" in out
    assert "Cache Observed" in out and "20.0%" in out
    assert "Attribution" in out and "TokenPak-caused only" in out
    assert _cli_core.SAVINGS_ESTIMATE_NOTE in out
    assert "$17.60" not in out


def test_cmd_savings_json_with_data_includes_live_fields_and_caveat(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "monitor.db"
    _create_monitor_db(db_path)
    _insert_request(db_path)
    monkeypatch.setenv("TOKENPAK_DB", str(db_path))

    _cli_core.cmd_savings(_args(days=30, as_json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload["section"] == "savings"
    assert payload["available"] is True
    assert payload["actual_cost"] == 3.96
    assert payload["baseline_cost"] == 5.72
    assert payload["savings_amount"] == 1.76
    assert payload["savings_pct"] == 30.7692
    assert payload["cache_hit_rate"] == 0.2
    assert payload["attribution"]["provider_or_client_cache"] == "observed_not_credited"
    assert payload["estimate_note"] == _cli_core.SAVINGS_ESTIMATE_NOTE


def test_cmd_savings_json_observes_client_cache_without_crediting_it(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "monitor.db"
    _create_monitor_db(db_path)
    _insert_request(
        db_path,
        cache_read_tokens=200_000,
        compressed_tokens=0,
        estimated_cost=3.96,
        cache_origin="client",
    )
    monkeypatch.setenv("TOKENPAK_DB", str(db_path))

    _cli_core.cmd_savings(_args(days=30, as_json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload["available"] is True
    assert payload["cache_hit_rate"] > 0
    assert payload["savings_amount"] == 0.0
    assert payload["actual_cost"] == payload["baseline_cost"]
    assert payload["attribution"]["provider_or_client_cache"] == "observed_not_credited"


def test_cmd_savings_empty_data_hint_path(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "monitor.db"
    _create_monitor_db(db_path)
    monkeypatch.setenv("TOKENPAK_DB", str(db_path))

    _cli_core.cmd_savings(_args(days=1))

    out = capsys.readouterr().out
    assert "No savings data yet." in out
    assert "Run your first request through the proxy" in out
    assert _cli_core.SAVINGS_ESTIMATE_NOTE not in out
