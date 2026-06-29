from __future__ import annotations

import json

import pytest

from tokenpak.prove.adapter import ArmResult, TurnResult
from tokenpak.prove.reporter import _PROVE_ESTIMATE_NOTE, format_matrix_report, save_result


def _arm(
    name: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cost_usd: float,
    latency_s: float = 1.0,
) -> ArmResult:
    result = ArmResult(
        arm_name=name,
        platform="api",
        provider="anthropic",
        model="claude-sonnet-4-6",
        via_tokenpak=name != "baseline",
    )
    result.turns.append(
        TurnResult(
            turn_number=1,
            label="known fixture",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd,
            latency_s=latency_s,
        )
    )
    result.finalize()
    return result


def test_format_matrix_report_value_math_and_cheaper_delta_direction():
    baseline = _arm(
        "baseline",
        input_tokens=1_000,
        output_tokens=200,
        cost_usd=0.0200,
        latency_s=2.0,
    )
    tokenpak = _arm(
        "tokenpak",
        input_tokens=600,
        output_tokens=200,
        cache_read_tokens=250,
        cost_usd=0.0120,
        latency_s=1.5,
    )

    report = format_matrix_report([baseline, tokenpak], "known-scenario", "prf_known")

    assert "Total cost" in report
    assert "-40.0%" in report
    assert "tokenpak: 400 fewer input tokens (40.0% compression)" in report
    assert "tokenpak: $0.0080 saved (40.0% cheaper)" in report
    assert "tokenpak: 250 cache-read tokens" in report
    assert f"Note: {_PROVE_ESTIMATE_NOTE}" in report


def test_save_result_writes_expected_schema_and_summary(tmp_path):
    baseline = _arm(
        "baseline",
        input_tokens=1_000,
        output_tokens=200,
        cost_usd=0.0200,
    )
    tokenpak = _arm(
        "tokenpak",
        input_tokens=600,
        output_tokens=200,
        cache_read_tokens=250,
        cost_usd=0.0120,
    )

    path = save_result([baseline, tokenpak], "known-scenario", "prf_known", tmp_path)

    payload = json.loads(path.read_text())
    assert payload["proof_id"] == "prf_known"
    assert payload["scenario"] == "known-scenario"
    assert len(payload["arms"]) == 2
    assert payload["arms"][0]["arm_name"] == "baseline"
    assert payload["arms"][1]["total_cache_read_tokens"] == 250
    assert payload["arms"][1]["turns"][0]["cost_usd"] == pytest.approx(0.012)
    assert payload["summary"]["baseline"] == "baseline"
    assert payload["summary"]["best_cost"] == pytest.approx(0.012)
    assert payload["summary"]["best_cost_arm"] == "tokenpak"
    assert payload["summary"]["estimate_note"] == _PROVE_ESTIMATE_NOTE
