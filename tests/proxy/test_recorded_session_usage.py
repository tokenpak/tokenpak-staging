# SPDX-License-Identifier: Apache-2.0
"""A rejected request must stay visible without hiding measured usage."""

from datetime import datetime, timezone

import pytest

from tests.proxy.test_session_forecast_state import _config, _create_ledger, _fresh_rates
from tokenpak.core.contracts.session_economics import (
    SessionEconomics,
    SessionEconomicsContractError,
    ValueState,
)
from tokenpak.core.contracts.session_economics_renderer import render_block
from tokenpak.proxy.session_forecast import _build_session_economics
from tokenpak.status.display import render
from tokenpak.status.snapshot import StatusSnapshot


def _replay(tmp_path, bad):
    db = _create_ledger(
        tmp_path,
        [
            {"provider_usage_ref": "rejected", "model": "other-model", **bad},
            {"provider_usage_ref": "ok", "timestamp": "2026-08-10T12:01:00Z"},
        ],
    )
    return _build_session_economics(
        "session-golden",
        monitor_db_path=str(db),
        now=datetime(2026, 8, 10, 12, 3, tzinfo=timezone.utc),
        spend_guard_config=_config(),
        rate_provenance=_fresh_rates(),
    )


def test_429_preserves_full_denominator_and_exposes_only_observed_subtotal(tmp_path):
    data = _replay(
        tmp_path,
        {
            "status_code": 429,
            "provider_usage_source": "unavailable",
            "provider_input_tokens": None,
            "provider_output_tokens": None,
            "provider_cache_read_tokens": None,
            "provider_cache_creation_tokens": None,
            "cost_basis": "non_success_cost_unmeasured",
        },
    )
    assert data.session.turns_observed == 2
    assert data.session.model.id == "mixed"
    assert data.facts.input_tokens.state is ValueState.UNAVAILABLE
    assert data.facts.cost_usd.state is ValueState.UNAVAILABLE
    assert data.state.burn_tokens_per_turn.state is ValueState.UNAVAILABLE
    assert data.forecast.status.value == "unavailable"
    recorded = data.recorded_usage
    assert (recorded.requests_observed, recorded.requests_total, recorded.failed_requests) == (
        1,
        2,
        1,
    )
    assert recorded.facts.input_tokens.value == 85
    assert recorded.facts.output_tokens.value == 20
    assert recorded.facts.cache_read_tokens.value == 10
    assert recorded.facts.cache_write_tokens.value == 5
    assert recorded.facts.cost_usd.value == pytest.approx(0.01)
    assert recorded.facts.cost_usd.state is ValueState.ESTIMATED
    assert SessionEconomics.from_json(data.to_json()) == data
    assert "subtotal; full-session totals incomplete" in render_block(data)
    assert "failed requests: 1" in render_block(data)
    assert "usage 1/2" in render(StatusSnapshot("session-golden", 0, "proxy", data), 80)


@pytest.mark.parametrize(
    "bad",
    [
        {"provider_output_tokens": None},
        {"provider_cache_read_tokens": -1},
        {"provider_usage_source": "estimated"},
    ],
)
def test_incomplete_or_estimated_usage_is_not_promoted_into_subtotal(tmp_path, bad):
    data = _replay(tmp_path, bad)
    assert data.recorded_usage.requests_observed == 1
    assert data.recorded_usage.requests_total == 2
    assert data.recorded_usage.facts.output_tokens.value == 20


def test_failed_response_with_real_usage_remains_in_observed_subtotal(tmp_path):
    data = _replay(tmp_path, {"status_code": 500})
    assert data.recorded_usage.requests_observed == 2
    assert data.recorded_usage.failed_requests == 1
    assert data.recorded_usage.facts.output_tokens.value == 40


def test_recorded_coverage_rejects_malformed_counts_and_false_denominator(tmp_path):
    data = _replay(tmp_path, {"provider_usage_source": "unavailable"}).to_dict()
    for key, value in [
        ("requests_observed", True),
        ("requests_observed", 3),
        ("requests_total", 3),
        ("failed_requests", -1),
    ]:
        changed = {**data, "recorded_usage": {**data["recorded_usage"], key: value}}
        with pytest.raises(SessionEconomicsContractError):
            SessionEconomics.from_dict(changed)
    del data["recorded_usage"]
    assert SessionEconomics.from_dict(data).recorded_usage is None
