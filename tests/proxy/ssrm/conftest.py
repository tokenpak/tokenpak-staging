"""Shared SSRM test fixtures — point all DBs at temp paths for isolation."""

from __future__ import annotations

import os
import tempfile

import pytest

from tokenpak.proxy.ssrm import state as ssrm_state
from tokenpak.proxy.ssrm.policy import reset_config_cache


@pytest.fixture
def tmp_ssrm_dbs(tmp_path, monkeypatch):
    """Set ssrm env vars to enable + redirect DBs to tmp_path.

    Returns a dict of paths the test can inspect afterwards.
    """
    audit_db = tmp_path / "ssrm_audit.db"
    state_db = tmp_path / "ssrm_state.db"
    monitor_db = tmp_path / "monitor.db"
    ledger_db = tmp_path / "no_progress_ledger.db"

    monkeypatch.setenv("TOKENPAK_SSRM_ENABLED", "true")
    monkeypatch.setenv("TOKENPAK_SSRM_AUDIT_DB_PATH", str(audit_db))
    monkeypatch.setenv("TOKENPAK_SSRM_STATE_DB_PATH", str(state_db))

    # Reset module-level caches so the test gets fresh state.
    reset_config_cache()
    ssrm_state.reset_handles_for_testing()

    yield {
        "audit_db": str(audit_db),
        "state_db": str(state_db),
        "monitor_db": str(monitor_db),
        "ledger_db": str(ledger_db),
    }

    reset_config_cache()
    ssrm_state.reset_handles_for_testing()


@pytest.fixture
def healthy_fleet_report():
    """Governor-style fleet-healthy report fixture (text body for SSRM body input)."""
    return {
        "model": "claude-opus-4-7",
        "messages": [
            {"role": "user", "content": (
                "STEP_PREFLIGHT: ok\n"
                "STEP_PRECHECK: rework=0 review=0 open=53 waiting=15\n"
                "STEP_QA: skipped:0-in-review\n"
                "STEP_TRIAGE: skipped:empty\n"
                "STEP_QUEUE_HEALTH: trix=6/9 cali=7/10\n"
                "STEP_PROMOTIONS: scanned=0\n"
                "STEP_TRIAGE_SUE: skipped:queue-clear\n"
                "STEP_AGENT_HEALTH: trix=ok cali=ok\n"
                "STEP_COMMIT: no-changes\n"
                "STEP_TELEMETRY: ok\n"
                "NOTES: fleet healthy"
            )},
        ],
    }
