"""Deliberation Receipt contract tests (deliberation policy §6 / §8 — OSS shape)."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from tokenpak.orchestration.deliberation import (
    DeliberationReceipt,
    NodeOutput,
    PartialResult,
)


def _node(**overrides):
    base = dict(
        node_id="n1",
        model_card="anthropic/claude-fable-5",
        verdict="approve",
        reason_codes=[],
        risk_flags=[],
        confidence="high",
        summary="looks correct",
    )
    base.update(overrides)
    return NodeOutput(**base)


def _receipt(**overrides):
    base = dict(
        receipt_id="dr_test",
        correlation_id="dlb_test",
        decision_ref="decision:example-001",
        risk_class="standards-change",
        created_at="2026-06-12T00:00:00+00:00",
        engine_version="0.1.0-minimum",
    )
    base.update(overrides)
    return DeliberationReceipt(**base)


class TestNodeOutput:
    def test_reason_codes_optional_only_for_approve(self):
        assert _node(verdict="approve", reason_codes=[]).verdict == "approve"
        with pytest.raises(ValidationError, match="reason_codes"):
            _node(verdict="revise", reason_codes=[])

    def test_non_approve_with_reason_codes_ok(self):
        node = _node(verdict="stop", reason_codes=["pak.conflict"])
        assert node.reason_codes == ["pak.conflict"]

    def test_verdict_ladder_enum_enforced(self):
        with pytest.raises(ValidationError):
            _node(verdict="maybe")


class TestReceiptOSSShape:
    def test_pro_numeric_fields_rejected_at_top_level(self):
        # §8.3: numeric scoring fields are never OSS receipt fields.
        for field in (
            "judge_score",
            "model_win_label",
            "calibration_delta",
            "downstream_success_label",
        ):
            with pytest.raises(ValidationError):
                _receipt(**{field: 1})

    def test_pro_fields_allowed_under_paid_extensions_envelope(self):
        receipt = _receipt(
            extensions={
                "tokenpak_paid": {
                    "judge_score": 0.87,
                    "model_win_label": "challenger",
                }
            }
        )
        assert receipt.extensions["tokenpak_paid"]["judge_score"] == 0.87

    def test_default_mode_is_advisory(self):
        # §15.2 shadow-before-gate: gating is never a default.
        assert _receipt().mode == "advisory"

    def test_non_complete_requires_partial_shape(self):
        with pytest.raises(ValidationError, match="partial-result"):
            _receipt(result_state="partial_budget_abort")
        receipt = _receipt(
            result_state="partial_budget_abort",
            partial=PartialResult(spend_guard_reason="rolling cap reached"),
        )
        assert receipt.partial.spend_guard_reason == "rolling cap reached"

    def test_unknown_keys_fail_loud(self):
        with pytest.raises(ValidationError):
            _receipt(raw_chain_of_thought="never")
