# SPDX-License-Identifier: Apache-2.0
"""Per-request ``skip_reason`` persistence + ``status --explain`` rendering.

Covers the additive monitor.db ``skip_reason`` column and that
``tokenpak status --explain <req_id>`` renders a recorded reason rather than the
``unknown`` fallback. The caller-side threading of the in-flight optimization
``StageTrace.skip_reason`` into ``Monitor.log()`` is intentionally NOT exercised
here — it is a separate, larger step (see the design note in the packet); this
suite validates the persistence substrate + render path the column unblocks.

A module-scoped fixture instantiates ``Monitor`` once (it carries a heavyweight
cold-start: migration framework + background writer), then each assertion reads
cheaply — keeping the suite well under the per-test timeout.
"""
import inspect
import sqlite3

import pytest

from tokenpak.cli.commands import explain
from tokenpak.proxy.monitor import Monitor


def _insert(db, **over):
    row = {
        "timestamp": "2026-06-04T00:00:00",
        "model": "claude-opus-4-8",
        "request_type": "chat",
        "input_tokens": 100,
        "output_tokens": 10,
        "estimated_cost": 0.01,
        "status_code": 200,
        "skip_reason": "",
    }
    row.update(over)
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        f"INSERT INTO requests ({cols}) VALUES ({qs})", tuple(row.values())
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


@pytest.fixture(scope="module")
def built_db(tmp_path_factory):
    """Build the monitor.db schema once via the real ``Monitor`` init."""
    db = tmp_path_factory.mktemp("monitor") / "monitor.db"
    Monitor(str(db))  # runs _init_db → creates requests schema incl. skip_reason
    return {
        "path": db,
        "rid_reason": _insert(db, skip_reason="route-unknown"),
        "rid_empty": _insert(db, skip_reason=""),
    }


def test_skip_reason_column_created_by_init_db(built_db):
    conn = sqlite3.connect(str(built_db["path"]))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    conn.close()
    assert "skip_reason" in cols


def test_monitor_log_accepts_skip_reason_kwarg():
    sig = inspect.signature(Monitor.log)
    assert "skip_reason" in sig.parameters
    assert sig.parameters["skip_reason"].default == ""


def test_explain_renders_recorded_skip_reason(built_db, monkeypatch, capsys):
    monkeypatch.setattr(
        "tokenpak._paths.monitor_db", lambda mode="read": built_db["path"]
    )
    rc = explain.explain_request(built_db["rid_reason"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "route-unknown" in out
    assert "unknown (per-request" not in out  # not the fallback


def test_explain_renders_unknown_when_skip_reason_empty(built_db, monkeypatch, capsys):
    monkeypatch.setattr(
        "tokenpak._paths.monitor_db", lambda mode="read": built_db["path"]
    )
    rc = explain.explain_request(built_db["rid_empty"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out.lower()
