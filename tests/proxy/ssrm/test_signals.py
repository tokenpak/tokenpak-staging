"""Unit tests for tokenpak.proxy.ssrm.signals."""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from tokenpak.proxy.ssrm.signals import (
    _compute_burn_rate_per_min,
    _compute_cache_read_ratio,
    _estimate_input_tokens,
    _monitor_recent_for_session,
    _progress_signal_for_agent,
    compute_signals,
)


def test_effective_context_pct_computation(tmp_ssrm_dbs):
    """effective_context_pct = (input + cache_read) / model_max * 100."""
    body = {
        "model": "claude-opus-4-7",
        "messages": [{"role": "user", "content": "x" * 8000}],  # ~2000 toks heuristic
    }
    sigs = compute_signals(
        body,
        "claude-opus-4-7",
        "test-session-1",
        headers={},
        monitor_db_path=tmp_ssrm_dbs["monitor_db"],
        state_db_path=tmp_ssrm_dbs["state_db"],
        ledger_path=tmp_ssrm_dbs["ledger_db"],
    )
    # 2000 / 200000 = 1.0% (input only) — cache_read is 0 since no monitor history
    assert sigs.model_max_context == 200_000
    assert sigs.input_tokens >= 1900 and sigs.input_tokens <= 2100
    assert sigs.effective_context_pct == pytest.approx(sigs.context_pct_now, abs=0.01)


def test_cache_read_ratio_nullsafe():
    """cache_read_ratio is 0.0 when there are no input tokens (no division by zero)."""
    assert _compute_cache_read_ratio([]) == 0.0
    assert _compute_cache_read_ratio([{"input_tokens": 0, "cache_read_tokens": 100}]) == 0.0
    # Normal case
    rows = [{"input_tokens": 100, "cache_read_tokens": 400}]
    assert _compute_cache_read_ratio(rows) == 4.0


def test_progress_signal_join_from_ledger(tmp_ssrm_dbs):
    """SSRM reads the most recent no_progress_ledger row for the requesting agent."""
    ledger = tmp_ssrm_dbs["ledger_db"]
    con = sqlite3.connect(ledger)
    con.execute("""CREATE TABLE cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent TEXT, started_at_epoch INTEGER, ended_at_epoch INTEGER,
        duration_seconds INTEGER, claude_rc INTEGER, no_progress INTEGER,
        progress_signals TEXT, queue_size_before INTEGER, queue_size_after INTEGER,
        commits_during INTEGER, has_next_step INTEGER, recorded_at TEXT)""")
    now_ep = int(time.time())
    # Inject two rows for "suki": older = no_progress=1, newer = no_progress=0
    con.execute(
        "INSERT INTO cycles (agent, started_at_epoch, ended_at_epoch, claude_rc, no_progress) VALUES (?,?,?,?,?)",
        ("suki", now_ep - 500, now_ep - 400, 0, 1),
    )
    con.execute(
        "INSERT INTO cycles (agent, started_at_epoch, ended_at_epoch, claude_rc, no_progress) VALUES (?,?,?,?,?)",
        ("suki", now_ep - 100, now_ep - 50, 0, 0),
    )
    con.commit()
    con.close()
    # Most recent wins → 'progress'
    assert _progress_signal_for_agent("suki", ledger) == "progress"
    # Unknown agent → 'neutral'
    assert _progress_signal_for_agent("nobody", ledger) == "neutral"
    # Empty agent_id → 'neutral'
    assert _progress_signal_for_agent("", ledger) == "neutral"


def test_session_age_turns_counts_monitor_rows(tmp_ssrm_dbs):
    """session_age_turns reflects the count of recent monitor.db rows for this session."""
    mdb = tmp_ssrm_dbs["monitor_db"]
    con = sqlite3.connect(mdb)
    con.execute("""CREATE TABLE requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, model TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
        cache_creation_tokens INTEGER, session_id TEXT)""")
    for i in range(7):
        con.execute(
            "INSERT INTO requests (timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, session_id) "
            "VALUES (datetime('now'), 'claude-opus-4-7', 10, 5, 100, 0, 'sess-A')"
        )
    con.commit()
    con.close()
    rows = _monitor_recent_for_session(mdb, "sess-A")
    assert len(rows) == 7
    # compute_signals should propagate this into session_age_turns
    sigs = compute_signals(
        {"messages": [{"role": "user", "content": "test"}], "model": "claude-opus-4-7"},
        "claude-opus-4-7",
        "sess-A",
        headers={},
        monitor_db_path=mdb,
        state_db_path=tmp_ssrm_dbs["state_db"],
        ledger_path=tmp_ssrm_dbs["ledger_db"],
    )
    assert sigs.session_age_turns == 7


def test_idx_requests_session_id_created_by_monitor_init(tmp_path):
    """The proxy Monitor schema creates idx_requests_session_id so the SSRM
    session lookup seeks instead of full-scanning the requests table."""
    from tokenpak.proxy.monitor import Monitor

    db = tmp_path / "monitor.db"
    Monitor(str(db))  # runs _init_db -> creates schema + indexes
    con = sqlite3.connect(str(db))
    idx = {r[1] for r in con.execute("PRAGMA index_list(requests)")}
    con.close()
    assert "idx_requests_session_id" in idx


def test_monitor_recent_for_session_is_bounded(tmp_ssrm_dbs):
    """_monitor_recent_for_session never returns more than `limit` rows, so a
    session with many rows cannot drive an unbounded result set; ordering stays
    most-recent-first."""
    mdb = tmp_ssrm_dbs["monitor_db"]
    con = sqlite3.connect(mdb)
    con.execute("""CREATE TABLE requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, model TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
        cache_creation_tokens INTEGER, session_id TEXT)""")
    for _ in range(60):
        con.execute(
            "INSERT INTO requests (timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, session_id) "
            "VALUES (datetime('now'), 'claude-opus-4-7', 10, 5, 100, 0, 'sess-big')"
        )
    con.commit()
    con.close()
    assert len(_monitor_recent_for_session(mdb, "sess-big")) == 50  # default cap
    assert len(_monitor_recent_for_session(mdb, "sess-big", limit=10)) == 10
    rows = _monitor_recent_for_session(mdb, "sess-big", limit=3)
    assert [r["id"] for r in rows] == sorted([r["id"] for r in rows], reverse=True)


def test_burn_rate_per_min_window(tmp_ssrm_dbs):
    """token_burn_rate_per_min sums (input + output) tokens over the rolling window."""
    mdb = tmp_ssrm_dbs["monitor_db"]
    con = sqlite3.connect(mdb)
    con.execute("""CREATE TABLE requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, model TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
        cache_creation_tokens INTEGER, session_id TEXT)""")
    con.execute(
        "INSERT INTO requests (timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, session_id) "
        "VALUES (datetime('now'), 'claude-opus-4-7', 1000, 200, 0, 0, 'sess-burn')"
    )
    con.execute(
        "INSERT INTO requests (timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, session_id) "
        "VALUES (datetime('now'), 'claude-opus-4-7', 500, 100, 0, 0, 'sess-burn')"
    )
    con.commit()
    con.close()
    # 1800 tokens / 10 min window = 180 tokens/min
    burn = _compute_burn_rate_per_min(mdb, "sess-burn", window_seconds=600)
    assert burn == pytest.approx(180.0, abs=1.0)


def test_signals_with_missing_dbs_dont_raise(tmp_path):
    """All read paths degrade gracefully when DBs don't exist."""
    sigs = compute_signals(
        {"messages": [{"role": "user", "content": "hi"}], "model": "claude-opus-4-7"},
        "claude-opus-4-7",
        "isolated-session",
        headers={"X-Tokenpak-Agent": "sue"},
        monitor_db_path=str(tmp_path / "nope.db"),
        state_db_path=str(tmp_path / "ssrm_state.db"),  # will be created
        ledger_path=str(tmp_path / "missing-ledger.db"),
    )
    assert sigs.session_age_turns == 0
    assert sigs.progress_signal == "neutral"
    assert sigs.cache_read_ratio == 0.0
    assert sigs.token_burn_rate_per_min == 0.0
