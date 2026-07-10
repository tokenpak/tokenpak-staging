"""Unit tests for tokenpak.proxy.ssrm.audit — separate DB + stats."""

from __future__ import annotations

import json
import os
import sqlite3

from tokenpak.proxy.ssrm.audit import (
    explain,
    record_decision,
    summary_stats,
    tail,
)
from tokenpak.proxy.ssrm.contracts import (
    ACTION_COMPRESS,
    ACTION_CONTINUE,
    ACTION_WARN,
    Decision,
    Signals,
)


def _make_decision(action: str, session_id: str, eff_ctx: float = 50.0) -> Decision:
    return Decision(
        action=action,
        signals=Signals(
            effective_context_pct=eff_ctx,
            cache_read_ratio=2.0,
            relevance_drift_score=0.9,
            prompt_fingerprint_repeat_count=1,
            progress_signal="neutral",
            session_id=session_id,
            agent_id="sue",
            model="claude-opus-4-7",
        ),
        reason=f"test-fixture-{action}",
        advisory_only=True,
    )


def test_record_decision_writes_to_separate_audit_db(tmp_ssrm_dbs):
    """Decisions land in ssrm_audit.db, NOT in monitor.db."""
    monitor_db = tmp_ssrm_dbs["monitor_db"]
    audit_db = tmp_ssrm_dbs["audit_db"]
    d = _make_decision(ACTION_WARN, "sess-1")
    rowid = record_decision(d, audit_db_path=audit_db)
    assert rowid is not None
    # Check audit DB has the row
    con_a = sqlite3.connect(audit_db)
    rows = list(con_a.execute("SELECT decision, session_id, advisory_only FROM ssrm_decisions"))
    assert len(rows) == 1
    assert rows[0][0] == ACTION_WARN
    assert rows[0][1] == "sess-1"
    assert rows[0][2] == 1
    # Confirm the audit DB and monitor DB are different files
    assert audit_db != monitor_db


def test_tail_returns_recent_decisions(tmp_ssrm_dbs):
    audit_db = tmp_ssrm_dbs["audit_db"]
    for i, action in enumerate([ACTION_CONTINUE, ACTION_WARN, ACTION_COMPRESS]):
        record_decision(_make_decision(action, f"sess-{i}"), audit_db_path=audit_db)
    rows = tail(audit_db_path=audit_db, limit=10)
    assert len(rows) == 3
    # Most recent first
    assert rows[0]["decision"] == ACTION_COMPRESS


def test_tail_filter_by_session(tmp_ssrm_dbs):
    audit_db = tmp_ssrm_dbs["audit_db"]
    record_decision(_make_decision(ACTION_WARN, "sess-A"), audit_db_path=audit_db)
    record_decision(_make_decision(ACTION_COMPRESS, "sess-B"), audit_db_path=audit_db)
    rows = tail(audit_db_path=audit_db, session_id="sess-A")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-A"


def test_explain_returns_signals_json(tmp_ssrm_dbs):
    audit_db = tmp_ssrm_dbs["audit_db"]
    rowid = record_decision(_make_decision(ACTION_WARN, "sess-explain"), audit_db_path=audit_db)
    expl = explain(rowid, audit_db_path=audit_db)
    assert expl is not None
    sig = json.loads(expl["signals_json"])
    assert sig["session_id"] == "sess-explain"
    assert sig["effective_context_pct"] == 50.0


def test_summary_stats_counts_by_action(tmp_ssrm_dbs):
    audit_db = tmp_ssrm_dbs["audit_db"]
    for action in [ACTION_CONTINUE, ACTION_CONTINUE, ACTION_WARN]:
        record_decision(_make_decision(action, "sess-s"), audit_db_path=audit_db)
    stats = summary_stats(audit_db_path=audit_db)
    assert stats["total_decisions"] == 3
    assert stats["by_action"][ACTION_CONTINUE] == 2
    assert stats["by_action"][ACTION_WARN] == 1
