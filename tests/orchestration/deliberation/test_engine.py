"""Deliberation Engine minimum-path tests (deliberation policy §5.1 / §8 / §9 / §15.2)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from tokenpak import _paths
from tokenpak.orchestration.deliberation import (
    DeliberationConfig,
    DeliberationEngine,
    DeliberationInput,
    DeliberationRecursionError,
    NodeOutput,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(_paths.ENV_VAR, str(tmp_path))
    return tmp_path


def _node(node_id, verdict, reason_codes=None, summary=None, confidence="high"):
    return NodeOutput(
        node_id=node_id,
        model_card=f"card/{node_id}",
        verdict=verdict,
        reason_codes=reason_codes if reason_codes is not None else (
            [] if verdict == "approve" else ["pak.generic"]
        ),
        confidence=confidence,
        summary=summary or f"{node_id} says {verdict}",
    )


def _inputs(nodes, **overrides):
    base = dict(decision_ref="decision:example-001", risk_class="standards-change", nodes=nodes)
    base.update(overrides)
    return DeliberationInput(**base)


def test_paths_registry_accepts_deliberation_subdir(home):
    # Home-layout fail-loud contract: 'deliberation' is a registered layout subdir.
    assert _paths.under("deliberation", "receipts") == home / "deliberation" / "receipts"


def test_receipt_written_under_std33_root(home):
    engine = DeliberationEngine()
    receipt = engine.run(_inputs([_node("a", "approve"), _node("b", "approve")]))

    receipts_dir = home / "deliberation" / "receipts"
    path = receipts_dir / f"{receipt.receipt_id}.json"
    assert path.is_file()
    assert (receipts_dir.stat().st_mode & 0o777) == 0o700
    on_disk = json.loads(path.read_text())
    assert on_disk["correlation_id"] == receipt.correlation_id
    assert on_disk["result_state"] == "complete"


def test_advisory_default_and_correlation_id_present(home):
    receipt = DeliberationEngine().run(_inputs([_node("a", "approve")]))
    assert receipt.mode == "advisory"  # §15.2: gating is never a default
    assert receipt.correlation_id  # §8.1 stable correlation identifier
    assert receipt.engine_version


def test_dissent_preserved_verbatim(home):
    minority = _node("b", "revise", ["pak.scope"], summary="schema drift in §6")
    receipt = DeliberationEngine().run(
        _inputs([_node("a", "approve"), _node("c", "approve"), minority])
    )
    assert len(receipt.dissent) == 1
    assert receipt.dissent[0].node_id == "b"
    assert receipt.dissent[0].summary == "schema drift in §6"  # verbatim, not averaged
    assert receipt.recommendation == "approve"
    assert receipt.adjudication == "annotate"


def test_escalation_uses_deterministic_fallback_without_judge(home):
    receipt = DeliberationEngine().run(
        _inputs([_node("a", "approve"), _node("b", "stop", ["pak.conflict"])])
    )
    assert receipt.disagreement.escalate is True
    assert receipt.recommendation == "escalate"
    assert receipt.fixed_judge is None  # no model call on the minimum path
    assert receipt.fallback_reason  # §5.1 deterministic fallback, not a silent drop
    assert receipt.adjudication == "escalate"


def test_error_path_still_emits_receipt(home):
    engine = DeliberationEngine()

    def exploding_scorer(nodes, thresholds):
        raise RuntimeError("scorer blew up")

    with pytest.raises(RuntimeError, match="scorer blew up"):
        engine.run(_inputs([_node("a", "approve")]), scorer=exploding_scorer)

    receipts = list((home / "deliberation" / "receipts").glob("*.json"))
    assert len(receipts) == 1  # §5.1: receipt on ALL exit paths
    on_disk = json.loads(receipts[0].read_text())
    assert on_disk["result_state"] == "partial_error_abort"
    assert on_disk["partial"]["recommended_resume_mode"] == "rerun"


def test_anti_recursion_guard(home):
    engine = DeliberationEngine()

    def recursive_scorer(nodes, thresholds):
        engine.run(_inputs([_node("z", "approve")]))

    with pytest.raises(DeliberationRecursionError):
        engine.run(_inputs([_node("a", "approve")]), scorer=recursive_scorer)


def test_explicit_stop_receipt(home):
    engine = DeliberationEngine()
    inputs = _inputs([_node("a", "approve"), _node("b", "approve")])
    receipt = engine.emit_stop_receipt(
        inputs, "partial_budget_abort", "rolling cap reached", completed_nodes=["a"]
    )
    assert receipt.result_state == "partial_budget_abort"
    assert receipt.partial.completed_nodes == ["a"]
    assert receipt.partial.missing_nodes == ["b"]
    assert receipt.partial.spend_guard_reason == "rolling cap reached"
    path = home / "deliberation" / "receipts" / f"{receipt.receipt_id}.json"
    assert path.is_file()

    with pytest.raises(ValueError):
        engine.emit_stop_receipt(inputs, "complete", "nope")


def test_no_raw_cot_fields_on_receipt(home):
    # §8.4: durable receipts persist summaries only; the model forbids unknown
    # keys, so a raw-trace field cannot ride along.
    receipt = DeliberationEngine().run(_inputs([_node("a", "approve")]))
    dumped = receipt.model_dump()
    assert "raw" not in json.dumps(dumped).lower()
