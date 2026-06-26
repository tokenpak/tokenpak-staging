# SPDX-License-Identifier: Apache-2.0
"""Spend/context preflight gates with receipt-backed decisions.

Covers the additions for ``p1-spend-context-preflight-gates``:

- top context contributors on the risk estimate (AC-2)
- the allow/warn/block decision projected into a Receipt-v1-ready proof (AC-3)
- the unknown-estimate case → ``warn`` instead of a silent allow (AC-4 + the
  "do not silently allow oversize requests when preflight cannot estimate"
  pitfall)
- existing allow/block/hard_block protections are not weakened (AC-5)

Covered claims: C04 (runaway request size), C07 (spend surprises),
C11 (no proof of optimization).
"""
from __future__ import annotations

import json

import pytest

from tokenpak.proxy.spend_guard import block_response
from tokenpak.proxy.spend_guard._context_window import get_model_max_context
from tokenpak.proxy.spend_guard.contracts import (
    PendingRequest,
    PreflightDecision,
    RiskEstimate,
)
from tokenpak.proxy.spend_guard.estimator import estimate
from tokenpak.proxy.spend_guard.policy import SpendGuardConfig, decide

MODEL = "claude-opus-4-7"  # 200K context in the registry
MAX_CTX = 200_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _anthropic_body(*, system: str = "", messages=None, tools=None, max_tokens=1024) -> bytes:
    body: dict = {"model": MODEL, "max_tokens": max_tokens, "messages": messages or []}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    return json.dumps(body).encode()


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": text}


def _chars_for_tokens(n: int) -> int:
    # estimator uses len(text) // 4 — pad slightly past the boundary.
    return n * 4 + 8


# ---------------------------------------------------------------------------
# AC-2: top context contributors
# ---------------------------------------------------------------------------

def test_estimate_ranks_context_contributors_biggest_first():
    body = _anthropic_body(
        system="s" * _chars_for_tokens(5_000),
        messages=[
            _msg("user", "u" * _chars_for_tokens(40_000)),  # biggest
            _msg("assistant", "a" * _chars_for_tokens(1_000)),
        ],
        tools=[{"name": "t", "description": "d" * _chars_for_tokens(2_000)}],
    )
    est = estimate(body, MODEL)
    assert est.contributors, "expected a populated contributor breakdown"
    # Ranked biggest-first.
    toks = [c["tokens"] for c in est.contributors]
    assert toks == sorted(toks, reverse=True)
    top = est.contributors[0]
    assert top["source"].startswith("message[0]:user")
    # Every entry carries a sane pct share of projected input.
    for c in est.contributors:
        assert set(c) == {"source", "tokens", "pct"}
        assert 0.0 <= c["pct"] <= 1.0
    assert est.contributors_reason is None


def test_contributors_capped_at_five():
    messages = [_msg("user", "x" * _chars_for_tokens(1_000 + i)) for i in range(12)]
    est = estimate(_anthropic_body(messages=messages), MODEL)
    assert len(est.contributors) == 5


def test_contributors_reason_when_non_messages_shape():
    # Valid JSON, but not an Anthropic /v1/messages body.
    est = estimate(json.dumps({"prompt": "hello world"}).encode(), MODEL)
    assert est.contributors == []
    assert est.contributors_reason == "non_messages_request_shape"
    assert est.estimate_unknown is False


# ---------------------------------------------------------------------------
# AC-4 + pitfall: unknown estimate → warn, never a silent allow
# ---------------------------------------------------------------------------

def test_unparseable_body_marks_estimate_unknown():
    est = estimate(b"this is not json {{{", MODEL)
    assert est.estimate_unknown is True
    assert est.unknown_reason == "unparseable_request_body"
    assert est.contributors_reason == "unparseable_request_body"


def test_empty_body_marks_estimate_unknown():
    est = estimate(b"", MODEL)
    assert est.estimate_unknown is True
    assert est.unknown_reason == "unparseable_request_body"


def test_unknown_estimate_decides_warn_not_allow():
    est = estimate(b"<<garbage>>", MODEL)
    decision = decide(est, SpendGuardConfig(), model_max_context_tokens=MAX_CTX)
    assert decision.decision == "warn"
    assert decision.reason == "estimate_unavailable"
    # The reason it's unknown is preserved for the receipt/audit trail.
    assert decision.threshold_hit == "unparseable_request_body"


def test_known_small_request_still_allows():
    # Regression guard (AC-5): a parseable, under-threshold request is allowed.
    est = estimate(_anthropic_body(messages=[_msg("user", "hi")]), MODEL)
    assert est.estimate_unknown is False
    decision = decide(est, SpendGuardConfig(), model_max_context_tokens=MAX_CTX)
    assert decision.decision == "allow"


# ---------------------------------------------------------------------------
# AC-5: protections not weakened — block / hard_block still fire
# ---------------------------------------------------------------------------

def test_oversize_request_blocks_with_contributors():
    # ~95% of a 200K context → soft block (>=90%, <100%).
    body = _anthropic_body(messages=[_msg("user", "z" * _chars_for_tokens(190_000))])
    est = estimate(body, MODEL)
    decision = decide(est, SpendGuardConfig(), model_max_context_tokens=MAX_CTX)
    assert decision.decision == "block"
    assert decision.requires_approval is True
    # The held decision still carries the contributor breakdown.
    assert decision.risk.contributors


def test_context_overflow_hard_blocks():
    # Over the 100% hard-stop ceiling → hard_block, not bypassable.
    body = _anthropic_body(messages=[_msg("user", "z" * _chars_for_tokens(205_000))])
    est = estimate(body, MODEL)
    decision = decide(est, SpendGuardConfig(), model_max_context_tokens=MAX_CTX)
    assert decision.decision == "hard_block"
    assert decision.requires_approval is False


def test_warn_band_for_mid_size_request():
    # Above the warn_tokens floor (100K) but below the 90% block line.
    body = _anthropic_body(messages=[_msg("user", "z" * _chars_for_tokens(110_000))])
    est = estimate(body, MODEL)
    decision = decide(est, SpendGuardConfig(), model_max_context_tokens=MAX_CTX)
    assert decision.decision == "warn"


# ---------------------------------------------------------------------------
# AC-3: allow/warn/block decision is attachable to Receipt v1
# ---------------------------------------------------------------------------

def _decide(body: bytes) -> PreflightDecision:
    return decide(estimate(body, MODEL), SpendGuardConfig(), model_max_context_tokens=MAX_CTX)


def test_proof_for_allow_has_available_risk_and_contributors():
    decision = _decide(_anthropic_body(messages=[_msg("user", "k" * _chars_for_tokens(500))]))
    proof = decision.to_receipt_proof()
    assert proof["proof_version"] == "preflight.v1"
    assert proof["decision"] == "allow"
    assert proof["risk"]["available"] is True
    assert proof["risk"]["projected_input_tokens"] > 0
    assert proof["risk"]["model"] == MODEL
    assert proof["top_context_contributors"]
    # JSON-serializable end to end (receipt attaches it verbatim).
    assert json.loads(json.dumps(proof))["decision"] == "allow"


def test_proof_for_block_carries_decision_and_threshold():
    decision = _decide(_anthropic_body(messages=[_msg("user", "z" * _chars_for_tokens(190_000))]))
    proof = decision.to_receipt_proof()
    assert proof["decision"] == "block"
    assert proof["requires_approval"] is True
    assert proof["threshold_hit"]
    assert proof["risk"]["available"] is True


def test_proof_for_unknown_estimate_marks_risk_unavailable():
    decision = _decide(b"not-json")
    proof = decision.to_receipt_proof()
    assert proof["decision"] == "warn"
    assert proof["risk"]["available"] is False
    assert proof["risk"]["reason"] == "unparseable_request_body"
    assert proof["contributors_reason"] == "unparseable_request_body"


def test_proof_with_no_risk_is_explicitly_unavailable():
    decision = PreflightDecision(
        decision="allow", reason="x", requires_approval=False, risk=None
    )
    proof = decision.to_receipt_proof()
    assert proof["risk"] == {"available": False, "reason": "no_estimate"}
    assert proof["top_context_contributors"] == []


# ---------------------------------------------------------------------------
# User-actionable block JSON (warnings are not telemetry-only)
# ---------------------------------------------------------------------------

def test_block_response_surfaces_top_contributors():
    decision = _decide(_anthropic_body(messages=[_msg("user", "z" * _chars_for_tokens(190_000))]))
    pending = PendingRequest(
        pending_id="p1",
        session_id="s1",
        created_at=0.0,
        expires_at=0.0,
        request_hash="h",
        provider="anthropic",
        model=MODEL,
        projected_tokens=decision.risk.projected_input_tokens,
        projected_cost_usd=decision.risk.projected_cost_usd,
        raw_request_blob=b"",
        raw_request_headers={},
        target_url="",
    )
    payload = json.loads(block_response.block(decision, pending))
    err = payload["error"]
    assert err["top_context_contributors"]
    assert err["top_context_contributors"][0]["source"].startswith("message[0]:user")


def test_estimate_total_pct_is_bounded():
    # Sanity: contributor pct shares never exceed 1.0 in aggregate (top-N).
    body = _anthropic_body(
        system="s" * _chars_for_tokens(10_000),
        messages=[_msg("user", "u" * _chars_for_tokens(30_000))],
    )
    est = estimate(body, MODEL)
    assert sum(c["pct"] for c in est.contributors) <= 1.0 + 1e-6


@pytest.mark.parametrize("model", ["claude-opus-4-7", "gpt-4.1", "gemini-1.5-pro"])
def test_known_models_resolve_for_percent_basis(model):
    # The percent basis needs a known max-context; guard the registry hookup.
    assert get_model_max_context(model) is not None
