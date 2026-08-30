# SPDX-License-Identifier: Apache-2.0
"""Time-remaining-band engine unit tests: honest states, gate compliance, fail-open.

``build_calibrated_time_forecast`` is a pure lookup against
``_PUBLISHED_TIME_PRIOR`` — it never fits a model live (see the module
docstring). These tests exercise its branching directly by monkeypatching
that table, plus the outer default-off gate in ``session_forecast.py`` that
must short-circuit to ``unavailable`` regardless of what the inner engine
would otherwise report.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tokenpak.core.contracts.session_economics import (
    DriftState,
    TimeForecastStatus,
    TimeForecastStreamMode,
)
from tokenpak.proxy import time_forecast_calibration as tf_cal

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _evidence(
    *,
    full_confidence: bool,
    history_n: int = 40,
    observed_coverage_50: float = 52.0,
    drift_state: DriftState = DriftState.STABLE,
    band50: dict[int, tuple[float, float]] | None = None,
    band90: dict[int, float] | None = None,
) -> tf_cal.PublishedTimeCellEvidence:
    return tf_cal.PublishedTimeCellEvidence(
        prior_version="time-prior-2026-09-01",
        band50_y_by_k=band50 if band50 is not None else {1: (-0.2, 0.6), 4: (-0.1, 0.9)},
        band90_hi_y_by_k=band90 if band90 is not None else {1: 1.2, 4: 1.8},
        coverage_method="walk-forward-split-conformal",
        observed_coverage_50=observed_coverage_50,
        history_n=history_n,
        drift_state=drift_state,
        full_confidence=full_confidence,
    )


def _build(
    *,
    turn_index: int = 4,
    elapsed_ms: float = 60_000.0,
    model: str = "model-a",
    effort: str = "high",
    stream_mode: TimeForecastStreamMode = TimeForecastStreamMode.STREAMING,
    session_id: str = "active",
):
    return tf_cal.build_calibrated_time_forecast(
        monitor_db_path="ignored",
        now=NOW,
        session_id=session_id,
        model=model,
        effort=effort,
        stream_mode=stream_mode,
        turn_index=turn_index,
        elapsed_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# The gate mechanism itself: an empty table is what ships
# ---------------------------------------------------------------------------


def test_shipped_published_prior_table_is_empty() -> None:
    """The literal shipped state: no cell has been reviewed/published yet."""
    assert tf_cal._PUBLISHED_TIME_PRIOR == {}


def test_cold_cell_is_insufficient_data_not_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tf_cal, "_PUBLISHED_TIME_PRIOR", {})
    forecast = _build()
    assert forecast.status is TimeForecastStatus.INSUFFICIENT_DATA
    assert forecast.remaining_time_likely_50_ms is None
    assert forecast.remaining_time_ceiling_90_ms is None


# ---------------------------------------------------------------------------
# Never-a-point-estimate / honest-null branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("turn_index,elapsed_ms", [(0, 60_000.0), (1, 0.0), (-1, 60_000.0)])
def test_no_elapsed_signal_yet_is_unknown(
    monkeypatch: pytest.MonkeyPatch, turn_index: int, elapsed_ms: float
) -> None:
    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    monkeypatch.setattr(tf_cal, "_PUBLISHED_TIME_PRIOR", {key: _evidence(full_confidence=True)})
    forecast = _build(turn_index=turn_index, elapsed_ms=elapsed_ms)
    assert forecast.status is TimeForecastStatus.UNKNOWN
    assert forecast.remaining_time_likely_50_ms is None
    assert forecast.remaining_time_ceiling_90_ms is None


def test_published_cell_with_no_bucket_coverage_is_insufficient_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    monkeypatch.setattr(
        tf_cal,
        "_PUBLISHED_TIME_PRIOR",
        {key: _evidence(full_confidence=True, band50={}, band90={})},
    )
    forecast = _build()
    assert forecast.status is TimeForecastStatus.INSUFFICIENT_DATA


def test_different_cell_key_is_not_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A published cell for a different (model, effort, stream_mode) never leaks in."""
    other_key = tf_cal._time_cell_key("model-b", "low", TimeForecastStreamMode.NON_STREAMING)
    monkeypatch.setattr(
        tf_cal, "_PUBLISHED_TIME_PRIOR", {other_key: _evidence(full_confidence=True)}
    )
    forecast = _build(model="model-a", effort="high", stream_mode=TimeForecastStreamMode.STREAMING)
    assert forecast.status is TimeForecastStatus.INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# learning / available — both populate a band; only the gate differs
# ---------------------------------------------------------------------------


def test_learning_cell_populates_a_borrowed_band(monkeypatch: pytest.MonkeyPatch) -> None:
    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    monkeypatch.setattr(
        tf_cal, "_PUBLISHED_TIME_PRIOR", {key: _evidence(full_confidence=False, history_n=12)}
    )
    forecast = _build()
    assert forecast.status is TimeForecastStatus.LEARNING
    band = forecast.remaining_time_likely_50_ms
    ceiling = forecast.remaining_time_ceiling_90_ms
    assert band is not None and ceiling is not None
    assert band.low is not None and band.high is not None
    assert 0 <= band.low <= band.high <= ceiling.value
    # The gate receipts are all true the moment ANY evidence exists for the
    # cell — "learning" is still real, reviewed evidence, just not yet at
    # the full-confidence threshold.
    assert forecast.gate.sedr_014_landed
    assert forecast.gate.calibration_evidence_published
    assert forecast.gate.inputs_verified_timing_facts_only
    assert forecast.coverage.history_n == 12


def test_available_cell_is_fully_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    monkeypatch.setattr(
        tf_cal, "_PUBLISHED_TIME_PRIOR", {key: _evidence(full_confidence=True, history_n=48)}
    )
    forecast = _build()
    assert forecast.status is TimeForecastStatus.AVAILABLE
    assert forecast.gate.sedr_014_landed
    assert forecast.gate.calibration_evidence_published
    assert forecast.gate.inputs_verified_timing_facts_only
    assert forecast.coverage.method == "walk-forward-split-conformal"
    assert forecast.coverage.observed is not None
    assert forecast.coverage.history_n == 48
    assert forecast.basis == "timing-facts-v1"


def test_available_never_serializes_as_a_bare_point_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract's own invariant, re-asserted at the engine boundary."""
    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    monkeypatch.setattr(tf_cal, "_PUBLISHED_TIME_PRIOR", {key: _evidence(full_confidence=True)})
    payload = _build().to_dict()
    assert isinstance(payload["remaining_time_likely_50_ms"], dict)
    assert payload["remaining_time_likely_50_ms"]["low"] is not None
    assert payload["remaining_time_likely_50_ms"]["high"] is not None
    assert isinstance(payload["remaining_time_ceiling_90_ms"], dict)
    assert payload["remaining_time_ceiling_90_ms"]["value"] is not None


def test_observed_coverage_is_clamped_and_scaled_to_a_fraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    monkeypatch.setattr(
        tf_cal,
        "_PUBLISHED_TIME_PRIOR",
        {key: _evidence(full_confidence=True, observed_coverage_50=123.4)},
    )
    forecast = _build()
    # A >100% measured figure (shouldn't happen, but defends the contract's
    # own <=1 invariant on Coverage.observed) is clamped before scaling.
    assert forecast.coverage.observed == 1.0


def test_nearest_bucket_is_used_when_turn_index_falls_between_published_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    monkeypatch.setattr(
        tf_cal,
        "_PUBLISHED_TIME_PRIOR",
        {
            key: _evidence(
                full_confidence=True,
                band50={1: (-0.2, 0.6), 10: (-0.05, 0.3)},
                band90={1: 1.2, 10: 0.5},
            )
        },
    )
    # turn_index=2 is nearer bucket 1 than bucket 10.
    forecast = _build(turn_index=2, elapsed_ms=60_000.0)
    assert forecast.status is TimeForecastStatus.AVAILABLE
    # Bucket 1's wider band should dominate, not bucket 10's tighter one.
    assert forecast.remaining_time_ceiling_90_ms.value > 60_000.0


def test_deep_turn_index_beyond_kmax_shares_the_last_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    monkeypatch.setattr(tf_cal, "_PUBLISHED_TIME_PRIOR", {key: _evidence(full_confidence=True)})
    forecast = _build(turn_index=tf_cal.KMAX + 50)
    assert forecast.status is TimeForecastStatus.AVAILABLE


# ---------------------------------------------------------------------------
# Fail-open: a corrupt/failing lookup degrades to unknown, never raises
# ---------------------------------------------------------------------------


def test_corrupt_published_evidence_degrades_to_unknown_not_a_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    # band50_y_by_k has a non-numeric value: math.exp() on it raises inside
    # the try/except, which must degrade rather than propagate.
    broken = _evidence(full_confidence=True, band50={4: ("not-a-float", 0.9)})
    monkeypatch.setattr(tf_cal, "_PUBLISHED_TIME_PRIOR", {key: broken})
    forecast = _build()
    assert forecast.status is TimeForecastStatus.UNKNOWN
    assert forecast.remaining_time_likely_50_ms is None


# ---------------------------------------------------------------------------
# Outer activation gate — default-off short-circuits regardless of evidence
# ---------------------------------------------------------------------------


def test_default_off_ignores_rich_published_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``session_forecast._time_calibrated_or_fallback`` never even calls the
    engine unless the outer flag is on — the inner engine having a fully
    warmed, available cell must not leak through a default-off deployment."""
    from tokenpak.proxy import session_forecast

    key = tf_cal._time_cell_key("model-a", "high", TimeForecastStreamMode.STREAMING)
    monkeypatch.setattr(tf_cal, "_PUBLISHED_TIME_PRIOR", {key: _evidence(full_confidence=True)})
    monkeypatch.delenv(session_forecast._TIME_FORECAST_ENV_VAR, raising=False)
    monkeypatch.setattr(session_forecast, "_time_forecast_enabled", lambda: False)

    turn = type(
        "Row",
        (),
        {"stream_mode": "sse", "ttfb_ms": 100, "stream_duration_ms": 59_900},
    )()
    wrapped = type("Turn", (), {"row": turn})()
    forecast = session_forecast._time_calibrated_or_fallback(
        monitor_db_path="ignored",
        as_of=NOW,
        session_id="active",
        model="model-a",
        effort="high",
        turns=[wrapped],
    )
    assert forecast.status is TimeForecastStatus.UNAVAILABLE
    assert forecast.remaining_time_likely_50_ms is None


def test_env_var_true_values_enable_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenpak.proxy import session_forecast

    monkeypatch.setenv(session_forecast._TIME_FORECAST_ENV_VAR, "1")
    assert session_forecast._time_forecast_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no"])
def test_env_var_false_values_keep_the_gate_off(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from tokenpak.proxy import session_forecast

    monkeypatch.setenv(session_forecast._TIME_FORECAST_ENV_VAR, value)
    assert session_forecast._time_forecast_enabled() is False


def test_no_env_var_and_no_config_key_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenpak.proxy import session_forecast

    monkeypatch.delenv(session_forecast._TIME_FORECAST_ENV_VAR, raising=False)
    monkeypatch.setattr("tokenpak.core.config.load_config", lambda: {}, raising=False)
    assert session_forecast._time_forecast_enabled() is False


def test_config_key_can_enable_without_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenpak.proxy import session_forecast

    monkeypatch.delenv(session_forecast._TIME_FORECAST_ENV_VAR, raising=False)
    monkeypatch.setattr(
        "tokenpak.core.config.load_config",
        lambda: {"time_forecast_bands": {"enabled": True}},
        raising=False,
    )
    assert session_forecast._time_forecast_enabled() is True
