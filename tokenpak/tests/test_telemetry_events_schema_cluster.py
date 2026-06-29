"""
Regression tests for the telemetry legacy-``events`` schema cluster.

Historically the integrity/operational readers (anomaly detection, health
checks, pruning, reconciliation) queried a bare ``events`` table with a
``created_at`` column that does not exist in a freshly-initialized canonical
store. The canonical ``TelemetryDB`` keeps events in ``tp_events`` and times
them with the epoch column ``ts``; the flat token/retry/cost columns the
readers need are now wired into the runtime migration via
``migrate_tp_events`` (Architecture B).

These tests assert two things against a *fresh* canonical store:
  1. No ``OperationalError`` is raised by any reader (the original bug).
  2. Planted anomalies are still detected end-to-end (no false negatives
     introduced by the repoint).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tokenpak.telemetry.integrity.anomalies import Anomaly, AnomalyDetector
from tokenpak.telemetry.integrity.reconciliation import ReconciliationManager
from tokenpak.telemetry.operational.health import HealthChecker
from tokenpak.telemetry.operational.pruning import PruneJob, RetentionConfig
from tokenpak.telemetry.storage import TelemetryDB

# Columns the readers depend on, added by the flat event-schema migration.
_FLAT_COLUMNS = {"final_input_tokens", "retry_count", "actual_cost", "output_tokens"}


def _fresh_store(tmp_path) -> str:
    """Initialize a canonical TelemetryDB on disk and return its path."""
    db_path = str(tmp_path / "telemetry.db")
    db = TelemetryDB(db_path)
    db.close()  # release the writer; readers open their own connections
    return db_path


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _insert_event(
    db_path: str,
    *,
    trace_id: str,
    ts: float,
    model: str = "gpt-4o",
    provider: str = "openai",
    final_input_tokens: int = 0,
    actual_cost: float = 0.0,
    retry_count: int = 0,
    status: str = "ok",
) -> None:
    """Insert one row directly into the canonical ``tp_events`` table."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tp_events "
            "(trace_id, request_id, event_type, ts, provider, model, status, "
            " final_input_tokens, actual_cost, retry_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace_id,
                trace_id,  # request_id (PK component) — unique per trace here
                "request",
                ts,
                provider,
                model,
                status,
                final_input_tokens,
                actual_cost,
                retry_count,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema wiring (Architecture B)
# ---------------------------------------------------------------------------
def test_fresh_store_carries_flat_event_columns(tmp_path):
    """A freshly migrated store exposes the flat columns the readers need."""
    db_path = _fresh_store(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tp_events)")}
    finally:
        conn.close()
    assert _FLAT_COLUMNS <= cols, f"missing flat columns: {_FLAT_COLUMNS - cols}"


def test_re_migration_is_idempotent(tmp_path):
    """Re-opening the same store re-runs the migration without error."""
    db_path = _fresh_store(tmp_path)
    # Second init applies the same migrations again (existing DB path).
    db = TelemetryDB(db_path)
    try:
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(tp_events)")}
    finally:
        db.close()
    assert _FLAT_COLUMNS <= cols


# ---------------------------------------------------------------------------
# Anomaly detection — no OperationalError on a fresh store
# ---------------------------------------------------------------------------
def test_anomaly_detectors_clean_on_fresh_store(tmp_path):
    db_path = _fresh_store(tmp_path)
    det = AnomalyDetector(db_path)
    # None of these should raise; all return None on an empty store.
    assert det.detect_token_spikes("gpt-4o", current_tokens=10) is None
    assert det.detect_cost_spikes(current_cost=10.0) is None
    assert det.detect_retry_surge() is None
    assert det.detect_error_surge() is None


def test_planted_token_spike_detected(tmp_path):
    db_path = _fresh_store(tmp_path)
    now = _now()
    # Baseline: three modest rows in the last few days -> avg 100 tokens.
    for i in range(3):
        _insert_event(
            db_path,
            trace_id=f"base-{i}",
            ts=now - (i + 1) * 86400,
            final_input_tokens=100,
        )
    det = AnomalyDetector(db_path)
    anomaly = det.detect_token_spikes("gpt-4o", current_tokens=5000)
    assert isinstance(anomaly, Anomaly)
    assert anomaly.anomaly_type == "token_spike"
    assert anomaly.baseline == pytest.approx(100.0)


def test_planted_cost_spike_detected(tmp_path):
    db_path = _fresh_store(tmp_path)
    # Yesterday at noon UTC so the row's date matches the detector's window.
    yday = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    for i in range(3):
        _insert_event(
            db_path,
            trace_id=f"cost-{i}",
            ts=yday.timestamp(),
            actual_cost=1.0,
        )
    det = AnomalyDetector(db_path)
    anomaly = det.detect_cost_spikes(current_cost=50.0, baseline_days=1)
    assert isinstance(anomaly, Anomaly)
    assert anomaly.anomaly_type == "cost_spike"


def test_planted_retry_surge_detected(tmp_path):
    db_path = _fresh_store(tmp_path)
    now = _now()
    # 5 events in the last 60 min, 3 of them retried -> 60% retry rate.
    for i in range(5):
        _insert_event(
            db_path,
            trace_id=f"retry-{i}",
            ts=now - 60,
            retry_count=1 if i < 3 else 0,
        )
    det = AnomalyDetector(db_path)
    anomaly = det.detect_retry_surge(time_window_minutes=60, threshold_pct=20.0)
    assert isinstance(anomaly, Anomaly)
    assert anomaly.anomaly_type == "retry_surge"


def test_planted_error_surge_detected(tmp_path):
    db_path = _fresh_store(tmp_path)
    now = _now()
    # 5 events in the last 60 min, 2 errored -> 40% error rate.
    for i in range(5):
        _insert_event(
            db_path,
            trace_id=f"err-{i}",
            ts=now - 60,
            status="error" if i < 2 else "ok",
        )
    det = AnomalyDetector(db_path)
    anomaly = det.detect_error_surge(time_window_minutes=60, threshold_pct=10.0)
    assert isinstance(anomaly, Anomaly)
    assert anomaly.anomaly_type == "error_surge"


# ---------------------------------------------------------------------------
# Health checks — no OperationalError on a fresh store
# ---------------------------------------------------------------------------
def test_health_checks_clean_on_fresh_store(tmp_path):
    db_path = _fresh_store(tmp_path)
    _insert_event(db_path, trace_id="h1", ts=_now(), final_input_tokens=10)

    checker = HealthChecker(db_path, version="test")

    status, err = checker.check_database()
    assert status == "ok", err

    status, err = checker.check_rollup_job()
    assert status in ("ok", "degraded"), err  # never "error"

    stats = checker.get_stats()
    assert "error" not in stats
    assert stats["events_total"] == 1
    assert stats["last_ingest_at"] is not None  # ISO string, not epoch float

    health = checker.health_check()
    assert health.status in ("healthy", "degraded")


# ---------------------------------------------------------------------------
# Pruning — no OperationalError, deletes only old rows
# ---------------------------------------------------------------------------
def test_pruning_clean_on_fresh_store(tmp_path):
    db_path = _fresh_store(tmp_path)
    now = _now()
    _insert_event(db_path, trace_id="old", ts=now - 100 * 86400)  # 100 days old
    _insert_event(db_path, trace_id="new", ts=now)

    job = PruneJob(db_path, RetentionConfig(events_days=90, rollups_days=365))

    deleted_events = job.prune_old_events(90)
    assert deleted_events == 1

    # Rollups query must not error even with no rollup rows present.
    assert job.prune_old_rollups(365) == 0

    conn = sqlite3.connect(db_path)
    try:
        remaining = conn.execute("SELECT COUNT(*) FROM tp_events").fetchone()[0]
    finally:
        conn.close()
    assert remaining == 1

    result = job.run_prune()
    assert result.success is True


# ---------------------------------------------------------------------------
# Reconciliation — no OperationalError, sums proxy tokens from tp_events
# ---------------------------------------------------------------------------
def test_reconciliation_clean_and_sums_tokens(tmp_path):
    db_path = _fresh_store(tmp_path)
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    period_start = today.date().isoformat()
    _insert_event(
        db_path,
        trace_id="recon-1",
        ts=today.timestamp(),
        provider="openai",
        model="gpt-4o",
        final_input_tokens=500,
    )

    mgr = ReconciliationManager(db_path)
    imported = mgr.import_billing_data(
        [
            {
                "period_start": period_start,
                "provider": "openai",
                "model": "gpt-4o",
                "billed_tokens": 500,
            }
        ]
    )
    assert imported == 1

    status = mgr.get_reconciliation_status()
    assert status["total_records"] == 1
    assert status["matched_records"] == 1  # proxy 500 == billed 500 -> matched
