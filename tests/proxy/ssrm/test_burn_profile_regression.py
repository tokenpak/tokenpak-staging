"""Regression test for the 2026-05-13 spend-spike pattern.

Per the SSRM Phase 1 packet §10 pass criteria:

> 2026-05-13 burn-profile regression test: synthetic spike (30 requests,
> input ~11K + cache_read 120-200K) records `decision` ≥
> `compress_or_recap` by request ≤10 in the audit log. (Proves SSRM
> *would* have caught the original incident at instrumentation-quality.)

Phase 1 still does not BLOCK these requests — but the audit row's
``decision`` column should show that SSRM recognized the pattern.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from tokenpak.proxy.ssrm.contracts import (
    ACTION_BLOCK,
    ACTION_COMPRESS,
    ACTION_QUARANTINE,
    ACTION_ROTATE,
    ACTION_SEVERITY,
    ACTION_WARN,
)
from tokenpak.proxy.ssrm import decide


def _seed_monitor_history(monitor_db_path: str, session_id: str, n: int) -> None:
    """Pre-populate monitor.db with the 2026-05-13 profile: input ~11K,
    cache_read 120-200K per request."""
    con = sqlite3.connect(monitor_db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL, model TEXT NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        session_id TEXT
    )""")
    base = 120_000
    for i in range(n):
        cr = base + (i % 8) * 10_000  # 120K-190K
        con.execute(
            "INSERT INTO requests (timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, session_id) "
            "VALUES (datetime('now'), 'claude-opus-4-7', 11000, 500, ?, 0, ?)",
            (cr, session_id),
        )
    con.commit()
    con.close()


def test_burn_profile_caught_by_request_10(tmp_ssrm_dbs, monkeypatch):
    """Simulate the 2026-05-13 spike pattern and verify SSRM recognizes it.

    Pre-load 5 monitor.db history rows so the cache_read_ratio signal
    has aggregate to read from, then issue a fresh request. SSRM should
    classify the request as at least ``compress_or_recap`` (severity 2)
    because:
      - cache_read aggregate ≈ 700K against input aggregate ≈ 55K → ratio ~13x
      - cache_read_amplification_act threshold = 10.0 → triggers compress
    """
    session = "burn-regression"
    # Seed history so cache_read_ratio computes against real prior rows
    _seed_monitor_history(tmp_ssrm_dbs["monitor_db"], session, 5)
    # Override the default monitor.db location SSRM reads from. Easiest
    # is to monkey-patch the helper module's reads — but our policy
    # module reads from default ~/.tokenpak/monitor.db. We need to point
    # SSRM at the tmp monitor.db. We do this via compute_signals'
    # explicit monitor_db_path arg by going through compute_signals
    # directly rather than the top-level decide(). For the regression
    # we additionally exercise the policy classifier on those signals.
    from tokenpak.proxy.ssrm.signals import compute_signals
    from tokenpak.proxy.ssrm.policy import _classify, DEFAULT_THRESHOLDS

    body = json.dumps({
        "messages": [{"role": "user", "content": "continue the workload " * 50}],
        "model": "claude-opus-4-7",
    }).encode()

    sigs = compute_signals(
        body,
        "claude-opus-4-7",
        session,
        headers={"X-Tokenpak-Agent": "suki"},
        monitor_db_path=tmp_ssrm_dbs["monitor_db"],
        state_db_path=tmp_ssrm_dbs["state_db"],
        ledger_path=tmp_ssrm_dbs["ledger_db"],
    )
    # The synthetic profile should produce a high cache_read_ratio
    assert sigs.cache_read_ratio >= 5.0, (
        f"expected cache_read_ratio >= 5 to trigger SSRM, got {sigs.cache_read_ratio}"
    )

    action, reason = _classify(sigs, DEFAULT_THRESHOLDS)
    # Must be at least compress_or_recap severity
    assert ACTION_SEVERITY[action] >= ACTION_SEVERITY[ACTION_COMPRESS], (
        f"expected action severity >= compress, got {action} (severity "
        f"{ACTION_SEVERITY[action]})  reason={reason}"
    )


def test_burn_profile_end_to_end_via_decide(tmp_ssrm_dbs, monkeypatch):
    """Same regression but invoked through decide()  — proves the public
    surface catches the pattern when SSRM is enabled. Audit row must
    show the recognition."""
    session = "burn-regression-e2e"
    _seed_monitor_history(tmp_ssrm_dbs["monitor_db"], session, 10)
    # Override the monitor.db path that signals.compute_signals reads
    # via patching the default-argument resolution. The signals module
    # reads monitor_db_path with a default of ~/.tokenpak/monitor.db, so
    # we patch the policy.decide config to inject our path.
    # Simpler approach: monkey-patch compute_signals to pass our path.
    import tokenpak.proxy.ssrm.signals as _sig_mod
    real_cs = _sig_mod.compute_signals
    def _cs(body, model, session_id, headers=None, **kw):
        kw.setdefault("monitor_db_path", tmp_ssrm_dbs["monitor_db"])
        kw.setdefault("state_db_path", tmp_ssrm_dbs["state_db"])
        kw.setdefault("ledger_path", tmp_ssrm_dbs["ledger_db"])
        return real_cs(body, model, session_id, headers, **kw)
    monkeypatch.setattr(_sig_mod, "compute_signals", _cs)
    import tokenpak.proxy.ssrm.policy as _policy_mod
    monkeypatch.setattr(_policy_mod, "compute_signals", _cs)

    body = json.dumps({
        "messages": [{"role": "user", "content": "another chunk of work " * 50}],
        "model": "claude-opus-4-7",
    }).encode()
    d = decide(body, "claude-opus-4-7", session, {"X-Tokenpak-Agent": "suki"})
    # Must be at least compress_or_recap severity (advisory only)
    assert ACTION_SEVERITY[d.action] >= ACTION_SEVERITY[ACTION_COMPRESS], (
        f"expected severity >= compress, got {d.action}  reason={d.reason}"
    )
    assert d.advisory_only is True
    # Audit row landed
    con = sqlite3.connect(tmp_ssrm_dbs["audit_db"])
    rows = list(con.execute("SELECT decision FROM ssrm_decisions"))
    assert len(rows) >= 1
    assert ACTION_SEVERITY[rows[-1][0]] >= ACTION_SEVERITY[ACTION_COMPRESS]
