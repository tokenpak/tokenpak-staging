"""Unit tests for tokenpak.proxy.ssrm.policy — rule engine + tie-breaks."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tokenpak.proxy.ssrm.contracts import (
    ACTION_BLOCK,
    ACTION_COMPRESS,
    ACTION_CONTINUE,
    ACTION_QUARANTINE,
    ACTION_SEVERITY,
    ACTION_WARN,
    Signals,
)
from tokenpak.proxy.ssrm.policy import (
    DEFAULT_THRESHOLDS,
    _classify,
    decide,
)


def test_classify_default_signals_returns_continue():
    sigs = Signals()
    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_CONTINUE


def test_classify_warn_when_effective_context_70():
    sigs = Signals(effective_context_pct=72.0)
    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_WARN


def test_classify_compress_when_effective_context_80():
    sigs = Signals(effective_context_pct=82.0)
    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_COMPRESS


def test_classify_block_when_effective_context_85():
    sigs = Signals(effective_context_pct=87.0)
    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_BLOCK


def test_classify_block_when_effective_context_90_hard():
    sigs = Signals(effective_context_pct=92.0)
    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_BLOCK


def test_classify_quarantine_overrides_other_rules():
    """no_progress + fingerprint_repeat >=3 wins over compress/warn rules
    via highest-severity tie-break."""
    sigs = Signals(
        progress_signal="no_progress",
        prompt_fingerprint_repeat_count=4,
        effective_context_pct=72.0,  # would be warn alone
    )
    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_QUARANTINE


def test_classify_cache_read_amplification_triggers_compress():
    sigs = Signals(cache_read_ratio=12.0, effective_context_pct=30.0)
    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_COMPRESS


def test_classify_drift_on_aged_session():
    sigs = Signals(
        relevance_drift_score=0.3,
        session_age_turns=25,
        effective_context_pct=40.0,
    )
    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_COMPRESS


def test_classify_projected_next_warn():
    sigs = Signals(
        context_pct_projected_next=85.0,
        effective_context_pct=40.0,
    )
    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_WARN


def test_classify_severity_tie_break():
    """When multiple rules fire, the highest-severity action wins."""
    # Three rules fire simultaneously:
    #   - eff_ctx=92 → block (severity 3)
    #   - cache_read_ratio=12 → compress (severity 2)
    #   - no_progress + fp_repeat=5 → quarantine (severity 5)
    # Quarantine should win.
    sigs = Signals(
        effective_context_pct=92.0,
        cache_read_ratio=12.0,
        progress_signal="no_progress",
        prompt_fingerprint_repeat_count=5,
    )
    action, _ = _classify(sigs, DEFAULT_THRESHOLDS)
    assert action == ACTION_QUARANTINE


def test_decide_when_disabled_is_noop(monkeypatch):
    """ssrm.enabled=false → decide returns continue with no signals computed
    and no audit row written."""
    monkeypatch.delenv("TOKENPAK_SSRM_ENABLED", raising=False)
    from tokenpak.proxy.ssrm.policy import reset_config_cache
    reset_config_cache()
    d = decide(
        b'{"messages":[{"role":"user","content":"x"}],"model":"claude-opus-4-7"}',
        "claude-opus-4-7",
        "sess-noop",
        {},
    )
    assert d.action == ACTION_CONTINUE
    assert d.reason == "ssrm.enabled=false"


def test_decide_writes_audit_row(tmp_ssrm_dbs):
    """When SSRM is enabled, decide() records a row in ssrm_audit.db."""
    body = json.dumps({
        "messages": [{"role": "user", "content": "small request"}],
        "model": "claude-opus-4-7",
    }).encode()
    d = decide(body, "claude-opus-4-7", "sess-audit", {"X-Tokenpak-Agent": "sue"})
    assert d.action == ACTION_CONTINUE  # benign request
    assert d.advisory_only is True
    # Audit row landed
    con = sqlite3.connect(tmp_ssrm_dbs["audit_db"])
    rows = list(con.execute("SELECT decision, advisory_only, signals_json FROM ssrm_decisions"))
    assert len(rows) == 1
    assert rows[0][0] == ACTION_CONTINUE
    assert rows[0][1] == 1  # advisory_only
    sig = json.loads(rows[0][2])
    assert "effective_context_pct" in sig
    assert "session_id" in sig
