"""Disagreement scorer v0 tests (deliberation policy §7 — deterministic, no judge call)."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from tokenpak.orchestration.deliberation import NodeOutput, ScorerThresholds, score


def _node(node_id, verdict, reason_codes=None, risk_flags=None, confidence="high"):
    return NodeOutput(
        node_id=node_id,
        model_card=f"card/{node_id}",
        verdict=verdict,
        reason_codes=reason_codes if reason_codes is not None else (
            [] if verdict == "approve" else ["pak.generic"]
        ),
        risk_flags=risk_flags or [],
        confidence=confidence,
        summary=f"{node_id} says {verdict}",
    )


def test_single_node_is_vacuous_agreement():
    result = score([_node("a", "approve")])
    assert result.classification == "agree"
    assert result.escalate is False


def test_identical_verdicts_and_codes_agree():
    nodes = [
        _node("a", "revise", ["pak.scope"]),
        _node("b", "revise", ["pak.scope"]),
    ]
    result = score(nodes)
    assert result.classification == "agree"
    assert result.max_verdict_distance == 0
    assert result.reason_code_overlap == 1.0
    assert result.escalate is False


def test_approve_vs_stop_is_material_and_escalates():
    result = score([_node("a", "approve"), _node("b", "stop", ["pak.conflict"])])
    assert result.classification == "material-disagreement"
    assert result.max_verdict_distance == 3
    assert result.escalate is True


def test_adjacent_verdicts_with_shared_codes_minor():
    nodes = [
        _node("a", "revise", ["pak.scope", "pak.naming"]),
        _node("b", "escalate", ["pak.scope", "pak.naming"]),
    ]
    result = score(nodes)
    assert result.classification == "minor-divergence"
    assert result.escalate is False


def test_adjacent_verdicts_with_disjoint_codes_material():
    # One rung apart but reasoning from disjoint causes → material (§7).
    nodes = [
        _node("a", "revise", ["pak.scope"]),
        _node("b", "escalate", ["pak.security"]),
    ]
    result = score(nodes)
    assert result.classification == "material-disagreement"
    assert result.escalate is True


def test_confidence_gap_alone_is_minor():
    nodes = [
        _node("a", "approve", confidence="high"),
        _node("b", "approve", confidence="low"),
    ]
    result = score(nodes)
    assert result.classification == "minor-divergence"
    assert result.max_confidence_gap == 2
    assert result.escalate is False


def test_thresholds_are_configurable():
    nodes = [_node("a", "approve"), _node("b", "revise", ["pak.scope"])]
    relaxed = ScorerThresholds(material_verdict_distance=3, reason_overlap_floor=0.0)
    assert score(nodes, relaxed).classification == "minor-divergence"
    strict = ScorerThresholds(material_verdict_distance=1)
    assert score(nodes, strict).classification == "material-disagreement"
