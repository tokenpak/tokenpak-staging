# SPDX-License-Identifier: Apache-2.0
"""Tests for `tokenpak status --explain` (ratified-C gap2, unified flag)."""
from __future__ import annotations

import sqlite3

import pytest

from tokenpak.cli.commands import explain as explain_mod


def _make_db(tmp_path, rows):
    db = tmp_path / "monitor.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY, timestamp TEXT, model TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, status_code INTEGER, "
        "compression_mode TEXT, compressed_tokens INTEGER, would_have_saved INTEGER, "
        "cache_read_tokens INTEGER)"
    )
    conn.executemany(
        "INSERT INTO requests (id,timestamp,model,input_tokens,output_tokens,status_code,"
        "compression_mode,compressed_tokens,would_have_saved,cache_read_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def patched_db(tmp_path, monkeypatch):
    db = _make_db(
        tmp_path,
        [
            (1, "2026-05-31T10:00:00Z", "claude-opus-4", 1000, 200, 200, "off", 0, 0, 0),
            (2, "2026-05-31T11:00:00Z", "claude-haiku", 500, 100, 200, "lz", 120, 130, 80),
        ],
    )

    def _row_conn():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(explain_mod, "_open", _row_conn)
    return db


def test_explain_existing_request_unknown_drop_reason(patched_db, capsys):
    rc = explain_mod.run_explain("1")
    out = capsys.readouterr().out
    assert rc == 0
    assert "Request #1" in out
    assert "claude-opus-4" in out
    # no explicit drop reason stored -> honest unknown (Constitution §5.3)
    assert "unknown" in out.lower()
    assert "no compression applied" in out.lower()


def test_explain_request_with_cache_and_compression(patched_db, capsys):
    rc = explain_mod.run_explain("2")
    out = capsys.readouterr().out
    assert rc == 0
    assert "Request #2" in out
    assert "compressed_tokens=120" in out
    assert "Cache" in out


def test_explain_missing_request(patched_db, capsys):
    rc = explain_mod.run_explain("999")
    out = capsys.readouterr().out
    assert rc == 1
    assert "No request found" in out


def test_explain_non_numeric_id(patched_db, capsys):
    rc = explain_mod.run_explain("abc")
    out = capsys.readouterr().out
    assert rc == 2
    assert "numeric request id" in out.lower()


def test_explain_no_arg_shows_value_tier_notes(capsys):
    rc = explain_mod.run_explain(explain_mod.NO_ARG)
    out = capsys.readouterr().out
    assert rc == 0
    assert "confirmed" in out
    assert "estimated" in out
    assert "unpriced" in out
